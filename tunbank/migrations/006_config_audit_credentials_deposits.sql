-- Dedicated configuration / security audit log, member API-key storage, and member-initiated deposits.

CREATE TABLE config_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    action     TEXT NOT NULL,          -- the command / button / startup check that made the change
    category   TEXT NOT NULL,
    setting    TEXT NOT NULL,
    previous   TEXT,
    new        TEXT,
    target     TEXT,                    -- affected role / nation / member / resource
    posted_at  TEXT,                    -- when it reached the private audit channel
    post_error TEXT
);
CREATE INDEX idx_config_audit_unposted ON config_audit(posted_at);
CREATE TRIGGER trg_config_audit_nodelete BEFORE DELETE ON config_audit
BEGIN SELECT RAISE(ABORT, 'configuration audit entries can never be deleted'); END;
CREATE TRIGGER trg_config_audit_fixed BEFORE UPDATE ON config_audit
WHEN OLD.ts != NEW.ts OR OLD.actor_id != NEW.actor_id OR OLD.action != NEW.action OR OLD.category != NEW.category
  OR OLD.setting != NEW.setting OR OLD.previous IS NOT NEW.previous OR OLD.new IS NOT NEW.new OR OLD.target IS NOT NEW.target
BEGIN SELECT RAISE(ABORT, 'configuration audit entries can never be changed'); END;

-- A member's own PnW API key, encrypted. Only ever usable for that member's own nation.
CREATE TABLE member_credentials (
    nation_id       INTEGER PRIMARY KEY,
    discord_id      TEXT NOT NULL,
    key_enc         BLOB NOT NULL,
    key_hint        TEXT NOT NULL,       -- last 4 characters, for the member to recognise it
    created_at      TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT,
    disabled        INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT
);

-- Deposits a member started from Discord with their own key. Credited ONLY when the real PnW bank record is seen.
CREATE TABLE member_deposits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id       INTEGER NOT NULL,
    discord_id      TEXT NOT NULL,
    amounts_json    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('PENDING','SENT','CREDITED','FAILED','UNCERTAIN')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempted_at    TEXT,
    pnw_record_id   INTEGER,
    failure_reason  TEXT,
    credited_at     TEXT,
    value_cents     INTEGER,
    price_snapshot_id INTEGER
);
CREATE INDEX idx_member_deposits_nation ON member_deposits(nation_id, status);
CREATE TRIGGER trg_member_deposits_nodelete BEFORE DELETE ON member_deposits
BEGIN SELECT RAISE(ABORT, 'member deposit history can never be deleted'); END;
CREATE TRIGGER trg_member_deposits_fixed BEFORE UPDATE ON member_deposits
WHEN OLD.amounts_json != NEW.amounts_json OR OLD.nation_id != NEW.nation_id OR OLD.discord_id != NEW.discord_id
BEGIN SELECT RAISE(ABORT, 'a member deposit''s nation and amounts can never be changed'); END;
CREATE TRIGGER trg_member_deposits_credit_ins BEFORE INSERT ON member_deposits
WHEN NEW.status = 'CREDITED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'a deposit cannot be CREDITED without a real PnW bank record'); END;
CREATE TRIGGER trg_member_deposits_credit_upd BEFORE UPDATE ON member_deposits
WHEN NEW.status = 'CREDITED' AND NEW.pnw_record_id IS NULL
BEGIN SELECT RAISE(ABORT, 'a deposit cannot be CREDITED without a real PnW bank record'); END;
