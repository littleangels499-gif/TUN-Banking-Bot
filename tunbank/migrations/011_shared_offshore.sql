-- 011: SHARED OFFSHORE. One physical PnW offshore bank, many registered alliances, one beneficial-ownership ledger.
--
-- The offshore's real PnW balance stays the physical source of truth. These tables only say WHOSE share of it is whose.
-- Nothing here changes member deposits; a registered alliance never sees another alliance's rows (enforced in the commands).

CREATE TABLE offshore_alliances (
    alliance_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    is_host     INTEGER NOT NULL DEFAULT 0,          -- 1 = the alliance that runs this bot (TUN)
    role_id     TEXT,                                -- Discord role (in the TUN server) allowed to view ONLY this alliance's account
    active      INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    added_by    TEXT NOT NULL,
    added_at    TEXT NOT NULL
);
CREATE TRIGGER trg_offshore_alliances_nodelete BEFORE DELETE ON offshore_alliances
BEGIN SELECT RAISE(ABORT, 'registered alliances can never be deleted (deactivate them instead)'); END;

CREATE TABLE offshore_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    group_id       TEXT NOT NULL,
    alliance_id    INTEGER NOT NULL REFERENCES offshore_alliances(alliance_id),
    resource       TEXT NOT NULL CHECK (resource IN (
        'money','food','coal','oil','uranium','iron','bauxite','lead','gasoline','munitions','steel','aluminum')),
    delta          INTEGER NOT NULL CHECK (delta != 0),
    entry_type     TEXT NOT NULL CHECK (entry_type IN ('OPENING','DEPOSIT','WITHDRAWAL','TRANSFER','ADJUSTMENT')),
    pnw_record_id  INTEGER,
    transfer_id    INTEGER,
    actor          TEXT NOT NULL,
    note           TEXT,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL
);
CREATE INDEX idx_offshore_entries_alliance ON offshore_entries(alliance_id, resource);
CREATE INDEX idx_offshore_entries_group ON offshore_entries(group_id);
CREATE UNIQUE INDEX uq_offshore_entry_record ON offshore_entries(pnw_record_id, alliance_id, resource, entry_type)
    WHERE pnw_record_id IS NOT NULL AND entry_type IN ('DEPOSIT','WITHDRAWAL');

CREATE TABLE offshore_balances (
    alliance_id INTEGER NOT NULL REFERENCES offshore_alliances(alliance_id),
    resource    TEXT NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount >= 0),         -- ownership can never go below zero
    PRIMARY KEY (alliance_id, resource)
);

CREATE TRIGGER trg_offshore_guard BEFORE INSERT ON offshore_entries
BEGIN
    SELECT RAISE(ABORT, 'offshore: that alliance is not registered')
    WHERE NOT EXISTS (SELECT 1 FROM offshore_alliances a WHERE a.alliance_id = NEW.alliance_id);

    SELECT RAISE(ABORT, 'offshore: a DEPOSIT needs the real PnW record it came from')
    WHERE NEW.entry_type = 'DEPOSIT'
      AND NOT (NEW.delta > 0 AND EXISTS (
            SELECT 1 FROM pnw_records p
            WHERE p.id = NEW.pnw_record_id
              AND json_extract(p.amounts_json, '$.' || NEW.resource) = NEW.delta
              -- the bot credits only the alliance that actually sent it; a person may attribute it on ECON's decision
              AND (p.sender_id = NEW.alliance_id OR NEW.actor NOT LIKE 'system%')));

    SELECT RAISE(ABORT, 'offshore: a WITHDRAWAL needs the real PnW record it came from')
    WHERE NEW.entry_type = 'WITHDRAWAL'
      AND NOT (NEW.delta < 0 AND EXISTS (
            SELECT 1 FROM pnw_records p
            WHERE p.id = NEW.pnw_record_id AND json_extract(p.amounts_json, '$.' || NEW.resource) = -NEW.delta));

    SELECT RAISE(ABORT, 'offshore: this entry needs a documented reason')
    WHERE NEW.entry_type IN ('OPENING','TRANSFER','ADJUSTMENT') AND length(trim(COALESCE(NEW.note, ''))) = 0;

    SELECT RAISE(ABORT, 'offshore: an OPENING assignment must add to an alliance')
    WHERE NEW.entry_type = 'OPENING' AND NEW.delta <= 0;

    SELECT RAISE(ABORT, 'offshore: an ADJUSTMENT can only reduce an alliance share (it returns it to unassigned)')
    WHERE NEW.entry_type = 'ADJUSTMENT' AND NEW.delta >= 0;
