"""The normalized case ledger.

Every land-use case, from any source system, becomes a ``Case`` plus an ordered list of
``CaseEvent`` rows. Each event records what happened and who holds the file *from that
moment until the next event*. Every metric in the product is a query over these rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventType(StrEnum):
    SUBMITTED = "submitted"
    INCOMPLETE = "incomplete"  # city tells the applicant the submittal is missing items
    COMPLETE = "complete"  # city accepts the submittal as complete
    REVIEW_STARTED = "review_started"
    COMMENTS_ISSUED = "comments_issued"
    RESUBMITTED = "resubmitted"
    HEARING_SCHEDULED = "hearing_scheduled"  # placed on a planning commission / council agenda
    APPROVED = "approved"
    DENIED = "denied"
    ISSUED = "issued"
    SUSPENDED = "suspended"  # an issued permit suspended by the city
    CLOSED = "closed"  # finaled, expired, or otherwise closed out
    WITHDRAWN = "withdrawn"


class Holder(StrEnum):
    CITY = "city"
    APPLICANT = "applicant"
    NONE = "none"  # case is closed
    UNKNOWN = "unknown"  # source records dates but not who had the file (most open data)


# Who holds the file after each event when the source does not say.
DEFAULT_HOLDER: dict[EventType, Holder] = {
    EventType.SUBMITTED: Holder.CITY,
    EventType.INCOMPLETE: Holder.APPLICANT,
    EventType.COMPLETE: Holder.CITY,
    EventType.REVIEW_STARTED: Holder.CITY,
    EventType.COMMENTS_ISSUED: Holder.APPLICANT,
    EventType.RESUBMITTED: Holder.CITY,
    EventType.HEARING_SCHEDULED: Holder.CITY,
    EventType.APPROVED: Holder.APPLICANT,  # applicant pays fees / picks up the permit
    EventType.DENIED: Holder.NONE,
    EventType.ISSUED: Holder.NONE,
    EventType.SUSPENDED: Holder.NONE,
    EventType.CLOSED: Holder.NONE,
    EventType.WITHDRAWN: Holder.NONE,
}

CLOSING_EVENTS = frozenset(
    {EventType.DENIED, EventType.ISSUED, EventType.SUSPENDED, EventType.CLOSED, EventType.WITHDRAWN}
)


class Jurisdiction(BaseModel):
    model_config = ConfigDict(frozen=True)

    jurisdiction: str  # slug plus state, e.g. "boulder-co"
    name: str
    state: str
    # Facts some laws scope on (Georgia: 250+ permits a year; Arizona: 30,000+ people).
    population: int | None = None
    permits_per_year: int | None = None


JURISDICTION_FACTS = ("population", "permits_per_year")


class Case(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    jurisdiction: str  # e.g. "boulder-co"
    state: str  # two-letter USPS code; selects the rule library
    case_type: str  # e.g. "single_family_new"
    attributes: dict[str, Any] = Field(default_factory=dict)  # units, affordable_share, ...


class CaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    event: EventType
    at: datetime
    holder: Holder  # optional on input; filled from DEFAULT_HOLDER when omitted
    department: str | None = None  # zoning | building | fire | engineering | ...
    source: str | None = None  # source system, e.g. "boulder-open-data"
    source_ref: str | None = None  # row / record id in the source system

    @model_validator(mode="before")
    @classmethod
    def _fill_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get("holder") is None and data.get("event") is not None:
                data = {**data, "holder": DEFAULT_HOLDER[EventType(data["event"])]}
            at = data.get("at")
            if isinstance(at, date) and not isinstance(at, datetime):
                data = {**data, "at": datetime(at.year, at.month, at.day)}
        return data


def sort_events(events: Iterable[CaseEvent]) -> list[CaseEvent]:
    """Order events in time. Stable, so same-timestamp events keep source order."""
    return sorted(events, key=lambda e: e.at)


def submitted_at(events: Iterable[CaseEvent]) -> datetime | None:
    """When the case was submitted: the first ``submitted`` event, else the first event."""
    ordered = sort_events(events)
    if not ordered:
        return None
    return next((e.at for e in ordered if e.event is EventType.SUBMITTED), ordered[0].at)


def holder_intervals(
    events: Iterable[CaseEvent], until: datetime
) -> list[tuple[Holder, datetime, datetime, str | None]]:
    """Turn events into ``(holder, start, end, department)`` intervals.

    The department is the one on the event that ends the interval when it names one: the
    department that issues comments or approves is the one that held the file. Otherwise,
    and for the open final interval, it is the department on the event that started it.
    The final interval of an open case runs to ``until``. Zero-length intervals are dropped.
    """
    ordered = sort_events(events)
    out: list[tuple[Holder, datetime, datetime, str | None]] = []
    for i, ev in enumerate(ordered):
        if ev.holder is Holder.NONE:
            continue
        nxt = ordered[i + 1] if i + 1 < len(ordered) and ordered[i + 1].at <= until else None
        end = nxt.at if nxt else until
        if end > ev.at:
            dept = (nxt.department if nxt else None) or ev.department
            out.append((ev.holder, ev.at, end, dept))
    return out
