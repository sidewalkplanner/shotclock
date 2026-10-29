"""The clock engine: apply the rule library to one case's ledger events.

A city's clock is the set of days the city held the file inside a clock's window, minus the
pauses the state's rule allows. Where the source cannot say who held the file
(``Holder.UNKNOWN``) the engine counts that time against the city, which is the safe
direction for deadline alerts, and flags the result with ``assumes_city_time``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from shotclock.days import DayBasis, add_days, count_days, covered_dates
from shotclock.ledger import (
    Case,
    CaseEvent,
    EventType,
    Holder,
    holder_intervals,
    sort_events,
    submitted_at,
)
from shotclock.rules import Clock, ClockKind, Rule, RuleLibrary


class ClockStatus(StrEnum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    PAUSED = "paused"  # open, file is with the applicant
    OVERDUE = "overdue"  # open, past the limit
    MET = "met"
    MISSED = "missed"
    WITHDRAWN = "withdrawn"


OPEN_STATUSES = frozenset(
    {ClockStatus.ON_TRACK, ClockStatus.AT_RISK, ClockStatus.PAUSED, ClockStatus.OVERDUE}
)


@dataclass(frozen=True)
class ClockResult:
    case_id: str
    rule_id: str
    clock_id: str
    label: str
    cycle: int | None
    day_basis: DayBasis
    limit_days: int
    started_at: datetime
    stopped_at: datetime | None
    days_used: int
    days_remaining: int
    due_date: date | None  # projected; None when closed or paused
    status: ClockStatus
    late_days: int
    penalty_estimate: float | None  # share of the fee forfeited, where the law is arithmetic
    assumes_city_time: bool
    rule_verified: bool


def _first(events: list[CaseEvent], types: Iterable[EventType], after: int) -> int | None:
    wanted = set(types)
    for i in range(after, len(events)):
        if events[i].event in wanted:
            return i
    return None


def _windows(clock: Clock, events: list[CaseEvent]) -> list[tuple[int | None, int, int | None]]:
    """``(cycle, start_index, stop_index_or_None)`` for each window of this clock."""
    stop_types = [*clock.stop, EventType.WITHDRAWN]
    start = _first(events, clock.start, 0)
    if start is None:
        return []
    if clock.kind is ClockKind.SPAN:
        return [(None, start, _first(events, stop_types, start + 1))]

    out: list[tuple[int | None, int, int | None]] = []
    cycle = 1
    while start is not None:
        stop = _first(events, stop_types, start + 1)
        out.append((cycle, start, stop))
        if stop is None or events[stop].event is not EventType.COMMENTS_ISSUED:
            break
        start = _first(events, clock.restart, stop + 1)
        cycle += 1
    return out


def _holder_at(events: list[CaseEvent], when: datetime) -> Holder:
    holder = Holder.NONE
    for ev in events:
        if ev.at > when:
            break
        holder = ev.holder
    return holder


def evaluate_clock(
    case: Case,
    events: list[CaseEvent],
    rule: Rule,
    clock: Clock,
    as_of: datetime,
    at_risk_fraction: float = 0.2,
) -> list[ClockResult]:
    events = sort_events(e for e in events if e.at <= as_of)
    basis = rule.basis_for(clock)
    results = []
    for cycle, start_i, stop_i in _windows(clock, events):
        start_ev = events[start_i]
        stop_ev = events[stop_i] if stop_i is not None else None
        end = stop_ev.at if stop_ev else as_of

        held = [
            (h, max(a, start_ev.at), min(b, end))
            for h, a, b, _ in holder_intervals(events, until=end)
            if b > start_ev.at and a < end
        ]
        if rule.pause_when_applicant_holds:
            counted = [(a, b) for h, a, b in held if h in (Holder.CITY, Holder.UNKNOWN)]
        else:
            counted = [(start_ev.at, end)]
        assumes_city_time = any(h is Holder.UNKNOWN for h, _, _ in held)

        limit = clock.limit_for(case, cycle or 1)
        used = count_days(covered_dates(counted), basis, case.state)
        remaining = limit - used
        due: date | None = None

        if stop_ev is not None:
            if stop_ev.event is EventType.WITHDRAWN:
                status = ClockStatus.WITHDRAWN
            else:
                status = ClockStatus.MET if used <= limit else ClockStatus.MISSED
        elif remaining < 0:
            status = ClockStatus.OVERDUE
        elif rule.pause_when_applicant_holds and _holder_at(events, as_of) is Holder.APPLICANT:
            status = ClockStatus.PAUSED
        else:
            due = add_days(as_of.date(), remaining, basis, case.state)
            at_risk = remaining <= math.ceil(limit * at_risk_fraction)
            status = ClockStatus.AT_RISK if at_risk else ClockStatus.ON_TRACK

        late = max(0, used - limit)
        rate = rule.penalty.fee_reduction_per_late_day if rule.penalty else None
        results.append(
            ClockResult(
                case_id=case.case_id,
                rule_id=rule.id,
                clock_id=clock.id,
                label=clock.label,
                cycle=cycle,
                day_basis=basis,
                limit_days=limit,
                started_at=start_ev.at,
                stopped_at=stop_ev.at if stop_ev else None,
                days_used=used,
                days_remaining=remaining,
                due_date=due,
                status=status,
                late_days=late,
                penalty_estimate=min(1.0, late * rate) if rate and late else None,
                assumes_city_time=assumes_city_time,
                rule_verified=rule.verified,
            )
        )
    return results


def evaluate_case(
    case: Case,
    events: list[CaseEvent],
    library: RuleLibrary,
    as_of: datetime,
    at_risk_fraction: float = 0.2,
) -> list[ClockResult]:
    """Every statutory clock that applies to this case, as of ``as_of``."""
    ordered = sort_events(events)
    submitted = submitted_at(ordered)
    if submitted is None:
        return []
    out: list[ClockResult] = []
    for rule in library.applicable(case, submitted.date()):
        for clock in rule.clocks:
            out.extend(evaluate_clock(case, ordered, rule, clock, as_of, at_risk_fraction))
    return out
