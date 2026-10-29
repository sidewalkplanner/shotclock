from datetime import date

from conftest import case, dt, events

from shotclock.clock import ClockStatus, evaluate_case
from shotclock.ledger import CaseEvent, Holder


def by_clock(results):
    return {(r.rule_id, r.clock_id, r.cycle): r for r in results}


def test_georgia_review_cycles_and_at_risk(library):
    evs = events(
        ("submitted", "2026-09-02T09:14"),
        ("complete", "2026-09-04T10:00"),
        ("comments_issued", "2026-10-10T16:00"),
        ("resubmitted", "2026-10-20T11:00"),
    )
    r = by_clock(evaluate_case(case("GA"), evs, library, as_of=dt("2026-11-05T12:00")))

    completeness = r[("us-ga-sb447", "completeness", None)]
    assert (completeness.days_used, completeness.status) == (2, ClockStatus.MET)

    first = r[("us-ga-sb447", "review", 1)]
    assert (first.limit_days, first.days_used, first.status) == (45, 36, ClockStatus.MET)

    second = r[("us-ga-sb447", "review", 2)]
    assert second.limit_days == 20
    assert second.days_used == 16  # Oct 21 .. Nov 5
    assert second.days_remaining == 4
    assert second.status is ClockStatus.AT_RISK
    assert second.due_date == date(2026, 11, 9)
    assert not second.rule_verified


def test_georgia_later_rounds_use_last_limit(library):
    evs = events(
        ("submitted", "2026-09-01"),
        ("complete", "2026-09-02"),
        ("comments_issued", "2026-09-10"),
        ("resubmitted", "2026-09-15"),
        ("comments_issued", "2026-09-20"),
        ("resubmitted", "2026-09-25"),
        ("comments_issued", "2026-09-30"),
        ("resubmitted", "2026-10-01"),
        ("approved", "2026-10-20"),
    )
    r = by_clock(evaluate_case(case("GA"), evs, library, as_of=dt("2026-11-01")))
    assert [r[("us-ga-sb447", "review", n)].limit_days for n in (1, 2, 3, 4)] == [45, 20, 14, 14]
    fourth = r[("us-ga-sb447", "review", 4)]
    assert (fourth.days_used, fourth.status, fourth.late_days) == (19, ClockStatus.MISSED, 5)


def test_rule_not_in_force_before_effective_date(library):
    evs = events(("submitted", "2026-06-15"), ("complete", "2026-06-16"))
    assert evaluate_case(case("GA"), evs, library, as_of=dt("2026-07-15")) == []


def test_florida_decision_pauses_while_applicant_holds(library):
    evs = events(
        ("submitted", "2026-03-02T09:00"),
        ("complete", "2026-03-04T09:00"),
        ("comments_issued", "2026-03-13T09:00"),
        ("resubmitted", "2026-05-06T09:00"),
        ("approved", "2026-05-15T09:00"),
    )
    r = by_clock(evaluate_case(case("FL"), evs, library, as_of=dt("2026-06-01")))
    decision = r[("us-fl-553-792", "decision", None)]
    # Business days with the city: Mar 3-13 (9) + May 7-15 (7). Applicant's 7 weeks excluded.
    assert (decision.limit_days, decision.days_used, decision.status) == (30, 16, ClockStatus.MET)


def test_florida_paused_status_while_open(library):
    evs = events(
        ("submitted", "2026-03-02T09:00"),
        ("comments_issued", "2026-03-13T09:00"),
    )
    r = by_clock(evaluate_case(case("FL"), evs, library, as_of=dt("2026-04-01")))
    decision = r[("us-fl-553-792", "decision", None)]
    assert decision.status is ClockStatus.PAUSED
    assert decision.due_date is None


def test_florida_fee_reduction_when_late(library):
    evs = events(("submitted", "2026-03-02T09:00"), ("approved", "2026-04-24T09:00"))
    r = by_clock(evaluate_case(case("FL"), evs, library, as_of=dt("2026-05-01")))
    decision = r[("us-fl-553-792", "decision", None)]
    assert decision.days_used == 39  # Mar 3 .. Apr 24, weekdays, no FL holidays
    assert decision.late_days == 9
    assert decision.penalty_estimate == 0.9


def test_florida_limit_by_case_type(library):
    evs = events(("submitted", "2026-03-02T09:00"))
    r = by_clock(evaluate_case(case("FL", "commercial_new"), evs, library, as_of=dt("2026-03-10")))
    assert r[("us-fl-553-792", "decision", None)].limit_days == 60


def test_colorado_prop123_only_for_mostly_affordable(library):
    evs = events(("submitted", "2026-02-01"), ("complete", "2026-02-10"))
    as_of = dt("2026-05-20")
    affordable = evaluate_case(
        case("CO", "multifamily_new", affordable_share=0.6), evs, library, as_of
    )
    market = evaluate_case(case("CO", "multifamily_new", affordable_share=0.4), evs, library, as_of)
    assert market == []
    (clock,) = affordable
    assert (clock.days_used, clock.status) == (99, ClockStatus.OVERDUE)  # Feb 11 .. May 20


def test_withdrawn_stops_the_clock(library):
    evs = events(("submitted", "2026-03-02"), ("withdrawn", "2026-03-05"))
    statuses = {
        r.clock_id: r.status for r in evaluate_case(case("FL"), evs, library, dt("2026-06-01"))
    }
    assert statuses == {"completeness": ClockStatus.WITHDRAWN, "decision": ClockStatus.WITHDRAWN}


def test_unknown_holder_counts_against_city_and_is_flagged(library):
    evs = [
        CaseEvent(case_id="X", event="submitted", at=dt("2026-03-02"), holder=Holder.UNKNOWN),
        CaseEvent(case_id="X", event="approved", at=dt("2026-03-09")),
    ]
    r = by_clock(evaluate_case(case("FL"), evs, library, as_of=dt("2026-04-01")))
    decision = r[("us-fl-553-792", "decision", None)]
    assert decision.assumes_city_time
    assert decision.days_used == 5


def test_events_after_as_of_are_ignored(library):
    evs = events(("submitted", "2026-03-02T09:00"), ("approved", "2026-03-20T09:00"))
    r = by_clock(evaluate_case(case("FL"), evs, library, as_of=dt("2026-03-06T12:00")))
    decision = r[("us-fl-553-792", "decision", None)]
    assert decision.status is ClockStatus.ON_TRACK
    assert decision.days_used == 4
