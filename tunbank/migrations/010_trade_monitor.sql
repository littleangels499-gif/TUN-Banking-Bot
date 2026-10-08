-- 010: trade monitoring. Alerts only: nothing here ever moves money or touches a balance.
CREATE TABLE trade_seen (
    trade_id INTEGER PRIMARY KEY,
    seen_at  TEXT NOT NULL
);
CREATE TABLE trade_alerts (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                   INTEGER NOT NULL UNIQUE,
    created_at                 TEXT NOT NULL,
    trade_date                 TEXT,
    trade_type                 TEXT,
    kinds                      TEXT NOT NULL,                 -- e.g. 'PRICE,NATIONALIST,EMBARGO'
    member_nation_id           INTEGER NOT NULL,
    member_name                TEXT,
    counterparty_id            INTEGER,
    counterparty_name          TEXT,
    counterparty_alliance_id   INTEGER,
    counterparty_alliance_name TEXT,
    resource                   TEXT NOT NULL,
    quantity                   INTEGER NOT NULL,
    price                      REAL NOT NULL,
    reference_price            REAL,
    multiple                   REAL,
    total_cents                INTEGER,
    direction                  TEXT NOT NULL CHECK (direction IN ('BUY','SELL')),   -- from the TUN member's side
    reasons_json               TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','REVIEWED')),
    reviewed_by                TEXT,
    reviewed_at                TEXT,
    review_note                TEXT,
    alerted_at                 TEXT
);
CREATE INDEX idx_trade_alerts_status ON trade_alerts(status, id);
CREATE INDEX idx_trade_alerts_member ON trade_alerts(member_nation_id);
CREATE TRIGGER trg_trade_alerts_nodelete BEFORE DELETE ON trade_alerts
BEGIN SELECT RAISE(ABORT, 'trade alerts can never be deleted'); END;
CREATE TRIGGER trg_trade_alerts_fixed BEFORE UPDATE ON trade_alerts
WHEN NEW.trade_id != OLD.trade_id OR NEW.kinds != OLD.kinds OR NEW.member_nation_id != OLD.member_nation_id
  OR NEW.resource != OLD.resource OR NEW.quantity != OLD.quantity OR NEW.price != OLD.price
  OR NEW.reasons_json != OLD.reasons_json OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a trade alert can only be marked reviewed or announced, never rewritten'); END;

-- Policy lists. Changes are written to the configuration audit log by the commands; these rows also keep who/when.
CREATE TABLE trade_nationalists (
    nation_id   INTEGER PRIMARY KEY,
    nation_name TEXT,
    resources   TEXT NOT NULL DEFAULT '*',       -- '*' = all resources, else comma list e.g. 'food,coal'
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE trade_embargoes (
    alliance_id   INTEGER PRIMARY KEY,
    alliance_name TEXT,
    resources     TEXT NOT NULL DEFAULT '*',
    updated_by    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
