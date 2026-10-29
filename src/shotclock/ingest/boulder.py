"""City of Boulder construction permits (open data CSV export).

Field names follow the city's construction permits data dictionary (updated 2023-05-18):
PermitNum, PermitID (GUID), AppliedDate, IssuedDate, CompletedDate (YYYY-MM-DD),
StatusCurrent, Description, OriginalAddress1 / OriginalCity / OriginalZip. Boulder publishes
dates but no stage history, so every event is recorded with ``Holder.UNKNOWN`` for the
submittal and the case supports elapsed-time metrics only, never city-time clocks.

PermitNum can repeat; repeats get the PermitID appended so no record is lost.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

from shotclock.ingest import ImportReport, parse_date
from shotclock.ledger import Case, CaseEvent, EventType, Holder, Jurisdiction

SOURCE = "boulder-open-data"
JURISDICTION = Jurisdiction(jurisdiction="boulder-co", name="City of Boulder", state="CO")

# The dictionary does not pin the type column's name across exports; take the first present.
CASE_TYPE_COLUMNS = (
    "PermitTypeMapped",
    "PermitType",
    "PermitClassMapped",
    "PermitClass",
    "WorkClass",
)
ATTRIBUTE_COLUMNS = {
    "StatusCurrent": "status_current",
    "Description": "description",
    "CompletedDate": "completed_date",
    "OriginalAddress1": "address",
    "OriginalZip": "zip",
    "PermitID": "permit_guid",
}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or "unknown"


def read_boulder(path: Path) -> tuple[list[tuple[Case, list[CaseEvent]]], ImportReport]:
    report = ImportReport(source=f"{SOURCE}:{path.name}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    report.rows = len(rows)
    if not rows:
        return [], report

    columns = rows[0].keys()
    missing = {"PermitNum", "AppliedDate", "IssuedDate"} - set(columns)
    if missing:
        raise ValueError(f"{path.name}: not a Boulder permits export, missing {sorted(missing)}")
    type_col = next((c for c in CASE_TYPE_COLUMNS if c in columns), None)
    if type_col is None:
        report.warn("no_case_type_column")

    counts = Counter((r.get("PermitNum") or "").strip() for r in rows)
    seen: set[str] = set()
    out: list[tuple[Case, list[CaseEvent]]] = []
    for row in rows:
        num = (row.get("PermitNum") or "").strip()
        guid = (row.get("PermitID") or "").strip()
        if not num and not guid:
            report.skip("no_permit_number")
            continue
        case_id = num or guid
        if num and counts[num] > 1:
            if not guid:
                report.skip("duplicate_permit_number_without_guid")
                continue
            case_id = f"{num}#{guid}"
            report.warn("duplicate_permit_number")
        if case_id in seen:
            report.skip("duplicate_row")
            continue
        seen.add(case_id)

        try:
            applied = parse_date(row.get("AppliedDate"))
            issued = parse_date(row.get("IssuedDate"))
        except ValueError:
            report.skip("unparseable_date")
            continue
        if applied is None:
            report.skip("no_applied_date")
            continue

        events = [
            CaseEvent(
                case_id=case_id,
                event=EventType.SUBMITTED,
                at=applied,
                holder=Holder.UNKNOWN,
                source=SOURCE,
                source_ref=guid or num,
            )
        ]
        if issued is not None:
            if issued < applied:
                report.warn("issued_before_applied")
            else:
                events.append(
                    CaseEvent(
                        case_id=case_id,
                        event=EventType.ISSUED,
                        at=issued,
                        source=SOURCE,
                        source_ref=guid or num,
                    )
                )

        attributes = {
            key: row[col].strip()
            for col, key in ATTRIBUTE_COLUMNS.items()
            if (row.get(col) or "").strip()
        }
        case = Case(
            case_id=case_id,
            jurisdiction=JURISDICTION.jurisdiction,
            state=JURISDICTION.state,
            case_type=slug(row[type_col]) if type_col and row.get(type_col) else "unknown",
            attributes=attributes,
        )
        out.append((case, events))

    report.cases = len(out)
    return out, report
