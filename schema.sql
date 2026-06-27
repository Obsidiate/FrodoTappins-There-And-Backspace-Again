CREATE TABLE IF NOT EXISTS stats (device_id TEXT NOT NULL, device_name TEXT, day TEXT NOT NULL, keystrokes INTEGER NOT NULL DEFAULT 0, words INTEGER NOT NULL DEFAULT 0, deletions INTEGER NOT NULL DEFAULT 0, alt_tabs INTEGER NOT NULL DEFAULT 0, power_cycles INTEGER NOT NULL DEFAULT 0, updated_at TEXT, PRIMARY KEY (device_id, day));
CREATE INDEX IF NOT EXISTS idx_stats_day ON stats (day);

/* ---------------------------------------------------------------------------
   NOTES — DO NOT PASTE ANYTHING BELOW THIS LINE INTO THE D1 CONSOLE.

   The two statements above are the whole schema. They are written on single
   lines with no "--" comments on purpose, so you can select them and paste
   straight into the Cloudflare D1 dashboard console (Storage & Databases ->
   your database -> Console). The console runs one statement per line and can
   choke on "--" comments, which is why the notes live in this block comment.

   Table layout (one row per device per day; totals are SUMs over these rows,
   so re-pushing the same day is idempotent -- the row is replaced, not added):

     device_id     TEXT     the syncing device's id
     device_name   TEXT     friendly name shown in the Cloud panel
     day           TEXT     ISO date, e.g. 2026-06-24
     keystrokes    INTEGER  total key presses that day
     words         INTEGER  detected word boundaries
     deletions     INTEGER  Backspace + Delete presses (also counted as keys)
     alt_tabs      INTEGER  Alt+Tab window switches
     power_cycles  INTEGER  restarts / shutdowns
     updated_at    TEXT     last write time (ISO)
     PRIMARY KEY (device_id, day)

   Upgrading a database created before the "deletions" column existed? Paste
   this single line into the console once (safe to skip on a fresh database):

     ALTER TABLE stats ADD COLUMN deletions INTEGER NOT NULL DEFAULT 0;
   --------------------------------------------------------------------------- */
