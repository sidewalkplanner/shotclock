import json
from pathlib import Path

from conftest import dt

from shotclock.ingest.workflow import WorkflowMapping, read_workflow
from shotclock.ledger import Case, CaseEvent, Jurisdiction
from shotclock.status import build_status, render_html, to_json
from shotclock.store import Store

ROOT = Path(__file__).parents[1]
PEACH_GROVE = Jurisdiction(jurisdiction="peach-grove-ga", name="Peach Grove", state="GA")


def load(store: Store) -> None:
    mapping = WorkflowMapping.load(ROOT / "mappings" / "example-accela.yaml")
    cases, _ = read_workflow(ROOT / "tests/fixtures/workflow_synthetic.csv", mapping, PEACH_GROVE)
    store.upsert_jurisdiction(PEACH_GROVE)
    store.write_cases(cases)


def test_status_mid_review(library):
    with Store() as store:
        load(store)
        summary = build_status(store, "peach-grove-ga", library, as_of=dt("2026-10-12T12:00"))

    assert summary.name == "Peach Grove"
    assert summary.pending == 2
    rows = {r.case_id: r for r in summary.rows}

    review = rows["B26-0001"]
    assert review.stage == "Resubmitted, in review"
    assert review.waiting_on == "City"
    assert review.clock == "Plan review, round 2: day 6 of 20 (calendar days)"
    assert review.clock_status == "on_track"
    assert str(review.due_date) == "2026-10-26"

    returned = rows["B26-0002"]
    assert returned.stage == "Returned: application incomplete"
    assert returned.waiting_on == "Applicant"
    assert returned.clock is None  # completeness clock met; review clock not started

    assert summary.recent_met == 3  # completeness x2, review round 1
    assert summary.recent_missed == 0
    assert not summary.rules_verified


def test_issued_cases_drop_off(library):
    with Store() as store:
        load(store)
        summary = build_status(store, "peach-grove-ga", library, as_of=dt("2026-11-01"))
    assert [r.case_id for r in summary.rows] == ["B26-0002"]


def test_html_escapes_and_flags_unverified_rules(library):
    j = Jurisdiction(jurisdiction="x-ga", name="Town <of> X", state="GA")
    case = Case(
        case_id="<script>alert(1)</script>",
        jurisdiction=j.jurisdiction,
        state="GA",
        case_type="deck",
        attributes={"address": '1 "Main" St'},
    )
    events = [CaseEvent(case_id=case.case_id, event="submitted", at=dt("2026-10-01"))]
    with Store() as store:
        store.upsert_jurisdiction(j)
        store.write_cases([(case, events)])
        summary = build_status(store, "x-ga", library, as_of=dt("2026-10-10"))

    page = render_html(summary)
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page
    assert "Town &lt;of&gt; X" in page
    assert "Preview." in page
    assert "Past deadline" in page  # 5-day completeness check blown
    assert json.loads(to_json(summary))["rows"][0]["clock_status"] == "overdue"
