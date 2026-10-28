"""SLA Business Calendar arithmetic.

Implements:
- business_rules.md section 8.1: Calendar functions
  (is_open, next_open, add_business_minutes, business_minutes)
- data_dictionary.md section 2 & 3: SLACalendar & SLACalendarWindow schemas

Windows are half-open: [open, close). "24:00" close allowed.
Uses integer seconds and timezone-aware UTC datetimes only.
"""

from __future__ import annotations

import datetime
from typing import Final, Sequence
import pandas as pd

# Standard reference epoch: Monday 2026-01-05 00:00:00 UTC (week 0, offset 0)
EPOCH_UTC: Final[datetime.datetime] = datetime.datetime(
    2026, 1, 5, 0, 0, 0, tzinfo=datetime.timezone.utc
)
WEEK_SECONDS: Final[int] = 7 * 24 * 3600  # 604,800 seconds in a week


def _parse_time_str(t_str: str) -> int:
    """Parse HH:MM string into seconds from midnight (business_rules.md 1.1)."""
    parts = t_str.split(":")
    h, m = int(parts[0]), int(parts[1])
    if h == 24 and m == 0:
        return 86400
    return h * 3600 + m * 60


def _ensure_utc(dt: datetime.datetime | str) -> datetime.datetime:
    """Ensure datetime is a timezone-aware UTC datetime with whole seconds."""
    if isinstance(dt, str):
        # Support ISO formats with 'Z'
        clean = dt.replace("Z", "+00:00")
        parsed = datetime.datetime.fromisoformat(clean)
    elif isinstance(dt, datetime.datetime):
        parsed = dt
    else:
        raise TypeError(f"Expected datetime or ISO string, got {type(dt)}")

    if parsed.tzinfo is None:
        raise ValueError(f"Datetime must be timezone-aware UTC, got naive: {dt}")

    # Convert to UTC and discard microseconds (whole seconds convention)
    utc_dt = parsed.astimezone(datetime.timezone.utc)
    if utc_dt.microsecond != 0:
        utc_dt = utc_dt.replace(microsecond=0)
    return utc_dt


class Calendar:
    """Weekly SLA Calendar model supporting O(1) interval arithmetic."""

    def __init__(self, calendar_code: str, intervals: Sequence[tuple[int, int]]) -> None:
        """Initialize calendar with sorted non-overlapping [start_sec, end_sec) intervals in week."""
        self.code = calendar_code
        self.intervals = tuple(sorted(intervals, key=lambda x: x[0]))
        self.weekly_open_seconds = sum(e - s for s, e in self.intervals)

    @classmethod
    def from_windows_df(cls, calendar_code: str, windows_df: pd.DataFrame) -> Calendar:
        """Build Calendar from SLACalendarWindow DataFrame (data_dictionary.md 3)."""
        df_cal = windows_df[windows_df["calendar_code"] == calendar_code]
        intervals: list[tuple[int, int]] = []
        for _, row in df_cal.iterrows():
            dow = int(row["day_of_week"])  # 1=Mon .. 7=Sun
            day_offset = (dow - 1) * 86400
            open_sec = _parse_time_str(str(row["open_time"]))
            close_sec = _parse_time_str(str(row["close_time"]))
            intervals.append((day_offset + open_sec, day_offset + close_sec))
        return cls(calendar_code, intervals)

    def is_open(self, t: datetime.datetime) -> bool:
        """Return True if t falls in a half-open window of this calendar (business_rules.md 8.1)."""
        sec = int((t - EPOCH_UTC).total_seconds())
        rem = sec % WEEK_SECONDS
        for s, e in self.intervals:
            if s <= rem < e:
                return True
        return False

    def next_open(self, t: datetime.datetime) -> datetime.datetime:
        """Return t if open, else the next window start (business_rules.md 8.1)."""
        if self.is_open(t):
            return t
        sec = int((t - EPOCH_UTC).total_seconds())
        rem = sec % WEEK_SECONDS
        # Check if there is an upcoming window in the current week
        for s, _ in self.intervals:
            if rem < s:
                return t + datetime.timedelta(seconds=(s - rem))
        # Otherwise, the next open window is the first window of the following week
        first_s = self.intervals[0][0]
        return t + datetime.timedelta(seconds=(WEEK_SECONDS - rem + first_s))

    def _cumulative_open_seconds(self, t: datetime.datetime) -> int:
        """Compute cumulative open seconds from EPOCH_UTC up to t."""
        sec = int((t - EPOCH_UTC).total_seconds())
        weeks = sec // WEEK_SECONDS
        rem = sec % WEEK_SECONDS
        cum = weeks * self.weekly_open_seconds
        for s, e in self.intervals:
            if rem <= s:
                break
            elif rem < e:
                cum += (rem - s)
                break
            else:
                cum += (e - s)
        return cum

    def _inverse_cumulative_open_seconds(self, target_cum: int) -> datetime.datetime:
        """Invert cumulative open seconds to find exact datetime."""
        weeks = target_cum // self.weekly_open_seconds
        rem = target_cum % self.weekly_open_seconds
        if rem == 0 and target_cum > 0:
            weeks -= 1
            rem = self.weekly_open_seconds

        offset = 0
        for s, e in self.intervals:
            dur = e - s
            if rem <= dur:
                offset = s + rem
                break
            else:
                rem -= dur

        return EPOCH_UTC + datetime.timedelta(seconds=weeks * WEEK_SECONDS + offset)

    def add_business_minutes(self, start: datetime.datetime, m: int) -> datetime.datetime:
        """Advance from start, consuming only open time; m = 0 returns start (business_rules.md 8.1)."""
        if m < 0:
            raise ValueError(f"m must be non-negative, got {m}")
        if m == 0:
            return start

        # If starting during a closed time, the SLA clock begins at next_open
        effective_start = self.next_open(start)
        target_cum = self._cumulative_open_seconds(effective_start) + m * 60
        return self._inverse_cumulative_open_seconds(target_cum)

    def business_minutes(self, a: datetime.datetime, b: datetime.datetime) -> int | float:
        """Total open time in [a, b); 0 if b <= a (business_rules.md 8.1)."""
        if b <= a:
            return 0
        open_sec = self._cumulative_open_seconds(b) - self._cumulative_open_seconds(a)
        if open_sec <= 0:
            return 0
        if open_sec % 60 == 0:
            return open_sec // 60
        return round(open_sec / 60.0, 1)


