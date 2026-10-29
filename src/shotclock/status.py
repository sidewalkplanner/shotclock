"""Public status for pending applications (the Georgia transparency product).

``build_status`` turns the ledger plus the rule library into plain-language rows: where each
pending case is, who has it, and where it stands against the state's clock. ``render_html``
turns those rows into one self-contained page a city can host anywhere; ``to_json`` is the
same data for a future web frontend.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from html import escape

from shotclock.clock import OPEN_STATUSES, ClockStatus, describe, evaluate_case
from shotclock.ledger import CLOSING_EVENTS, EventType, Holder, sort_events, submitted_at
from shotclock.rules import RuleLibrary
from shotclock.store import Store

STAGE_LABELS = {
    EventType.SUBMITTED: "Received, checking completeness",
    EventType.INCOMPLETE: "Returned: application incomplete",
    EventType.COMPLETE: "In review",
    EventType.REVIEW_STARTED: "In review",
    EventType.COMMENTS_ISSUED: "Corrections requested",
    EventType.RESUBMITTED: "Resubmitted, in review",
    EventType.HEARING_SCHEDULED: "Scheduled for a public hearing",
    EventType.APPROVED: "Approved, awaiting permit issuance",
}
WAITING_ON = {Holder.CITY: "City", Holder.APPLICANT: "Applicant", Holder.UNKNOWN: "Not recorded"}
STATUS_LABELS = {
    ClockStatus.ON_TRACK: "On track",
    ClockStatus.AT_RISK: "Due soon",
    ClockStatus.PAUSED: "Paused, with applicant",
    ClockStatus.OVERDUE: "Past deadline",
}
# Worst first, so the most urgent clock represents the case.
_URGENCY = [ClockStatus.OVERDUE, ClockStatus.AT_RISK, ClockStatus.ON_TRACK, ClockStatus.PAUSED]
RECENT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class StatusRow:
    case_id: str
    case_type: str
    address: str | None
    submitted_on: date
    stage: str
    stage_since: date
    waiting_on: str
    clock: str | None  # "Review, round 2: day 16 of 20"
    clock_status: str | None  # ClockStatus value
    due_date: date | None
    days_late: int  # > 0 only when the clock is overdue


@dataclass(frozen=True)
class StatusSummary:
    jurisdiction: str
    name: str
    as_of: datetime
    pending: int
    by_status: dict[str, int]
    recent_met: int  # clocks closed in the last RECENT_WINDOW_DAYS
    recent_missed: int
    rules_verified: bool
    rows: list[StatusRow]


def build_status(
    store: Store, jurisdiction: str, library: RuleLibrary, as_of: datetime
) -> StatusSummary:
    j = store.get_jurisdiction(jurisdiction)

    rows: list[StatusRow] = []
    recent_met = recent_missed = 0
    verified = True
    recent_from = as_of - timedelta(days=RECENT_WINDOW_DAYS)
    for case, events in store.iter_cases(jurisdiction):
        events = sort_events(e for e in events if e.at <= as_of)
        if not events:
            continue
        clocks = evaluate_case(case, events, library, as_of, jurisdiction=j)
        verified = verified and all(c.rule_verified for c in clocks)
        for c in clocks:
            if c.stopped_at and c.stopped_at >= recent_from:
                recent_met += c.status is ClockStatus.MET
                recent_missed += c.status is ClockStatus.MISSED

        last = events[-1]
        if last.event in CLOSING_EVENTS:
            continue
        open_clocks = sorted(
            (c for c in clocks if c.status in OPEN_STATUSES),
            key=lambda c: (_URGENCY.index(c.status), c.days_remaining),
        )
        top = open_clocks[0] if open_clocks else None
        rows.append(
            StatusRow(
                case_id=case.case_id,
                case_type=case.case_type.replace("_", " "),
                address=case.attributes.get("address"),
                submitted_on=submitted_at(events).date(),
                stage=STAGE_LABELS.get(last.event, last.event.value),
                stage_since=last.at.date(),
                waiting_on=WAITING_ON.get(last.holder, "Not recorded"),
                clock=describe(top) if top else None,
                clock_status=top.status.value if top else None,
                due_date=top.due_date if top else None,
                days_late=top.late_days if top else 0,
            )
        )

    # Most urgent first: overdue (latest first), then by due date; no running clock last.
    def urgency(r: StatusRow) -> tuple:
        rank = _URGENCY.index(ClockStatus(r.clock_status)) if r.clock_status else len(_URGENCY)
        return (rank, -r.days_late, r.due_date or date.max, r.submitted_on)

    rows.sort(key=urgency)
    by_status: dict[str, int] = {}
    for r in rows:
        key = r.clock_status or "no_clock"
        by_status[key] = by_status.get(key, 0) + 1
    return StatusSummary(
        jurisdiction=jurisdiction,
        name=j.name,
        as_of=as_of,
        pending=len(rows),
        by_status=by_status,
        recent_met=recent_met,
        recent_missed=recent_missed,
        rules_verified=verified,
        rows=rows,
    )


def to_json(summary: StatusSummary) -> str:
    return json.dumps(asdict(summary), default=str, indent=2)


def _fmt(d: date | None) -> str:
    return f"{d:%b} {d.day}, {d.year}" if d else ""


_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5e5a;--line:#e3e1da;--card:#fff;
--ok:#1f6f43;--ok-bg:#e3f1e8;--warn:#8a5a00;--warn-bg:#fbefd5;--bad:#a1281b;--bad-bg:#f9e0dc;
--pause:#3d4f73;--pause-bg:#e4e9f3;--accent:#24508f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;
--line:#33322f;--card:#1f1f1d;--ok:#7fcf9d;--ok-bg:#1d3326;--warn:#f0c26a;--warn-bg:#3a2e15;
--bad:#f29a8e;--bad-bg:#3d1f1a;--pause:#a9bce3;--pause-bg:#232b3b;--accent:#8fb4ee}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:1.75rem;line-height:1.2;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 24px}
.banner{border:1px solid var(--warn);background:var(--warn-bg);color:var(--fg);
border-radius:8px;padding:12px 16px;margin:0 0 24px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
margin:0 0 24px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px}
.stat b{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:.875rem}
label{display:block;font-weight:600;margin:0 0 6px}
input{width:100%;max-width:420px;font:inherit;padding:8px 12px;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg);margin:0 0 16px}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.8125rem;color:var(--muted);font-weight:600;background:var(--bg)}
td small{display:block;color:var(--muted)}
.nowrap{white-space:nowrap}
.pill{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.8125rem;
font-weight:600;white-space:nowrap}
.on_track{color:var(--ok);background:var(--ok-bg)}.at_risk{color:var(--warn);background:var(--warn-bg)}
.overdue{color:var(--bad);background:var(--bad-bg)}.paused{color:var(--pause);background:var(--pause-bg)}
.empty{padding:24px;text-align:center;color:var(--muted)}
footer{color:var(--muted);font-size:.875rem;margin-top:24px}
@media (max-width:720px){thead{display:none}table,tbody,tr,td{display:block;width:100%}
tr{border-bottom:1px solid var(--line);padding:8px 0}td{border:0;padding:4px 12px}
td[data-label]::before{content:attr(data-label);display:block;font-size:.75rem;color:var(--muted)}}
"""

