-- Tallyton D1 schema
-- One row per (device, day). Totals are SUMs over these rows, so re-pushing the
-- same day is idempotent (the row is replaced, not added to).

CREATE TABLE IF NOT EXISTS stats (
  device_id    TEXT    NOT NULL,
  device_name  TEXT,
  day          TEXT    NOT NULL,            -- ISO date, e.g. 2026-06-24
  keystrokes   INTEGER NOT NULL DEFAULT 0,
  words        INTEGER NOT NULL DEFAULT 0,
  deletions    INTEGER NOT NULL DEFAULT 0,  -- Backspace + Delete presses
  alt_tabs     INTEGER NOT NULL DEFAULT 0,
  power_cycles INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT,
  PRIMARY KEY (device_id, day)
);

CREATE INDEX IF NOT EXISTS idx_stats_day ON stats (day);

-- Upgrading a database created before the "deletions" column existed? Run this
-- once in the D1 console (safe to skip on a fresh database):
--   ALTER TABLE stats ADD COLUMN deletions INTEGER NOT NULL DEFAULT 0;
