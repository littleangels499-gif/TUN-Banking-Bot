-- Offshore transfers between our own banks (never touches member balances) + simple controlled grants.

CREATE TABLE offshore_transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('TO_OFFSHORE','TO_MAIN')),
    mode            TEXT NOT NULL CHECK (mode IN ('AUTO','MANUAL','OBSERVED')),
    status          TEXT NOT NULL CHECK (status IN ('PLANNED','PENDING','COMPLETED','FAILED','UNCERTAIN','CANCELLED')),
    actor           TEXT NOT NULL,
    reason          TEXT,
    amounts_json    TEXT NOT NULL,
    value_cents     INTEGER,
    price_snapshot_id INTEGER,
    idempotency_key TEXT UNIQUE,
    attempted_at    TEXT,
    pnw_record_id   INTEGER,
    failure_reason  TEXT,
    completed_at    TEXT
);
CREATE TRIGGER trg_offshore_nodelete BEFORE DELETE ON offshore_transfers
BEGIN SELECT RAISE(ABORT, 'offshore transfer history can never be deleted'); END;
CREATE TRIGGER trg_offshore_fixed BEFORE UPDATE ON offshore_transfers
WHEN OLD.amounts_json != NEW.amounts_json OR OLD.direction != NEW.direction
BEGIN SELECT RAISE(ABORT, 'an offshore transfer''s amounts and direction can never be changed'); END;

CREATE TABLE grants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    recipient_nation_id INTEGER NOT NULL,
    amounts_json        TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    project             TEXT,
    requested_by        TEXT NOT NULL,
    approver            TEXT,
    tx_id               INTEGER REFERENCES transactions(id),
    pnw_record_id       INTEGER,
    value_cents         INTEGER,
    price_snapshot_id   INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('PENDING','COMPLETED','FAILED','UNCERTAIN')),
    message             TEXT,
    completed_at        TEXT
);
CREATE TRIGGER trg_grants_nodelete BEFORE DELETE ON grants
BEGIN SELECT RAISE(ABORT, 'grants can never be deleted'); END;
CREATE TRIGGER trg_grants_fixed BEFORE UPDATE ON grants
WHEN OLD.amounts_json != NEW.amounts_json OR OLD.recipient_nation_id != NEW.recipient_nation_id OR OLD.purpose != NEW.purpose
BEGIN SELECT RAISE(ABORT, 'a grant''s recipient, amounts and purpose can never be changed'); END;
