# shotclock

The feedback loop for local land use: a neutral data layer that shows where a city's
land-use cases wait, whether it is meeting state permitting law, and what its rules produce.

Everything rests on one asset, the **normalized case ledger**: every case, from any city
system, as a list of events recording who had the file and from when to when. Three
products read from it:

| Loop | Question it answers | Buyer |
| --- | --- | --- |
| Process | Where do our cases wait, and what should we change first? | City staff |
| Compliance | Will we miss a statutory deadline, and can residents see status? | Cities, residents |
| Outcomes | What did our rules and reforms actually produce? | States, builders, lenders |

This repo is the core engine: ledger, state rule library, clock engine, connectors.

## Quick start

```sh
uv sync
uv run pytest
uv run shotclock rules --checks             # rule library + open verification checklist

# Boulder open data -> elapsed-time benchmark
uv run shotclock ingest boulder path/to/boulder_permits.csv
uv run shotclock benchmark --jurisdiction boulder-co --out data/out/boulder.csv

# A city export mapped to ledger CSV -> statutory clocks
uv run shotclock ingest ledger export.csv --jurisdiction elk-valley-co --state CO
uv run shotclock clocks --jurisdiction elk-valley-co --open-only

# A city's workflow/task-history export, mapped by a per-city YAML file
uv run shotclock ingest workflow tasks.csv --mapping mappings/peach-grove.yaml \
    --jurisdiction peach-grove-ga --state GA --name "Peach Grove"

# Jurisdiction facts some laws scope on (Arizona: population; Georgia: permits a year)
uv run shotclock jurisdiction peach-grove-ga --population 41000 --permits-per-year 900

# Morning digest of overdue and soon-due deadlines, grouped by department
uv run shotclock alerts --jurisdiction peach-grove-ga --within 5

# Public status page for pending applications (index.html + status.json)
uv run shotclock status-page --jurisdiction peach-grove-ga --out data/out/peach-grove
```

The local database defaults to `./shotclock.duckdb` (git-ignored, like `data/raw/` and `data/out/`).

## Layout

```
src/shotclock/
  ledger.py          Case, CaseEvent, EventType, Holder: the ledger shape
  schema.sql         ledger tables (DuckDB now, Postgres-compatible)
  store.py           DuckDB store: write/replace cases, read them back
  days.py            statutory day counting: calendar / business days, state holidays
  rules/             rule library loader + data/*.yaml, one file per law, versioned by date
  clock.py           clock engine: apply rules to one case's events
  metrics.py         process metrics (time by holder/department), elapsed benchmark
  ingest/boulder.py  City of Boulder permits CSV
  ingest/ledger_csv.py  generic ledger-shaped CSV (one row per event)
  ingest/workflow.py    task-history exports via a mapping file (Accela, Tyler, ...)
  status.py          public status page for pending applications
  alerts.py          staff digest of overdue and soon-due deadlines
  cli.py             `shotclock` command
mappings/            per-city workflow mappings; example-accela.yaml is illustrative
docs/design.md       how the clock works, decisions, open questions, backlog
```

## Two rules for anything published

1. **Unverified rules are internal only.** A rule is verified when every claim in its
   `checks` list is confirmed or corrected with a pin cite to statute text. None are yet;
   `shotclock rules --checks` prints the checklist. Status pages show a preview banner
   until every rule behind them is verified.
2. **Date-only open data supports elapsed time, never city time.** Boulder's data has
   application and issue dates but no stage history, so its benchmark is total elapsed
   time (city plus applicant) and must be labeled that way.
