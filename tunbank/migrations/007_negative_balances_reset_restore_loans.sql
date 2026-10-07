-- 007: negative balances, formal deposit reset + restore, imported-loan preservation.
--
-- Additive in effect: no ledger row, PnW record, audit entry or import batch is changed or removed.
-- Two tables are REBUILT only because SQLite cannot alter a CHECK constraint in place:
--   * balances             (a derived cache; its non-negative CHECK is removed)
--   * ledger_entries       (new entry types RESET / RESTORE / LOAN_DEDUCTION / CONVERSION and a reset_id column;
--                           every existing row is copied with its original id, so the hash chain is untouched)
--   * opening_balance_rows (amount may now be negative; zero is still refused)
-- The bot takes a verified backup before this runs (Database.migrate) and re-verifies the chain afterwards.

-- Drop the ledger triggers first: they reference tables that are rebuilt below. They are re-created at the end.
DROP TRIGGER trg_ledger_guard;
DROP TRIGGER trg_ledger_apply;
DROP TRIGGER trg_ledger_no_update;
DROP TRIGGER trg_ledger_no_delete;

-- ---------------------------------------------------------------- new tables
CREATE TABLE deposit_resets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    actor            TEXT NOT NULL,
    reason           TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    nations_affected INTEGER NOT NULL,
    lines            INTEGER NOT NULL,
    totals_json      TEXT NOT NULL,          -- net totals before the reset, per resource (units)
    value_cents      INTEGER,                -- total net value before the reset
    price_snapshot_id INTEGER,
    price_as_of      TEXT,
    before_json      TEXT NOT NULL,          -- every balance that existed before the reset
    restore_batch_id INTEGER                 -- set once, when the matching restore import is committed
);
CREATE TABLE deposit_reset_items (
    reset_id  INTEGER NOT NULL REFERENCES deposit_resets(id),
    nation_id INTEGER NOT NULL,
    bucket    TEXT NOT NULL CHECK (bucket IN ('AVAILABLE','LOCKED')),
    resource  TEXT NOT NULL,
    lock_id   INTEGER,
    amount    INTEGER NOT NULL CHECK (amount != 0)     -- the balance that was cleared (may be negative)
);
CREATE UNIQUE INDEX uq_reset_items ON deposit_reset_items(reset_id, nation_id, bucket, resource, COALESCE(lock_id, 0));
CREATE TRIGGER trg_resets_nodelete BEFORE DELETE ON deposit_resets
BEGIN SELECT RAISE(ABORT, 'deposit reset history can never be deleted'); END;
CREATE TRIGGER trg_resets_fixed BEFORE UPDATE ON deposit_resets
WHEN OLD.restore_batch_id IS NOT NULL OR NEW.id != OLD.id OR NEW.created_at != OLD.created_at OR NEW.actor != OLD.actor
  OR NEW.reason != OLD.reason OR NEW.before_json != OLD.before_json OR NEW.totals_json != OLD.totals_json
  OR NEW.value_cents IS NOT OLD.value_cents OR NEW.lines != OLD.lines OR NEW.nations_affected != OLD.nations_affected
BEGIN SELECT RAISE(ABORT, 'a deposit reset record can never be changed (only its restore link may be set, once)'); END;
CREATE TRIGGER trg_reset_items_noupdate BEFORE UPDATE ON deposit_reset_items
BEGIN SELECT RAISE(ABORT, 'deposit reset records can never be changed'); END;
CREATE TRIGGER trg_reset_items_nodelete BEFORE DELETE ON deposit_reset_items
BEGIN SELECT RAISE(ABORT, 'deposit reset records can never be deleted'); END;

-- Outstanding loans carried in a restoration spreadsheet. NEVER a deposit. Kept until the loan module exists.
CREATE TABLE imported_loans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES import_batches(id),
    nation_id         INTEGER NOT NULL,
    outstanding_cents INTEGER NOT NULL CHECK (outstanding_cents > 0),
    source_column     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING_LOAN_MODULE' CHECK (status IN ('PENDING_LOAN_MODULE','APPLIED')),
    created_at        TEXT NOT NULL,
    applied_at        TEXT,
    UNIQUE (batch_id, nation_id)
);
CREATE INDEX idx_imported_loans_nation ON imported_loans(nation_id);
CREATE TRIGGER trg_imported_loans_nodelete BEFORE DELETE ON imported_loans
BEGIN SELECT RAISE(ABORT, 'imported loan records can never be deleted'); END;
CREATE TRIGGER trg_imported_loans_fixed BEFORE UPDATE ON imported_loans
WHEN NEW.batch_id != OLD.batch_id OR NEW.nation_id != OLD.nation_id OR NEW.outstanding_cents != OLD.outstanding_cents
  OR NEW.source_column != OLD.source_column OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'an imported loan amount can never be changed'); END;

-- Import batches say what kind of import they were (existing batches are OPENING).
ALTER TABLE import_batches ADD COLUMN kind TEXT NOT NULL DEFAULT 'OPENING';
ALTER TABLE import_batches ADD COLUMN reset_id INTEGER;

-- ------------------------------------------------- opening_balance_rows (allow negatives)
DROP TRIGGER trg_opening_rows_no_update;
DROP TRIGGER trg_opening_rows_no_delete;
CREATE TABLE opening_balance_rows_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  INTEGER NOT NULL REFERENCES import_batches(id),
    nation_id INTEGER NOT NULL,
    resource  TEXT NOT NULL,
    amount    INTEGER NOT NULL CHECK (amount != 0)
);
INSERT INTO opening_balance_rows_new(id, batch_id, nation_id, resource, amount)
    SELECT id, batch_id, nation_id, resource, amount FROM opening_balance_rows;
