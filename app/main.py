"""Main loop: poll the tracking page for each container and notify Telegram."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from .config import Config, load_config
from .formatter import (
    Event,
    format_no_data_message,
    format_telegram_message,
    latest_event,
)
from .raw_dislocation_log import append_raw_dislocation
from .notifier import Telegram
from .tracking_client import (
    CaptchaError,
    ScrapeError,
    TrackingClient,
)
from .state import (
    add_history_point,
    average_speed_kmh,
    load_state,
    previous_distance_point,
    save_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tracker")


_shutdown_event: asyncio.Event | None = None


def _parse_iso_safe(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:  # noqa: BLE001
        return None


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    event = asyncio.Event()

    def _handle(signame: str) -> None:
        logger.info("Received %s, shutting down...", signame)
        event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _handle, s.name)
        except NotImplementedError:
            # Windows / restricted env: just rely on KeyboardInterrupt.
            pass
    return event


async def _process_equipment(
    cfg: Config,
    client: TrackingClient,
    telegram: Telegram,
    equipment_number: str,
) -> bool:
    """Fetch + maybe notify for a single container. Returns True on success."""
    state = load_state(cfg.state_dir, equipment_number)

    last_exc: Optional[Exception] = None
    data: Optional[dict] = None
    captcha_hit = False

    # Phase 1: regular retry loop. Treat CaptchaError as a hard signal to
    # break out and let the smart-backoff phase handle it - we don't want
    # to hammer the site with retries while SmartCaptcha is angry.
    for attempt in range(1, cfg.fetch_attempts + 1):
        try:
            data = await client.fetch(equipment_number)
            break
        except CaptchaError as exc:
            last_exc = exc
            captcha_hit = True
            logger.warning("[%s] captcha challenge: %s", equipment_number, exc)
            break
        except ScrapeError as exc:
            logger.warning("[%s] scrape error (attempt %s): %s",
                           equipment_number, attempt, exc)
            last_exc = exc
            await asyncio.sleep(cfg.captcha_retry_delay_s * attempt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] unexpected error", equipment_number)
            last_exc = exc
            await asyncio.sleep(cfg.captcha_retry_delay_s * attempt)

    # Phase 2: smart backoff on captcha. Restart the browser (fresh process
    # but same persistent profile, so cookies/spravka are kept) and retry
    # once after CAPTCHA_BACKOFF_S. If still captcha - skip this cycle and
    # let the next regular poll try again (this matches how the older
    # version "occasionally errored, then recovered on its own").
    if data is None and captcha_hit:
        logger.warning(
            "[%s] captcha hit, restarting browser and backing off %.0fs",
            equipment_number, cfg.captcha_backoff_s,
        )
        try:
            await telegram.send(
                f"\u26a0\ufe0f SmartCaptcha \u043f\u043e\u043f\u0440\u043e\u0441\u0438\u043b\u0430 "
                f"\u0440\u0443\u0447\u043d\u043e\u0439 \u0447\u0435\u043b\u043b\u0435\u043d\u0434\u0436 "
                f"\u043f\u043e {equipment_number}.\n"
                f"\u041f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a\u0430\u044e \u0431\u0440"
                f"\u0430\u0443\u0437\u0435\u0440 \u0438 \u043f\u0440\u043e\u0431\u0443\u044e "
                f"\u0435\u0449\u0451 \u0440\u0430\u0437 \u0447\u0435\u0440\u0435\u0437 "
                f"{int(cfg.captcha_backoff_s)} \u0441."
            )
        except Exception:  # noqa: BLE001
            logger.exception("[%s] failed to send captcha alert", equipment_number)

        try:
            await client.restart()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] failed to restart browser", equipment_number)

        await asyncio.sleep(cfg.captcha_backoff_s)

        try:
            data = await client.fetch(equipment_number)
        except CaptchaError as exc:
            last_exc = exc
            logger.warning(
                "[%s] captcha persists after backoff, skipping cycle: %s",
                equipment_number, exc,
            )
            try:
                await telegram.send(
                    f"\u26a0\ufe0f SmartCaptcha \u043f\u043e {equipment_number} "
                    f"\u0434\u0435\u0440\u0436\u0438\u0442\u0441\u044f. \u041f\u0440\u043e\u043f"
                    f"\u0443\u0441\u043a\u0430\u044e \u0446\u0438\u043a\u043b, \u043f\u043e\u0432"
                    f"\u0442\u043e\u0440\u044e \u0447\u0435\u0440\u0435\u0437 "
                    f"{cfg.poll_interval} \u0441."
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[%s] failed to send captcha-persists alert", equipment_number,
                )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.exception("[%s] retry after backoff failed", equipment_number)

    if data is None:
        logger.error("[%s] giving up: %s", equipment_number, last_exc)
        return False

    if cfg.save_raw_dislocation:
        try:
            append_raw_dislocation(cfg.state_dir, equipment_number, data)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] failed to append raw dislocation log", equipment_number)

    event = latest_event(data, equipment_number)
    if event is None:
        # The site has no dislocation data right now. Notify only on transition.
        if state.last_event_signature != "no-data":
            text = format_no_data_message(equipment_number)
            sent = await telegram.send(text)
            logger.info("[%s] no data → telegram %s", equipment_number,
                        "ok" if sent else "FAILED")
            state.last_event_id = None
            state.last_event_signature = "no-data"
            state.last_event_datetime_iso = None
            save_state(cfg.state_dir, state)
        else:
            logger.info("[%s] still no data, skipping notification", equipment_number)
        return True

    # Guard against backend regressions where API suddenly returns an older
    # event than we have already seen (can happen transiently during cache lag).
    last_seen_dt = _parse_iso_safe(state.last_event_datetime_iso)
    if last_seen_dt is not None and event.event_dt < last_seen_dt:
        logger.warning(
            "[%s] stale event ignored: incoming=%s (%s, %s) < last_seen=%s",
            equipment_number,
            event.datetime_iso,
            event.station_name or "—",
            event.event_id or event.signature,
            state.last_event_datetime_iso,
        )
        return True

    # If the API didn't ship a previous-event entry (it usually returns just
    # the latest record), fall back to the local history we've been keeping.
    if event.prev_left_distance_km is None:
        prev = previous_distance_point(
            state,
            current_left_km=event.left_distance_km,
            current_station_name=event.station_name,
        )
        if prev is not None:
            event.prev_left_distance_km = prev.left_distance_km
            event.prev_station_name = prev.station_name

    # Update history (we keep history even if event is unchanged, in case the
    # left_distance_leg is updated separately).
    add_history_point(
        state,
        event.datetime_iso,
        event.left_distance_km,
        station_name=event.station_name,
    )

    speed = average_speed_kmh(state, cfg.speed_window_hours)

    new_signature = event.signature
    if new_signature == state.last_event_signature:
        logger.info("[%s] event unchanged (%s), skipping notification",
                    equipment_number, new_signature)
        save_state(cfg.state_dir, state)
        return True

    text = format_telegram_message(event, speed, cfg.speed_window_hours)
    sent = await telegram.send(text)
    logger.info("[%s] event changed → telegram %s\n%s",
                equipment_number, "ok" if sent else "FAILED", text)

    state.last_event_id = event.event_id
    state.last_event_signature = new_signature
    state.last_event_datetime_iso = event.datetime_iso
    save_state(cfg.state_dir, state)
    return True


async def _run_cycle(
    cfg: Config,
    client: TrackingClient,
    telegram: Telegram,
) -> int:
    """Run one polling cycle. Returns the number of successful equipment fetches."""
    successes = 0
    for i, eq in enumerate(cfg.equipment_numbers):
        ok = await _process_equipment(cfg, client, telegram, eq)
        if ok:
            successes += 1
        if i + 1 < len(cfg.equipment_numbers) and cfg.per_equipment_delay > 0:
            await asyncio.sleep(cfg.per_equipment_delay)
    return successes


async def main_async() -> int:
    cfg = load_config()
    logger.info("Loaded config: equipment=%s, poll_interval=%ss, state_dir=%s",
                cfg.equipment_numbers, cfg.poll_interval, cfg.state_dir)

    loop = asyncio.get_running_loop()
    shutdown = _install_signal_handlers(loop)

    consecutive_full_failures = 0
    last_heartbeat: Optional[datetime] = None
    last_full_success: Optional[datetime] = None

    async with Telegram(cfg.bot_token, cfg.chat_ids) as telegram:
        if cfg.send_startup_message:
            await telegram.send("Tracker started ✅")

        async with TrackingClient(
            state_dir=cfg.state_dir,
            headless=cfg.headless,
            nav_timeout_ms=cfg.nav_timeout_ms,
            search_timeout_ms=cfg.search_timeout_ms,
            pre_click_delay_ms=cfg.pre_click_delay_ms,
        ) as client:
            while not shutdown.is_set():
                cycle_started = datetime.now(timezone.utc)
                logger.info("=== Starting cycle at %s ===", cycle_started.isoformat())
                try:
                    successes = await _run_cycle(cfg, client, telegram)
                except Exception:  # noqa: BLE001
                    logger.exception("Cycle crashed")
                    successes = 0
                    try:
                        await client.restart()
                    except Exception:  # noqa: BLE001
                        logger.exception("Failed to restart client after crash")

                if successes == 0:
                    consecutive_full_failures += 1
                    logger.warning("Cycle had 0 successful fetches "
                                   "(consecutive failures: %s)",
                                   consecutive_full_failures)
                    if (
                        cfg.alert_after_full_fail_cycles > 0
                        and consecutive_full_failures == cfg.alert_after_full_fail_cycles
                    ):
                        await telegram.send(
                            f"⚠️ Tracker: {consecutive_full_failures} cycles without "
                            f"a single successful fetch. Last success: "
                            f"{last_full_success.isoformat() if last_full_success else 'never'}"
                        )
                else:
                    consecutive_full_failures = 0
                    last_full_success = datetime.now(timezone.utc)

                # Heartbeat
                if cfg.heartbeat_interval > 0:
                    now = datetime.now(timezone.utc)
                    if (
                        last_heartbeat is None
                        or (now - last_heartbeat).total_seconds() >= cfg.heartbeat_interval
                    ):
                        last_ok = (
                            last_full_success.isoformat() if last_full_success else "never"
                        )
                        await telegram.send(
                            f"💓 Tracker heartbeat\n"
                            f"Last successful cycle: {last_ok}\n"
                            f"Equipment: {', '.join(cfg.equipment_numbers)}"
                        )
                        last_heartbeat = now

                # Sleep until next cycle (interruptible).
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=cfg.poll_interval)
                except asyncio.TimeoutError:
                    pass

    logger.info("Shutdown complete")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
