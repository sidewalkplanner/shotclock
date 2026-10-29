"""Process-loop and benchmark metrics over the ledger."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from shotclock.ledger import CaseEvent, EventType, Holder, holder_intervals
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
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.5) AS median_days,
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.75) AS p75_days,
    quantile_cont(date_diff('day', submitted_at, issued_at), 0.9) AS p90_days
FROM spans
WHERE submitted_at IS NOT NULL
  AND (issued_at IS NULL OR issued_at >= submitted_at)
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
    median_days: float
    p75_days: float
    p90_days: float


def elapsed_benchmark(store: Store, jurisdiction: str, min_cases: int = 10) -> list[ElapsedRow]:
    """Submittal-to-issuance percentiles. Groups with fewer than ``min_cases`` are dropped."""
    rows = store.con.execute(
        _ELAPSED_SQL, {"jurisdiction": jurisdiction, "min_cases": min_cases}
    ).fetchall()
    return [ElapsedRow(*r) for r in rows]
