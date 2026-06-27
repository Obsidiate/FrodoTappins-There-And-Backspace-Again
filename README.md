# FrodoTappins — There and Backspace Again

So. I was idly wondering how often I type the equivalent of the Lord Of The Rings trilogy. 
Then I excercised free will. 

A small, privacy-respecting activity keystroke counter for Windows, that can sync across devices via a free cloudflare account. 

<img width="1200" height="627" alt="frodotappins-linkedin-v2" src="https://github.com/user-attachments/assets/0729daeb-1542-4f45-92cd-a68efa713bb3" />
<p align="center">
And a larger preview. </p>

<p align="center">
<img width="571" height="1035" alt="frodotappins" src="https://github.com/user-attachments/assets/b978ce63-2f48-497e-aa79-ec9cb6508a4c" />
</p>
<p align="center">
<em>Day is ended, dim my eyes,<br>
but journey long before me lies.</em>
</p>

It tracks, per day:

- **Keystrokes** — total key presses
- **Words** — counted by detecting word boundaries
- **Deletions** — Backspace and Delete presses (also counted as keystrokes)
- **Alt-Tabs** — Alt+Tab window switches
- **Power cycles** — restarts / shutdowns, detected from the OS boot time

It also shows, against your all-time word total, how far you've typed toward
some of Tolkien's works (*The Hobbit*, *The Lord of the Rings*, *The
Silmarillion*) — a percentage and the words you have left to go for each.

It can run entire offline, or sync multiple devices via a free-tier Cloudflare account's DB + workers (setup instructions included). 

Why? Idle curiousity. Open source so you can verify keystrokes are not logged, stored, or transmitted. Only counted.

It also shows your **start date** and **days since** you began tracking, and a
live **breakdown** across the last hour, day, week, month, year, and all-time.

It is fully **portable and offline**: everything lives in the app's own folder
(move it, copy it to a USB stick, or delete it to uninstall — see below), and it
makes no network connections unless you set up cloud sync yourself. It runs in
the system tray and can start at login. With cloud sync configured it **auto-syncs
every 15 minutes** (and on demand), showing each device's total and the combined
cloud total side by side, so several machines roll into the same figures.

### Privacy: it counts, it does not log

This is a *counter*, not a keylogger. On each key press it increments a number
and, to count words, it checks whether the key was a letter/digit or a
separator — then discards that. **The actual character is never stored, written
to disk, or transmitted.** Only the daily totals above ever leave your
machine, and only when it syncs to a cloud you've set up yourself. No window
titles, no clipboard, no key content. The source is short on purpose so you can
verify this yourself (see `Counter._on_press` in `tracker.py`).

---

## Files

```
tracker.py          The whole local app (listener + GUI + tray + sync + autostart)
requirements.txt    Python dependencies
build.bat           One-command build -> dist/FrodoTappins.exe
worker.js           Cloudflare Worker (the sync API)
schema.sql          D1 table definition (top two lines paste straight into the D1 console)
wrangler.toml       Optional — only used if you deploy via the Wrangler CLI
```

---

A downloadable portable EXE is available, else 

---

## 1. Build the .exe (Windows)

You need [Python 3.9+](https://www.python.org/downloads/) installed (tick *Add
Python to PATH* in the installer). Then, in this folder:

```
build.bat
```

That installs the dependencies + PyInstaller and produces **`dist\FrodoTappins.exe`**
— a single portable file with no console window. Double-click it and a small
window appears showing today's figures and your all-time totals.

> Prefer to just run it without building? `pip install -r requirements.txt`
> then `python tracker.py`.

### Where your data lives (portable)

FrodoTappins keeps everything in **its own folder**, right next to the executable:
`data.json` (your counts, device id, start date) and `config.json` (your Worker
URL + key, and the auto-sync setting). Nothing is written to AppData or the
registry. To move it, move the whole folder; to back it up, copy the folder; to
**uninstall, just delete the folder.** The window shows the exact location and
has an **Open folder** button. The folder must be somewhere writable — a normal
folder, Desktop, or USB drive, not `Program Files` — or the app warns you on start.

### Start at login

