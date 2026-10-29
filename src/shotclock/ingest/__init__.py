"""Connectors: each turns one source system's export into ledger cases and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ImportReport:
    """What a connector did with a source file. Publish this next to any figure built on it."""

    source: str
    rows: int = 0
    cases: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def warn(self, reason: str) -> None:
        self.warnings[reason] = self.warnings.get(reason, 0) + 1


def parse_date(value: str | None) -> datetime | None:
    """Parse the date formats open-data portals actually emit. Blank means missing."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {value!r}")
