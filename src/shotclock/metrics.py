"""Process-loop and benchmark metrics over the ledger."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from shotclock.ledger import (
    CLOSING_EVENTS,
    CaseEvent,
    EventType,
    Holder,
    holder_intervals,
    submitted_at,
)
from shotclock.store import Store


@dataclass(frozen=True)
class CaseProcess:
    """Where one case's time went, in hours."""

    case_id: str
    hours_by_holder: dict[Holder, float]
    hours_by_department: dict[str, float]  # city-held time only
    review_cycles: int


def case_process(case_id: str, events: list[CaseEvent], as_of: datetime) -> CaseProcess:
    by_holder: dict[Holder, float] = defaultdict(float)
    by_dept: dict[str, float] = defaultdict(float)
    for holder, start, end, dept in holder_intervals(events, until=as_of):
        hours = (end - start).total_seconds() / 3600
        by_holder[holder] += hours
        if holder is Holder.CITY:
            by_dept[dept or "unassigned"] += hours
    cycles = sum(1 for e in events if e.event is EventType.COMMENTS_ISSUED)
    if any(e.event in (EventType.APPROVED, EventType.DENIED) for e in events):
        cycles += 1
    return CaseProcess(case_id, dict(by_holder), dict(by_dept), cycles)


# Elapsed time from first submittal to first issuance, by case type and submittal year.
# This is total elapsed time (city plus applicant). It is the only honest metric for
# date-only open data, and every published figure from it must say so.
_ELAPSED_SQL = """
WITH spans AS (
    SELECT
        c.case_type,
        min(e.occurred_at) FILTER (WHERE e.event = 'submitted') AS submitted_at,
        min(e.occurred_at) FILTER (WHERE e.event = 'issued') AS issued_at
    FROM cases c
    JOIN case_events e USING (jurisdiction, case_id)
    WHERE c.jurisdiction = $jurisdiction
    GROUP BY c.case_id, c.case_type
)
SELECT
    case_type,
    year(submitted_at) AS submitted_year,
    count(*) AS cases,
    count(issued_at) AS issued,
    round(1 - count(issued_at) / count(*), 4) AS open_share,
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.5) AS median_days,
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.75) AS p75_days,
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.9) AS p90_days
FROM spans
WHERE submitted_at IS NOT NULL
  AND (issued_at IS NULL OR issued_at >= submitted_at)
  AND year(submitted_at) >= $since
GROUP BY ALL
HAVING count(issued_at) >= $min_cases
ORDER BY case_type, submitted_year
"""


@dataclass(frozen=True)
class ElapsedRow:
    case_type: str
    submitted_year: int
    cases: int
    issued: int
    open_share: float  # not yet issued; above CENSORED_SHARE the percentiles run fast
    median_days: float
    p75_days: float
    p90_days: float


# Percentiles cover issued cases only. When many cases in a group are still open, the slow
# ones are missing and the percentiles understate the wait (right-censoring): recent years
# always look faster than they will turn out to be.
CENSORED_SHARE = 0.05


def elapsed_benchmark(
    store: Store, jurisdiction: str, min_cases: int = 10, since: int = 0
) -> list[ElapsedRow]:
    """Submittal-to-issuance percentiles. Groups with fewer than ``min_cases`` issued are
    dropped; ``since`` drops submittal years before it."""
    rows = store.con.execute(
        _ELAPSED_SQL, {"jurisdiction": jurisdiction, "min_cases": min_cases, "since": since}
    ).fetchall()
    return [ElapsedRow(*r) for r in rows]


# -- process report ------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks, the same as DuckDB's quantile_cont."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass(frozen=True)
class CaseTypeProcess:
    case_type: str
    decided: int
    median_days: float  # submittal to approval or denial
    p90_days: float
    median_city_days: float
    median_applicant_days: float
    median_rounds: float
    open: int
    median_open_age_days: float | None


@dataclass(frozen=True)
class DepartmentLoad:
    department: str
    city_days: float  # summed across cases
    share: float  # of all city-held time
    cases: int


@dataclass(frozen=True)
class ProcessReport:
    jurisdiction: str
    as_of: datetime
    by_case_type: list[CaseTypeProcess]
    by_department: list[DepartmentLoad]
    open_with_city: int
    open_with_applicant: int
    open_holder_unknown: int
    median_open_age_days: float | None
    unknown_share: float  # share of measured time with no recorded holder


