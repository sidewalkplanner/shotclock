from conftest import dt

from shotclock.alerts import build_alerts, render_text
from shotclock.ledger import Case, CaseEvent, Jurisdiction
from shotclock.store import Store

GA = Jurisdiction(jurisdiction="peach-grove-ga", name="Peach Grove", state="GA")
AS_OF = dt("2026-11-20T08:00")


def put(store: Store, case_id: str, *evs: tuple[str, str, str | None], address=None) -> None:
    case = Case(
        case_id=case_id,
        jurisdiction=GA.jurisdiction,
        state="GA",
        case_type="single_family_new",
        attributes={"address": address} if address else {},
    )
    events = [
        CaseEvent(case_id=case_id, event=ev, at=dt(at), department=dept) for ev, at, dept in evs
    ]
    store.write_cases([(case, events)])


def build(library):
    with Store() as store:
        store.upsert_jurisdiction(GA)
        # Review round 1 opened Oct 2: day 49 of 45 on Nov 20.
        put(
            store,
            "LATE",
            ("submitted", "2026-10-01T09:00", "Permit Center"),
            ("complete", "2026-10-02T09:00", "Building"),
            address="12 Elm St",
        )
        # Completeness check opened Nov 17: day 3 of 5, due Nov 22.
        put(store, "SOON", ("submitted", "2026-11-17T09:00", "Permit Center"))
        # Review round 1 opened Oct 21: day 30 of 45, nothing to flag.
        put(
            store,
            "FINE",
            ("submitted", "2026-10-20T09:00", None),
            ("complete", "2026-10-21T09:00", "Building"),
        )
        # Comments out, with the applicant: no open clock.
        put(
            store,
            "WAITING",
            ("submitted", "2026-10-01T09:00", None),
            ("complete", "2026-10-02T09:00", "Building"),
            ("comments_issued", "2026-10-20T09:00", "Building"),
        )
        # Issued: closed.
        put(
            store,
            "DONE",
            ("submitted", "2026-09-01T09:00", None),
            ("complete", "2026-09-02T09:00", None),
            ("approved", "2026-09-20T09:00", None),
            ("issued", "2026-09-25T09:00", None),
        )
        return build_alerts(store, GA.jurisdiction, library, AS_OF, within_days=5)


def test_alerts_pick_out_what_needs_action(library):
    alerts = build(library)
    assert [(a.case_id, a.status, a.days_remaining) for a in alerts] == [
        ("LATE", "overdue", -4),
        ("SOON", "on_track", 2),
    ]
    late, soon = alerts
    assert (late.department, late.address) == ("Building", "12 Elm St")
    assert str(soon.due_date) == "2026-11-22"


def test_horizon_widens_the_net(library):
    with Store() as store:
        store.upsert_jurisdiction(GA)
        put(
            store, "FINE", ("submitted", "2026-10-20T09:00", None), ("complete", "2026-10-21", None)
        )
        assert build_alerts(store, GA.jurisdiction, library, AS_OF, within_days=5) == []
        (alert,) = build_alerts(store, GA.jurisdiction, library, AS_OF, within_days=15)
    assert alert.days_remaining == 15


def test_digest_groups_by_department(library):
    text = render_text("Peach Grove", build(library), AS_OF, within_days=5)
    assert text.startswith("Peach Grove: permit deadlines as of Nov 20, 2026\n")
    assert "1 past a state deadline, 1 due soon" in text
    late_block = text.split("PAST DEADLINE")[1].split("DUE SOON")[0]
    assert "  Building\n    LATE  12 Elm St: 4 days late\n      Plan review, round 1" in late_block
    assert "  Permit Center\n    SOON: 2 days left, Sun Nov 22" in text
    assert "not yet verified" in text


def test_quiet_day(library):
    assert "Nothing needs action today." in render_text("Peach Grove", [], AS_OF, 5)
