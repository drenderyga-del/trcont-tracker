from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple

from dotenv import load_dotenv


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _headless(value: str | None, default: bool = True) -> bool:
    """HEADLESS=true|false for Playwright Chromium."""
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("0", "false", "no", "n", "off"):
        return False
    return True


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_ids: Tuple[str, ...]
    equipment_numbers: List[str]

    poll_interval: int = 3600
    per_equipment_delay: int = 3
    state_dir: str = "state"

    heartbeat_interval: int = 0
    alert_after_full_fail_cycles: int = 0
    send_startup_message: bool = False

    nav_timeout_ms: int = 60000
    search_timeout_ms: int = 60000
    fetch_attempts: int = 4
    captcha_retry_delay_s: float = 1.5
    pre_click_delay_ms: int = 500

    # When SmartCaptcha shows a manual challenge: restart browser, wait, retry once.
    captcha_backoff_s: float = 300.0

    headless: bool = True
    speed_window_hours: float = 6.0

    # Append each successful dislocation JSON to STATE_DIR/raw_dislocation/*.jsonl
    save_raw_dislocation: bool = True


def load_config() -> Config:
    load_dotenv(override=False)

    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    eq_raw = os.environ.get("EQUIPMENT_NUMBERS", "").strip()

    if not bot:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    chat_ids = tuple(p.strip() for p in chat_raw.split(",") if p.strip())
    if not chat_ids:
        raise SystemExit(
            "TELEGRAM_CHAT_ID is required (one id or several comma-separated, e.g. "
            "123456789,-1001234567890)"
        )

    equipment = [e.strip().upper() for e in eq_raw.split(",") if e.strip()]
    if not equipment:
        raise SystemExit("EQUIPMENT_NUMBERS is required (comma-separated)")

    return Config(
        bot_token=bot,
        chat_ids=chat_ids,
        equipment_numbers=equipment,
        poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "3600")),
        per_equipment_delay=int(os.environ.get("PER_EQUIPMENT_DELAY_SECONDS", "3")),
        state_dir=os.environ.get("STATE_DIR", "state"),
        heartbeat_interval=int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "0")),
        alert_after_full_fail_cycles=int(
            os.environ.get("ALERT_AFTER_FULL_FAIL_CYCLES", "0")
        ),
        send_startup_message=_bool(os.environ.get("SEND_STARTUP_MESSAGE")),
        nav_timeout_ms=int(os.environ.get("NAV_TIMEOUT_MS", "60000")),
        search_timeout_ms=int(os.environ.get("SEARCH_TIMEOUT_MS", "60000")),
        fetch_attempts=int(os.environ.get("FETCH_ATTEMPTS", "4")),
        captcha_retry_delay_s=float(os.environ.get("CAPTCHA_RETRY_DELAY_S", "1.5")),
        captcha_backoff_s=float(os.environ.get("CAPTCHA_BACKOFF_S", "300")),
        pre_click_delay_ms=int(os.environ.get("PRE_CLICK_DELAY_MS", "500")),
        headless=_headless(os.environ.get("HEADLESS"), default=True),
        speed_window_hours=float(os.environ.get("SPEED_WINDOW_HOURS", "6")),
        save_raw_dislocation=_bool(
            os.environ.get("SAVE_RAW_DISLOCATION"),
            default=True,
        ),
    )
