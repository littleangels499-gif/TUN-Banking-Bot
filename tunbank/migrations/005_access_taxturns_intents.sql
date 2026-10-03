-- Separate permission flags for confidential alliance information, tax-turn alerting, expected deposits,
-- link history, and database-level guards that stop "completed" without a real PnW record.

CREATE TABLE role_flags (
    flag    TEXT NOT NULL,
    role_id TEXT NOT NULL,
    PRIMARY KEY (flag, role_id)
);

CREATE TABLE tax_turns (
    turn_key     TEXT PRIMARY KEY,                 -- 'YYYY-MM-DD HH' (UTC, start of the 2-hour turn)
    started_at   TEXT NOT NULL,
    records      INTEGER NOT NULL DEFAULT 0,
    totals_json  TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT NOT NULL,
    alerted_at   TEXT,
    value_cents  INTEGER,
    price_snapshot_id INTEGER
);

CREATE TABLE deposit_intents (                      -- what a member said they are about to deposit (NEVER money)
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id    INTEGER NOT NULL,
    amounts_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('WAITING','MATCHED','EXPIRED')),
    pnw_record_id INTEGER
);
CREATE INDEX idx_intents_nation ON deposit_intents(nation_id, status);

CREATE TABLE nation_link_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id           INTEGER NOT NULL,
    discord_id          TEXT,
    previous_discord_id TEXT,
    action              TEXT NOT NULL,
    actor               TEXT NOT NULL,
    verified            INTEGER NOT NULL DEFAULT 0,
    note                TEXT,
    at                  TEXT NOT NULL
);
CREATE TRIGGER trg_link_history_noupdate BEFORE UPDATE ON nation_link_history
BEGIN SELECT RAISE(ABORT, 'link history can never be changed'); END;
CREATE TRIGGER trg_link_history_nodelete BEFORE DELETE ON nation_link_history
BEGIN SELECT RAISE(ABORT, 'link history can never be deleted'); END;

-- "Completed" always needs the real PnW record behind it.
CREATE TRIGGER trg_offshore_done_ins BEFORE INSERT ON offshore_transfers
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'an offshore transfer cannot be COMPLETED without a real PnW bank record'); END;
CREATE TRIGGER trg_offshore_done_upd BEFORE UPDATE ON offshore_transfers
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'an offshore transfer cannot be COMPLETED without a real PnW bank record'); END;
CREATE TRIGGER trg_grants_done_ins BEFORE INSERT ON grants
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'a grant cannot be COMPLETED without a real PnW bank record'); END;
CREATE TRIGGER trg_grants_done_upd BEFORE UPDATE ON grants
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'a grant cannot be COMPLETED without a real PnW bank record'); END;
