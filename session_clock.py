"""
session_clock.py - NQ CALLS 2026
=================================
Single source of truth for all time-based session events.

Trading session logic:
  - NQ/GC session OPENS at 6:00 PM ET (Sun-Thu)
  - NQ/GC session CLOSES at 4:00 PM ET (Mon-Fri)
  - Pre-flatten warning at 3:55 PM ET
  - ALL session boundaries (including crypto) are at 4:00 PM ET
  - Session date = the date the session is trading INTO:
      After 4 PM ET  ->  next calendar day's session
      Before 4 PM ET ->  today's session
    A trade opened at 7 PM on April 15 belongs to the April 16 session.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
    ET_ZONE = ZoneInfo("America/New_York")
except ImportError:
    ET_ZONE = None

_log = logging.getLogger("nqcalls.session_clock")


def _to_et(dt_utc: datetime) -> datetime:
    """Convert a UTC datetime to Eastern Time (DST-aware)."""
    if ET_ZONE:
        return dt_utc.astimezone(ET_ZONE)
    return dt_utc - timedelta(hours=4)  # fallback, no DST


def _now_et() -> datetime:
    if ET_ZONE:
        return datetime.now(ET_ZONE)
    return datetime.now(timezone.utc) - timedelta(hours=4)


# ── Session Events ───────────────────────────────────────────────
class SessionEvent(Enum):
    FUTURES_SESSION_CLOSE = auto()   # 4:00 PM ET Mon-Fri
    FUTURES_PRE_FLATTEN   = auto()   # 3:55 PM ET Mon-Fri
    FUTURES_SESSION_OPEN  = auto()   # 6:00 PM ET Sun-Thu
    CRYPTO_DAY_BOUNDARY   = auto()   # 4:00 PM ET daily — unified with futures


# Event schedule: (event, hour_ET, minute_ET, weekday_filter)
# weekday_filter: None = every day, set of ints = only those days (Mon=0..Sun=6)
_EVENT_SCHEDULE = [
    (SessionEvent.FUTURES_SESSION_CLOSE, 16,  0, {0, 1, 2, 3, 4}),   # Mon-Fri
    (SessionEvent.FUTURES_PRE_FLATTEN,   15, 55, {0, 1, 2, 3, 4}),   # Mon-Fri
    (SessionEvent.FUTURES_SESSION_OPEN,  18,  0, {6, 0, 1, 2, 3}),   # Sun-Thu
    (SessionEvent.CRYPTO_DAY_BOUNDARY,   16,  0, None),              # every day
]


class SessionClock:
    """
    Fires session events exactly once per trigger window (60 seconds).

    Usage:
        clock = SessionClock()
        clock.on(SessionEvent.FUTURES_SESSION_CLOSE, my_handler)
        # in your scan loop:
        clock.tick(datetime.now(timezone.utc))
    """

    FIRE_WINDOW_SECONDS = 60  # events fire within 60s after target time

    def __init__(self):
        self._handlers: dict[SessionEvent, list[Callable]] = {}
        self._last_fired: dict[str, str] = {}  # "event_name" -> "YYYY-MM-DD_event" dedup key

    def on(self, event: SessionEvent, handler: Callable) -> None:
        """Register a callback for an event. Handler receives (event, now_et)."""
        self._handlers.setdefault(event, []).append(handler)

    def tick(self, now_utc: datetime) -> list[SessionEvent]:
        """
        Check which events should fire. Call this every scan loop iteration.
        Returns list of events that fired this tick.
        """
        fired = []
        now_et = _to_et(now_utc)

        for event, target_hour, target_min, weekday_filter in _EVENT_SCHEDULE:
            if weekday_filter is not None and now_et.weekday() not in weekday_filter:
                continue

            target_minutes = target_hour * 60 + target_min
            current_minutes = now_et.hour * 60 + now_et.minute
            seconds_past = (current_minutes - target_minutes) * 60 + now_et.second

            if 0 <= seconds_past < self.FIRE_WINDOW_SECONDS:
                fire_key = f"{now_et.date()}_{event.name}"
                if self._last_fired.get(event.name) != fire_key:
                    self._last_fired[event.name] = fire_key
                    self._fire(event, now_et)
                    fired.append(event)

        return fired

    def _fire(self, event: SessionEvent, now_et: datetime):
        _log.info("SessionClock firing: %s", event.name)
        for handler in self._handlers.get(event, []):
            try:
                handler(event, now_et)
            except Exception as e:
                _log.error("SessionClock handler error [%s]: %s", event.name, e)


# ── Session Date ─────────────────────────────────────────────────

def get_session_date(now_et: Optional[datetime] = None) -> str:
    """
    Returns the current trading session date as YYYY-MM-DD.

    Rules:
      - If current ET time is >= 4:00 PM (16:00), the session date is
        the NEXT calendar day. This is because the 6PM open starts the
        next day's trading session. The 4PM-6PM gap is dead time that
        belongs to the upcoming session.
      - If current ET time is < 4:00 PM, the session date is today.

    Examples:
      - 3:30 PM ET on April 15 -> "2026-04-15"
      - 4:01 PM ET on April 15 -> "2026-04-16"
      - 7:00 PM ET on April 15 -> "2026-04-16"
      - 9:30 AM ET on April 16 -> "2026-04-16"
    """
    if now_et is None:
        now_et = _now_et()

    if now_et.hour >= 16:
        # 4PM or later: belongs to next calendar day's session
        return (now_et.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        return now_et.date().strftime("%Y-%m-%d")


def session_date_from_timestamp(ts_str: str) -> str:
    """
    Compute session date from an ISO timestamp string.
    Used for backward compatibility when filling missing session_id values.
    """
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        et = _to_et(dt)
        return get_session_date(et)
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


# Keep old names as aliases so nothing breaks during transition
current_session_id = get_session_date
session_id_from_timestamp = session_date_from_timestamp
