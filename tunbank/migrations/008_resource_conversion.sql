-- 008: resource conversion inside a member's TUN Bank account (ledger-only; nothing happens in-game).
--
-- A conversion swaps ownership between a member and the alliance inside the same physical bank: the member's
-- resource A becomes alliance-owned, an equal market value of alliance-owned resource B becomes the member's.
-- No PnW transaction, no resource is created. The bot refuses a conversion unless the alliance really owns the
-- resource being received (checked against the live bank), so reconciliation (ledger <= real bank) keeps holding.
CREATE TABLE conversions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    nation_id        INTEGER NOT NULL,
    from_resource    TEXT NOT NULL CHECK (from_resource IN (
        'money','food','coal','oil','uranium','iron','bauxite','lead','gasoline','munitions','steel','aluminum')),
    from_units       INTEGER NOT NULL CHECK (from_units > 0),
    to_resource      TEXT NOT NULL CHECK (to_resource IN (
        'money','food','coal','oil','uranium','iron','bauxite','lead','gasoline','munitions','steel','aluminum')),
    to_units         INTEGER NOT NULL CHECK (to_units > 0),
    value_cents      INTEGER NOT NULL CHECK (value_cents > 0),
    from_price       TEXT NOT NULL,
    to_price         TEXT NOT NULL,
    price_snapshot_id INTEGER,
    price_as_of      TEXT,
    actor            TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    CHECK (from_resource != to_resource)
);
CREATE INDEX idx_conversions_nation ON conversions(nation_id, id);
CREATE TRIGGER trg_conversions_noupdate BEFORE UPDATE ON conversions
BEGIN SELECT RAISE(ABORT, 'conversion records can never be changed'); END;
CREATE TRIGGER trg_conversions_nodelete BEFORE DELETE ON conversions
BEGIN SELECT RAISE(ABORT, 'conversion records can never be deleted'); END;

ALTER TABLE ledger_entries ADD COLUMN conversion_id INTEGER;
CREATE UNIQUE INDEX uq_ledger_conversion ON ledger_entries(conversion_id, resource) WHERE entry_type = 'CONVERSION';

-- Replace the guard with the same rules plus the CONVERSION rule (ledger rows are not rebuilt or touched).
DROP TRIGGER trg_ledger_guard;
CREATE TRIGGER trg_ledger_guard BEFORE INSERT ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger: EMERGENCY LOCK is active, this change is blocked')
    WHERE NEW.entry_type IN ('OPENING','DEPOSIT','LOCK','RELEASE','ADJUSTMENT','RESET','RESTORE','CONVERSION')
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

    SELECT RAISE(ABORT, 'ledger: a CONVERSION must match a recorded conversion (one side debited, the other credited)')
    WHERE NEW.entry_type = 'CONVERSION'
      AND NOT (
        NEW.bucket = 'AVAILABLE'
        AND EXISTS (
            SELECT 1 FROM conversions c
            WHERE c.id = NEW.conversion_id AND c.nation_id = NEW.nation_id
              AND ((NEW.resource = c.from_resource AND NEW.delta = -c.from_units)
                OR (NEW.resource = c.to_resource AND NEW.delta = c.to_units))
        )
      );

    SELECT RAISE(ABORT, 'ledger: this entry type is not enabled yet')
    WHERE NEW.entry_type = 'LOAN_DEDUCTION';

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

