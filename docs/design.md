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
- **Jurisdiction facts.** Conditions can be scoped to the jurisdiction (`population`,
  `permits_per_year`) as well as the case. An unrecorded fact leaves the rule undetermined.
  Upserts never erase a stored fact, so re-importing a city keeps what we know about it.
- **Applicability is three-valued.** A rule applies, does not apply, or cannot be decided
  because the case lacks an attribute a condition needs (Prop 123 without
  `affordable_share`). Undecided cases get no clock, but `shotclock clocks` reports how many
  there are, so a missing field never silently hides a deadline.

## Case types

Rules match normalized case types, so every mapping file should translate a city's record
types into this vocabulary. Add to it here when a rule needs a new one.

| Case type | Used by |
| --- | --- |
| `land_disturbance` | GA SB 447 (O.C.G.A. 12-7-9): the only Georgia permits with deadlines |
| `single_family_new` | FL decision limit (30 business days) |
| `multifamily_new`, `commercial_new`, `commercial_alteration`, `residential_addition` | general; any rule with `case_types: ["*"]` |
| `rezoning`, `zoning_amendment`, `zoning_variance`, `conditional_use` | AZ SB 1162 |
| `preliminary_plat`, `final_plat`, `replat`, `subdivision_plan`, `site_plan` | TX ch. 247, TN PC 1044 |

Case attributes rules read: `affordable_share` (CO), `units` (CA). Jurisdiction facts:
`population` (AZ), `permits_per_year` (GA transparency, not yet a rule).

## Remedies

A rule's penalty can name a `remedy` the applicant gains once the city is more than
`remedy_after_late_days` late: Texas third-party review (15), California third-party plan
check (0), Tennessee deemed approval (0). Clock results carry it, and the alert digest
prints it, so staff see the consequence as well as the lateness.

## Verifying a rule

Each rule file lists every claim it encodes under `checks`. To verify one, read the
primary text and set `status: confirmed` (the encoding is right) or `status: corrected`
(you changed the encoding to match), with `cite:` pointing to the section that settles it.
A closed check without a cite fails validation. A rule is verified only when all of its
checks are closed; nothing else flips it. `shotclock rules --checks` is the to-do list.

## Workflow exports

City systems export task history, not ledger events. A mapping file per city
(`mappings/*.yaml`) names the columns and lists task/status patterns that are case-level
events. The import report counts every unmapped task/status pair: map each new export
until the unmapped list holds only things that genuinely are not case events
(inspections, fee postings), then mark those `ignore: true`.

## Process metrics

`shotclock process` measures decided cases from submittal to decision (approval or denial;
issuance stands in when a source never records approval), so time after approval is
nobody's delay. City-held time is credited to the department on the event that **ends** each
interval: whoever issues the comments or the approval held the file. An open interval goes
to the department that opened it. When most time has no recorded holder (date-only open
data), the report says so and blanks the city/applicant split instead of printing zeros.

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
- **The status page is static.** One self-contained HTML file plus `status.json`,
  regenerated on each import, which a city can host anywhere with no procurement of
  servers. A live TS frontend can read the same JSON later. It shows who must act next and
  the most urgent open clock, never applicant names.

## Open questions (need statute text or a real export)

Statute questions now live in each rule's `checks`. The ones that most affect the product:

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
- Parallel department review: the ledger is case-level, so one department's sign-off is
  ignored and "comments issued" marks the consolidated letter. A city that sends each
  department's comments separately needs a decision on when the city's clock stops.
- Alert routing: an alert goes to the department on the case's latest event. After a
  resubmittal that is usually intake, not the reviewer. A real export with per-department
  "review started" rows fixes this through the mapping; check it on the first one.
- Georgia's transparency duty (250+ permits a year) is an obligation on the jurisdiction,
  not a clock, so it is not yet modeled. It belongs in the prospect list (Census Building
  Permits Survey counts), which needs network access to census.gov.
- Tennessee's two-deficiency-notice cap and Texas's deemed approval of unacted plats are
  recorded as checks but not encoded; both would be valuable alerts once confirmed.

## Backlog, in order

Done: ledger, clock engine, GA/FL/CO/AZ/CA/TX/TN rules (unverified), Boulder, ledger-CSV and
workflow connectors, public status page, jurisdiction-scoped rules, staff deadline alerts,
process report.

1. Run the Boulder connector on the real export and review the import report.
2. Work the `rules --checks` list against statute text, Georgia first.
3. Map the first real workflow export (records request or design partner) and record
   hours spent, per the thesis's onboarding-cost metric.
4. Georgia prospect list: every jurisdiction over 250 permits a year, from Census
   Building Permits Survey data (needs census.gov allowed).
5. Hosted Postgres (Supabase) once a design partner's data arrives and GSA outside-activity
   approval is in hand.
