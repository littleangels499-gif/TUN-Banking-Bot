-- 015: (1) EXTERNAL WITHDRAWALS and (2) the member NOTIFICATION outbox. Purely additive.
--
-- (1) Money that leaves the offshore for a linked member WITHOUT going through TUN Bank (in-game, or another bot such as
--     Locutus) is recorded here, once per real PnW record, and deducted from that member's balance through a normal,
--     guarded ledger ADJUSTMENT. The journal is the label that tells it apart from a deposit, a donation or a staff
--     adjustment. The trigger below refuses a row that is not backed by the real outgoing PnW record.

CREATE TABLE external_withdrawals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pnw_record_id   INTEGER NOT NULL UNIQUE,              -- one PnW transaction is processed exactly once
    nation_id       INTEGER NOT NULL,                     -- the recipient, charged
    source_bank_id  INTEGER NOT NULL,                     -- alliance id of the bank the funds left
    source_label    TEXT NOT NULL,
    amounts_json    TEXT NOT NULL,
    before_json     TEXT NOT NULL,                        -- the member's AVAILABLE balance before / after
    after_json      TEXT NOT NULL,
    negative_json   TEXT NOT NULL DEFAULT '{}',           -- resources this pushed below zero (a real debt, never clamped)
    decision        TEXT NOT NULL CHECK (decision IN ('AUTO','STAFF')),
    decided_by      TEXT NOT NULL,
    note            TEXT NOT NULL CHECK (length(trim(note)) > 0),
    group_id        TEXT NOT NULL,
    record_date     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_external_withdrawals_nation ON external_withdrawals(nation_id);

CREATE TRIGGER trg_external_withdrawals_guard BEFORE INSERT ON external_withdrawals
BEGIN
    SELECT RAISE(ABORT, 'external withdrawal: needs the real outgoing PnW record from our own bank to that nation')
    WHERE NOT EXISTS (
        SELECT 1 FROM pnw_records p
        WHERE p.id = NEW.pnw_record_id AND p.direction = 'OUT' AND p.receiver_type = 1
          AND p.receiver_id = NEW.nation_id AND p.sender_id = NEW.source_bank_id
          AND p.amounts_json = NEW.amounts_json);
    SELECT RAISE(ABORT, 'external withdrawal: that record is a transfer TUN Bank made itself')
    WHERE EXISTS (SELECT 1 FROM pnw_records p WHERE p.id = NEW.pnw_record_id AND (p.tx_id IS NOT NULL OR p.classification = 'OUTGOING_TX'));
END;
CREATE TRIGGER trg_external_withdrawals_no_update BEFORE UPDATE ON external_withdrawals
BEGIN SELECT RAISE(ABORT, 'external withdrawals can never be changed'); END;
CREATE TRIGGER trg_external_withdrawals_no_delete BEFORE DELETE ON external_withdrawals
BEGIN SELECT RAISE(ABORT, 'external withdrawals can never be deleted'); END;

-- (2) Member notification outbox. A row is written INSIDE the same database transaction as the money movement, so a
--     notification exists if and only if the movement was really recorded. A separate sender delivers it afterwards; a
--     failed DM never touches the transaction and stays visible here for staff.
CREATE TABLE member_notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT NOT NULL UNIQUE,                   -- one notification per event, ever
    nation_id     INTEGER NOT NULL,
    discord_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED','SKIPPED')),
    error         TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT
);
CREATE INDEX idx_member_notifications_status ON member_notifications(status, id);
