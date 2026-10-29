# Design notes

## The ledger

A case is `Case` (id, jurisdiction, state, case type, attributes) plus ordered `CaseEvent`s.
Each event's `holder` is who has the file **from that event until the next one**. If a
connector does not set it, the default for the event type applies (`comments_issued` hands
the file to the applicant, `resubmitted` hands it back, and so on; see `DEFAULT_HOLDER`).

`Holder.UNKNOWN` is for sources that record dates but not stages (most open data). The clock
engine counts unknown time against the city, which is the safe direction for deadline alerts,
and sets `assumes_city_time` on the result. Never publish a city-time figure that rests on it.

## How a clock is counted

- **Window.** A `span` clock runs from the first `start` event to the first `stop` event
  after it. A `review_cycles` clock opens one window per review round: round 1 from `start`,
  later rounds from each `restart` (resubmittal) after `comments_issued`, each with its own
  limit (Georgia: 45, 20, then 14 for every later round). `withdrawn` closes any window.
- **Pauses.** With `pause_when_applicant_holds`, only city-held (or unknown) intervals inside
  the window count.
- **Days.** An interval covers the dates strictly after its start date up to and including its
  end date: the arrival day is not counted, the departure day is. Dates are pooled across
  intervals, so a same-day hand-back never double counts. Business days skip weekends and the
  state's holidays from the `holidays` package.
- **Status.** Closed windows are `met` or `missed` (with `late_days`). Open windows are
  `overdue`, `paused` (applicant holds the file), `at_risk` (remaining days at or under 20% of
  the limit), or `on_track`, with a projected `due_date` when the city holds the file.
- **Versioning.** A case is measured against rule versions in force on its submittal date.

## Decisions taken

- **Python core, DuckDB locally, Postgres later.** The schema avoids DuckDB-only features.
  Foreign keys are left out locally because DuckDB cannot delete parent and child rows in one
  transaction; add them (with `ON DELETE CASCADE`) in the hosted Postgres migration.
- **Rules are data, not code.** One YAML file per law, validated by Pydantic. A smaller
  permitting vendor licensing the library receives these files plus the engine.
- **Import reports are first-class.** Every connector returns what it skipped and why. The
  count of permits with usable dates is itself a finding for the Front Range inventory.
- **Benchmarks suppress small groups** (`--min-cases`, default 10) so one odd permit cannot
  define a city.

## Open questions (need statute text or a real export)

- Georgia SB 447: calendar vs business days; 14 vs 20 days for resubmittal review; whether
  the 45-day clock starts at submittal when a city never makes a completeness determination;
  which permit types are covered. Secondary sources disagree on the first two.
- Florida 553.792: the permit-type and square-footage thresholds behind 30 vs 60 business
  days, applicant-agreed extensions, how the 20% after-resubmittal reduction compounds.
- Colorado Prop 123: when the fast-track requirement binds a given city (`effective_from`
  is a placeholder), whether applicant time pauses the 90 days, and the affordability test.
- Business days: state legal holidays (for example Florida's Susan B. Anthony Day) may not
  match a city's actual closures. A per-jurisdiction holiday calendar may be needed.
- Boulder export: the data dictionary does not fix the permit-type column name, so the
  connector takes the first of several candidates. Confirm against a real download.

## Backlog, in order

1. Run the Boulder connector on the real export and review the import report.
2. Verify the GA, FL and CO rules against statute text; flip `verified` per rule.
3. Add the remaining mandate-map rows: Texas HB 14 (third-party review after 15 days late),
   Arizona SB 1162, Tennessee (deemed approval), California AB 253.
4. First partner connector (Accela or Tyler workflow/task history) mapping to ledger CSV.
5. Georgia public status view and deadline alerts over `shotclock clocks --open-only`.
6. Hosted Postgres (Supabase) once a design partner's data arrives and GSA outside-activity
   approval is in hand.
