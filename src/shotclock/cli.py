"""Command line: ``shotclock <command> --help``."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from shotclock.clock import OPEN_STATUSES, evaluate_case
from shotclock.ingest import ImportReport
from shotclock.ingest.boulder import JURISDICTION as BOULDER
from shotclock.ingest.boulder import read_boulder
from shotclock.ingest.ledger_csv import read_ledger_csv
from shotclock.ingest.workflow import WorkflowMapping, read_workflow
from shotclock.ledger import Jurisdiction
from shotclock.metrics import elapsed_benchmark
from shotclock.rules import RuleLibrary
from shotclock.status import build_status, render_html, to_json
from shotclock.store import Store

DEFAULT_DB = "shotclock.duckdb"


def _print_report(report: ImportReport) -> None:
    print(f"{report.source}: {report.rows} rows -> {report.cases} cases")
    for label, counts in (("skipped", report.skipped), ("warnings", report.warnings)):
        for reason, n in sorted(counts.items()):
            print(f"  {label}: {reason} = {n}")


def _write_rows(rows: list[dict], out: Path | None) -> None:
    if not rows:
        print("(no rows)")
        return
    fh = out.open("w", newline="") if out else sys.stdout
    try:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if out:
            fh.close()
            print(f"wrote {len(rows)} rows to {out}")


def cmd_rules(args: argparse.Namespace) -> None:
    for r in RuleLibrary.load().rules:
        flag = "verified" if r.verified else "UNVERIFIED"
        clocks = ", ".join(c.id for c in r.clocks)
        print(
            f"{r.id:<16} {r.state}  from {r.effective_from}  {flag:<10}  {r.citation}  [{clocks}]"
        )


def cmd_ingest_boulder(args: argparse.Namespace) -> None:
    cases, report = read_boulder(args.csv)
    with Store(args.db) as store:
        store.upsert_jurisdiction(BOULDER)
        store.write_cases(cases)
    _print_report(report)


def _jurisdiction(args: argparse.Namespace) -> Jurisdiction:
    return Jurisdiction(
        jurisdiction=args.jurisdiction,
        name=args.name or args.jurisdiction,
        state=args.state.upper(),
    )


def cmd_ingest_ledger(args: argparse.Namespace) -> None:
    j = _jurisdiction(args)
    cases, report = read_ledger_csv(args.csv, j, source=args.source)
    with Store(args.db) as store:
        store.upsert_jurisdiction(j)
        store.write_cases(cases)
    _print_report(report)


def cmd_ingest_workflow(args: argparse.Namespace) -> None:
    j = _jurisdiction(args)
    cases, report = read_workflow(args.csv, WorkflowMapping.load(args.mapping), j)
    with Store(args.db) as store:
        store.upsert_jurisdiction(j)
        store.write_cases(cases)
    _print_report(report)


def cmd_benchmark(args: argparse.Namespace) -> None:
    with Store(args.db) as store:
        rows = elapsed_benchmark(store, args.jurisdiction, min_cases=args.min_cases)
    print("# elapsed calendar days, submittal to issuance (city + applicant time)", file=sys.stderr)
    _write_rows([asdict(r) for r in rows], args.out)


def cmd_clocks(args: argparse.Namespace) -> None:
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    library = RuleLibrary.load()
    rows = []
    with Store(args.db) as store:
        cases = (
            [store.get_case(args.jurisdiction, args.case)]
            if args.case
            else store.iter_cases(args.jurisdiction)
        )
        for case, events in cases:
            for r in evaluate_case(case, events, library, as_of):
                if args.open_only and r.status not in OPEN_STATUSES:
                    continue
                rows.append(
                    {k: (v.value if hasattr(v, "value") else v) for k, v in asdict(r).items()}
                )
    if any(not r["rule_verified"] for r in rows):
        print("# includes UNVERIFIED rules: internal use only", file=sys.stderr)
    _write_rows(rows, args.out)


def cmd_status_page(args: argparse.Namespace) -> None:
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    with Store(args.db) as store:
        summary = build_status(store, args.jurisdiction, RuleLibrary.load(), as_of)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(render_html(summary))
    (args.out / "status.json").write_text(to_json(summary))
    print(f"{summary.pending} pending applications -> {args.out}/index.html, status.json")
    if not summary.rules_verified:
        print("# rules UNVERIFIED: page shows a preview banner", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shotclock", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("rules", help="list the rule library")
    sp.set_defaults(func=cmd_rules)

    ingest = sub.add_parser("ingest", help="load a source export into the ledger")
    isub = ingest.add_subparsers(dest="source_kind", required=True)

    sp = isub.add_parser("boulder", help="City of Boulder construction permits CSV")
    sp.add_argument("csv", type=Path)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_ingest_boulder)

    sp = isub.add_parser("ledger", help="ledger-shaped CSV (one row per event)")
    sp.add_argument("csv", type=Path)
    sp.add_argument("--jurisdiction", required=True, help="slug, e.g. elk-valley-co")
    sp.add_argument("--state", required=True, help="two-letter code, e.g. CO")
    sp.add_argument("--name")
    sp.add_argument("--source")
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_ingest_ledger)

    sp = isub.add_parser("workflow", help="task-history export, mapped by a YAML file")
    sp.add_argument("csv", type=Path)
    sp.add_argument("--mapping", type=Path, required=True, help="see mappings/")
    sp.add_argument("--jurisdiction", required=True)
    sp.add_argument("--state", required=True)
    sp.add_argument("--name")
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_ingest_workflow)

    sp = sub.add_parser("benchmark", help="elapsed-time percentiles by case type and year")
    sp.add_argument("--jurisdiction", required=True)
    sp.add_argument("--min-cases", type=int, default=10)
    sp.add_argument("--out", type=Path)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser("clocks", help="statutory clocks for a jurisdiction or one case")
    sp.add_argument("--jurisdiction", required=True)
    sp.add_argument("--case")
    sp.add_argument("--as-of", help="ISO date or datetime; default now")
    sp.add_argument("--open-only", action="store_true")
    sp.add_argument("--out", type=Path)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_clocks)

    sp = sub.add_parser("status-page", help="public status page for pending applications")
    sp.add_argument("--jurisdiction", required=True)
    sp.add_argument("--out", type=Path, default=Path("data/out/status"))
    sp.add_argument("--as-of", help="ISO date or datetime; default now")
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.set_defaults(func=cmd_status_page)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
