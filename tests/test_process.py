from pathlib import Path

import duckdb
from conftest import dt

from shotclock.ingest.boulder import JURISDICTION as BOULDER
from shotclock.ingest.boulder import read_boulder
from shotclock.ingest.workflow import WorkflowMapping, read_workflow
from shotclock.ledger import Case, CaseEvent, Jurisdiction
from shotclock.metrics import percentile, process_report, render_process
from shotclock.store import Store

ROOT = Path(__file__).parents[1]
GA = Jurisdiction(jurisdiction="peach-grove-ga", name="Peach Grove", state="GA")


def test_percentile_matches_duckdb():
    values = [3.0, 1.0, 10.0, 7.0, 4.0]
    for q in (0.5, 0.75, 0.9):
        expected = duckdb.sql(
            f"SELECT quantile_cont(v::DOUBLE, {q}) FROM unnest({values}) t(v)"
        ).fetchall()[0][0]
        assert percentile(values, q) == expected


def test_workflow_export_process_report():
    mapping = WorkflowMapping.load(ROOT / "mappings" / "example-accela.yaml")
    cases, _ = read_workflow(ROOT / "tests/fixtures/workflow_synthetic.csv", mapping, GA)
    withdrawn = Case(case_id="W1", jurisdiction=GA.jurisdiction, state="GA", case_type="deck")
    withdrawn_events = [
        CaseEvent(case_id="W1", event="submitted", at=dt("2026-09-01")),
        CaseEvent(case_id="W1", event="withdrawn", at=dt("2026-09-05")),
    ]
    with Store() as store:
        store.upsert_jurisdiction(GA)
        store.write_cases([*cases, (withdrawn, withdrawn_events)])
        report = process_report(store, GA.jurisdiction, as_of=dt("2026-11-01"))

    rows = {r.case_type: r for r in report.by_case_type}
    assert "deck" not in rows  # withdrawn without a decision has no duration
    sfr = rows["single_family_new"]
    # Submitted Sep 1 08:30, approved Oct 21 11:00.
    assert (sfr.decided, round(sfr.median_days)) == (1, 50)
    assert (round(sfr.median_city_days), round(sfr.median_applicant_days)) == (39, 11)
    assert sfr.median_rounds == 2
    assert rows["commercial_alteration"].open == 1
    assert (report.open_with_city, report.open_with_applicant) == (0, 1)
    assert [d.department for d in report.by_department] == ["Building", "Permit Center"]
    assert report.unknown_share == 0

    text = render_process("Peach Grove", report)
    assert "  Building                  51%  22 days across 1 case\n" in text
    assert "Open now: 1 case (1 with the applicant), median age 31 days." in text


def test_date_only_source_warns_and_hides_split():
    cases, _ = read_boulder(ROOT / "tests/fixtures/boulder_synthetic.csv")
    with Store() as store:
        store.upsert_jurisdiction(BOULDER)
        store.write_cases(cases)
        report = process_report(store, "boulder-co", as_of=dt("2025-06-01"))

    assert report.unknown_share == 1.0
    new = next(r for r in report.by_case_type if r.case_type == "residential_new")
    assert (new.decided, new.median_days) == (2, 60.0)  # issuance stands in for approval
    text = render_process("Boulder", report)
    assert "no recorded holder" in text
    assert "  residential new              2      60    60     -     -       -" in text
