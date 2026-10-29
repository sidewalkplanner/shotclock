"""The state rule library.

Each YAML file in ``data/`` encodes one law: which cases it covers, its clocks, how it pauses,
and what happens when a city misses it. Rules are versioned by effective date so a case is
always measured against the law in force when it was submitted.

Every rule lists the claims it encodes as ``checks``. A rule is verified only when every
check is confirmed or corrected against statute text with a pin cite. Nothing unverified
should appear in sales material or a public report.
"""

from __future__ import annotations

import operator
from datetime import date
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shotclock.days import DayBasis
from shotclock.ledger import Case, EventType

_OPS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
    "in": lambda a, b: a in b,
}


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True)

    attribute: str
    op: str = Field(pattern=r"^(>=|>|<=|<|==|!=|in)$")
    value: Any

    def holds(self, case: Case) -> bool | None:
        """None when the case lacks the attribute, so applicability cannot be decided."""
        if self.attribute not in case.attributes:
            return None
        try:
            return bool(_OPS[self.op](case.attributes[self.attribute], self.value))
        except TypeError:  # e.g. a text value compared with a number
            return None


class AppliesTo(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_types: list[str] = Field(default_factory=lambda: ["*"])
    conditions: list[Condition] = Field(default_factory=list)

    def matches(self, case: Case) -> bool | None:
        """True or False, or None when a condition's attribute is missing from the case."""
        if "*" not in self.case_types and case.case_type not in self.case_types:
            return False
        results = [c.holds(case) for c in self.conditions]
        if False in results:
            return False
        return None if None in results else True

    def missing_attributes(self, case: Case) -> list[str]:
        return [c.attribute for c in self.conditions if c.holds(case) is None]


class ClockKind(StrEnum):
    SPAN = "span"  # one clock from the first start event to the first stop event
    REVIEW_CYCLES = "review_cycles"  # one clock per review round, limits vary by round


class Clock(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    kind: ClockKind = ClockKind.SPAN
    start: list[EventType]  # span: starts the clock. review_cycles: starts round 1
    restart: list[EventType] = Field(default_factory=list)  # review_cycles: starts later rounds
    stop: list[EventType]
    day_basis: DayBasis | None = None  # overrides the rule's basis
    limit_days: int | None = None
    limit_days_by_case_type: dict[str, int] = Field(default_factory=dict)
    limit_days_by_cycle: list[int] = Field(default_factory=list)  # last value repeats

    @model_validator(mode="after")
    def _check(self) -> Clock:
        if self.kind is ClockKind.REVIEW_CYCLES:
            if not self.limit_days_by_cycle or not self.restart:
                raise ValueError(f"{self.id}: review_cycles needs limit_days_by_cycle and restart")
        elif self.limit_days is None and not self.limit_days_by_case_type:
            raise ValueError(f"{self.id}: span clock needs limit_days or limit_days_by_case_type")
        return self

    def limit_for(self, case: Case, cycle: int = 1) -> int:
        if self.kind is ClockKind.REVIEW_CYCLES:
            return self.limit_days_by_cycle[min(cycle, len(self.limit_days_by_cycle)) - 1]
        if case.case_type in self.limit_days_by_case_type:
            return self.limit_days_by_case_type[case.case_type]
        if self.limit_days is None:
            raise LookupError(f"{self.id}: no limit for case type {case.case_type!r}")
        return self.limit_days


class Penalty(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    # Structured where the law is arithmetic (Florida's fee reduction); otherwise text only.
    fee_reduction_per_late_day: float | None = None


class CheckStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"  # statute text says what the rule encodes
    CORRECTED = "corrected"  # the encoding was changed to match statute text


class Check(BaseModel):
    """One claim the rule encodes, to be checked against primary text."""

    model_config = ConfigDict(frozen=True)

    claim: str
    status: CheckStatus = CheckStatus.OPEN
    cite: str | None = None  # pin cite to the statute or bill section that settles it
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Check:
        if self.status is not CheckStatus.OPEN and not self.cite:
            raise ValueError(f"check {self.claim!r} is {self.status.value} but has no cite")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    state: str
    name: str
    citation: str
    effective_from: date
    effective_to: date | None = None
    checks: list[Check] = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    day_basis: DayBasis = DayBasis.CALENDAR
    pause_when_applicant_holds: bool = True
    clocks: list[Clock]
    penalty: Penalty | None = None
    transparency: str | None = None

    @property
    def verified(self) -> bool:
        return all(c.status is not CheckStatus.OPEN for c in self.checks)

    @property
    def open_checks(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.OPEN]

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on < self.effective_to)

    def basis_for(self, clock: Clock) -> DayBasis:
        return clock.day_basis or self.day_basis


class RuleLibrary:
    def __init__(self, rules: list[Rule]):
        ids = [r.id for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule ids in library")
        self.rules = rules

    @classmethod
    def load(cls, directory: Path | None = None) -> RuleLibrary:
        if directory is None:
            files = [
                f
                for f in resources.files("shotclock.rules").joinpath("data").iterdir()
                if f.name.endswith((".yaml", ".yml"))
            ]
        else:
            files = sorted(directory.glob("*.y*ml"))
        rules = [Rule.model_validate(yaml.safe_load(f.read_text())) for f in files]
        return cls(sorted(rules, key=lambda r: (r.state, r.effective_from, r.id)))

    def for_state(self, state: str) -> list[Rule]:
        return [r for r in self.rules if r.state == state.upper()]

    def applicable(self, case: Case, submitted_on: date) -> list[Rule]:
        """Rules covering this case, in the version in force on its submission date."""
        return [
            r
            for r in self.for_state(case.state)
            if r.in_force(submitted_on) and r.applies_to.matches(case) is True
        ]

    def undetermined(self, case: Case, submitted_on: date) -> list[tuple[Rule, list[str]]]:
        """Rules that may cover this case but need attributes the case lacks."""
        return [
            (r, r.applies_to.missing_attributes(case))
            for r in self.for_state(case.state)
            if r.in_force(submitted_on) and r.applies_to.matches(case) is None
        ]

    def get(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)
