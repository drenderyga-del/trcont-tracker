"""Picks the latest event from the dislocation API response and formats it
for Telegram."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Ignore unrelated old rows still present in ``result[]`` next to the latest event.
PREV_DISLOC_EVENT_MAX_TIMEDELTA = timedelta(days=14)


@dataclass
class Event:
    raw: Dict[str, Any]
    event_id: Optional[str]
    equipment_number: str
    datetime_iso: str
    event_dt: datetime
    sequence: Optional[int]
    station_name: Optional[str]
    station_id: Optional[str]
    country_name: Optional[str]
    status_name: Optional[str]
    left_distance_km: Optional[float]
    prev_station_name: Optional[str] = None
    prev_left_distance_km: Optional[float] = None

    @property
    def traveled_from_prev_km(self) -> Optional[float]:
        """How many km we covered since the previous event (positive = closer
        to destination). None if we don't have a previous distance."""
        if self.left_distance_km is None or self.prev_left_distance_km is None:
            return None
        return self.prev_left_distance_km - self.left_distance_km

    @property
    def signature(self) -> str:
        """A stable identifier for change detection.
        Prefer event_id, fall back to (datetime, status, station)."""
        if self.event_id:
            return f"id:{self.event_id}"
        return (
            f"sig:{self.datetime_iso}|"
            f"{self.status_name or ''}|"
            f"{self.station_name or ''}|"
            f"{self.station_id or ''}"
        )


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def latest_event(response: Dict[str, Any], equipment_number: str) -> Optional[Event]:
    result = response.get("result") or []
    if not result:
        return None
    # Pick the latest by datetime, falling back to `sequence`.
    def sort_key(item: Dict[str, Any]):
        dt_str = item.get("datetime", "")
        try:
            dt = _parse_iso(dt_str)
        except Exception:
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
        seq = item.get("sequence", 0) or 0
        return (dt, seq)

    sorted_items = sorted(result, key=sort_key)
    item = sorted_items[-1]

    pos = item.get("position") or {}
    country = item.get("country") or {}
    status = item.get("status") or {}

    dt_str = item.get("datetime", "")
    try:
        dt = _parse_iso(dt_str)
    except Exception:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

    cur_station_name = pos.get("name_ru") or pos.get("name_en")

    # Look back through earlier events to find the most recent one at a
    # *different* station that also reports a valid `left_distance_leg`.
    # Same-station events (Прибытие/Отправление) and events without a
    # distance don't tell us anything about how far we travelled.
    prev_station_name: Optional[str] = None
    prev_left_distance: Optional[float] = None
    for prev_item in reversed(sorted_items[:-1]):
        prev_pos = prev_item.get("position") or {}
        candidate_name = prev_pos.get("name_ru") or prev_pos.get("name_en")
        candidate_left = prev_pos.get("left_distance_leg")
        if not candidate_name or candidate_left is None:
            continue
        if candidate_name == cur_station_name:
            continue
        prev_dt_str = prev_item.get("datetime", "")
        try:
            prev_dt = _parse_iso(prev_dt_str)
        except Exception:
            continue
        try:
            if prev_dt > dt:
                continue
            if dt - prev_dt > PREV_DISLOC_EVENT_MAX_TIMEDELTA:
                continue
        except TypeError:
            pass
        prev_station_name = candidate_name
        prev_left_distance = float(candidate_left)
        break

    return Event(
        raw=item,
        event_id=item.get("event_id"),
        equipment_number=item.get("equipment_number") or equipment_number,
        datetime_iso=dt_str,
        event_dt=dt,
        sequence=item.get("sequence"),
        station_name=cur_station_name,
        station_id=str(item.get("station_id")) if item.get("station_id") else None,
        country_name=country.get("name_ru") or country.get("name_en"),
        status_name=status.get("name_ru") or status.get("name_en"),
        left_distance_km=pos.get("left_distance_leg"),
        prev_station_name=prev_station_name,
        prev_left_distance_km=prev_left_distance,
    )


def _format_msk(dt: datetime) -> str:
    """Format the event datetime as `DD.MM.YYYY HH:MM МСК`.
    The API returns +03:00 (Moscow) already, so we just format directly."""
    return dt.strftime("%d.%m.%Y %H:%M") + " МСК"


def _format_distance(km: Optional[float]) -> str:
    if km is None:
        return "—"
    # API gives floats like 6192.0; show without trailing zeros.
    return f"{km:.1f}".rstrip("0").rstrip(".") + " км"


def _format_traveled(km: Optional[float], prev_station: Optional[str]) -> str:
    """Render the "Проехал" line. Negative deltas (left_distance grew) usually
    mean a re-routing happened - we still report the magnitude so it's visible."""
    if km is None:
        return "Проехал: —"
    suffix = f" (от ст. {prev_station})" if prev_station else ""
    if km < 0:
        return (
            f"⚠️ Остаток пересчитан источником: +{_format_distance(-km)}{suffix}"
        )
    return f"Проехал: {_format_distance(km)}{suffix}"


def _format_speed(speed_kmh: Optional[float], window_hours: float) -> str:
    window_str = f"{int(window_hours)}ч" if window_hours == int(window_hours) else f"{window_hours:g}ч"
    if speed_kmh is None:
        return f"Средняя скорость (за {window_str}): —"
    return f"Средняя скорость (за {window_str}): {speed_kmh:.0f} км/ч"


def format_telegram_message(
    event: Event,
    avg_speed_kmh: Optional[float],
    speed_window_hours: float,
) -> str:
    lines: List[str] = []
    lines.append(event.equipment_number)

    location_parts: List[str] = []
    if event.country_name:
        location_parts.append(event.country_name)
    if event.station_name:
        location_parts.append(event.station_name)
    lines.append(", ".join(location_parts) if location_parts else "—")

    status_line = event.status_name or "—"
    status_line += " · " + _format_msk(event.event_dt)
    lines.append(status_line)

    lines.append(f"Осталось: {_format_distance(event.left_distance_km)}")
    lines.append(_format_traveled(event.traveled_from_prev_km, event.prev_station_name))
    lines.append(_format_speed(avg_speed_kmh, speed_window_hours))

    return "\n".join(lines)


def format_no_data_message(equipment_number: str) -> str:
    return (
        f"{equipment_number}\n"
        "Нет данных по дислокации (DISLOC-NA)"
    )
