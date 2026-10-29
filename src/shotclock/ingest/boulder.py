"""City of Boulder construction permits (open data CSV export).

Built against the real export (Construction_Permits.csv, 2026-10): one row per permit with
PermitID (GUID), PermitNum, MasterPermitNum, AppliedDate / IssuedDate / CompletedDate
(YYYY/MM/DD), StatusCurrent, OriginalAddress, COBPIN (city parcel number), PermitType,
PermitWorkType, housing-unit counts and contractor fields. Descriptions can span lines.

Boulder publishes dates but no stage history, so the submittal is recorded with
``Holder.UNKNOWN`` and the data supports elapsed-time metrics only, never city-time clocks.

Void and withdrawn permits are skipped: they have no meaningful duration and would distort
a benchmark. PermitNum can repeat; repeats get the PermitID appended so no record is lost.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

from shotclock.ingest import ImportReport, parse_date, parse_value, slug
from shotclock.ledger import Case, CaseEvent, EventType, Holder, Jurisdiction

SOURCE = "boulder-open-data"
NOTES = [
    "Dates only, no stage history: elapsed time (city plus applicant), never city time.",
    "Permit numbering changes in 2018 and same-day issuance of alterations ends in 2020; "
    "confirm with the city before publishing trends across either.",
]
JURISDICTION = Jurisdiction(jurisdiction="boulder-co", name="City of Boulder", state="CO")

REQUIRED = {"PermitID", "PermitNum", "AppliedDate", "IssuedDate", "StatusCurrent", "PermitType"}
SKIP_STATUSES = {"void": "status_void", "withdrawn": "status_withdrawn"}
ATTRIBUTE_COLUMNS = {
    "StatusCurrent": "status_current",
    "PermitType": "permit_type",
    "PermitWorkType": "work_type",
    "MasterPermitNum": "master_permit",
    "CompletedDate": "completed_date",
    "OriginalAddress": "address",
    "OriginalZip": "zip",
    "COBPIN": "parcel_id",
    "PermitID": "permit_guid",
}
NUMBER_COLUMNS = {
    "NewHousingUnits": "units",
    "AffordableHousingUnits": "affordable_units",
    "EstProjectCost": "valuation",
}


def case_type(permit_type: str, work_type: str) -> str:
    """``PermitType`` alone lumps a new house in with a bathroom remodel, so add the work
    type unless it only repeats the permit type ("Electrical Permit" / "Electrical Permit")."""
    t, w = slug(permit_type), slug(work_type)
    if not w or w == "unknown" or w == t:
        return t
    return f"{t}__{w}"


def read_boulder(path: Path) -> tuple[list[tuple[Case, list[CaseEvent]]], ImportReport]:
    report = ImportReport(source=f"{SOURCE}:{path.name}")
    csv.field_size_limit(sys.maxsize)  # some descriptions are very long
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    report.rows = len(rows)
    if not rows:
        return [], report

    missing = REQUIRED - set(rows[0].keys())
    if missing:
        raise ValueError(f"{path.name}: not a Boulder permits export, missing {sorted(missing)}")

    counts = Counter((r.get("PermitNum") or "").strip() for r in rows)
    seen: set[str] = set()
    out: list[tuple[Case, list[CaseEvent]]] = []
    for row in rows:
        status = (row.get("StatusCurrent") or "").strip()
        if status.lower() in SKIP_STATUSES:
            report.skip(SKIP_STATUSES[status.lower()])
            continue
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
        for col, key in NUMBER_COLUMNS.items():
            raw = (row.get(col) or "").strip()
            if raw:
                value = parse_value(raw)
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                attributes[key] = value
        units, affordable = attributes.get("units"), attributes.get("affordable_units")
        if isinstance(units, int | float) and isinstance(affordable, int | float) and units > 0:
            attributes["affordable_share"] = round(affordable / units, 4)

        case = Case(
            case_id=case_id,
            jurisdiction=JURISDICTION.jurisdiction,
            state=JURISDICTION.state,
            case_type=case_type(row.get("PermitType") or "", row.get("PermitWorkType") or ""),
            attributes=attributes,
        )
        out.append((case, events))

    report.cases = len(out)
    return out, report
