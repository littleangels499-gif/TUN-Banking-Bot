-- 013: one shared cache of nation names, so people see "Igala [#123456]" instead of a bare id.
-- It is only a cache of what PnW says (refreshed periodically). Every table keeps using the nation id as the real identifier.
CREATE TABLE nation_names (
    nation_id     INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    alliance_id   INTEGER,
    alliance_name TEXT,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX idx_nation_names_name ON nation_names(name COLLATE NOCASE);
