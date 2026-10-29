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
    assert {r.state for r in library.rules} == {"AZ", "CA", "CO", "FL", "GA"}
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
