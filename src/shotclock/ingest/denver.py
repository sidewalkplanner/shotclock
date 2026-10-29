"""City and County of Denver construction permits (Open Data Catalog CSV exports).

Built against the 2026-10 exports ODC_DEV_COMMERCIALCONSTPERMIT_P and
ODC_DEV_RESIDENTIALCONSTPERMIT_P: one row per permit with permit_num
(e.g. 2020-COMMCON-0001796, 2012-RESCON-0000004625), date_received, date_issued,
final_date, cancel, class, units, valuation, permit_fee, SCHEDNUM (parcel), NEIGHBORHOOD,
log_num and address fields. Some rows omit trailing columns.

Two properties limit what this data can say:

- It lists issued permits only. Pending and abandoned applications never appear, so a
  benchmark from it cannot see right-censoring: recent years look faster than they are,
  by an amount the data cannot measure.
- Denver's documentation says date_issued is the day the permit was paid for, so
  issuance time includes the applicant's time to pay.

Like Boulder, there is no stage history: the submittal is recorded with
``Holder.UNKNOWN`` and the data supports elapsed-time metrics only. Cancelled permits are
skipped.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from shotclock.ingest import ImportReport, parse_date, parse_value, slug
from shotclock.ledger import Case, CaseEvent, EventType, Holder, Jurisdiction

SOURCE = "denver-open-data"
NOTES = [
    "Lists issued permits only: pending and abandoned applications are absent, so recent "
    "years are censored by an amount the data cannot show.",
    "date_issued is the day the permit was paid for, per Denver's documentation, so it "
    "includes the applicant's time to pay.",
    "Dates only, no stage history: elapsed time (city plus applicant), never city time.",
    "Issue times are midnight-only from 2015, and residential alterations stop being "
    "issued same-day in 2020; confirm with the city before publishing trends across either.",
]
JURISDICTION = Jurisdiction(jurisdiction="denver-co", name="City and County of Denver", state="CO")
REQUIRED = {"permit_num", "date_received", "date_issued", "class"}
SEGMENTS = {"COMMCON": "commercial", "RESCON": "residential"}
ATTRIBUTE_COLUMNS = {
    "address": "address",
    "SCHEDNUM": "parcel_id",
    "NEIGHBORHOOD": "neighborhood",
    "log_num": "project_log",
    "contractor_name": "contractor",
    "final_date": "final_date",
    "date_co_issued": "co_issued_date",
}
NUMBER_COLUMNS = {"units": "units", "valuation": "valuation", "permit_fee": "permit_fee"}


def _get(row: dict, col: str) -> str:
    """Short rows leave trailing columns as None."""
    return (row.get(col) or "").strip()


def case_type(permit_num: str, permit_class: str, units: str = "") -> str:
    """``commercial__new_building`` and the like. Class labels vary in case and sometimes
    carry a numeric prefix ("4 - Alteration"), so both are normalized away.

    Residential "new building" mixes homes with accessory structures: in the 2026-10 export
    a third have no housing units and most of those are under $50k (garages, sheds). Those
    become ``residential__new_building_no_units`` so a benchmark of new homes stays clean.
    """
    match = re.search(r"-(COMMCON|RESCON)-", permit_num)
    segment = SEGMENTS[match.group(1)] if match else "unknown"
    label = slug(re.sub(r"^\s*\d+\s*-\s*", "", permit_class or ""))
    if (
        segment == "residential"
        and label == "new_building"
        and parse_value(units or "0") in (0, "")
    ):
        label = "new_building_no_units"
    return f"{segment}__{label}"


def read_denver(paths: list[Path]) -> tuple[list[tuple[Case, list[CaseEvent]]], ImportReport]:
    report = ImportReport(source=f"{SOURCE}:{'+'.join(p.name for p in paths)}")
    seen: set[str] = set()
    out: list[tuple[Case, list[CaseEvent]]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            missing = REQUIRED - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path.name}: not a Denver permits export, missing {missing}")
            for row in reader:
                report.rows += 1
                if None in row:
                    report.warn("extra_columns")
                num = _get(row, "permit_num")
                if not num:
                    report.skip("no_permit_number")
                    continue
                if num in seen:
                    report.skip("duplicate_row")
                    continue
                seen.add(num)
                if _get(row, "cancel"):
                    report.skip("cancelled")
                    continue
                try:
                    received = parse_date(_get(row, "date_received"))
                    issued = parse_date(_get(row, "date_issued"))
                except ValueError:
                    report.skip("unparseable_date")
                    continue
                if received is None:
                    report.skip("no_received_date")
                    continue

                events = [
                    CaseEvent(
                        case_id=num,
                        event=EventType.SUBMITTED,
                        at=received,
                        holder=Holder.UNKNOWN,
                        source=SOURCE,
                        source_ref=_get(row, "GLOBALID") or num,
                    )
                ]
                if issued is not None:
                    if issued < received:
                        report.warn("issued_before_received")
                    else:
                        events.append(
                            CaseEvent(
                                case_id=num,
                                event=EventType.ISSUED,
                                at=issued,
                                source=SOURCE,
                                source_ref=_get(row, "GLOBALID") or num,
                            )
                        )
                attributes = {
                    key: _get(row, col) for col, key in ATTRIBUTE_COLUMNS.items() if _get(row, col)
                }
                attributes["permit_class"] = _get(row, "class")
                for col, key in NUMBER_COLUMNS.items():
                    if _get(row, col):
                        value = parse_value(_get(row, col))
                        if isinstance(value, float) and value.is_integer():
                            value = int(value)
                        attributes[key] = value
                out.append(
                    (
                        Case(
                            case_id=num,
                            jurisdiction=JURISDICTION.jurisdiction,
                            state=JURISDICTION.state,
                            case_type=case_type(num, _get(row, "class"), _get(row, "units")),
                            attributes=attributes,
                        ),
                        events,
                    )
                )
    report.cases = len(out)
    return out, report
