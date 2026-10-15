-- 014: FUNDED BALANCE TRANSFERS ("/bank credit").
-- A credit to a member is never created from nothing: every transfer is a two-sided journal. The member side is posted to
-- the normal ledger (ADJUSTMENT entries, guarded exactly as before); the funding side is recorded here and, for an
-- ownership account, in the offshore ownership ledger. Purely additive: no existing table or trigger is touched.

CREATE TABLE balance_transfers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key         TEXT NOT NULL UNIQUE,
    recipient_nation_id     INTEGER NOT NULL,
    source_type             TEXT NOT NULL CHECK (source_type IN ('ALLIANCE','MEMBER','OWNERSHIP')),
    source_id               INTEGER,                          -- nation id (MEMBER) / alliance id (OWNERSHIP); NULL for ALLIANCE
    reason                  TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    reference               TEXT,
    operator                TEXT NOT NULL,
    approval_id             INTEGER REFERENCES approval_requests(id),
    deficit_override        INTEGER NOT NULL DEFAULT 0 CHECK (deficit_override IN (0,1)),
    override_reason         TEXT,
    override_by             TEXT,
    price_snapshot_id       INTEGER,
    group_id                TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    CHECK ((source_type = 'ALLIANCE' AND source_id IS NULL) OR (source_type != 'ALLIANCE' AND source_id IS NOT NULL)),
    CHECK (deficit_override = 0 OR (length(trim(COALESCE(override_reason, ''))) > 0 AND override_by IS NOT NULL))
);
CREATE INDEX idx_balance_transfers_recipient ON balance_transfers(recipient_nation_id);

-- Both sides of the journal. Per transfer and resource the deltas always add up to exactly zero (verified by reconciliation).
CREATE TABLE balance_transfer_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id   INTEGER NOT NULL REFERENCES balance_transfers(id),
    account_type  TEXT NOT NULL CHECK (account_type IN ('MEMBER','ALLIANCE','OWNERSHIP')),
    account_id    INTEGER,
    resource      TEXT NOT NULL CHECK (resource IN (
        'money','food','coal','oil','uranium','iron','bauxite','lead','gasoline','munitions','steel','aluminum')),
    delta         INTEGER NOT NULL CHECK (delta != 0)
);
CREATE INDEX idx_balance_transfer_lines_tx ON balance_transfer_lines(transfer_id);

CREATE TRIGGER trg_balance_transfers_no_update BEFORE UPDATE ON balance_transfers
BEGIN SELECT RAISE(ABORT, 'balance transfers can never be changed'); END;
CREATE TRIGGER trg_balance_transfers_no_delete BEFORE DELETE ON balance_transfers
BEGIN SELECT RAISE(ABORT, 'balance transfers can never be deleted'); END;
CREATE TRIGGER trg_balance_transfer_lines_no_update BEFORE UPDATE ON balance_transfer_lines
BEGIN SELECT RAISE(ABORT, 'balance transfer lines can never be changed'); END;
CREATE TRIGGER trg_balance_transfer_lines_no_delete BEFORE DELETE ON balance_transfer_lines
BEGIN SELECT RAISE(ABORT, 'balance transfer lines can never be deleted'); END;
