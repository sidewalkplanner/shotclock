from pathlib import Path

from shotclock.ingest.denver import JURISDICTION as DENVER
from shotclock.ingest.denver import case_type, read_denver
from shotclock.ledger import EventType, Holder
from shotclock.metrics import elapsed_benchmark
from shotclock.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
FILES = [
    FIXTURES / "denver_commercial_synthetic.csv",
    FIXTURES / "denver_residential_synthetic.csv",
]


def test_case_type_normalizes_class_labels():
    assert case_type("2012-RESCON-0000004625", "NEW BUILDING", "1") == "residential__new_building"
    assert case_type("2025-RESCON-0000011", "New Building", "2") == "residential__new_building"
    assert case_type("2025-COMMCON-0000002", "4 - Alteration") == "commercial__alteration"
    assert case_type("X-1", "Addition") == "unknown__addition"
    # Residential new buildings with no housing units are garages and sheds, not homes.
    assert case_type("2025-RESCON-1", "NEW BUILDING", "") == "residential__new_building_no_units"
    assert case_type("2025-RESCON-1", "NEW BUILDING", "0") == "residential__new_building_no_units"
    assert case_type("2025-RESCON-1", "NEW BUILDING", "1") == "residential__new_building"
    assert case_type("2025-COMMCON-1", "New Building", "") == "commercial__new_building"


def test_denver_import():
    cases, report = read_denver(FILES)
    assert (report.rows, report.cases) == (7, 6)
    assert report.skipped == {"cancelled": 1}
    assert report.warnings == {"issued_before_received": 1}

    by_id = {c.case_id: (c, evs) for c, evs in cases}
    case, evs = by_id["2025-COMMCON-0000001"]
    assert case.case_type == "commercial__new_building"
    assert case.attributes["parcel_id"] == "0501100001000"
    assert (case.attributes["units"], case.attributes["permit_fee"]) == (12, 18000.5)
    assert [e.event for e in evs] == [EventType.SUBMITTED, EventType.ISSUED]
    assert evs[0].holder is Holder.UNKNOWN

    short, _ = by_id["2020-COMMCON-0001796"]  # row missing its last two columns
    assert short.attributes["address"] == "1375 N EXAMPLE ST"
    assert "neighborhood" not in short.attributes

    _, evs = by_id["2025-RESCON-0000012"]  # issued before received: keep, drop the event
    assert [e.event for e in evs] == [EventType.SUBMITTED]


def test_denver_benchmark():
    cases, _ = read_denver(FILES)
    with Store() as store:
        store.upsert_jurisdiction(DENVER)
        store.write_cases(cases)
        rows = {r.case_type: r for r in elapsed_benchmark(store, "denver-co", min_cases=1)}
    new = rows["residential__new_building"]
    assert (new.cases, new.median_days, new.open_share) == (2, 74.5, 0.0)  # 90 and 59 days


def test_source_notes_replace_per_source():
    with Store() as store:
        store.upsert_jurisdiction(DENVER)
        store.set_source_notes("denver-co", "denver-open-data", ["a", "b"])
        store.set_source_notes("denver-co", "denver-open-data", ["c"])
        store.set_source_notes("denver-co", "other", ["d"])
        assert store.source_notes("denver-co") == [("denver-open-data", "c"), ("other", "d")]
