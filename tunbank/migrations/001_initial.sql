-- TUN Bank initial schema.
-- IMPORTANT: never edit this file after the bot has run in production.
-- To change the database later, add a NEW file (002_something.sql).

CREATE TABLE system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
INSERT INTO system_state(key, value, updated_at) VALUES
    ('emergency_lock', '0', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('emergency_reason', '', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('bank_paused', '0', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('database_created_at', strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'));

CREATE TABLE bank_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

-- One row per PnW nation that has (or had) an account. Keyed by PnW nation id.
CREATE TABLE members (
    nation_id     INTEGER PRIMARY KEY,
    nation_name   TEXT,
    discord_id    TEXT UNIQUE,
    linked_at     TEXT,
    frozen        INTEGER NOT NULL DEFAULT 0,
    frozen_reason TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE price_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  TEXT NOT NULL,
    prices_json TEXT NOT NULL,
    source      TEXT NOT NULL
);

-- ---------------------------------------------------------------- PnW records
-- Every bank record the bot has ever seen from PnW. The primary key IS the PnW
-- record id, so the same record can never be stored (or credited) twice.
CREATE TABLE pnw_records (
    id                 INTEGER PRIMARY KEY,
    record_date        TEXT,
    sender_id          INTEGER,
    sender_type        INTEGER,
    receiver_id        INTEGER,
    receiver_type      INTEGER,
    banker_id          INTEGER,
    note               TEXT,
    tax_id             INTEGER,
    amounts_json       TEXT NOT NULL,
    raw_json           TEXT NOT NULL,
    raw_sha256         TEXT NOT NULL,
    direction          TEXT NOT NULL CHECK (direction IN ('IN','OUT','OTHER')),
    classification     TEXT NOT NULL CHECK (classification IN (
        'MEMBER_DEPOSIT','ALLIANCE_DONATION','TAX','LOAN_REPAYMENT',
        'REVIEW','OUTGOING_TX','EXTERNAL_OUTFLOW','OTHER')),
    status             TEXT NOT NULL CHECK (status IN (
        'CREDITED','NO_CREDIT','PENDING_CREDIT','AWAITING_REVIEW',
        'BASELINE','LINKED_TX','DISMISSED')),
    credited_nation_id INTEGER,
    tx_id              INTEGER,
    price_snapshot_id  INTEGER,
    first_seen_at      TEXT NOT NULL
);
CREATE INDEX idx_pnw_records_status ON pnw_records(status);
CREATE INDEX idx_pnw_records_sender ON pnw_records(sender_id);

CREATE TRIGGER trg_pnw_records_immutable BEFORE UPDATE ON pnw_records
WHEN OLD.raw_sha256 != NEW.raw_sha256 OR OLD.amounts_json != NEW.amounts_json
  OR OLD.direction != NEW.direction OR OLD.sender_id IS NOT NEW.sender_id
  OR OLD.receiver_id IS NOT NEW.receiver_id OR OLD.id != NEW.id
BEGIN
    SELECT RAISE(ABORT, 'pnw_records: the original PnW data can never be changed');
END;
CREATE TRIGGER trg_pnw_records_nodelete BEFORE DELETE ON pnw_records
BEGIN
    SELECT RAISE(ABORT, 'pnw_records: records can never be deleted');
END;

-- -------------------------------------------------------- opening balances
CREATE TABLE import_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    admin_discord_id  TEXT NOT NULL,
    source_filename   TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    row_count         INTEGER NOT NULL,
    totals_json       TEXT NOT NULL,
    rows_sha256       TEXT NOT NULL,
    price_snapshot_id INTEGER,
    value_cents       INTEGER,
    note              TEXT
);
CREATE TABLE import_files (
    batch_id  INTEGER PRIMARY KEY REFERENCES import_batches(id),
    content   BLOB NOT NULL
);
CREATE TABLE opening_balance_rows (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  INTEGER NOT NULL REFERENCES import_batches(id),
    nation_id INTEGER NOT NULL,
    resource  TEXT NOT NULL,
    amount    INTEGER NOT NULL CHECK (amount > 0)
);
CREATE INDEX idx_opening_rows_batch ON opening_balance_rows(batch_id);

-- ------------------------------------------------------------- locks / txs
CREATE TABLE locks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id         INTEGER NOT NULL,
    lock_type         TEXT NOT NULL,
    reason            TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    price_snapshot_id INTEGER
);

CREATE TABLE transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    tx_type           TEXT NOT NULL CHECK (tx_type IN ('WITHDRAW_SELF','WITHDRAW_ECON')),
    status            TEXT NOT NULL CHECK (status IN (
        'PENDING','CONFIRMED','COMPLETED','FAILED','CANCELLED',
        'AWAITING_REVIEW','RECONCILIATION_REQUIRED','EMERGENCY_LOCKED')),
    funding_source    TEXT NOT NULL CHECK (funding_source IN
        ('ALLIANCE','MEMBER_AVAILABLE','MEMBER_LOCKED')),
    member_nation_id  INTEGER,
    lock_id           INTEGER REFERENCES locks(id),
    dest_nation_id    INTEGER NOT NULL,
    actor_discord_id  TEXT NOT NULL,
    approver_discord_id TEXT,
    note              TEXT,
    reason            TEXT,
    idempotency_key   TEXT NOT NULL UNIQUE,
    pnw_record_id     INTEGER,
    failure_reason    TEXT,
    price_snapshot_id INTEGER,
    value_cents       INTEGER,
    balance_before_json TEXT,
    balance_after_json  TEXT,
    attempted_at      TEXT,
    completed_at      TEXT,
    CHECK (funding_source = 'ALLIANCE' OR member_nation_id IS NOT NULL)
);
CREATE INDEX idx_tx_status ON transactions(status);
CREATE INDEX idx_tx_member ON transactions(member_nation_id);

CREATE TABLE tx_items (
    tx_id    INTEGER NOT NULL REFERENCES transactions(id),
    resource TEXT NOT NULL,
    amount   INTEGER NOT NULL CHECK (amount > 0),
    PRIMARY KEY (tx_id, resource)
);

-- -------------------------------------------------- adjustments / approvals
CREATE TABLE approval_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('ADJUSTMENT','ECON_WITHDRAW')),
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
        ('PENDING','EXECUTED','REJECTED','REVOKED','EXPIRED')),
    requested_by  TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    approved_by   TEXT,
    approved_at   TEXT,
    reason        TEXT,
    result_json   TEXT
);

