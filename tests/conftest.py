from datetime import datetime

import pytest

from shotclock.ledger import Case, CaseEvent
from shotclock.rules import RuleLibrary


@pytest.fixture(scope="session")
def library() -> RuleLibrary:
    return RuleLibrary.load()


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def case(state: str, case_type: str = "single_family_new", **attributes) -> Case:
    return Case(
        case_id="BLD-2026-04418",
        jurisdiction=f"elk-valley-{state.lower()}",
        state=state,
        case_type=case_type,
        attributes=attributes,
    )


def events(*pairs: tuple[str, str]) -> list[CaseEvent]:
    return [CaseEvent(case_id="BLD-2026-04418", event=ev, at=dt(at)) for ev, at in pairs]
