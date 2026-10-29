import pytest
from conftest import case, dt, events
from pydantic import ValidationError

from shotclock.clock import ClockStatus, evaluate_case
from shotclock.ledger import Jurisdiction
from shotclock.rules import Check, CheckStatus, Condition, Rule

MINIMAL = {
    "id": "us-xx-test",
    "state": "XX",
    "name": "Test",
    "citation": "Test Act",
    "effective_from": "2026-01-01",
    "clocks": [
        {"id": "c", "label": "C", "start": ["submitted"], "stop": ["approved"], "limit_days": 10}
    ],
}


def test_library_loads_every_state(library):
    assert {r.state for r in library.rules} == {"AZ", "CA", "CO", "FL", "GA", "TN", "TX"}
    assert all(not r.verified for r in library.rules)  # nothing checked against statute yet


def test_rule_needs_at_least_one_check():
    with pytest.raises(ValidationError):
        Rule.model_validate({**MINIMAL, "checks": []})


def test_closing_a_check_requires_a_cite():
    with pytest.raises(ValidationError, match="no cite"):
        Check(claim="45 days", status="confirmed")


def test_verified_only_when_every_check_is_closed():
    open_one = Rule.model_validate(
        {
            **MINIMAL,
            "checks": [{"claim": "a", "status": "confirmed", "cite": "s. 1"}, {"claim": "b"}],
        }
    )
    assert not open_one.verified
    assert [c.claim for c in open_one.open_checks] == ["b"]
    closed = Rule.model_validate(
        {**MINIMAL, "checks": [{"claim": "a", "status": "corrected", "cite": "s. 1(b)"}]}
    )
    assert closed.verified
    assert closed.checks[0].status is CheckStatus.CORRECTED


def test_missing_attribute_is_undetermined_not_silently_excluded(library):
    no_share = case("CO", "multifamily_new")
    assert library.applicable(no_share, dt("2026-03-01").date()) == []
    ((rule, attrs),) = library.undetermined(no_share, dt("2026-03-01").date())
    assert (rule.id, attrs) == ("us-co-prop123", ["affordable_share"])


def test_attribute_of_wrong_type_is_undetermined(library):
    bad = case("CO", "multifamily_new", affordable_share="most")
    assert [r.id for r, _ in library.undetermined(bad, dt("2026-03-01").date())] == [
        "us-co-prop123"
    ]


def az_city(population):
    return Jurisdiction(jurisdiction="mesa-az", name="Mesa", state="AZ", population=population)


def test_arizona_zoning_decision_runs_from_completeness(library):
    evs = events(("submitted", "2026-01-05"), ("complete", "2026-02-02"))
    as_of = dt("2026-03-04")
    results = evaluate_case(
        case("AZ", "rezoning"), evs, library, as_of, jurisdiction=az_city(500_000)
    )
    r = {x.clock_id: x for x in results}
    assert (r["completeness"].days_used, r["completeness"].status) == (28, ClockStatus.MET)
    assert (r["decision"].days_used, r["decision"].limit_days) == (30, 180)
    not_zoning = case("AZ", "single_family_new")
    assert evaluate_case(not_zoning, evs, library, as_of, jurisdiction=az_city(500_000)) == []


def test_arizona_skips_small_cities_and_flags_unknown_population(library):
    evs = events(("submitted", "2026-01-05"))
    rezoning = case("AZ", "rezoning")
    assert (
        evaluate_case(rezoning, evs, library, dt("2026-02-01"), jurisdiction=az_city(12_000)) == []
    )
    assert library.undetermined(rezoning, dt("2026-01-05").date(), az_city(12_000)) == []
    ((rule, missing),) = library.undetermined(rezoning, dt("2026-01-05").date(), az_city(None))
    assert (rule.id, missing) == ("us-az-sb1162", ["jurisdiction.population"])


def test_jurisdiction_conditions_only_take_known_facts():
    with pytest.raises(ValidationError, match="jurisdiction conditions"):
        Condition(scope="jurisdiction", attribute="vibes", op=">=", value=1)


def test_california_small_residential_only(library):
    evs = events(("submitted", "2026-02-02"), ("complete", "2026-02-04"))
    small = evaluate_case(case("CA", "multifamily_new", units=8), evs, library, dt("2026-03-20"))
    big = evaluate_case(case("CA", "multifamily_new", units=40), evs, library, dt("2026-03-20"))
    assert big == []
    (clock,) = small
    assert (clock.days_used, clock.status) == (44, ClockStatus.OVERDUE)


def test_texas_third_party_review_opens_after_15_days_late(library):
    evs = events(("submitted", "2026-03-02T09:00"))
    plat = case("TX", "final_plat")
    (on_day_15,) = evaluate_case(plat, evs, library, dt("2026-04-16T12:00"))
    assert (on_day_15.days_used, on_day_15.late_days, on_day_15.remedy) == (45, 15, None)
    (on_day_16,) = evaluate_case(plat, evs, library, dt("2026-04-17T12:00"))
    assert on_day_16.status is ClockStatus.OVERDUE
    assert on_day_16.remedy.startswith("Use a qualified third-party reviewer")


def test_texas_resubmittal_round_is_15_days(library):
    evs = events(
        ("submitted", "2026-03-02"),
        ("comments_issued", "2026-03-20"),
        ("resubmitted", "2026-04-01"),
    )
    r = {x.cycle: x for x in evaluate_case(case("TX", "site_plan"), evs, library, dt("2026-04-10"))}
    assert (r[1].limit_days, r[1].status) == (30, ClockStatus.MET)
    assert (r[2].limit_days, r[2].days_used, r[2].days_remaining) == (15, 9, 6)


def test_tennessee_hearing_stops_decision_clock_and_late_comments_deem_approval(library):
    evs = events(
        ("submitted", "2027-01-04T09:00"),
        ("comments_issued", "2027-02-22T09:00"),
        ("resubmitted", "2027-03-08T09:00"),
        ("hearing_scheduled", "2027-03-12T09:00"),
    )
    r = {
        x.clock_id: x
        for x in evaluate_case(case("TN", "final_plat"), evs, library, dt("2027-04-01"))
    }
    # Business days Jan 5 - Feb 22, less MLK Day and Presidents' Day.
    comments = r["comments"]
    assert (comments.days_used, comments.status, comments.late_days) == (33, ClockStatus.MISSED, 3)
    assert comments.remedy.startswith("Treat the application as approved")
    decision = r["decision"]
    assert (decision.days_used, decision.status, decision.remedy) == (37, ClockStatus.MET, None)


def test_tennessee_not_in_force_before_2027(library):
    evs = events(("submitted", "2026-12-15"))
    assert evaluate_case(case("TN", "final_plat"), evs, library, dt("2027-02-01")) == []


def test_remedy_needs_its_threshold():
    from shotclock.rules import Penalty

    with pytest.raises(ValidationError, match="go together"):
        Penalty(summary="x", remedy="third-party review")
