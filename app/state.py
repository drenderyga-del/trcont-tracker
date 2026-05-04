"""Per-equipment persistent state.

We need to:
  * detect when an event changes (so we don't spam Telegram on every poll);
  * keep a small rolling history of (datetime, left_distance_leg) so we can
    compute the average speed over the last N hours.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

# Keep at most this many history points per container.
MAX_HISTORY_POINTS = 200
# Drop history points older than this many hours (keeps state file small).
HISTORY_RETENTION_HOURS = 24 * 14


@dataclass
class HistoryPoint:
    datetime_iso: str           # event timestamp (ISO 8601 with tz)
    left_distance_km: float     # km still to travel by rail in RF
    sampled_at_iso: str         # when we recorded this sample (UTC ISO)
    station_name: Optional[str] = None  # station name at this point (optional)

    @property
    def event_time(self) -> datetime:
        return _parse_iso(self.datetime_iso)


@dataclass
class EquipmentState:
    equipment_number: str
    last_event_id: Optional[str] = None
    last_event_signature: Optional[str] = None
    last_event_datetime_iso: Optional[str] = None
    history: List[HistoryPoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["history"] = [asdict(p) for p in self.history]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EquipmentState":
        # Tolerate older history entries that lack newer optional keys, and
        # silently drop any unknown keys we don't understand.
        allowed = {f for f in HistoryPoint.__dataclass_fields__}
        history = [
            HistoryPoint(**{k: v for k, v in p.items() if k in allowed})
            for p in d.get("history", [])
        ]
        return cls(
            equipment_number=d["equipment_number"],
            last_event_id=d.get("last_event_id"),
            last_event_signature=d.get("last_event_signature"),
            last_event_datetime_iso=d.get("last_event_datetime_iso"),
            history=history,
        )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def _parse_iso(s: str) -> datetime:
    # Python's fromisoformat accepts "+03:00" since 3.11.
    return datetime.fromisoformat(s)


def _state_path(state_dir: str, equipment_number: str) -> str:
    safe = "".join(c for c in equipment_number.upper() if c.isalnum() or c in "-_")
    return os.path.join(state_dir, f"equipment_{safe}.json")


def load_state(state_dir: str, equipment_number: str) -> EquipmentState:
    path = _state_path(state_dir, equipment_number)
    if not os.path.exists(path):
        return EquipmentState(equipment_number=equipment_number)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return EquipmentState.from_dict(data)
    except Exception:
        logger.exception("Failed to read state file %s, starting fresh", path)
        return EquipmentState(equipment_number=equipment_number)


def reset_history_keep_last_sample(state: EquipmentState) -> bool:
    """Drop all but the last history point (most recent poll sample).

    Keeps ``last_event_id`` / ``last_event_signature`` / ``last_event_datetime_iso``
    unchanged so Telegram does not re-send the current event. After this,
    «Проехал» and average speed only gain meaning again once new samples arrive.
    """
    if len(state.history) <= 1:
        return False
    state.history = [state.history[-1]]
    return True


def save_state(state_dir: str, state: EquipmentState) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = _state_path(state_dir, state.equipment_number)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def add_history_point(
    state: EquipmentState,
    event_datetime_iso: str,
    left_distance_km: Optional[float],
    station_name: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> None:
    if left_distance_km is None:
        return
    now_utc = now_utc or datetime.now(timezone.utc)
    point = HistoryPoint(
        datetime_iso=event_datetime_iso,
        left_distance_km=float(left_distance_km),
        sampled_at_iso=now_utc.isoformat(),
        station_name=station_name,
    )
    # Avoid storing the exact same (datetime, distance) pair twice.
    if state.history and state.history[-1].datetime_iso == event_datetime_iso \
            and abs(state.history[-1].left_distance_km - point.left_distance_km) < 1e-6:
        return
    state.history.append(point)

    # Drop old points & cap size.
    cutoff = now_utc - timedelta(hours=HISTORY_RETENTION_HOURS)
    state.history = [
        p for p in state.history if _parse_iso(p.datetime_iso) >= cutoff
    ]
    if len(state.history) > MAX_HISTORY_POINTS:
        state.history = state.history[-MAX_HISTORY_POINTS:]


def previous_distance_point(
    state: EquipmentState,
    current_left_km: Optional[float],
    current_station_name: Optional[str] = None,
) -> Optional[HistoryPoint]:
    """Return the most recent history point that should be considered the
    "previous station" relative to the supplied current values.

    Prefer a different *station name* first so we do not skip the newest row
    when ``left_distance_km`` matches within 1 km and then latch onto an old
    point further back in history.
    """
    if not state.history:
        return None
    for p in reversed(state.history):
        if (
            current_station_name
            and p.station_name
            and p.station_name != current_station_name
        ):
            return p
        if current_left_km is not None and abs(p.left_distance_km - current_left_km) >= 1.0:
            return p
    return None


def average_speed_kmh(
    state: EquipmentState,
    window_hours: float,
) -> Optional[float]:
    """Return average speed (km/h) over the last `window_hours`, or None.

    We use absolute km deltas between consecutive points so route-distance
    recalculations (left_distance up/down) do not collapse speed to zero.
    """
    if len(state.history) < 2:
        return None

    points = sorted(state.history, key=lambda p: _parse_iso(p.datetime_iso))
    newest = points[-1]
    newest_dt = _parse_iso(newest.datetime_iso)
    cutoff = newest_dt - timedelta(hours=window_hours)

    # Find the oldest point that's still within (or at the edge of) the window.
    candidates = [p for p in points if _parse_iso(p.datetime_iso) >= cutoff]
    if len(candidates) < 2:
        return None
    oldest = candidates[0]
    oldest_dt = _parse_iso(oldest.datetime_iso)

    duration_hours = (newest_dt - oldest_dt).total_seconds() / 3600.0
    if duration_hours <= 0:
        return None

    # Distance travelled estimate = sum of absolute deltas in the window.
    distance = 0.0
    prev = candidates[0]
    for cur in candidates[1:]:
        distance += abs(cur.left_distance_km - prev.left_distance_km)
        prev = cur

    return distance / duration_hours
