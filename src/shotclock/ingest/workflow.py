"""Workflow / task-history exports (Accela, Tyler EnerGov, GovWell, spreadsheets).

Every permitting system can export task history as rows: record, task, status, date,
department. A per-city mapping file (YAML) says which columns hold what and which
task/status combinations are ledger events. Mapping rules are tried in order; the first
match wins. Rows no rule matches are counted in the import report by task/status, so the
mapping can be refined against a real export until nothing important is left unmapped.

Mapping file shape::

    source: accela-elk-valley
    columns:            # logical field -> export column. case_id, at required
      case_id: Record ID
      case_type: Record Type
      at: Status Date
      task: Task
      status: Status
      department: Department
    attributes:         # case attribute -> export column (first non-blank value wins)
      units: Housing Units
    case_types:         # raw case_type -> normalized; unlisted values are slugified
      Building/Residential/New/NA: single_family_new
    events:
      - when: {task: Intake, status: Received}
        event: submitted
      - when: {task: "* Review", status: [Corrections Required, Revisions Required]}
        event: comments_issued
      - when: {task: "* Review", status: Approved}
        ignore: true    # one department's sign-off is not a case-level event

``when`` values are case-insensitive shell-style globs (``*``, ``?``); a list matches any.
"""

from __future__ import annotations

import csv
from fnmatch import fnmatchcase
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from shotclock.ingest import ImportReport, parse_date, parse_value, slug
from shotclock.ledger import Case, CaseEvent, EventType, Holder, Jurisdiction


class EventRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    when: dict[str, str | list[str]]
    event: EventType | None = None
    holder: Holder | None = None  # override the event type's default holder
    ignore: bool = False

    @model_validator(mode="after")
    def _check(self) -> EventRule:
        if self.ignore == (self.event is not None):
            raise ValueError("each events entry needs exactly one of event or ignore: true")
        return self

    def matches(self, fields: dict[str, str]) -> bool:
        for name, patterns in self.when.items():
            value = fields.get(name, "").lower()
            options = [patterns] if isinstance(patterns, str) else patterns
            if not any(fnmatchcase(value, p.lower()) for p in options):
                return False
        return True


class WorkflowMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    columns: dict[str, str]
    attributes: dict[str, str] = Field(default_factory=dict)
    case_types: dict[str, str] = Field(default_factory=dict)
    events: list[EventRule]

    @model_validator(mode="after")
    def _check(self) -> WorkflowMapping:
        missing = {"case_id", "at"} - self.columns.keys()
        if missing:
            raise ValueError(f"columns must map {sorted(missing)}")
        for rule in self.events:
            unknown = rule.when.keys() - self.columns.keys()
            if unknown:
                raise ValueError(f"events match on unmapped fields {sorted(unknown)}")
        return self

    @classmethod
    def load(cls, path: Path) -> WorkflowMapping:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def classify(self, fields: dict[str, str]) -> EventRule | None:
        return next((r for r in self.events if r.matches(fields)), None)


def read_workflow(
    path: Path, mapping: WorkflowMapping, jurisdiction: Jurisdiction
) -> tuple[list[tuple[Case, list[CaseEvent]]], ImportReport]:
    report = ImportReport(source=f"{mapping.source}:{path.name}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        needed = set(mapping.columns.values()) | set(mapping.attributes.values())
        missing = needed - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: export lacks columns {sorted(missing)}")

        case_types: dict[str, str] = {}
        attributes: dict[str, dict] = {}
        events: dict[str, list[CaseEvent]] = {}
        for i, row in enumerate(reader, start=2):  # line 1 is the header
            report.rows += 1
            fields = {k: (row[col] or "").strip() for k, col in mapping.columns.items()}
            case_id = fields["case_id"]
            if not case_id:
                report.skip("no_case_id")
                continue
            raw_type = fields.get("case_type", "")
            case_types.setdefault(case_id, mapping.case_types.get(raw_type) or slug(raw_type))
            attrs = attributes.setdefault(case_id, {})
            for name, col in mapping.attributes.items():
                if name not in attrs and (row[col] or "").strip():
                    attrs[name] = parse_value(row[col].strip())

            rule = mapping.classify(fields)
            if rule is None:
                label = "/".join(fields.get(k, "") for k in ("task", "status") if k in fields)
                report.warn(f"unmapped: {label or '(no task/status columns)'}")
                continue
            if rule.ignore:
                continue
            try:
                at = parse_date(fields["at"])
                if at is None:
                    raise ValueError("blank date")
                event = CaseEvent(
                    case_id=case_id,
                    event=rule.event,
                    at=at,
                    holder=rule.holder,
                    department=fields.get("department") or None,
                    source=mapping.source,
                    source_ref=f"{path.name}:{i}",
                )
            except (ValidationError, ValueError):
                report.skip("bad_date")
                continue
            events.setdefault(case_id, []).append(event)

    out = []
    for case_id, evs in events.items():
        case = Case(
            case_id=case_id,
            jurisdiction=jurisdiction.jurisdiction,
            state=jurisdiction.state,
            case_type=case_types[case_id],
            attributes=attributes[case_id],
        )
        out.append((case, sorted(evs, key=lambda e: e.at)))
    no_events = len(case_types) - len(out)
    if no_events:
        report.skipped["case_with_no_mapped_events"] = no_events
    report.cases = len(out)
    return out, report