def process_report(store: Store, jurisdiction: str, as_of: datetime) -> ProcessReport:
    """Where cases spend their time: city vs applicant, by department, and what is open now.

    Decided cases are measured from submittal to the decision, so time after approval
    (fees, pick-up) is not counted against anyone. Open cases are measured to ``as_of``.
    """
    decided: dict[str, list[tuple[float, float, float, int]]] = defaultdict(list)
    open_ages: dict[str, list[float]] = defaultdict(list)
    dept_hours: dict[str, float] = defaultdict(float)
    dept_cases: dict[str, set[str]] = defaultdict(set)
    open_with = {Holder.CITY: 0, Holder.APPLICANT: 0, Holder.UNKNOWN: 0}
    total_hours = unknown_hours = 0.0

    for case, events in store.iter_cases(jurisdiction):
        events = sorted((e for e in events if e.at <= as_of), key=lambda e: e.at)
        start = submitted_at(events)
        if start is None:
            continue
        # Issuance implies approval for sources that never record the approval itself.
        decision = next(
            (e.at for e in events if e.event in (EventType.APPROVED, EventType.DENIED)),
            next((e.at for e in events if e.event is EventType.ISSUED), None),
        )
        closed = any(e.event in CLOSING_EVENTS for e in events)
        if decision is None and closed:
            continue  # withdrawn without a decision: no meaningful duration
        proc = case_process(case.case_id, events, as_of=decision or as_of)
        hours = sum(proc.hours_by_holder.values())
        total_hours += hours
        unknown_hours += proc.hours_by_holder.get(Holder.UNKNOWN, 0.0)
        for dept, h in proc.hours_by_department.items():
            dept_hours[dept] += h
            dept_cases[dept].add(case.case_id)

        if decision is not None:
            decided[case.case_type].append(
                (
                    (decision - start).total_seconds() / 86400,
                    proc.hours_by_holder.get(Holder.CITY, 0.0) / 24,
                    proc.hours_by_holder.get(Holder.APPLICANT, 0.0) / 24,
                    proc.review_cycles,
                )
            )
        else:
            open_ages[case.case_type].append((as_of - start).total_seconds() / 86400)
            holder = events[-1].holder
            if holder in open_with:
                open_with[holder] += 1

    rows = []
    for case_type in sorted(decided.keys() | open_ages.keys()):
        d = decided.get(case_type, [])
        ages = open_ages.get(case_type, [])
        col = list(zip(*d, strict=True)) if d else [[], [], [], []]
        rows.append(
            CaseTypeProcess(
                case_type=case_type,
                decided=len(d),
                median_days=percentile(col[0], 0.5) if d else float("nan"),
                p90_days=percentile(col[0], 0.9) if d else float("nan"),
                median_city_days=percentile(col[1], 0.5) if d else float("nan"),
                median_applicant_days=percentile(col[2], 0.5) if d else float("nan"),
                median_rounds=percentile(col[3], 0.5) if d else float("nan"),
                open=len(ages),
                median_open_age_days=percentile(ages, 0.5) if ages else None,
            )
        )
    all_ages = [a for ages in open_ages.values() for a in ages]
    city_total = sum(dept_hours.values())
    depts = sorted(
        (
            DepartmentLoad(
                dept, h / 24, h / city_total if city_total else 0.0, len(dept_cases[dept])
            )
            for dept, h in dept_hours.items()
        ),
        key=lambda x: -x.city_days,
    )
    return ProcessReport(
        jurisdiction=jurisdiction,
        as_of=as_of,
        by_case_type=rows,
        by_department=depts,
        open_with_city=open_with[Holder.CITY],
        open_with_applicant=open_with[Holder.APPLICANT],
        open_holder_unknown=open_with[Holder.UNKNOWN],
        median_open_age_days=percentile(all_ages, 0.5) if all_ages else None,
        unknown_share=unknown_hours / total_hours if total_hours else 0.0,
    )


def render_process(name: str, r: ProcessReport) -> str:
    def num(x: float | None) -> str:
        return "-" if x is None or x != x else f"{x:.0f}"

    lines = [f"{name}: where cases wait, as of {r.as_of:%b} {r.as_of.day}, {r.as_of.year}", ""]
    if r.unknown_share > 0.5:
        lines += [
            f"{r.unknown_share:.0%} of measured time has no recorded holder, so the split",
            "between city and applicant time below is not meaningful. This source records",
            "dates but not stages; a workflow export is needed.",
            "",
        ]
    lines.append("Decided cases, submittal to decision (days)")
    lines.append(
        f"  {'case type':<24}{'cases':>6}{'median':>8}{'p90':>6}"
        f"{'city':>6}{'appl.':>6}{'rounds':>8}"
    )
    no_stages = r.unknown_share > 0.5
    for row in r.by_case_type:
        if not row.decided:
            continue
        split = (
            ("-", "-", "-")
            if no_stages
            else (
                num(row.median_city_days),
                num(row.median_applicant_days),
                num(row.median_rounds),
            )
        )
        lines.append(
            f"  {row.case_type.replace('_', ' '):<24}{row.decided:>6}{num(row.median_days):>8}"
            f"{num(row.p90_days):>6}{split[0]:>6}{split[1]:>6}{split[2]:>8}"
        )
    if r.by_department:
        lines += ["", "City-held time by department"]
        for d in r.by_department:
            days = f"{d.city_days:.0f} days across {d.cases} case{'' if d.cases == 1 else 's'}"
            lines.append(f"  {d.department:<24}{d.share:>5.0%}  {days}")
    open_total = sum(row.open for row in r.by_case_type)
    if open_total:
        parts = [
            f"{n} {label}"
            for n, label in (
                (r.open_with_city, "with the city"),
                (r.open_with_applicant, "with the applicant"),
                (r.open_holder_unknown, "holder not recorded"),
            )
            if n
        ]
        noun = "case" if open_total == 1 else "cases"
        lines += [
            "",
            f"Open now: {open_total} {noun} ({', '.join(parts)}), "
            f"median age {num(r.median_open_age_days)} days.",
        ]
    return "\n".join(lines) + "\n"
