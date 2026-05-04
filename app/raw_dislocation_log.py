"""Append raw dislocation API JSON for debugging (see ``STATE_DIR/raw_dislocation/``)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict


def _equipment_file_stem(equipment_number: str) -> str:
    return "".join(
        c for c in equipment_number.upper() if c.isalnum() or c in "-_"
    )


def append_raw_dislocation(
    state_dir: str,
    equipment_number: str,
    payload: Dict[str, Any],
) -> None:
    """Append one JSON object per line: ``sampled_at`` (UTC ISO) + full ``response``."""
    stem = _equipment_file_stem(equipment_number)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subdir = os.path.join(state_dir, "raw_dislocation")
    os.makedirs(subdir, exist_ok=True)
    path = os.path.join(subdir, f"{stem}_{day}.jsonl")
    record = {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "equipment": equipment_number.strip().upper(),
        "response": payload,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
