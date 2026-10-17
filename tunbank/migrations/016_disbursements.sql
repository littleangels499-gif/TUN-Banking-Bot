-- 016: DISBURSEMENTS - /transfer raws and /grant warchest|city|infrastructure|land. Purely additive.
-- The money itself still moves through the existing, guarded withdrawal pipeline (one `transactions` row per recipient, sent to
-- PnW at most once). These tables record WHAT was calculated, from WHICH inputs, funded by WHOM, with a structured
-- classification that is separate from the human-readable PnW note.

CREATE TABLE disbursement_batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('RAWS','WARCHEST_GRANT','CITY_GRANT','INFRA_GRANT','LAND_GRANT')),
    actor               TEXT NOT NULL,
    funding_source      TEXT NOT NULL CHECK (funding_source IN ('ALLIANCE','MEMBER_AVAILABLE','MEMBER_LOCKED')),
    source_member_id    INTEGER,                         -- the member whose funds pay (NULL = alliance funds, or each recipient pays their own)
    source_description  TEXT NOT NULL,                   -- exactly what the confirmation screen told the operator
    paying_bank         TEXT,
    note                TEXT NOT NULL,
    parameters_json     TEXT NOT NULL,                   -- the command inputs (days, ratio, targets, flags)
    rules_version       TEXT,
    totals_json         TEXT NOT NULL,
    value_cents         INTEGER,
    price_snapshot_id   INTEGER,
    approval_id         INTEGER REFERENCES approval_requests(id),
    content_sha256      TEXT NOT NULL,                   -- identical content within minutes is refused (double-run protection)
    request_key         TEXT NOT NULL UNIQUE,            -- the same confirmed request can never create a second batch
    status              TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','COMPLETED','PARTIAL','FAILED','HALTED')),
    finished_at         TEXT
);
CREATE INDEX idx_disb_batches_sha ON disbursement_batches(content_sha256, created_at);

CREATE TABLE disbursement_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES disbursement_batches(id),
    seq              INTEGER NOT NULL,
    nation_id        INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('RAWS','WARCHEST_GRANT','CITY_GRANT','INFRA_GRANT','LAND_GRANT')),
    amounts_json     TEXT NOT NULL,                      -- what is sent (units/100); '{}' for a nation left out for review
    requirement_json TEXT,
    stock_json       TEXT,
    calculation_json TEXT NOT NULL,                      -- calculation lines, inputs, assumptions, estimate flag
    source_member_id INTEGER,
    lock_id          INTEGER,
    status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','COMPLETED','FAILED','UNCERTAIN','REVIEW')),
    tx_id            INTEGER REFERENCES transactions(id),
    pnw_record_id    INTEGER,
    message          TEXT,
    UNIQUE (batch_id, seq)
);
CREATE INDEX idx_disb_items_nation ON disbursement_items(nation_id);

-- Only the outcome of an item / the status of a batch may change. What was calculated, for whom and from whom never can.
CREATE TRIGGER trg_disb_items_frozen BEFORE UPDATE ON disbursement_items
WHEN NEW.batch_id != OLD.batch_id OR NEW.seq != OLD.seq OR NEW.nation_id != OLD.nation_id OR NEW.kind != OLD.kind
  OR NEW.amounts_json != OLD.amounts_json OR NEW.calculation_json != OLD.calculation_json
  OR IFNULL(NEW.requirement_json,'') != IFNULL(OLD.requirement_json,'') OR IFNULL(NEW.stock_json,'') != IFNULL(OLD.stock_json,'')
  OR IFNULL(NEW.source_member_id,0) != IFNULL(OLD.source_member_id,0) OR IFNULL(NEW.lock_id,0) != IFNULL(OLD.lock_id,0)
  OR (OLD.tx_id IS NOT NULL AND IFNULL(NEW.tx_id,0) != OLD.tx_id)
BEGIN SELECT RAISE(ABORT, 'a disbursement item can only change its outcome, never what was calculated'); END;
CREATE TRIGGER trg_disb_items_no_delete BEFORE DELETE ON disbursement_items
BEGIN SELECT RAISE(ABORT, 'disbursement items can never be deleted'); END;
CREATE TRIGGER trg_disb_batches_frozen BEFORE UPDATE ON disbursement_batches
WHEN NEW.kind != OLD.kind OR NEW.actor != OLD.actor OR NEW.funding_source != OLD.funding_source OR NEW.totals_json != OLD.totals_json
  OR NEW.parameters_json != OLD.parameters_json OR NEW.source_description != OLD.source_description OR NEW.note != OLD.note
  OR NEW.request_key != OLD.request_key OR NEW.content_sha256 != OLD.content_sha256 OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a disbursement batch can only change its status'); END;
CREATE TRIGGER trg_disb_batches_no_delete BEFORE DELETE ON disbursement_batches
BEGIN SELECT RAISE(ABORT, 'disbursement batches can never be deleted'); END;
