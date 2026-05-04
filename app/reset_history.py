"""Trim per-equipment history to the last sample only (one-shot CLI).

Usage::

    docker compose run --rm tracker python -m app.reset_history

Or locally (from repo root, with .env loaded)::

    python -m app.reset_history

Requires ``EQUIPMENT_NUMBERS`` and ``STATE_DIR`` (default ``state``) in the
environment. Telegram vars are not required.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from .state import load_state, reset_history_keep_last_sample, save_state


def main() -> int:
    load_dotenv(override=False)
    state_dir = os.environ.get("STATE_DIR", "state").strip() or "state"
    raw = os.environ.get("EQUIPMENT_NUMBERS", "").strip()
    equipment = [e.strip().upper() for e in raw.split(",") if e.strip()]
    if not equipment:
        print("EQUIPMENT_NUMBERS is required", file=sys.stderr)
        return 1

    changed = 0
    for eq in equipment:
        st = load_state(state_dir, eq)
        if reset_history_keep_last_sample(st):
            save_state(state_dir, st)
            print(f"{eq}: history trimmed to 1 point")
            changed += 1
        else:
            print(f"{eq}: unchanged (0 or 1 history points already)")

    print(f"Done. Updated {changed} file(s) under {state_dir!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
