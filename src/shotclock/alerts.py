"""Deadline alerts for city staff: every clock that is past due or about to be.

The compliance promise is "you will not miss a statutory deadline without knowing in
advance". ``build_alerts`` finds the clocks that need action, and ``render_text`` turns
them into a digest grouped by the department holding each file, ready to email each morning.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from shotclock.clock import ClockStatus, describe, evaluate_case
from shotclock.ledger import CLOSING_EVENTS, Holder, sort_events
from shotclock.rules import RuleLibrary
from shotclock.store import Store

UNASSIGNED = "Unassigned"


@dataclass(frozen=True)
class Alert:
    case_id: str
    case_type: str
    address: str | None
    department: str  # who holds the file now, from the latest event
    waiting_on: str  # city | applicant | unknown
    rule_id: str
    clock: str
    status: str  # overdue | at_risk | on_track
    days_remaining: int  # negative when overdue
    due_date: date | None
    remedy: str | None  # what the applicant may now do because the city is late
    rule_verified: bool


def build_alerts(
    store: Store,
    jurisdiction: str,
    library: RuleLibrary,
    as_of: datetime,
    within_days: int = 5,
) -> list[Alert]:
    """Overdue clocks, at-risk clocks, and any clock due within ``within_days`` counting days.

    Paused clocks (file with the applicant) are left out unless already overdue: the city
    cannot act on them, but an overdue clock is a liability whoever holds the file.
    """
    j = store.get_jurisdiction(jurisdiction)
    alerts: list[Alert] = []
    for case, events in store.iter_cases(jurisdiction):
        events = sort_events(e for e in events if e.at <= as_of)
        if not events or events[-1].event in CLOSING_EVENTS:
            continue
        last = events[-1]
        for r in evaluate_case(case, events, library, as_of, jurisdiction=j):
            due_soon = r.status is ClockStatus.ON_TRACK and r.days_remaining <= within_days
            if r.status not in (ClockStatus.OVERDUE, ClockStatus.AT_RISK) and not due_soon:
                continue
            alerts.append(
                Alert(
                    case_id=case.case_id,
                    case_type=case.case_type,
                    address=case.attributes.get("address"),
                    department=last.department or UNASSIGNED,
                    waiting_on=last.holder.value if last.holder is not Holder.NONE else "none",
                    rule_id=r.rule_id,
                    clock=describe(r),
                    status=r.status.value,
                    days_remaining=r.days_remaining,
                    due_date=r.due_date,
                    remedy=r.remedy,
                    rule_verified=r.rule_verified,
                )
            )
    alerts.sort(key=lambda a: (a.status != ClockStatus.OVERDUE.value, a.days_remaining, a.case_id))
    return alerts


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _when(a: Alert) -> str:
    if a.days_remaining < 0:
        return f"{_days(-a.days_remaining)} late"
    if a.days_remaining == 0:
        return "due today"
    due = f", {a.due_date:%a %b} {a.due_date.day}" if a.due_date else ""
    return f"{_days(a.days_remaining)} left{due}"


def render_text(name: str, alerts: list[Alert], as_of: datetime, within_days: int) -> str:
    overdue = [a for a in alerts if a.status == ClockStatus.OVERDUE.value]
    soon = [a for a in alerts if a.status != ClockStatus.OVERDUE.value]
    lines = [
        f"{name}: permit deadlines as of {as_of:%b} {as_of.day}, {as_of.year}",
        f"{len(overdue)} past a state deadline, {len(soon)} due soon "
        f"(at risk or within {within_days} days).",
    ]
    if not alerts:
        lines.append("\nNothing needs action today.")
    for title, group in (("PAST DEADLINE", overdue), ("DUE SOON", soon)):
        if not group:
            continue
        lines.append(f"\n{title}")
        by_dept: dict[str, list[Alert]] = defaultdict(list)
        for a in group:
            by_dept[a.department].append(a)
        for dept in sorted(by_dept, key=lambda d: (d == UNASSIGNED, d)):
            lines.append(f"  {dept}")
            for a in by_dept[dept]:
                where = f"  {a.address}" if a.address else ""
                with_applicant = (
                    "  (file is with the applicant)" if a.waiting_on == "applicant" else ""
                )
                lines.append(f"    {a.case_id}{where}: {_when(a)}{with_applicant}")
                lines.append(f"      {a.clock}")
                if a.remedy:
                    lines.append(f"      Applicant may now: {a.remedy}")
    if any(not a.rule_verified for a in alerts):
        lines.append(
            "\nNote: some deadlines come from state rules not yet verified against statute text."
        )
    return "\n".join(lines) + "\n"
