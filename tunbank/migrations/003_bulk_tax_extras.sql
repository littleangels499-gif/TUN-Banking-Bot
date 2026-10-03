-- Bulk transfers + tax extras (brackets synced from PnW, TUN-policy exemption tracking).

CREATE TABLE tax_brackets (
    id         INTEGER PRIMARY KEY,          -- the PnW bracket id
    data_json  TEXT NOT NULL,                -- exactly what PnW returned
    synced_at  TEXT NOT NULL
);

CREATE TABLE tax_exemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nation_id    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    set_by       TEXT NOT NULL,
    set_at       TEXT NOT NULL,
    expires_at   TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    removed_by   TEXT,
    removed_at   TEXT,
    removal_note TEXT
);
CREATE INDEX idx_tax_exempt_nation ON tax_exemptions(nation_id, active);
CREATE TRIGGER trg_tax_exempt_nodelete BEFORE DELETE ON tax_exemptions
BEGIN SELECT RAISE(ABORT, 'tax exemption history can never be deleted'); END;

CREATE TABLE bulk_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    actor             TEXT NOT NULL,
    source_filename   TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    reason            TEXT NOT NULL,
    row_count         INTEGER NOT NULL,
    totals_json       TEXT NOT NULL,
    value_cents       INTEGER,
    price_snapshot_id INTEGER,
    status            TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','PARTIAL','HALTED')),
    approval_id       INTEGER,
    finished_at       TEXT
);
CREATE TRIGGER trg_bulk_batches_nodelete BEFORE DELETE ON bulk_batches
BEGIN SELECT RAISE(ABORT, 'bulk batches can never be deleted'); END;

CREATE TABLE bulk_items (
    batch_id     INTEGER NOT NULL REFERENCES bulk_batches(id),
    row_no       INTEGER NOT NULL,
    nation_id    INTEGER NOT NULL,
    amounts_json TEXT NOT NULL,
    note         TEXT,
    tx_id        INTEGER REFERENCES transactions(id),
    status       TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN
                 ('PENDING','COMPLETED','FAILED','UNCERTAIN')),
    message      TEXT,
    PRIMARY KEY (batch_id, row_no)
);
CREATE TRIGGER trg_bulk_items_nodelete BEFORE DELETE ON bulk_items
BEGIN SELECT RAISE(ABORT, 'bulk items can never be deleted'); END;
CREATE TRIGGER trg_bulk_items_fixed BEFORE UPDATE ON bulk_items
WHEN OLD.nation_id != NEW.nation_id OR OLD.amounts_json != NEW.amounts_json OR OLD.batch_id != NEW.batch_id
  OR OLD.row_no != NEW.row_no
BEGIN SELECT RAISE(ABORT, 'a bulk item''s destination and amounts can never be changed'); END;
