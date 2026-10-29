"""DuckDB-backed ledger store. The schema is Postgres-compatible for the hosted move later."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from importlib import resources
from pathlib import Path

import duckdb

from shotclock.ledger import Case, CaseEvent, Jurisdiction


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.con = duckdb.connect(str(path))
        self.con.execute(resources.files("shotclock").joinpath("schema.sql").read_text())

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------------------------

    def upsert_jurisdiction(self, j: Jurisdiction) -> None:
        self.con.execute(
            """
            INSERT INTO jurisdictions VALUES (?, ?, ?, ?)
            ON CONFLICT (jurisdiction) DO UPDATE SET
                name = excluded.name, state = excluded.state,
                permits_per_year = excluded.permits_per_year
            """,
            [j.jurisdiction, j.name, j.state, j.permits_per_year],
        )

    def write_cases(self, rows: Iterable[tuple[Case, list[CaseEvent]]]) -> int:
        """Insert or replace cases with their full event history. Returns cases written."""
        cases, events = [], []
        for case, evs in rows:
            cases.append(
                (case.case_id, case.jurisdiction, case.case_type, json.dumps(case.attributes))
            )
            for seq, e in enumerate(evs):
                events.append(
                    (
                        case.jurisdiction,
                        case.case_id,
                        seq,
                        e.event.value,
                        e.holder.value,
                        e.department,
                        e.at,
                        e.source,
                        e.source_ref,
                    )
                )
        if not cases:
            return 0
        self.con.execute("BEGIN")
        try:
            self.con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _keys (jurisdiction TEXT, case_id TEXT)"
            )
            self.con.execute("DELETE FROM _keys")
            self.con.executemany("INSERT INTO _keys VALUES (?, ?)", [(c[1], c[0]) for c in cases])
            self.con.execute(
                "DELETE FROM case_events WHERE (jurisdiction, case_id) IN (SELECT * FROM _keys)"
            )
            self.con.execute(
                "DELETE FROM cases WHERE (jurisdiction, case_id) IN (SELECT * FROM _keys)"
            )
            self.con.executemany("INSERT INTO cases VALUES (?, ?, ?, ?)", cases)
            if events:
                self.con.executemany(
                    "INSERT INTO case_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", events
                )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return len(cases)

    # -- reads ----------------------------------------------------------------------------

    def get_case(self, jurisdiction: str, case_id: str) -> tuple[Case, list[CaseEvent]]:
        row = self.con.execute(
            """
            SELECT c.case_id, c.jurisdiction, j.state, c.case_type, c.attributes
            FROM cases c JOIN jurisdictions j USING (jurisdiction)
            WHERE c.jurisdiction = ? AND c.case_id = ?
            """,
            [jurisdiction, case_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"{jurisdiction}/{case_id}")
        return self._case(row), self._events(jurisdiction, [case_id])[case_id]

    def iter_cases(self, jurisdiction: str) -> Iterator[tuple[Case, list[CaseEvent]]]:
        rows = self.con.execute(
            """
            SELECT c.case_id, c.jurisdiction, j.state, c.case_type, c.attributes
            FROM cases c JOIN jurisdictions j USING (jurisdiction)
            WHERE c.jurisdiction = ? ORDER BY c.case_id
            """,
            [jurisdiction],
        ).fetchall()
        events = self._events(jurisdiction, None)
        for row in rows:
            yield self._case(row), events.get(row[0], [])

    @staticmethod
    def _case(row: tuple) -> Case:
        case_id, jurisdiction, state, case_type, attributes = row
        return Case(
            case_id=case_id,
            jurisdiction=jurisdiction,
            state=state,
            case_type=case_type,
            attributes=json.loads(attributes) if attributes else {},
        )

    def _events(self, jurisdiction: str, case_ids: list[str] | None) -> dict[str, list[CaseEvent]]:
        sql = """
            SELECT case_id, event, holder, department, occurred_at, source, source_ref
            FROM case_events WHERE jurisdiction = ?
        """
        params: list = [jurisdiction]
        if case_ids is not None:
            sql += " AND case_id IN (SELECT unnest(?))"
            params.append(case_ids)
        sql += " ORDER BY case_id, occurred_at, seq"
        out: dict[str, list[CaseEvent]] = {}
        for case_id, event, holder, dept, at, source, ref in self.con.execute(
            sql, params
        ).fetchall():
            out.setdefault(case_id, []).append(
                CaseEvent(
                    case_id=case_id,
                    event=event,
                    holder=holder,
                    department=dept,
                    at=at,
                    source=source,
                    source_ref=ref,
                )
            )
        return out
