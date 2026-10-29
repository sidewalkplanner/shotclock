"""DuckDB-backed ledger store. The schema is Postgres-compatible for the hosted move later."""

from __future__ import annotations

import csv
import json
import tempfile
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
        """Insert or update. Facts left as None keep their stored value, so a re-import that
        does not know a city's population never erases it."""
        self.con.execute(
            """
            INSERT INTO jurisdictions (jurisdiction, name, state, permits_per_year, population)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (jurisdiction) DO UPDATE SET
                name = excluded.name,
                state = excluded.state,
                permits_per_year = coalesce(excluded.permits_per_year,
                                            jurisdictions.permits_per_year),
                population = coalesce(excluded.population, jurisdictions.population)
            """,
            [j.jurisdiction, j.name, j.state, j.permits_per_year, j.population],
        )

    def get_jurisdiction(self, jurisdiction: str) -> Jurisdiction:
        row = self.con.execute(
            """
            SELECT jurisdiction, name, state, population, permits_per_year
            FROM jurisdictions WHERE jurisdiction = ?
            """,
            [jurisdiction],
        ).fetchone()
        if row is None:
            raise KeyError(jurisdiction)
        return Jurisdiction(
            jurisdiction=row[0],
            name=row[1],
            state=row[2],
            population=row[3],
            permits_per_year=row[4],
        )

    def write_cases(self, rows: Iterable[tuple[Case, list[CaseEvent]]]) -> int:
        """Insert or replace cases with their full event history. Returns cases written.

        Rows are staged as CSV and bulk-loaded: DuckDB's row-at-a-time inserts take minutes
        for a city's full permit history, a bulk load takes seconds.
        """
        with tempfile.TemporaryDirectory(prefix="shotclock-") as tmp:
            cases_csv, events_csv = Path(tmp) / "cases.csv", Path(tmp) / "events.csv"
            n = 0
            with cases_csv.open("w", newline="") as cf, events_csv.open("w", newline="") as ef:
                cw, ew = csv.writer(cf), csv.writer(ef)
                for case, evs in rows:
                    n += 1
                    cw.writerow(
                        [
                            case.case_id,
                            case.jurisdiction,
                            case.case_type,
                            json.dumps(case.attributes),
                        ]
                    )
                    for seq, e in enumerate(evs):
                        ew.writerow(
                            [
                                case.jurisdiction,
                                case.case_id,
                                seq,
                                e.event.value,
                                e.holder.value,
                                e.department or "",
                                e.at.isoformat(sep=" "),
                                e.source or "",
                                e.source_ref or "",
                            ]
                        )
            if not n:
                return 0
            cases_src = (
                f"read_csv('{cases_csv}', header=false, nullstr='', quote='\"', columns="
                "{'case_id': 'VARCHAR', 'jurisdiction': 'VARCHAR', 'case_type': 'VARCHAR', "
                "'attributes': 'VARCHAR'})"
            )
            events_src = (
                f"read_csv('{events_csv}', header=false, nullstr='', quote='\"', columns="
                "{'jurisdiction': 'VARCHAR', 'case_id': 'VARCHAR', 'seq': 'INTEGER', "
                "'event': 'VARCHAR', 'holder': 'VARCHAR', 'department': 'VARCHAR', "
                "'occurred_at': 'TIMESTAMP', 'source': 'VARCHAR', 'source_ref': 'VARCHAR'})"
            )
            self.con.execute("BEGIN")
            try:
                self.con.execute(
                    "CREATE OR REPLACE TEMP TABLE _keys AS "
                    f"SELECT DISTINCT jurisdiction, case_id FROM {cases_src}"
                )
                self.con.execute(
                    "DELETE FROM case_events WHERE (jurisdiction, case_id) IN (SELECT * FROM _keys)"
                )
                self.con.execute(
                    "DELETE FROM cases WHERE (jurisdiction, case_id) IN (SELECT * FROM _keys)"
                )
                self.con.execute(
                    "INSERT INTO cases SELECT case_id, jurisdiction, case_type, "
                    f"attributes::JSON FROM {cases_src}"
                )
                self.con.execute(f"INSERT INTO case_events SELECT * FROM {events_src}")
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        return n

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