_JS = """
const q=document.getElementById('q'),rows=[...document.querySelectorAll('tbody tr[data-search]')];
q.addEventListener('input',()=>{const t=q.value.trim().toLowerCase();
rows.forEach(r=>{r.hidden=t&&!r.dataset.search.includes(t)})});
"""


def render_html(summary: StatusSummary) -> str:
    e = escape
    closed = summary.recent_met + summary.recent_missed
    on_time = f"{round(100 * summary.recent_met / closed)}%" if closed else "None yet"
    stats = [
        (str(summary.pending), "pending applications"),
        (str(summary.by_status.get("overdue", 0)), "past a state deadline"),
        (str(summary.by_status.get("at_risk", 0)), "due soon"),
        (on_time, f"deadlines met, last {RECENT_WINDOW_DAYS} days"),
    ]
    body_rows = []
    for r in summary.rows:
        search = " ".join(filter(None, [r.case_id, r.address, r.case_type, r.stage])).lower()
        pill = (
            f'<span class="pill {e(r.clock_status)}">'
            f"{e(STATUS_LABELS[ClockStatus(r.clock_status)])}</span>"
            if r.clock_status
            else ""
        )
        if r.days_late:
            due = f"<small>{r.days_late} day{'' if r.days_late == 1 else 's'} late</small>"
        else:
            due = f"<small>Due {_fmt(r.due_date)}</small>" if r.due_date else ""
        body_rows.append(
            f'<tr data-search="{e(search)}">'
            f'<td data-label="Application"><b class="nowrap">{e(r.case_id)}</b>'
            f"<small>{e(r.address or '')}</small></td>"
            f'<td data-label="Type">{e(r.case_type)}</td>'
            f'<td data-label="Submitted" class="nowrap">{_fmt(r.submitted_on)}</td>'
            f'<td data-label="Stage">{e(r.stage)}<small>since {_fmt(r.stage_since)}</small></td>'
            f'<td data-label="Waiting on">{e(r.waiting_on)}</td>'
            f'<td data-label="State deadline">{pill}<small>{e(r.clock or "No state clock running")}'
            f"</small>{due}</td></tr>"
        )
    if not body_rows:
        body_rows.append('<tr><td colspan="6" class="empty">No pending applications.</td></tr>')

    banner = (
        ""
        if summary.rules_verified
        else '<div class="banner" role="note"><b>Preview.</b> Deadlines on this page come from '
        "state rules that have not yet been verified against statute text. Do not rely on "
        "them.</div>"
    )
    stat_html = "".join(
        f'<div class="stat"><b>{e(v)}</b><span>{e(k)}</span></div>' for v, k in stats
    )
    t = summary.as_of
    as_of = f"{_fmt(t.date())} at {t.hour % 12 or 12}:{t:%M %p}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(summary.name)} permit status</title><style>{_CSS}</style></head>
<body><main>
<h1>{e(summary.name)}: pending permit applications</h1>
<p class="sub">Status as of {e(as_of)}. Updated from the city's permitting system.</p>
{banner}
<section class="stats" aria-label="Summary">{stat_html}</section>
<label for="q">Search by application number, address or type</label>
<input id="q" type="search" autocomplete="off">
<table><thead><tr><th>Application</th><th>Type</th><th>Submitted</th><th>Stage</th>
<th>Waiting on</th><th>State deadline</th></tr></thead>
<tbody>{"".join(body_rows)}</tbody></table>
<footer>"Waiting on" shows who must act next. State deadlines pause while an application
is with the applicant. Day counts follow the state's rules for calendar or business days.
</footer></main><script>{_JS}</script></body></html>
"""