DROP TABLE opening_balance_rows;
ALTER TABLE opening_balance_rows_new RENAME TO opening_balance_rows;
CREATE INDEX idx_opening_rows_batch ON opening_balance_rows(batch_id);
CREATE TRIGGER trg_opening_rows_no_update BEFORE UPDATE ON opening_balance_rows
BEGIN SELECT RAISE(ABORT, 'opening balance records can never be changed'); END;
CREATE TRIGGER trg_opening_rows_no_delete BEFORE DELETE ON opening_balance_rows
BEGIN SELECT RAISE(ABORT, 'opening balance records can never be deleted'); END;

-- ------------------------------------------------------------ balances (allow negatives)
CREATE TABLE balances_new (
    nation_id INTEGER NOT NULL,
    bucket    TEXT NOT NULL CHECK (bucket IN ('AVAILABLE','LOCKED')),
    resource  TEXT NOT NULL,
    amount    INTEGER NOT NULL,
    PRIMARY KEY (nation_id, bucket, resource)
);
INSERT INTO balances_new(nation_id, bucket, resource, amount)
    SELECT nation_id, bucket, resource, amount FROM balances;
DROP TABLE balances;
ALTER TABLE balances_new RENAME TO balances;

-- ------------------------------------------------------------ ledger_entries (new types)
CREATE TABLE ledger_entries_new (
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
        'OPENING','DEPOSIT','WITHDRAWAL','LOCK','RELEASE','ADJUSTMENT',
        'RESET','RESTORE','LOAN_DEDUCTION','CONVERSION')),
    pnw_record_id  INTEGER,
    tx_id          INTEGER,
    lock_id        INTEGER,
    batch_id       INTEGER,
    adjustment_id  INTEGER,
    actor          TEXT NOT NULL,
    note           TEXT,
    price_snapshot_id INTEGER,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL,
    reset_id       INTEGER
);
INSERT INTO ledger_entries_new(id, ts, group_id, nation_id, bucket, resource, delta, entry_type, pnw_record_id,
        tx_id, lock_id, batch_id, adjustment_id, actor, note, price_snapshot_id, prev_hash, entry_hash)
    SELECT id, ts, group_id, nation_id, bucket, resource, delta, entry_type, pnw_record_id,
        tx_id, lock_id, batch_id, adjustment_id, actor, note, price_snapshot_id, prev_hash, entry_hash
    FROM ledger_entries ORDER BY id;
DROP TABLE ledger_entries;
ALTER TABLE ledger_entries_new RENAME TO ledger_entries;

CREATE INDEX idx_ledger_nation ON ledger_entries(nation_id, bucket, resource);
CREATE INDEX idx_ledger_group ON ledger_entries(group_id);
CREATE INDEX idx_ledger_lock ON ledger_entries(lock_id);
CREATE UNIQUE INDEX uq_ledger_deposit ON ledger_entries(pnw_record_id, resource)
    WHERE entry_type = 'DEPOSIT';
CREATE UNIQUE INDEX uq_ledger_withdrawal ON ledger_entries(tx_id, resource, bucket)
    WHERE entry_type = 'WITHDRAWAL';
CREATE UNIQUE INDEX uq_ledger_opening ON ledger_entries(nation_id, resource)
    WHERE entry_type = 'OPENING';
CREATE UNIQUE INDEX uq_ledger_restore ON ledger_entries(batch_id, nation_id, resource)
    WHERE entry_type = 'RESTORE';
CREATE UNIQUE INDEX uq_ledger_reset ON ledger_entries(reset_id, nation_id, bucket, resource, COALESCE(lock_id, 0))
    WHERE entry_type = 'RESET';

-- THE GUARD (same rules as before, plus RESET / RESTORE; OPENING stays positive-only).
CREATE TRIGGER trg_ledger_guard BEFORE INSERT ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger: EMERGENCY LOCK is active, this change is blocked')
    WHERE NEW.entry_type IN ('OPENING','DEPOSIT','LOCK','RELEASE','ADJUSTMENT','RESET','RESTORE')
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
            JOIN import_batches b ON b.id = r.batch_id AND b.kind = 'OPENING'
            WHERE r.batch_id = NEW.batch_id AND r.nation_id = NEW.nation_id
              AND r.resource = NEW.resource AND r.amount = NEW.delta
        )
      );

    SELECT RAISE(ABORT, 'ledger: a RESTORE must come from a committed restore import batch tied to a reset')
    WHERE NEW.entry_type = 'RESTORE'
      AND NOT (
        NEW.bucket = 'AVAILABLE'
        AND EXISTS (
            SELECT 1 FROM opening_balance_rows r
            JOIN import_batches b ON b.id = r.batch_id AND b.kind = 'RESTORE' AND b.reset_id IS NOT NULL
            WHERE r.batch_id = NEW.batch_id AND r.nation_id = NEW.nation_id
              AND r.resource = NEW.resource AND r.amount = NEW.delta
        )
      );

    SELECT RAISE(ABORT, 'ledger: a RESET must clear exactly a balance recorded in its reset record')
    WHERE NEW.entry_type = 'RESET'
      AND NOT EXISTS (
        SELECT 1 FROM deposit_reset_items i
        WHERE i.reset_id = NEW.reset_id AND i.nation_id = NEW.nation_id AND i.bucket = NEW.bucket
          AND i.resource = NEW.resource AND i.lock_id IS NEW.lock_id AND i.amount = -NEW.delta
      );

    SELECT RAISE(ABORT, 'ledger: this entry type is not enabled yet')
    WHERE NEW.entry_type IN ('LOAN_DEDUCTION','CONVERSION');

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