Tick **"Start at login"** in the window. That drops a shortcut (pointing at the
exe, with `--minimized` so it starts hidden in the tray) into your Startup folder
— no registry changes, no admin rights. Because the shortcut stores the exe's
current path, **moving the folder breaks auto-start**; just re-tick the box to
repoint it. Untick to remove.

### A note on antivirus

Any program that hooks the keyboard *and* is packed into a one-file exe can trip
heuristic antivirus flags — this is common for legitimate tools too (AutoHotkey,
WhatPulse, etc.). Since you built it yourself from source you can verify it's
benign and add a Defender exclusion if needed. 

---

## 2. Cloud sync with Cloudflare (free tier) — optional

The app works fully standalone; this part just adds the cross-device totals. You
set up one Worker backed by one D1 database **entirely in the Cloudflare web
dashboard — no Wrangler, no command-line tools, nothing to install.** All you
need is a free Cloudflare account and a browser. (If you'd rather use the
Wrangler CLI, see the note at the end of this section; you can ignore
`wrangler.toml` otherwise.)

Everything you paste below is given as ready-to-paste blocks: the SQL statements
are single lines with no `--` comments (the D1 console rejects those), and the
Worker code is the whole `worker.js` file verbatim.

Two names must match the code exactly (both case-sensitive): the database binding
**`DB`** and the secret **`API_KEY`** — these are what `worker.js` reads via
`env.DB` and `env.API_KEY`.

**a. Create the database and load the table**

1. Dashboard → **Storage & Databases → D1 SQL Database → Create Database**. Name
   it (e.g. `frodotappins`) and create it.
2. Open the database → **Console** (on some accounts this is behind an *Explore
   Data* button) and paste these two statements, then run them. They are the top
   two lines of `schema.sql` — already on single lines with no comments, exactly
   as the console needs (one statement per line, no `--` comments):

   ```sql
   CREATE TABLE IF NOT EXISTS stats (device_id TEXT NOT NULL, device_name TEXT, day TEXT NOT NULL, keystrokes INTEGER NOT NULL DEFAULT 0, words INTEGER NOT NULL DEFAULT 0, deletions INTEGER NOT NULL DEFAULT 0, alt_tabs INTEGER NOT NULL DEFAULT 0, power_cycles INTEGER NOT NULL DEFAULT 0, updated_at TEXT, PRIMARY KEY (device_id, day));
   ```
   ```sql
   CREATE INDEX IF NOT EXISTS idx_stats_day ON stats (day);
   ```

**b. Create the Worker**