CREATE TABLE adjustments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id         INTEGER NOT NULL,
    bucket            TEXT NOT NULL CHECK (bucket = 'AVAILABLE'),
    reason            TEXT NOT NULL,
    evidence          TEXT NOT NULL,
    actor             TEXT NOT NULL,
    approval_id       INTEGER REFERENCES approval_requests(id),
    price_snapshot_id INTEGER,
    created_at        TEXT NOT NULL
);
CREATE TABLE adjustment_items (
    adjustment_id INTEGER NOT NULL REFERENCES adjustments(id),
    resource      TEXT NOT NULL,
    delta         INTEGER NOT NULL CHECK (delta != 0),
    PRIMARY KEY (adjustment_id, resource)
);

-- ------------------------------------------------------------------ LEDGER
-- Append-only. Every change to any member balance is a row here. A hash chain
-- links each row to the one before it, so silent edits or deletions are
-- detectable.
CREATE TABLE ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    group_id       TEXT NOT NULL,
    nation_id      INTEGER NOT NULL,
    bucket         TEXT NOT NULL CHECK (bucket IN ('AVAILABLE','LOCKED')),
    resource       TEXT NOT NULL CHECK (resource IN (
        'money','food','coal','oil','uranium','iron','bauxite','lead',
        'gasoline','munitions','steel','aluminum')),
    delta          INTEGER NOT NULL CHECK (delta != 0),
    entry_type     TEXT NOT NULL CHECK (entry_type IN (
        'OPENING','DEPOSIT','WITHDRAWAL','LOCK','RELEASE','ADJUSTMENT')),
    pnw_record_id  INTEGER,
    tx_id          INTEGER,
    lock_id        INTEGER,
    batch_id       INTEGER,
    adjustment_id  INTEGER,
    actor          TEXT NOT NULL,
    note           TEXT,
    price_snapshot_id INTEGER,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL
);
CREATE INDEX idx_ledger_nation ON ledger_entries(nation_id, bucket, resource);
CREATE INDEX idx_ledger_group ON ledger_entries(group_id);
CREATE INDEX idx_ledger_lock ON ledger_entries(lock_id);

-- A PnW record can credit a resource only once. A transaction can debit a
-- resource only once. A nation gets one opening balance per resource.
CREATE UNIQUE INDEX uq_ledger_deposit ON ledger_entries(pnw_record_id, resource)
    WHERE entry_type = 'DEPOSIT';
CREATE UNIQUE INDEX uq_ledger_withdrawal ON ledger_entries(tx_id, resource, bucket)
    WHERE entry_type = 'WITHDRAWAL';
CREATE UNIQUE INDEX uq_ledger_opening ON ledger_entries(nation_id, resource)
    WHERE entry_type = 'OPENING';

