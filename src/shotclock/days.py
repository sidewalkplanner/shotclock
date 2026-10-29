"""Statutory day counting.

Convention: an interval from ``a`` to ``b`` covers the dates strictly after ``a``'s date up to
and including ``b``'s date. The day the file arrives is not counted; the day it leaves is.
That matches the usual statutory rule ("within 45 days of receipt") and means back-to-back
intervals on the same day never double count.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache

import holidays as holidays_lib


class DayBasis(StrEnum):
    CALENDAR = "calendar"
    BUSINESS = "business"


@lru_cache(maxsize=64)
def _holiday_calendar(state: str) -> holidays_lib.HolidayBase:
    return holidays_lib.country_holidays("US", subdiv=state.upper())


def is_counting_day(d: date, basis: DayBasis, state: str) -> bool:
    if basis is DayBasis.CALENDAR:
        return True
    return d.weekday() < 5 and d not in _holiday_calendar(state)


def covered_dates(intervals: Iterable[tuple[datetime, datetime]]) -> set[date]:
    """All dates covered by the intervals, under the convention above."""
    out: set[date] = set()
    for start, end in intervals:
        d = start.date() + timedelta(days=1)
        while d <= end.date():
            out.add(d)
            d += timedelta(days=1)
    return out


def count_days(dates: Iterable[date], basis: DayBasis, state: str) -> int:
    return sum(1 for d in dates if is_counting_day(d, basis, state))


def add_days(start: date, n: int, basis: DayBasis, state: str) -> date:
    """The date on which the ``n``-th counting day after ``start`` falls."""
    d = start
    remaining = n
    while remaining > 0:
        d += timedelta(days=1)
        if is_counting_day(d, basis, state):
            remaining -= 1
    return d
