-- 009: loans. Loan balances live in their own tables, SEPARATE from deposits. A loan is never a deposit.
--
--   * loans         one row per loan: principal, flat interest fixed when it is recorded, how much has been settled
--   * loan_events   append-only history: issued / imported / repayment / deduction / write-off
--   * A real #loan payment is applied interest -> principal; anything MORE than what is owed becomes the member's
--     deposit (a normal DEPOSIT ledger entry, allowed only because a loan_events row says so).
--   * A loan deduction takes cash from the member's available balance to pay the loan (LOAN_DEDUCTION ledger entry).
CREATE TABLE loans (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id            INTEGER NOT NULL,
    principal_cents      INTEGER NOT NULL CHECK (principal_cents > 0),
    interest_cents       INTEGER NOT NULL DEFAULT 0 CHECK (interest_cents >= 0),
    principal_paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (principal_paid_cents >= 0),
    interest_paid_cents  INTEGER NOT NULL DEFAULT 0 CHECK (interest_paid_cents >= 0),
    written_off_cents    INTEGER NOT NULL DEFAULT 0 CHECK (written_off_cents >= 0),
    status               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','PAID','WRITTEN_OFF')),
    source               TEXT NOT NULL CHECK (source IN ('RECORDED','IMPORTED')),
    imported_loan_id     INTEGER,
    issued_at            TEXT NOT NULL,
    due_at               TEXT,
    note                 TEXT,
    created_by           TEXT NOT NULL,
    overdue_alerted_at   TEXT,
    closed_at            TEXT,
    CHECK (principal_paid_cents <= principal_cents AND interest_paid_cents <= interest_cents)
);
CREATE INDEX idx_loans_nation ON loans(nation_id, status);
CREATE TABLE loan_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id         INTEGER NOT NULL REFERENCES loans(id),
    nation_id       INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('ISSUED','IMPORTED','REPAYMENT','DEDUCTION','WRITE_OFF')),
    interest_cents  INTEGER NOT NULL DEFAULT 0 CHECK (interest_cents >= 0),
    principal_cents INTEGER NOT NULL DEFAULT 0 CHECK (principal_cents >= 0),
    excess_cents    INTEGER NOT NULL DEFAULT 0 CHECK (excess_cents >= 0),
    pnw_record_id   INTEGER,
    actor           TEXT NOT NULL,
    note            TEXT
);
CREATE INDEX idx_loan_events_loan ON loan_events(loan_id);
CREATE UNIQUE INDEX uq_loan_event_record ON loan_events(pnw_record_id, loan_id) WHERE kind = 'REPAYMENT';
CREATE TRIGGER trg_loan_events_noupdate BEFORE UPDATE ON loan_events
BEGIN SELECT RAISE(ABORT, 'loan history can never be changed'); END;
CREATE TRIGGER trg_loan_events_nodelete BEFORE DELETE ON loan_events
BEGIN SELECT RAISE(ABORT, 'loan history can never be deleted'); END;
CREATE TRIGGER trg_loans_nodelete BEFORE DELETE ON loans
BEGIN SELECT RAISE(ABORT, 'loans can never be deleted'); END;
CREATE TRIGGER trg_loans_fixed BEFORE UPDATE ON loans
WHEN NEW.nation_id != OLD.nation_id OR NEW.principal_cents != OLD.principal_cents OR NEW.interest_cents != OLD.interest_cents
  OR NEW.issued_at != OLD.issued_at OR NEW.source != OLD.source OR NEW.created_by != OLD.created_by
  OR NEW.principal_paid_cents < OLD.principal_paid_cents OR NEW.interest_paid_cents < OLD.interest_paid_cents
  OR NEW.written_off_cents < OLD.written_off_cents
BEGIN SELECT RAISE(ABORT, 'a loan''s amounts can only move forward (payments), never be edited'); END;

ALTER TABLE ledger_entries ADD COLUMN loan_event_id INTEGER;
CREATE UNIQUE INDEX uq_ledger_loan_deduction ON ledger_entries(loan_event_id) WHERE entry_type = 'LOAN_DEDUCTION';

DROP TRIGGER trg_ledger_guard;
CREATE TRIGGER trg_ledger_guard BEFORE INSERT ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger: EMERGENCY LOCK is active, this change is blocked')
    WHERE NEW.entry_type IN ('OPENING','DEPOSIT','LOCK','RELEASE','ADJUSTMENT','RESET','RESTORE','CONVERSION','LOAN_DEDUCTION')
      AND (SELECT value FROM system_state WHERE key = 'emergency_lock') = '1';

    SELECT RAISE(ABORT, 'ledger: a DEPOSIT needs a matching real PnW deposit record')
    WHERE NEW.entry_type = 'DEPOSIT'
      AND NOT (
        NEW.delta > 0 AND NEW.bucket = 'AVAILABLE'
        AND (
            EXISTS (
                SELECT 1 FROM pnw_records p
                WHERE p.id = NEW.pnw_record_id
                  AND p.classification = 'MEMBER_DEPOSIT'
                  AND p.direction = 'IN'
                  AND p.credited_nation_id = NEW.nation_id
                  AND json_extract(p.amounts_json, '$.' || NEW.resource) = NEW.delta
            )
            OR (
                -- the part of a real #loan repayment that is MORE than the loan still owed becomes the member's deposit
                NEW.resource = 'money'
                AND EXISTS (
                    SELECT 1 FROM pnw_records p
                    JOIN loan_events e ON e.pnw_record_id = p.id
                    WHERE p.id = NEW.pnw_record_id
                      AND p.classification = 'LOAN_REPAYMENT'
                      AND p.direction = 'IN'
                      AND e.kind = 'REPAYMENT'
                      AND e.nation_id = NEW.nation_id
                      AND e.excess_cents = NEW.delta
                )
            )
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

    SELECT RAISE(ABORT, 'ledger: a LOAN_DEDUCTION must match a recorded loan deduction')
    WHERE NEW.entry_type = 'LOAN_DEDUCTION'
      AND NOT (
        NEW.bucket = 'AVAILABLE' AND NEW.resource = 'money' AND NEW.delta < 0
        AND EXISTS (
            SELECT 1 FROM loan_events e
            WHERE e.id = NEW.loan_event_id AND e.kind = 'DEDUCTION' AND e.nation_id = NEW.nation_id
              AND e.interest_cents + e.principal_cents = -NEW.delta
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