-- Cached balances. Kept in sync automatically by the trigger below. Reconciliation
-- re-adds the whole ledger and compares, so any direct edit here is caught.
CREATE TABLE balances (
    nation_id INTEGER NOT NULL,
    bucket    TEXT NOT NULL CHECK (bucket IN ('AVAILABLE','LOCKED')),
    resource  TEXT NOT NULL,
    amount    INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (nation_id, bucket, resource)
);

-- THE GUARD: a ledger row is only accepted if there is real evidence for it.
CREATE TRIGGER trg_ledger_guard BEFORE INSERT ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger: EMERGENCY LOCK is active, this change is blocked')
    WHERE NEW.entry_type IN ('OPENING','DEPOSIT','LOCK','RELEASE','ADJUSTMENT')
      AND (SELECT value FROM system_state WHERE key = 'emergency_lock') = '1';

    SELECT RAISE(ABORT, 'ledger: a DEPOSIT needs a matching real PnW deposit record')
    WHERE NEW.entry_type = 'DEPOSIT'
      AND NOT (
        NEW.delta > 0 AND NEW.bucket = 'AVAILABLE'
        AND EXISTS (
            SELECT 1 FROM pnw_records p
            WHERE p.id = NEW.pnw_record_id
              AND p.classification = 'MEMBER_DEPOSIT'
              AND p.direction = 'IN'
              AND p.credited_nation_id = NEW.nation_id
              AND json_extract(p.amounts_json, '$.' || NEW.resource) = NEW.delta
        )
      );

    SELECT RAISE(ABORT, 'ledger: a WITHDRAWAL needs an in-flight transaction that PnW has confirmed')
    WHERE NEW.entry_type = 'WITHDRAWAL'
      AND NOT (
        NEW.delta < 0
        AND EXISTS (
            SELECT 1
            FROM transactions t
            JOIN tx_items i ON i.tx_id = t.id
            JOIN pnw_records p ON p.id = t.pnw_record_id AND p.direction = 'OUT'
            WHERE t.id = NEW.tx_id
              AND t.status IN ('CONFIRMED','RECONCILIATION_REQUIRED')
              AND t.member_nation_id = NEW.nation_id
              AND i.resource = NEW.resource
              AND i.amount = -NEW.delta
              AND (
                (t.funding_source = 'MEMBER_AVAILABLE' AND NEW.bucket = 'AVAILABLE')
                OR (t.funding_source = 'MEMBER_LOCKED' AND NEW.bucket = 'LOCKED'
                    AND NEW.lock_id = t.lock_id)
              )
        )
      );

    SELECT RAISE(ABORT, 'ledger: an OPENING balance must come from a committed import batch')
    WHERE NEW.entry_type = 'OPENING'
      AND NOT (
        NEW.delta > 0 AND NEW.bucket = 'AVAILABLE'
        AND EXISTS (
            SELECT 1 FROM opening_balance_rows r
            WHERE r.batch_id = NEW.batch_id AND r.nation_id = NEW.nation_id
              AND r.resource = NEW.resource AND r.amount = NEW.delta
        )
      );

    SELECT RAISE(ABORT, 'ledger: an ADJUSTMENT needs a documented reason and evidence')
    WHERE NEW.entry_type = 'ADJUSTMENT'
      AND NOT EXISTS (
        SELECT 1 FROM adjustments j
        JOIN adjustment_items a ON a.adjustment_id = j.id
        WHERE j.id = NEW.adjustment_id AND j.nation_id = NEW.nation_id
          AND j.bucket = NEW.bucket AND a.resource = NEW.resource
          AND a.delta = NEW.delta
          AND length(trim(j.reason)) > 0 AND length(trim(j.evidence)) > 0
      );

    SELECT RAISE(ABORT, 'ledger: LOCK/RELEASE must reference a real lock with the right direction')
    WHERE NEW.entry_type IN ('LOCK','RELEASE')
      AND NOT (
        EXISTS (SELECT 1 FROM locks l WHERE l.id = NEW.lock_id AND l.nation_id = NEW.nation_id)
        AND (
            (NEW.entry_type = 'LOCK' AND
                ((NEW.bucket = 'AVAILABLE' AND NEW.delta < 0) OR (NEW.bucket = 'LOCKED' AND NEW.delta > 0)))
            OR
            (NEW.entry_type = 'RELEASE' AND
                ((NEW.bucket = 'LOCKED' AND NEW.delta < 0) OR (NEW.bucket = 'AVAILABLE' AND NEW.delta > 0)))
        )
      );
END;

