from pathlib import Path

import pytest
from conftest import dt
from pydantic import ValidationError

from shotclock.clock import ClockStatus, evaluate_case
from shotclock.ingest.workflow import WorkflowMapping, read_workflow
from shotclock.ledger import EventType, Jurisdiction

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_MAPPING = Path(__file__).parents[1] / "mappings" / "example-accela.yaml"
PEACH_GROVE = Jurisdiction(jurisdiction="peach-grove-ga", name="Peach Grove", state="GA")


@pytest.fixture
def imported():
    mapping = WorkflowMapping.load(EXAMPLE_MAPPING)
    cases, report = read_workflow(FIXTURES / "workflow_synthetic.csv", mapping, PEACH_GROVE)
    return {c.case_id: (c, evs) for c, evs in cases}, report


def test_workflow_rows_become_case_events(imported):
    cases, _ = imported
    case, evs = cases["B26-0001"]
    assert case.case_type == "land_disturbance"
    assert case.attributes == {"units": 1}
    assert [e.event for e in evs] == [
        EventType.SUBMITTED,
        EventType.COMPLETE,
        EventType.COMMENTS_ISSUED,
        EventType.RESUBMITTED,
        EventType.APPROVED,
        EventType.ISSUED,
    ]
    assert evs[2].department == "Building"
    assert cases["B26-0002"][0].case_type == "commercial_alteration"


def test_workflow_report_lists_what_the_mapping_missed(imported):
    cases, report = imported
    assert report.rows == 13
    assert report.cases == 2
    assert report.warnings == {"unmapped: Inspection Scheduling/Scheduled": 2}
    # B26-0003's only row has a bad date, B26-0004's only row is unmapped
    assert report.skipped == {"bad_date": 1, "case_with_no_mapped_events": 2}


def test_workflow_export_drives_georgia_clocks(imported, library):
    cases, _ = imported
    case, evs = cases["B26-0001"]
    results = {
        (r.clock_id, r.cycle): r for r in evaluate_case(case, evs, library, dt("2026-11-01"))
    }
    assert results[("completeness", None)].days_used == 2
    assert results[("review", 1)].days_used == 24  # from filing: Sep 2 .. Sep 25
    second = results[("review", 2)]
    assert (second.days_used, second.limit_days, second.status) == (15, 20, ClockStatus.MET)


def test_mapping_rejects_rules_on_unmapped_fields():
    with pytest.raises(ValidationError, match="unmapped fields"):
        WorkflowMapping.model_validate(
            {
                "source": "x",
                "columns": {"case_id": "ID", "at": "Date"},
                "events": [{"when": {"task": "Intake"}, "event": "submitted"}],
            }
        )


def test_mapping_rule_needs_event_or_ignore():
    with pytest.raises(ValidationError, match="exactly one"):
        WorkflowMapping.model_validate(
            {
                "source": "x",
                "columns": {"case_id": "ID", "at": "Date", "task": "Task"},
                "events": [{"when": {"task": "Intake"}}],
            }
        )
