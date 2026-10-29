from pathlib import Path

from conftest import dt

from shotclock.clock import ClockStatus, evaluate_case
from shotclock.ingest.boulder import JURISDICTION as BOULDER
from shotclock.ingest.boulder import read_boulder
from shotclock.ingest.ledger_csv import read_ledger_csv
from shotclock.ledger import EventType, Holder, Jurisdiction
from shotclock.metrics import case_process, elapsed_benchmark
from shotclock.rules import RuleLibrary
from shotclock.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
ELK_VALLEY = Jurisdiction(jurisdiction="elk-valley-co", name="Elk Valley", state="CO")


def test_boulder_import_reports_every_problem():
    cases, report = read_boulder(FIXTURES / "boulder_synthetic.csv")
    assert report.rows == 11  # a quoted description spanning two lines is still one row
    assert report.cases == 7
    assert report.skipped == {
        "no_applied_date": 1,
        "duplicate_row": 1,
        "unparseable_date": 1,
        "status_void": 1,
    }
    assert report.warnings == {"issued_before_applied": 1, "duplicate_permit_number": 3}

    by_id = {c.case_id: (c, evs) for c, evs in cases}
    assert {"PMT2025-00006#a6", "PMT2025-00006#a7"} <= by_id.keys()

    case, evs = by_id["PMT2025-00001"]
    assert case.case_type == "residential_new__new"
    assert case.attributes["address"] == "100 EXAMPLE ST"
    assert case.attributes["parcel_id"] == "146301000001"
    assert (case.attributes["units"], case.attributes["valuation"]) == (1, 450000)
    assert [e.event for e in evs] == [EventType.SUBMITTED, EventType.ISSUED]
    assert evs[0].holder is Holder.UNKNOWN  # open data has no stage history

    # Work type that only repeats the permit type is not appended.
    assert by_id["PMT2025-00006#a6"][0].case_type == "residential_addition"
    assert by_id["PMT2025-00010"][0].attributes["affordable_share"] == 0.6

    _, evs = by_id["PMT2025-00004"]  # issued before applied: keep case, drop bad event
    assert [e.event for e in evs] == [EventType.SUBMITTED]


def test_boulder_benchmark_round_trip():
    cases, _ = read_boulder(FIXTURES / "boulder_synthetic.csv")
    with Store() as store:
        store.upsert_jurisdiction(BOULDER)
        store.write_cases(cases)
        store.write_cases(cases)  # re-import replaces, never duplicates
        rows = {r.case_type: r for r in elapsed_benchmark(store, "boulder-co", min_cases=1)}

    new = rows["residential_new__new"]
    assert (new.cases, new.issued) == (3, 2)  # the open case counts, but has no duration
    assert new.median_days == 60.0  # 60 and 60 days
    assert new.open_share == round(1 / 3, 4)  # one of three still open: censored
    addition = rows["residential_addition"]
    assert (addition.cases, addition.issued, addition.median_days) == (3, 2, 24.0)  # 20 and 28


def test_benchmark_suppresses_small_groups():
    cases, _ = read_boulder(FIXTURES / "boulder_synthetic.csv")
    with Store() as store:
        store.upsert_jurisdiction(BOULDER)
        store.write_cases(cases)
        assert elapsed_benchmark(store, "boulder-co") == []


def test_ledger_csv_to_clock_end_to_end():
    cases, report = read_ledger_csv(FIXTURES / "ledger_synthetic.csv", ELK_VALLEY)
    assert report.skipped == {"invalid_event_row": 1}

    with Store() as store:
        store.upsert_jurisdiction(ELK_VALLEY)
        store.write_cases(cases)
        case, events = store.get_case("elk-valley-co", "BLD-2026-04418")

    assert case.state == "CO"
    assert case.attributes == {"affordable_share": 0.6, "units": 48}
    (clock,) = evaluate_case(case, events, RuleLibrary.load(), as_of=dt("2026-05-01"))
    # City days: Feb 11 - Mar 1 (19) + Mar 21 - Apr 15 (26). Applicant's 19 days paused.
    assert (clock.rule_id, clock.days_used, clock.status) == ("us-co-prop123", 45, ClockStatus.MET)

    process = case_process(case.case_id, events, as_of=dt("2026-04-15T15:00"))
    assert process.review_cycles == 2
    assert round(process.hours_by_holder[Holder.APPLICANT]) == 19 * 24 - 5
    # Each city interval belongs to the department that ended it.
    assert {d: round(h / 24) for d, h in process.hours_by_department.items()} == {
        "zoning": 9,  # intake Feb 1 -> completeness Feb 10
        "engineering": 19,  # Feb 10 10:00 -> comments Mar 1 16:00
        "building": 26,  # resubmitted Mar 20 -> approved Apr 15
    }


def test_reimport_keeps_known_jurisdiction_facts():
    with Store() as store:
        store.upsert_jurisdiction(
            Jurisdiction(
                jurisdiction="elk-valley-co",
                name="Elk Valley",
                state="CO",
                population=41_000,
                permits_per_year=900,
            )
        )
        store.upsert_jurisdiction(ELK_VALLEY)  # what an ingest command sends: no facts
        j = store.get_jurisdiction("elk-valley-co")
    assert (j.population, j.permits_per_year) == (41_000, 900)


def test_schema_upgrades_an_older_database(tmp_path):
    import duckdb

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE jurisdictions (jurisdiction TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "state TEXT NOT NULL, permits_per_year INTEGER)"
    )
    con.execute("INSERT INTO jurisdictions VALUES ('a-co', 'A', 'CO', 300)")
    con.close()
    with Store(path) as store:
        j = store.get_jurisdiction("a-co")
    assert (j.permits_per_year, j.population) == (300, None)


def test_benchmark_since_drops_earlier_years():
    cases, _ = read_boulder(FIXTURES / "boulder_synthetic.csv")
    with Store() as store:
        store.upsert_jurisdiction(BOULDER)
        store.write_cases(cases)
        assert elapsed_benchmark(store, "boulder-co", min_cases=1, since=2026) == []