3. **Workers & Pages → Create application → Create Worker**, name it (e.g.
   `frodotappins-sync`), and **Deploy** (this deploys a placeholder you'll replace).

**c. Bind the database**

4. Open the Worker → **Bindings → Add binding → D1 database**. Set the variable
   name to **`DB`** and select your database.

**d. Add the API key**

5. Worker → **Settings → Variables and Secrets → Add**. Choose type **Secret**,
   name **`API_KEY`**, value = any long random string you choose. **Deploy.** Keep
   a copy — the value is hidden once saved, and it's the same key you put in the
   app. Treat it like a password.

**e. Paste the code**

6. Worker → **Edit code** (`</>`), clear the file, paste the full contents of
   `worker.js`, and **Deploy**. Make sure the workers.dev URL is enabled
   (Settings → Domains & Routes). The Worker's page shows its
   `https://<worker-name>.<your-subdomain>.workers.dev` URL.

**Connect the app:** in FrodoTappins's *Cloud* box paste that Worker URL and the same
API key, click **Save**, then **Push to Cloud**. Sanity-check in a browser by
visiting `<your worker URL>/totals` — it should return `{"error":"unauthorized"}`
(the Worker is alive; a browser doesn't send the key). Install the app on another
PC, point it at the same URL + key (it appears as its own device), and both
machines tally into the combined total.

> **Recreating or renaming the Worker?** The `DB` binding and `API_KEY` secret are
> per-Worker and don't carry over — re-add both (each followed by a Deploy) on the
> new Worker. A missing `DB` binding shows up as HTTP 500 on sync; a wrong/missing
> key shows up as 401; a request blocked by Cloudflare's edge shows up as 403.

> Prefer the command line? `wrangler.toml` is there for the Wrangler CLI
> route, but the dashboard steps above don't need it.

---

## How the numbers work

- **Two storage tiers, kept locally.** Every event increments two independent
  counters: a per-**minute** bucket and a per-**day** bucket. The minute tier is
  kept for ~26 hours (older buckets are pruned); the day tier is kept forever.
  This keeps the file small while making every window cheap to compute.
- **The breakdown** sums whichever tier fits the window:
  - *Last hour* / *Last day* come from the minute tier — true rolling windows
    (the trailing 60 minutes / 24 hours), accurate to the minute.
  - *Last week* / *Last month* / *Last year* / *All-time* come from the day tier
    (the last 7 / 30 / 365 day-buckets, and every bucket respectively),
    accurate to the day.
- **Start date / days since.** The first run records a start timestamp; "days
  since" is calendar days from that date. Existing installs inherit their
  earliest recorded day as the start date when you upgrade.
- **Per-device, per-day rows in the cloud.** Each sync sends your full local
  day-by-day history. The Worker *upserts* by `(device_id, day)`, so re-syncing
  a day overwrites rather than double-counts — syncing twice is harmless, and a
  device that was offline self-heals on its next sync. The app syncs every 15
  minutes when configured (push this device, then pull all devices) and the
  Cloud panel lists each device's total plus a combined **Cloud total** row,
  with this device highlighted.
- **Power cycles** are detected by comparing the current OS boot time against the
  last one seen. Relaunching the app within one session does not count; only a
  real restart does. The first run records the boot time without counting it.
- **Deletions** count every Backspace and Delete press. They are *also* counted
  as keystrokes (every physical press is one keystroke), so the deletions column
  is a subset of the keys column, not a separate total.
- **Words vs. Tolkien** compares your **all-time** word total against fixed,
  widely-cited word counts for *The Hobbit*, the three *Lord of the Rings*
  volumes (and their aggregate), and *The Silmarillion*. For each it shows the
  book's total, the percentage you've typed, and the words you have left to go;
  a finished work turns green. It's a bit of fun, not a literary measurement —
  "words" here are typing-detected word boundaries, not published wordcount
  methodology.

### API reference (for building your own dashboard)

Both endpoints need `X-API-Key: <your key>`.

- `POST /sync` → `{ "ok": true, "rows": N }`
- `GET /totals` → `{ "global": {...}, "device_count": N, "devices": [...] }`

CORS is open, but a public browser dashboard would expose your key in client
code — keep the key private and query `/totals` from somewhere trusted (or add a
separate read-only endpoint if you want a public view).

---

## Limitations / notes

- **After upgrading, the rolling windows start fresh.** The minute tier can't be
  reconstructed from old data, so *Last hour* / *Last day* fill in as you use the
  app. *Last week / month / year / all-time* draw on the day tier, so your
  existing history counts immediately.
- **The cloud figures are all-time**, per device and combined. The Cloud panel
  shows each device's running total and a combined total row; the per-window
  breakdown (hour/day/week/...) stays a local, this-device view, since rolling
  windows across devices would need fine-grained synced data and server-side
  windowing (a deliberate scope choice — the Worker just keeps the totals).
- **Words are approximate.** A word is counted when a letter/digit run is
  followed by a separator, which matches normal typing well but won't reflect
  IME / CJK composition precisely.
- **Auto-sync runs every 15 minutes** whenever a Worker URL and key are set, and
  only then — with no cloud configured the app makes no network calls at all.
  Use the "Auto-sync to cloud every 15 min" checkbox to turn it off; "Push to
  Cloud" (push + pull) and "Refresh total" (pull only) remain for on-demand use.
- Built and tested against the structure of `pynput`/`psutil`/`PySide6` (the Qt
  GUI). The `build.bat` excludes the heavy Qt modules the app never uses to keep
  the single-file exe small; if PyInstaller misses something on your machine,
  adjust the `--exclude-module` / add `--hidden-import` flags there.
- Windows-oriented as written (Startup-folder shortcut via PowerShell,
  `os.startfile`). The counting core (`pynput`) and the Qt GUI/tray are
  cross-platform; only the autostart shortcut is Windows-specific.