CREATE TRIGGER trg_ledger_apply AFTER INSERT ON ledger_entries
BEGIN
    INSERT OR IGNORE INTO balances(nation_id, bucket, resource, amount)
    VALUES (NEW.nation_id, NEW.bucket, NEW.resource, 0);
    UPDATE balances SET amount = amount + NEW.delta
    WHERE nation_id = NEW.nation_id AND bucket = NEW.bucket AND resource = NEW.resource;
END;

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries can never be changed');
END;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries can never be deleted');
END;

CREATE TRIGGER trg_opening_rows_no_update BEFORE UPDATE ON opening_balance_rows
BEGIN SELECT RAISE(ABORT, 'opening balance records can never be changed'); END;
CREATE TRIGGER trg_opening_rows_no_delete BEFORE DELETE ON opening_balance_rows
BEGIN SELECT RAISE(ABORT, 'opening balance records can never be deleted'); END;
CREATE TRIGGER trg_batches_no_update BEFORE UPDATE ON import_batches
BEGIN SELECT RAISE(ABORT, 'import batches can never be changed'); END;
CREATE TRIGGER trg_batches_no_delete BEFORE DELETE ON import_batches
BEGIN SELECT RAISE(ABORT, 'import batches can never be deleted'); END;
CREATE TRIGGER trg_adjustments_no_update BEFORE UPDATE ON adjustments
BEGIN SELECT RAISE(ABORT, 'adjustments can never be changed'); END;
CREATE TRIGGER trg_adjustments_no_delete BEFORE DELETE ON adjustments
BEGIN SELECT RAISE(ABORT, 'adjustments can never be deleted'); END;
CREATE TRIGGER trg_adjitems_no_update BEFORE UPDATE ON adjustment_items
BEGIN SELECT RAISE(ABORT, 'adjustments can never be changed'); END;
CREATE TRIGGER trg_adjitems_no_delete BEFORE DELETE ON adjustment_items
BEGIN SELECT RAISE(ABORT, 'adjustments can never be deleted'); END;
CREATE TRIGGER trg_prices_no_update BEFORE UPDATE ON price_snapshots
BEGIN SELECT RAISE(ABORT, 'price snapshots can never be changed'); END;
CREATE TRIGGER trg_prices_no_delete BEFORE DELETE ON price_snapshots
BEGIN SELECT RAISE(ABORT, 'price snapshots can never be deleted'); END;

-- ------------------------------------------------------------- audit / logs
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT,
    details_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    entry_hash   TEXT NOT NULL
);
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log entries can never be changed'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log entries can never be deleted'); END;

CREATE TABLE alert_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    nation_id    INTEGER,
    channel      TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE integrity_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('WARNING','RECON','CRITICAL')),
    kind            TEXT NOT NULL,
    nation_id       INTEGER,
    ref_type        TEXT,
    ref_id          TEXT,
    details_json    TEXT NOT NULL,
    dedupe_key      TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT
);
CREATE UNIQUE INDEX uq_integrity_open_dedupe ON integrity_events(dedupe_key)
    WHERE status = 'OPEN' AND dedupe_key IS NOT NULL;

CREATE TABLE reconciliation_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    triggered_by    TEXT NOT NULL,
    result          TEXT NOT NULL,
    findings_json   TEXT NOT NULL,
    bank_json       TEXT,
    ledger_head_id  INTEGER,
    ledger_head_hash TEXT,
    ledger_count    INTEGER,
    price_snapshot_id INTEGER
);

-- ------------------------------------------------------------ tax / config
CREATE TABLE tax_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pnw_record_id     INTEGER NOT NULL UNIQUE REFERENCES pnw_records(id),
    nation_id         INTEGER NOT NULL,
    tax_id            INTEGER,
    record_date       TEXT,
    amounts_json      TEXT NOT NULL,
    price_snapshot_id INTEGER,
    recorded_at       TEXT NOT NULL
);
CREATE INDEX idx_tax_nation ON tax_records(nation_id);

CREATE TABLE bankers (
    discord_id TEXT PRIMARY KEY,
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL
);
CREATE TABLE role_permissions (
    level   TEXT NOT NULL CHECK (level IN ('AUDITOR','BANKER','MINISTER','ADMIN')),
    role_id TEXT NOT NULL,
    PRIMARY KEY (level, role_id)
);
CREATE TABLE limits (
    scope        TEXT NOT NULL CHECK (scope IN ('GLOBAL','ROLE','NATION')),
    scope_id     TEXT NOT NULL,
    per_tx_cents INTEGER,
    daily_cents  INTEGER,
    updated_by   TEXT,
    updated_at   TEXT,
    PRIMARY KEY (scope, scope_id)
);
