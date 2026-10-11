-- 012: (a) an audit table for shared-offshore ownership corrections, (b) automatic overdue-loan recovery records.
-- Additive only: nothing existing is rebuilt or changed.

-- A correction sets an alliance's recorded share of the offshore. It moves NOTHING in PnW.
CREATE TABLE offshore_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    alliance_id   INTEGER NOT NULL REFERENCES offshore_alliances(alliance_id),
    reason        TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    before_json   TEXT NOT NULL,           -- the alliance's share before
    after_json    TEXT NOT NULL,           -- ...and after
    physical_json TEXT,                    -- what the offshore really held at that moment
    totals_after_json TEXT,                -- every alliance's share added up, after
    group_id      TEXT NOT NULL
);
CREATE TRIGGER trg_offshore_corrections_noupdate BEFORE UPDATE ON offshore_corrections
BEGIN SELECT RAISE(ABORT, 'ownership correction records can never be changed'); END;
CREATE TRIGGER trg_offshore_corrections_nodelete BEFORE DELETE ON offshore_corrections
BEGIN SELECT RAISE(ABORT, 'ownership correction records can never be deleted'); END;

-- When a loan goes overdue the bot settles it against the member's cash deposit. ONE row per loan, ever.
CREATE TABLE loan_recoveries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id          INTEGER NOT NULL UNIQUE REFERENCES loans(id),    -- UNIQUE: a loan is recovered at most once
    nation_id        INTEGER NOT NULL,
    ts               TEXT NOT NULL,
    due_cents        INTEGER NOT NULL CHECK (due_cents > 0),          -- what was owed when it was recovered
    recovered_cents  INTEGER NOT NULL CHECK (recovered_cents >= 0),   -- covered by cash the member really had
    deficit_cents    INTEGER NOT NULL CHECK (deficit_cents >= 0),     -- the rest, now a NEGATIVE cash balance
    cash_before      INTEGER NOT NULL,
    cash_after       INTEGER NOT NULL,
    loan_event_id    INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    CHECK (recovered_cents + deficit_cents = due_cents)
);
CREATE TRIGGER trg_loan_recoveries_noupdate BEFORE UPDATE ON loan_recoveries
BEGIN SELECT RAISE(ABORT, 'loan recovery records can never be changed'); END;
CREATE TRIGGER trg_loan_recoveries_nodelete BEFORE DELETE ON loan_recoveries
BEGIN SELECT RAISE(ABORT, 'loan recovery records can never be deleted'); END;