END;
CREATE TRIGGER trg_offshore_apply AFTER INSERT ON offshore_entries
BEGIN
    INSERT OR IGNORE INTO offshore_balances(alliance_id, resource, amount) VALUES (NEW.alliance_id, NEW.resource, 0);
    UPDATE offshore_balances SET amount = amount + NEW.delta WHERE alliance_id = NEW.alliance_id AND resource = NEW.resource;
END;
CREATE TRIGGER trg_offshore_entries_noupdate BEFORE UPDATE ON offshore_entries
BEGIN SELECT RAISE(ABORT, 'offshore ownership entries can never be changed'); END;
CREATE TRIGGER trg_offshore_entries_nodelete BEFORE DELETE ON offshore_entries
BEGIN SELECT RAISE(ABORT, 'offshore ownership entries can never be deleted'); END;

-- ------------------------------------------------------------------------------------------------
-- offshore_transfers: allow PAYOUT (the bot sends from the offshore on behalf of a registered alliance).
-- SQLite cannot alter a CHECK in place, so the table is rebuilt with every row copied unchanged.
DROP TRIGGER trg_offshore_nodelete;
DROP TRIGGER trg_offshore_fixed;
CREATE TABLE offshore_transfers_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('TO_OFFSHORE','TO_MAIN','PAYOUT')),
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
    completed_at    TEXT,
    alliance_id     INTEGER,        -- PAYOUT: whose share is reduced
    dest_type       INTEGER,        -- PAYOUT: PnW receiver type (1 nation / 2 alliance)
    dest_id         INTEGER         -- PAYOUT: receiver id
);
INSERT INTO offshore_transfers_new(id, created_at, updated_at, direction, mode, status, actor, reason, amounts_json, value_cents,
        price_snapshot_id, idempotency_key, attempted_at, pnw_record_id, failure_reason, completed_at)
    SELECT id, created_at, updated_at, direction, mode, status, actor, reason, amounts_json, value_cents,
        price_snapshot_id, idempotency_key, attempted_at, pnw_record_id, failure_reason, completed_at FROM offshore_transfers ORDER BY id;
DROP TABLE offshore_transfers;
ALTER TABLE offshore_transfers_new RENAME TO offshore_transfers;
CREATE TRIGGER trg_offshore_nodelete BEFORE DELETE ON offshore_transfers
BEGIN SELECT RAISE(ABORT, 'offshore transfer history can never be deleted'); END;
CREATE TRIGGER trg_offshore_fixed BEFORE UPDATE ON offshore_transfers
WHEN OLD.amounts_json != NEW.amounts_json OR OLD.direction != NEW.direction
  OR OLD.alliance_id IS NOT NEW.alliance_id OR OLD.dest_id IS NOT NEW.dest_id OR OLD.dest_type IS NOT NEW.dest_type
BEGIN SELECT RAISE(ABORT, 'an offshore transfer''s amounts, direction and destination can never be changed'); END;
-- the rule from migration 005 (a transfer is never COMPLETED without a real PnW bank record) must survive the rebuild
CREATE TRIGGER trg_offshore_done_ins BEFORE INSERT ON offshore_transfers
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'an offshore transfer cannot be COMPLETED without a real PnW bank record'); END;
CREATE TRIGGER trg_offshore_done_upd BEFORE UPDATE ON offshore_transfers
WHEN NEW.status = 'COMPLETED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'an offshore transfer cannot be COMPLETED without a real PnW bank record'); END;