# Built-in standard calendars according to business_rules.md 2.2
def _build_default_calendars() -> dict[str, Calendar]:
    # CAL-EMAIL: Mon-Fri (days 1-5), 08:00 to 20:00 UTC
    email_ints = [(d * 86400 + 28800, d * 86400 + 72000) for d in range(5)]

    # CAL-CHAT: Mon-Sun (days 1-7), 06:00 to 22:00 UTC
    chat_ints = [(d * 86400 + 21600, d * 86400 + 79200) for d in range(7)]

    # CAL-PHONE: Mon-Fri (days 1-5), 09:00 to 18:00 UTC
    phone_ints = [(d * 86400 + 32400, d * 86400 + 64800) for d in range(5)]

    # CAL-247: Mon-Sun (days 1-7), 00:00 to 24:00 UTC
    all247_ints = [(d * 86400, (d + 1) * 86400) for d in range(7)]

    return {
        "CAL-EMAIL": Calendar("CAL-EMAIL", email_ints),
        "CAL-CHAT": Calendar("CAL-CHAT", chat_ints),
        "CAL-PHONE": Calendar("CAL-PHONE", phone_ints),
        "CAL-247": Calendar("CAL-247", all247_ints),
    }


_DEFAULT_CALENDARS: Final[dict[str, Calendar]] = _build_default_calendars()


def get_calendar(cal: str | Calendar | pd.DataFrame) -> Calendar:
    """Resolve calendar instance from code, DataFrame, or object."""
    if isinstance(cal, Calendar):
        return cal
    if isinstance(cal, str):
        if cal in _DEFAULT_CALENDARS:
            return _DEFAULT_CALENDARS[cal]
        raise ValueError(f"Unknown calendar code '{cal}'. Known: {list(_DEFAULT_CALENDARS.keys())}")
    if isinstance(cal, pd.DataFrame):
        code = str(cal["calendar_code"].iloc[0])
        return Calendar.from_windows_df(code, cal)
    raise TypeError(f"Cannot resolve calendar from type {type(cal)}")


def is_open(cal: str | Calendar | pd.DataFrame, t: datetime.datetime | str) -> bool:
    """True if t falls in a half-open window of cal on its weekday (business_rules.md 8.1)."""
    c = get_calendar(cal)
    return c.is_open(_ensure_utc(t))


def next_open(
    cal: str | Calendar | pd.DataFrame, t: datetime.datetime | str
) -> datetime.datetime:
    """Return t if open, else the next window start (business_rules.md 8.1)."""
    c = get_calendar(cal)
    return c.next_open(_ensure_utc(t))


def add_business_minutes(
    cal: str | Calendar | pd.DataFrame, start: datetime.datetime | str, m: int
) -> datetime.datetime:
    """Advance from start, consuming only open time. m = 0 returns start (business_rules.md 8.1)."""
    c = get_calendar(cal)
    return c.add_business_minutes(_ensure_utc(start), m)


def business_minutes(
    cal: str | Calendar | pd.DataFrame,
    a: datetime.datetime | str,
    b: datetime.datetime | str,
) -> int | float:
    """Total open time in [a, b); 0 if b <= a (business_rules.md 8.1)."""
    c = get_calendar(cal)
    return c.business_minutes(_ensure_utc(a), _ensure_utc(b))
