-- The normalized case ledger. Written to run unchanged on DuckDB and Postgres.
--
-- No foreign keys here: DuckDB cannot delete parent and child rows in one transaction,
-- which the replace-on-reimport path needs. Store enforces the relationships; the hosted
-- Postgres migration should add FKs on cases.jurisdiction and case_events (jurisdiction,
-- case_id) with ON DELETE CASCADE.

CREATE TABLE IF NOT EXISTS jurisdictions (
    jurisdiction    TEXT PRIMARY KEY,           -- e.g. 'boulder-co'
    name            TEXT NOT NULL,
    state           TEXT NOT NULL,              -- two-letter USPS code
    permits_per_year INTEGER                    -- drives thresholds like Georgia's 250
);

CREATE TABLE IF NOT EXISTS cases (
    case_id         TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    case_type       TEXT NOT NULL,
    attributes      JSON,                        -- units, affordable_share, valuation, ...
    PRIMARY KEY (jurisdiction, case_id)
);

CREATE TABLE IF NOT EXISTS case_events (
    jurisdiction    TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,            -- source order, breaks same-timestamp ties
    event           TEXT NOT NULL,               -- shotclock.ledger.EventType
    holder          TEXT NOT NULL,               -- shotclock.ledger.Holder, after this event
    department      TEXT,
    occurred_at     TIMESTAMP NOT NULL,          -- local time in the jurisdiction
    source          TEXT,
    source_ref      TEXT,
    PRIMARY KEY (jurisdiction, case_id, seq)
);
