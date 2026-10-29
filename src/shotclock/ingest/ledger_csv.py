"""Ledger-shaped CSV: the target format for any city export we map by hand or by script.

One row per event. Required columns: case_id, case_type, event, at.
Optional: holder, department, source_ref, and any ``attr_<name>`` column, which becomes a case
attribute (numbers are parsed, so ``attr_affordable_share`` = 0.6 works with rule conditions).
Attribute values are taken from the first row of each case that has them.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shotclock.ingest import ImportReport, parse_date, parse_value
from shotclock.ledger import Case, CaseEvent, Jurisdiction


def read_ledger_csv(
    path: Path, jurisdiction: Jurisdiction, source: str | None = None
) -> tuple[list[tuple[Case, list[CaseEvent]]], ImportReport]:
    source = source or path.stem
    report = ImportReport(source=f"{source}:{path.name}")
    cases: dict[str, tuple[str, dict[str, Any]]] = {}
    events: dict[str, list[CaseEvent]] = {}

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"case_id", "case_type", "event", "at"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        for row in reader:
            report.rows += 1
            case_id = row["case_id"].strip()
            try:
                event = CaseEvent(
                    case_id=case_id,
                    event=row["event"].strip(),
                    at=parse_date(row["at"]),
                    holder=(row.get("holder") or "").strip() or None,
                    department=(row.get("department") or "").strip() or None,
                    source=source,
                    source_ref=(row.get("source_ref") or "").strip() or None,
                )
            except (ValidationError, ValueError):
                report.skip("invalid_event_row")
                continue
            case_type, attrs = cases.setdefault(case_id, (row["case_type"].strip(), {}))
            for col, raw in row.items():
                if col.startswith("attr_") and raw and raw.strip() and col[5:] not in attrs:
                    attrs[col[5:]] = parse_value(raw.strip())
            events.setdefault(case_id, []).append(event)

    out = [
        (
            Case(
                case_id=cid,
                jurisdiction=jurisdiction.jurisdiction,
                state=jurisdiction.state,
                case_type=case_type,
                attributes=attrs,
            ),
            sorted(events[cid], key=lambda e: e.at),
        )
        for cid, (case_type, attrs) in cases.items()
    ]
    report.cases = len(out)
    return out, report
