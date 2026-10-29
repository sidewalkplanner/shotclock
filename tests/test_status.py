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
    assert review.clock == "Land-disturbance permit review, round 2: day 6 of 20 (calendar days)"
    assert review.clock_status == "on_track"
    assert str(review.due_date) == "2026-10-26"

    returned = rows["B26-0002"]
    assert returned.stage == "Returned: application incomplete"
    assert returned.waiting_on == "Applicant"
    assert returned.clock is None  # a building permit: Georgia sets no deadline for it

    assert summary.recent_met == 2  # B26-0001 completeness and review round 1
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
        case_type="land_disturbance",
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


def test_overdue_sorts_first_and_says_how_late(library):
    j = Jurisdiction(jurisdiction="y-ga", name="Y", state="GA")

    def mk(case_id, submitted):
        c = Case(
            case_id=case_id, jurisdiction=j.jurisdiction, state="GA", case_type="land_disturbance"
        )
        return c, [CaseEvent(case_id=case_id, event="submitted", at=dt(submitted))]

    with Store() as store:
        store.upsert_jurisdiction(j)
        # FRESH: day 1 of 5. LATE: day 8 of 5. Paused/none sort after both.
        store.write_cases([mk("FRESH", "2026-10-09"), mk("LATE", "2026-10-02")])
        summary = build_status(store, "y-ga", library, as_of=dt("2026-10-10"))
    assert [r.case_id for r in summary.rows] == ["LATE", "FRESH"]
    assert summary.rows[0].days_late == 3
    assert "3 days late" in render_html(summary)


def test_parcel_id_is_shown_and_searchable(library):
    j = Jurisdiction(jurisdiction="z-ga", name="Z", state="GA")
    c = Case(
        case_id="P1",
        jurisdiction=j.jurisdiction,
        state="GA",
        case_type="deck",
        attributes={"address": "1 Main St", "parcel_id": "14-0072-0001"},
    )
    with Store() as store:
        store.upsert_jurisdiction(j)
        store.write_cases([(c, [CaseEvent(case_id="P1", event="submitted", at=dt("2026-10-01"))])])
        page = render_html(build_status(store, "z-ga", library, as_of=dt("2026-10-02")))
    assert "Parcel 14-0072-0001" in page
    assert 'data-search="p1 1 main st 14-0072-0001' in page
