"""Playwright + Chromium client for isales.trcont.com tracking.

The dislocation API requires a fresh ``smart-token`` (Yandex SmartCaptcha
``spravka``) for almost every search — reusing a captured curl token fails with
``captcha_verification_failed``. The SPA therefore obtains a new token via
in-page JS on each search. We drive the real UI: one warm tab, clear the
search box, submit with Enter (avoids iframe blocking the click), intercept
``GET /api/unauthorized/dislocation``, parse JSON.

This matches the older ``trcont-tracker`` Playwright-in-Docker approach.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Response, TimeoutError as PlaywrightTimeoutError, async_playwright

logger = logging.getLogger(__name__)

TRACKING_URL = "https://isales.trcont.com/tracking?lang=ru"
DISLOCATION_FRAGMENT = "/api/unauthorized/dislocation"


def _wants_full_browser_restart(exc: BaseException) -> bool:
    """Reload alone often leaves SmartCaptcha stuck; a fresh Chromium fixes it."""
    if isinstance(exc, PlaywrightTimeoutError):
        return True
    name = type(exc).__name__
    if "TimeoutError" in name:
        s = str(exc).lower()
        if "search" in s or "disabled" in s or "#search" in s:
            return True
    return False


class CaptchaError(RuntimeError):
    """Raised when SmartCaptcha shows a manual (visible) challenge."""


class ScrapeError(RuntimeError):
    """Generic tracking fetch failure."""


def _dislocation_url_matches(url: str, term: str) -> bool:
    if DISLOCATION_FRAGMENT not in url:
        return False
    try:
        qs = parse_qs(urlparse(url).query)
        got = (qs.get("term") or [""])[0].strip().upper()
        return got == term.strip().upper()
    except Exception:  # noqa: BLE001
        return False


def _response_predicate(term: str):
    def _pred(response: Response) -> bool:
        if response.request.method.upper() != "GET":
            return False
        st = response.status
        if 300 <= st < 400:
            return False
        return _dislocation_url_matches(response.url, term)

    return _pred


class TrackingClient:
    """Long-lived Chromium session with a single tracking tab."""

    def __init__(
        self,
        state_dir: str,
        headless: bool = True,
        nav_timeout_ms: int = 60_000,
        search_timeout_ms: int = 60_000,
        pre_click_delay_ms: int = 500,
        search_unlock_budget_s: float = 180.0,
    ) -> None:
        self.state_dir = state_dir  # reserved for debug dumps / future use
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.search_timeout_ms = search_timeout_ms
        self.pre_click_delay_ms = pre_click_delay_ms
        self.search_unlock_budget_s = search_unlock_budget_s

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    async def __aenter__(self) -> "TrackingClient":
        await self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _start(self) -> None:
        if self._pw is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._context = await self._browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        # Do not open the tracking tab here: SmartCaptcha may block #search for
        # minutes and would crash the whole process on container start. The tab
        # is opened lazily from ``fetch`` via ``_ensure_page`` (with retries).
        self._page = None
        logger.info("Playwright ready; tracking page opens on first fetch")

    async def close(self) -> None:
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    await self._page.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing tracking page")
            self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing browser context")
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing browser")
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Error stopping Playwright")
            self._pw = None

    async def restart(self) -> None:
        logger.warning("Restarting Playwright browser")
        await self.close()
        await self._start()

    async def _open_tracking_page_atomic(self) -> None:
        """Open a fresh tracking tab; only assign ``self._page`` after success."""
        assert self._context is not None, "call _start() before _open_tracking_page_atomic"

        page = await self._context.new_page()
        page.set_default_navigation_timeout(self.nav_timeout_ms)
        page.set_default_timeout(self.search_timeout_ms)
        try:
            await page.goto(
                TRACKING_URL,
                wait_until="load",
                timeout=self.nav_timeout_ms,
            )
            await self._wait_search_enabled(page)
        except Exception:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            raise

        old = self._page
        self._page = page
        if old is not None:
            try:
                if not old.is_closed():
                    await old.close()
            except Exception:  # noqa: BLE001
                pass

        logger.info("tracking page ready: %s", TRACKING_URL)

    async def _wait_search_enabled(self, page) -> None:
        """Wait until SmartCaptcha precheck enables the search box (can take >60s)."""
        await page.locator("input#search").first.wait_for(
            state="attached",
            timeout=self.nav_timeout_ms,
        )
        budget_s = max(
            self.search_unlock_budget_s,
            self.search_timeout_ms / 1000.0,
        )
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            loc = page.locator("input#search:not([disabled])").first
            try:
                await loc.wait_for(state="visible", timeout=2_000)
                return
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1_000)
        raise ScrapeError(
            "search input #search did not become enabled within "
            f"{int(budget_s)}s (SmartCaptcha precheck or manual challenge)"
        )

    async def _ensure_page(self):
        if self._browser is None or self._context is None:
            await self._start()
        page = self._page
        if page is None or page.is_closed():
            await self._open_tracking_page_atomic()
            return self._page
        try:
            cur = page.url or ""
            if "isales.trcont.com" not in cur:
                await self._open_tracking_page_atomic()
                return self._page
        except Exception:  # noqa: BLE001
            await self._open_tracking_page_atomic()
            return self._page
        return page

    async def _dismiss_error_modal_if_any(self, page) -> None:
        """The SPA sometimes shows ``ErrorModal`` on top of the search field; it
        intercepts normal clicks. Escape + optional close button, then callers
        use ``force=True`` on the input as a fallback.
        """
        try:
            modal = page.locator("section.ErrorModal_wrap").first
            if not await modal.is_visible(timeout=500):
                return
            logger.info("dismissing ErrorModal overlay")
            for sel in (
                'button:has-text("Закрыть")',
                'button:has-text("OK")',
                'button:has-text("Понятно")',
                'button:has-text("Закрыть окно")',
            ):
                try:
                    btn = modal.locator(sel).first
                    if await btn.is_visible(timeout=300):
                        await btn.click(timeout=2_000, force=True)
                        await page.wait_for_timeout(400)
                        return
                except Exception:  # noqa: BLE001
                    continue
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

    async def _submit_search_enter(self, page, term: str) -> Dict[str, Any]:
        inp = page.locator("input#search:not([disabled])").first
        await self._dismiss_error_modal_if_any(page)
        await inp.click(force=True, timeout=15_000)
        try:
            await inp.fill("", force=True)
        except Exception:  # noqa: BLE001
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        await inp.fill(term, force=True)
        if self.pre_click_delay_ms:
            await page.wait_for_timeout(self.pre_click_delay_ms)

        pred = _response_predicate(term)
        timeout_ms = self.search_timeout_ms

        try:
            async with page.expect_response(pred, timeout=timeout_ms) as resp_info:
                await page.keyboard.press("Enter")
            response = await resp_info.value
        except PlaywrightTimeoutError as exc:
            raise asyncio.TimeoutError from exc

        return await self._response_to_payload(response, term)

    async def _submit_search_button_force(self, page, term: str) -> Dict[str, Any]:
        inp = page.locator("input#search:not([disabled])").first
        await self._dismiss_error_modal_if_any(page)
        await inp.click(force=True, timeout=15_000)
        try:
            await inp.fill("", force=True)
        except Exception:  # noqa: BLE001
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        await inp.fill(term, force=True)
        if self.pre_click_delay_ms:
            await page.wait_for_timeout(self.pre_click_delay_ms)

        pred = _response_predicate(term)
        timeout_ms = self.search_timeout_ms

        btn = page.locator('button:has-text("Найти")').first
        try:
            async with page.expect_response(pred, timeout=timeout_ms) as resp_info:
                await btn.click(timeout=5_000, force=True, no_wait_after=True)
            response = await resp_info.value
        except PlaywrightTimeoutError as exc:
            raise asyncio.TimeoutError from exc

        return await self._response_to_payload(response, term)

    async def _response_to_payload(self, response: Response, term: str) -> Dict[str, Any]:
        status = response.status
        if status >= 400:
            text = ""
            try:
                text = (await response.text())[:500]
            except Exception:  # noqa: BLE001
                pass
            raise ScrapeError(f"dislocation HTTP {status}: {text}")

        try:
            data = await response.json()
        except Exception as exc:  # noqa: BLE001
            raise ScrapeError(f"dislocation response is not JSON: {exc}") from exc

        if isinstance(data, dict) and data.get("error") == "captcha_verification_failed":
            raise ScrapeError("captcha_verification_failed")

        return data

    async def fetch(self, equipment_number: str) -> Dict[str, Any]:
        term = equipment_number.strip().upper()
        page = await self._ensure_page()

        last_err: Optional[Exception] = None
        for attempt in range(1, 6):
            page = await self._ensure_page()
            try:
                try:
                    return await self._submit_search_enter(page, term)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] Enter did not produce dislocation response, "
                        "trying force-click Найти (attempt %s)",
                        term,
                        attempt,
                    )
                    page = await self._ensure_page()
                    return await self._submit_search_button_force(page, term)
            except ScrapeError as exc:
                last_err = exc
                msg = str(exc).lower()
                if "captcha_verification_failed" in msg:
                    logger.warning(
                        "[%s] captcha_verification_failed, reloading page "
                        "(attempt %s)",
                        term,
                        attempt,
                    )
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=self.nav_timeout_ms,
                        )
                        await self._wait_search_enabled(page)
                    except Exception as reload_exc:  # noqa: BLE001
                        logger.warning(
                            "[%s] reload failed, reopening tab: %s",
                            term,
                            reload_exc,
                        )
                        await self._open_tracking_page_atomic()
                    await asyncio.sleep(1.0 * attempt)
                    continue
                if "did not become enabled" in msg:
                    logger.warning(
                        "[%s] search stayed disabled after navigation, "
                        "full browser restart (attempt %s)",
                        term,
                        attempt,
                    )
                    try:
                        await self.restart()
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] browser restart failed", term)
                    await asyncio.sleep(1.0 * attempt)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.exception("[%s] unexpected fetch error", term)
                if (
                    _wants_full_browser_restart(exc)
                    or "TargetClosedError" in type(exc).__name__
                    or "closed" in str(exc).lower()
                ):
                    try:
                        await self.restart()
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] full browser restart failed", term)
                await asyncio.sleep(1.0 * attempt)
                continue

        raise ScrapeError(f"dislocation fetch failed after retries: {last_err}")
