#!/usr/bin/env python3
"""
Tallyton - a small, privacy-respecting activity counter for Windows.

It COUNTS (it never records WHICH keys you press, only how many):
  - total keystrokes per day
  - words per day        (counted by detecting word boundaries; the actual
                          characters are inspected only to classify them and
                          are then discarded - never stored or transmitted)
  - deletions per day    (Backspace and Delete presses)
  - Alt+Tab switches per day
  - power cycles / restarts per day (detected from the OS boot time)

Data accumulates per day in a local JSON file and can be pushed to your own
Cloudflare D1 database with one click. No key content, clipboard text, or
window titles are ever stored or sent anywhere.
"""

import os
import sys
import json
import uuid
import queue
import socket
import time
import string
import threading
import subprocess
import datetime as dt
import urllib.request
import urllib.error

import psutil
from pynput import keyboard

import tkinter as tk
from tkinter import ttk, messagebox

# System-tray support is optional. If pystray/Pillow are missing the app still
# runs as a normal window (closing it then quits instead of hiding to tray).
try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False

# Internal/short name: used for filesystem paths (Startup shortcut) and
# non-visible code.
APP_NAME = "Tallyton"
# Full display name: shown in the window title, tray tooltip, and dialogs.
APP_DISPLAY_NAME = "Tallyton - There and Backspace Again"
FIELDS = ("keystrokes", "words", "deletions", "alt_tabs", "power_cycles")

# Word-count milestones: how your all-time word total compares to Tolkien's
# works. Counts are widely-cited approximate figures. The LotR aggregate is the
# sum of its three volumes. Order here is the display order; "aggregate" rows
# are flagged so the GUI can render them in bold.
BOOK_WORD_COUNTS = (
    ("The Hobbit", 95356, False),
    ("The Lord of the Rings (trilogy)", 455125, True),
    ("  The Fellowship of the Ring", 177227, False),
    ("  The Two Towers", 143436, False),
    ("  The Return of the King", 134462, False),
    ("The Silmarillion", 130115, False),
)


# --------------------------------------------------------------------------- #
# Paths  --  portable: data lives in the same folder as the executable, so the
# whole app can be moved, copied to a USB stick, or deleted as a single unit.
# --------------------------------------------------------------------------- #
def app_dir() -> str:
    """Folder the app and its data live in (next to the .exe).

    With --onefile PyInstaller, sys.executable is the real .exe path (not the
    temporary extraction directory), so it's the right anchor when frozen; in
    dev we fall back to the folder containing this script.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_writable(path: str) -> bool:
    """True if we can actually write in `path` (portable data lives here)."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".tallyton_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


DATA_PATH = os.path.join(app_dir(), "data.json")
CONFIG_PATH = os.path.join(app_dir(), "config.json")


def today_str() -> str:
    return dt.date.today().isoformat()


def now_minute() -> int:
    """Current time as an integer count of minutes since the Unix epoch.

    Using epoch-minutes makes the rolling windows (last hour / last day) exact
    and immune to timezone/DST quirks; calendar windows below use local dates.
    """
    return int(time.time() // 60)


def blank_day() -> dict:
    return {f: 0 for f in FIELDS}


# How long fine-grained per-minute buckets are kept. 26h comfortably covers the
# "last hour" and "last day" windows with a little margin; older minute buckets
# are pruned (the daily tier retains the long-term history).
MINUTE_RETENTION = 26 * 60  # minutes


# --------------------------------------------------------------------------- #
# Persistent store
#
# Two independent tiers, each just a dict of counters:
#   data["minutes"][epoch_minute] -> per-minute counts (recent, for hour/day)
#   data["days"][YYYY-MM-DD]      -> per-day counts    (kept forever)
# Every event bumps both, so each tier can be summed on its own without rollups.
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = self._load()
        if not self.data.get("device_id"):
            self.data["device_id"] = str(uuid.uuid4())
        self.data.setdefault("device_name", socket.gethostname())
        self.data.setdefault("days", {})
        self.data.setdefault("minutes", {})
        # Start date: first ever run. Migrating users (who only have day data)
        # inherit their earliest recorded day as the start date.
        if not self.data.get("started_at"):
            if self.data["days"]:
                self.data["started_at"] = min(self.data["days"]) + "T00:00:00"
            else:
                self.data["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self._dirty = True  # ensure identity/start get written on first save

    def _load(self) -> dict:
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self):
        with self.lock:
            if not self._dirty:
                return
            tmp = DATA_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, DATA_PATH)  # atomic on the same filesystem
            self._dirty = False

    def bump(self, field: str, n: int = 1, day: str = None):
        day = day or today_str()
        minute = str(now_minute())
        with self.lock:
            dbucket = self.data["days"].setdefault(day, blank_day())
            dbucket[field] = dbucket.get(field, 0) + n
            mbucket = self.data["minutes"].setdefault(minute, blank_day())
            mbucket[field] = mbucket.get(field, 0) + n
            self._dirty = True

    def day(self, day: str = None) -> dict:
        day = day or today_str()
        with self.lock:
            return dict(self.data["days"].get(day, blank_day()))

    def totals(self) -> dict:
        out = blank_day()
        with self.lock:
            for bucket in self.data["days"].values():
                for f in FIELDS:
                    out[f] += bucket.get(f, 0)
        return out

    def window_minutes(self, minutes: int) -> dict:
        """Sum the per-minute tier over the trailing `minutes` (rolling)."""
        cutoff = now_minute() - minutes
        out = blank_day()
        with self.lock:
            for key, bucket in self.data["minutes"].items():
                if int(key) > cutoff:
                    for f in FIELDS:
                        out[f] += bucket.get(f, 0)
        return out

    def window_days(self, days: int) -> dict:
        """Sum the per-day tier over the last `days` calendar days (incl. today)."""
        cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
        out = blank_day()
        with self.lock:
            for key, bucket in self.data["days"].items():
                if key >= cutoff:
                    for f in FIELDS:
                        out[f] += bucket.get(f, 0)
        return out

    def breakdown(self) -> dict:
        """All requested windows in one pass-friendly call."""
        return {
            "Last hour": self.window_minutes(60),
            "Last day": self.window_minutes(24 * 60),
            "Last week": self.window_days(7),
            "Last month": self.window_days(30),
            "Last year": self.window_days(365),
            "All-time": self.totals(),
        }

    def started_at(self) -> str:
        with self.lock:
            return self.data.get("started_at", "")

    def start_date(self) -> str:
        return self.started_at()[:10]

    def days_since(self) -> int:
        try:
            start = dt.date.fromisoformat(self.start_date())
        except Exception:
            return 0
        return max(0, (dt.date.today() - start).days)

    def prune(self):
        """Drop minute buckets older than the retention window."""
        cutoff = now_minute() - MINUTE_RETENTION
        with self.lock:
            old = [k for k in self.data["minutes"] if int(k) <= cutoff]
            for k in old:
                del self.data["minutes"][k]
            if old:
                self._dirty = True

    def days_payload(self) -> list:
        with self.lock:
            return [
                {"day": d, **{f: bucket.get(f, 0) for f in FIELDS}}
                for d, bucket in self.data["days"].items()
            ]

    def identity(self):
        with self.lock:
            return self.data["device_id"], self.data.get("device_name", "")

    def boot_check(self):
        """Increment power_cycles when the OS boot time differs from last run.

        This means relaunching the app inside one session does NOT count; only a
        genuine restart/shutdown-then-boot (a new boot time) does. The very first
        run records the current boot time without counting it as a cycle.
        """
        try:
            current = int(psutil.boot_time())
        except Exception:
            return
        with self.lock:
            last = self.data.get("last_boot_time")
            self.data["last_boot_time"] = current
            self._dirty = True
        if last is not None and current != last:
            self.bump("power_cycles", 1)


# --------------------------------------------------------------------------- #
# Config (cloud endpoint + API key)
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    @property
    def endpoint(self) -> str:
        return self.data.get("endpoint", "").rstrip("/")

    @property
    def api_key(self) -> str:
        return self.data.get("api_key", "")

    @property
    def autosync(self) -> bool:
        # Auto-sync to the cloud on a timer. Defaults on; only ever runs when an
        # endpoint + key are configured, so standalone users see no network use.
        return bool(self.data.get("autosync", True))


# --------------------------------------------------------------------------- #
# Counter (global keyboard listener) - counts only, never records content
# --------------------------------------------------------------------------- #
class Counter:
    def __init__(self, store: Store):
        self.store = store
        self.alt_down = False
        self.in_word = False
        self.listener = None

    def start(self):
        self.listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self.listener.daemon = True
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()

    @staticmethod
    def _is_alt(key) -> bool:
        return key in (
            keyboard.Key.alt,
            keyboard.Key.alt_l,
            keyboard.Key.alt_r,
            getattr(keyboard.Key, "alt_gr", None),
        )

    def _on_press(self, key):
        # Every physical key press counts as one keystroke. We look at the key
        # only to classify it (alt / tab / word char / delimiter) and then throw
        # that information away. The character itself is never kept.
        self.store.bump("keystrokes", 1)

        if self._is_alt(key):
            self.alt_down = True
            return

        # Backspace and Delete both count as a deletion. This is in addition to
        # the keystroke bumped above - deletions are a subset of total keys.
        if key in (keyboard.Key.backspace, keyboard.Key.delete):
            self.store.bump("deletions", 1)

        if key == keyboard.Key.tab and self.alt_down:
            self.store.bump("alt_tabs", 1)

        # Word-boundary detection: a "word" is counted when an alphanumeric run
        # is followed by a delimiter (space / enter / tab / punctuation).
        char = getattr(key, "char", None)
        if char and char.isalnum():
            self.in_word = True
        else:
            is_delim = key in (
                keyboard.Key.space,
                keyboard.Key.enter,
                keyboard.Key.tab,
            ) or (char is not None and not char.isalnum())
            if is_delim and self.in_word:
                self.store.bump("words", 1)
                self.in_word = False

    def _on_release(self, key):
        if self._is_alt(key):
            self.alt_down = False


# --------------------------------------------------------------------------- #
# Cloud sync (your own Cloudflare Worker + D1). API key goes in a header,
# never in the URL. HTTPS is provided by Workers.
#
# We set an explicit User-Agent. urllib's default ("Python-urllib/3.x") is
# rejected by Cloudflare's edge bot protection with a 403 before the request
# ever reaches the Worker, so any normal-looking UA string is required here.
# --------------------------------------------------------------------------- #
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def push_to_cloud(store: Store, cfg: Config, timeout: int = 15) -> dict:
    if not cfg.endpoint or not cfg.api_key:
        raise RuntimeError("Set the Worker URL and API key in the Cloud box first.")
    device_id, device_name = store.identity()
    payload = {
        "device_id": device_id,
        "device_name": device_name,
        "days": store.days_payload(),  # full history; upsert keeps it idempotent
    }
    req = urllib.request.Request(
        cfg.endpoint + "/sync",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": cfg.api_key,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_cloud_totals(cfg: Config, timeout: int = 15) -> dict:
    if not cfg.endpoint or not cfg.api_key:
        raise RuntimeError("Set the Worker URL and API key in the Cloud box first.")
    req = urllib.request.Request(
        cfg.endpoint + "/totals",
        method="GET",
        headers={"X-API-Key": cfg.api_key, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Autostart via a shortcut in the user's Startup folder (portable-friendly:
# no registry writes). The shortcut stores the exe's current path, so moving
# the app's folder breaks autostart -- re-enabling it repoints the shortcut.
# --------------------------------------------------------------------------- #
def _startup_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _shortcut_path() -> str:
    return os.path.join(_startup_dir(), APP_NAME + ".lnk")


def autostart_enabled() -> bool:
    return os.path.exists(_shortcut_path())


def set_autostart(enable: bool):
    lnk = _shortcut_path()
    if not enable:
        if os.path.exists(lnk):
            os.remove(lnk)
        return

    if getattr(sys, "frozen", False):
        target, arguments = sys.executable, "--minimized"
        workdir = os.path.dirname(sys.executable)
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        target = pyw if os.path.exists(pyw) else sys.executable
        arguments = f'"{os.path.abspath(__file__)}" --minimized'
        workdir = os.path.dirname(os.path.abspath(__file__))

    os.makedirs(_startup_dir(), exist_ok=True)
    esc = lambda s: s.replace("'", "''")  # PowerShell single-quote escaping
    ps = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{esc(lnk)}');"
        f"$s.TargetPath='{esc(target)}';"
        f"$s.Arguments='{esc(arguments)}';"
        f"$s.WorkingDirectory='{esc(workdir)}';"
        f"$s.Save()"
    )
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
SYNC_INTERVAL_MS = 15 * 60 * 1000  # auto-sync cadence: 15 minutes
INITIAL_SYNC_MS = 8 * 1000         # first auto-sync shortly after launch


class App:
    def __init__(self, store, counter, cfg, start_minimized=False,
                 first_run=False, writable=True):
        self.store = store
        self.counter = counter
        self.cfg = cfg
        self.cmd_q = queue.Queue()  # tray/worker threads -> Tk thread
        self.tray = None
        self.running = True
        self._sync_in_flight = False

        self.root = tk.Tk()
        self.root.title(APP_DISPLAY_NAME)
        self.root.geometry("560x1040")
        self.root.minsize(540, 760)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        if HAVE_TRAY:
            self._start_tray()
            if start_minimized:
                self.root.withdraw()

        self._tick()
        self._poll_commands()
        self._autosave()
        self.root.after(INITIAL_SYNC_MS, self._autosync)  # first sync, then every 15 min

        # Startup notices (after the window has had a moment to draw).
        if not writable:
            self.root.after(300, self._warn_not_writable)
        elif first_run:
            self.root.after(300, self._show_welcome)

    # -- layout ------------------------------------------------------------- #
    WINDOW_ROWS = ("Last hour", "Last day", "Last week", "Last month", "Last year", "All-time")

    def _build_ui(self):
        pad = {"padx": 10}

        # Start date + days-since header
        self.since_lbl = ttk.Label(self.root, text="", font=("Segoe UI", 10, "bold"))
        self.since_lbl.pack(anchor="w", pady=(8, 2), **pad)

        # This-device breakdown table: one row per time window, five metric columns.
        ttk.Label(self.root, text="This device", font=("Segoe UI", 11, "bold")).pack(anchor="w", **pad)
        cols = ("keystrokes", "words", "deletions", "alt_tabs", "power_cycles")
        headers = {"keystrokes": "Keys", "words": "Words", "deletions": "Del",
                   "alt_tabs": "Alt-Tab", "power_cycles": "Cycles"}
        blank_row = tuple("0" for _ in cols)
        tv = ttk.Treeview(self.root, columns=("window",) + cols, show="headings",
                          height=len(self.WINDOW_ROWS), selectmode="none")
        tv.heading("window", text="Window")
        tv.column("window", width=92, anchor="w", stretch=False)
        for c in cols:
            tv.heading(c, text=headers[c])
            tv.column(c, width=72, anchor="e", stretch=True)
        for name in self.WINDOW_ROWS:
            tv.insert("", "end", iid=name, values=(name,) + blank_row)
        tv.pack(fill="x", **pad)
        self.tree = tv

        ttk.Separator(self.root).pack(fill="x", pady=8, **pad)
        ttk.Label(self.root, text="Cloud \u2014 all devices", font=("Segoe UI", 11, "bold")).pack(anchor="w", **pad)

        # One row per device (this device's row is highlighted), plus a bold
        # combined "Cloud total" row. Repopulated on every sync.
        ctv = ttk.Treeview(self.root, columns=("device",) + cols, show="headings",
                           height=2, selectmode="none")
        ctv.heading("device", text="Device")
        ctv.column("device", width=150, anchor="w", stretch=True)
        for c in cols:
            ctv.heading(c, text=headers[c])
            ctv.column(c, width=64, anchor="e", stretch=True)
        ctv.tag_configure("total", font=("Segoe UI", 9, "bold"))
        ctv.tag_configure("self", foreground="#1a7f37")
        ctv.pack(fill="x", **pad)
        self.cloud_tree = ctv
        ctv.insert("", "end", values=("Not synced yet",) + tuple("" for _ in cols))

        self.sync_lbl = ttk.Label(self.root, text="Last cloud sync: never", anchor="w")
        self.sync_lbl.pack(fill="x", pady=(2, 0), **pad)

        # Cloud connection settings
        cf = ttk.Frame(self.root); cf.pack(fill="x", pady=(6, 0), **pad)
        ttk.Label(cf, text="Worker URL").grid(row=0, column=0, sticky="w")
        self.e_url = ttk.Entry(cf, width=40)
        self.e_url.grid(row=1, column=0, sticky="we")
        self.e_url.insert(0, self.cfg.data.get("endpoint", ""))
        ttk.Label(cf, text="API key").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.e_key = ttk.Entry(cf, width=40, show="\u2022")
        self.e_key.grid(row=3, column=0, sticky="we")
        self.e_key.insert(0, self.cfg.data.get("api_key", ""))
        ttk.Button(cf, text="Save", command=self.save_cfg).grid(row=4, column=0, sticky="w", pady=4)

        bf = ttk.Frame(self.root); bf.pack(fill="x", **pad)
        ttk.Button(bf, text="Push to Cloud", command=self.on_push).pack(side="left")
        ttk.Button(bf, text="Refresh total", command=self.on_refresh).pack(side="left", padx=6)

        self.autosync_var = tk.BooleanVar(value=self.cfg.autosync)
        ttk.Checkbutton(
            self.root, text="Auto-sync to cloud every 15 min",
            variable=self.autosync_var, command=self.toggle_autosync,
        ).pack(anchor="w", pady=(6, 0), **pad)

        # Install / data location. Autostart is what breaks if this folder moves,
        # so make the location visible and offer a one-click way to open it.
        ttk.Separator(self.root).pack(fill="x", pady=8, **pad)
        ttk.Label(self.root, text="This app and its data live here (delete the folder to uninstall):",
                  font=("Segoe UI", 9)).pack(anchor="w", **pad)
        self.path_lbl = ttk.Label(self.root, text=app_dir(), foreground="#555",
                                   wraplength=455, justify="left", font=("Segoe UI", 9))
        self.path_lbl.pack(anchor="w", **pad)

        row = ttk.Frame(self.root); row.pack(fill="x", pady=(4, 0), **pad)
        ttk.Button(row, text="Open folder", command=self.open_folder).pack(side="left")
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(row, text="Start at login", variable=self.autostart_var,
                        command=self.toggle_autostart).pack(side="left", padx=10)
        ttk.Label(self.root,
                  text="Moving this folder breaks auto-start \u2014 re-tick \u201cStart at login\u201d to fix it.",
                  foreground="#777", font=("Segoe UI", 8),
                  wraplength=455, justify="left").pack(anchor="w", **pad)

        # How your all-time words stack up against Tolkien's works.
        ttk.Separator(self.root).pack(fill="x", pady=8, **pad)
        ttk.Label(self.root, text="Words vs. Tolkien (all-time)",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", **pad)
        bcols = ("total", "pct", "left")
        bheaders = {"total": "Book total", "pct": "% written", "left": "Words to go"}
        btv = ttk.Treeview(self.root, columns=("book",) + bcols, show="headings",
                           height=len(BOOK_WORD_COUNTS), selectmode="none")
        btv.heading("book", text="Work")
        btv.column("book", width=210, anchor="w", stretch=True)
        for c in bcols:
            btv.heading(c, text=bheaders[c])
            btv.column(c, width=96, anchor="e", stretch=True)
        btv.tag_configure("aggregate", font=("Segoe UI", 9, "bold"))
        btv.tag_configure("done", foreground="#1a7f37")
        for title, count, is_agg in BOOK_WORD_COUNTS:
            btv.insert("", "end", iid=title,
                       tags=("aggregate",) if is_agg else (),
                       values=(title, f"{count:,}", "0.0%", f"{count:,}"))
        btv.pack(fill="x", **pad)
        self.books_tree = btv

        self.status = ttk.Label(self.root, text="Counting\u2026", relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    # -- helpers ------------------------------------------------------------ #
    def set_status(self, msg):
        self.status.config(text=msg)

    def open_folder(self):
        try:
            os.startfile(app_dir())  # Windows
        except Exception as e:
            self.set_status(f"Couldn't open folder: {e}")

    def _show_welcome(self):
        try:
            self.root.deiconify(); self.root.lift()
        except Exception:
            pass
        messagebox.showinfo(
            APP_DISPLAY_NAME,
            "This tool saves all data in the folder you've installed it in. "
            "To uninstall, just delete everything.\n\n"
            "This tool runs offline and standalone, but can tally totals across "
            "multiple devices leveraging a free Cloudflare account. See the README "
            "file for instructions.",
        )

    def _warn_not_writable(self):
        try:
            self.root.deiconify(); self.root.lift()
        except Exception:
            pass
        messagebox.showwarning(
            APP_DISPLAY_NAME,
            "Tallyton can't save data in its current folder:\n\n"
            f"{app_dir()}\n\n"
            "It needs to live somewhere writable \u2014 a normal folder, your Desktop, "
            "or a USB drive, but not Program Files or a read-only location. Move the "
            "whole folder somewhere writable and start it again.",
        )

    def save_cfg(self):
        self.cfg.data["endpoint"] = self.e_url.get().strip()
        self.cfg.data["api_key"] = self.e_key.get().strip()
        self.cfg.save()
        self.set_status("Settings saved.")

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
            self.set_status("Autostart " + ("enabled." if self.autostart_var.get() else "disabled."))
        except Exception as e:
            messagebox.showerror(APP_DISPLAY_NAME, f"Could not change autostart:\n{e}")
            self.autostart_var.set(autostart_enabled())

    def on_push(self):
        # Manual "Push to Cloud" = push this device's totals, then pull everyone's.
        self._sync_now("manual")

    def on_refresh(self):
        # Pull-only: refresh the cloud table without pushing.
        if not (self.cfg.endpoint and self.cfg.api_key):
            self.set_status("Set the Worker URL and API key first.")
            return
        self.set_status("Fetching cloud totals\u2026")

        def work():
            try:
                totals = fetch_cloud_totals(self.cfg)
                self.cmd_q.put(("cloud_totals", totals))
                self.cmd_q.put(("status", "Cloud totals updated."))
            except urllib.error.HTTPError as e:
                self.cmd_q.put(("status", f"Fetch failed: HTTP {e.code}"))
            except Exception as e:
                self.cmd_q.put(("status", f"Fetch failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _sync_now(self, reason="manual"):
        if not (self.cfg.endpoint and self.cfg.api_key):
            if reason == "manual":
                self.set_status("Set the Worker URL and API key first.")
            return
        if self._sync_in_flight:
            if reason == "manual":
                self.set_status("Sync already in progress\u2026")
            return
        self._sync_in_flight = True
        self.set_status("Syncing\u2026")

        def work():
            try:
                self.store.save()
                push_to_cloud(self.store, self.cfg)    # push this device's history
                totals = fetch_cloud_totals(self.cfg)  # pull all devices' totals
                self.cmd_q.put(("cloud_totals", totals))
                self.cmd_q.put(("synced", None))
            except urllib.error.HTTPError as e:
                self.cmd_q.put(("status", f"Sync failed: HTTP {e.code}"))
            except Exception as e:
                self.cmd_q.put(("status", f"Sync failed: {e}"))
            finally:
                self.cmd_q.put(("sync_done", None))

        threading.Thread(target=work, daemon=True).start()

    def _render_cloud(self, totals):
        g = totals.get("global", {}) or {}
        devices = totals.get("devices", []) or []
        my_id = self.store.identity()[0]
        tv = self.cloud_tree
        tv.delete(*tv.get_children())

        def fmt(v):
            try:
                return f"{int(v):,}"
            except Exception:
                return "0"

        for d in devices:
            name = d.get("device_name") or (d.get("device_id", "") or "")[:8]
            tags = ()
            if d.get("device_id") == my_id:
                name += "  (this device)"
                tags = ("self",)
            tv.insert("", "end", tags=tags, values=(
                name, fmt(d.get("keystrokes")), fmt(d.get("words")),
                fmt(d.get("deletions")), fmt(d.get("alt_tabs")),
                fmt(d.get("power_cycles")),
            ))

        count = totals.get("device_count", len(devices))
        tv.insert("", "end", tags=("total",), values=(
            f"Cloud total ({count} device{'s' if count != 1 else ''})",
            fmt(g.get("keystrokes")), fmt(g.get("words")),
            fmt(g.get("deletions")), fmt(g.get("alt_tabs")),
            fmt(g.get("power_cycles")),
        ))
        tv.configure(height=min(max(len(devices) + 1, 1), 6))

    def toggle_autosync(self):
        self.cfg.data["autosync"] = bool(self.autosync_var.get())
        self.cfg.save()
        if self.autosync_var.get():
            self.set_status("Auto-sync on (every 15 min).")
            self._sync_now("manual")  # sync immediately when switched on
        else:
            self.set_status("Auto-sync off.")

    # -- loops -------------------------------------------------------------- #
    def _poll_commands(self):
        try:
            while True:
                kind, val = self.cmd_q.get_nowait()
                if kind == "status":
                    self.set_status(val)
                elif kind == "cloud_totals":
                    self._render_cloud(val)
                elif kind == "synced":
                    now = dt.datetime.now().strftime("%H:%M:%S")
                    self.sync_lbl.config(text=f"Last cloud sync: {now}")
                    self.set_status("Synced.")
                elif kind == "sync_done":
                    self._sync_in_flight = False
                elif kind == "push":
                    self.on_push()
                elif kind == "show":
                    self.show_window()
                elif kind == "quit":
                    self.quit_app()
        except queue.Empty:
            pass
        if self.running:
            self.root.after(150, self._poll_commands)

    def _tick(self):
        n = self.store.days_since()
        self.since_lbl.config(
            text=f"Tracking since {self.store.start_date()}  \u00b7  {n} day{'s' if n != 1 else ''}"
        )
        bd = self.store.breakdown()
        for name in self.WINDOW_ROWS:
            b = bd[name]
            self.tree.item(name, values=(
                name,
                f"{b['keystrokes']:,}",
                f"{b['words']:,}",
                f"{b['deletions']:,}",
                f"{b['alt_tabs']:,}",
                f"{b['power_cycles']:,}",
            ))
        self._update_books()
        if self.running:
            self.root.after(1000, self._tick)

    def _update_books(self):
        # Compare all-time words typed against each Tolkien milestone.
        typed = self.store.totals().get("words", 0)
        for title, count, is_agg in BOOK_WORD_COUNTS:
            pct = (typed / count * 100) if count else 0.0
            left = max(0, count - typed)
            tags = ("aggregate",) if is_agg else ()
            if typed >= count:
                tags += ("done",)
            self.books_tree.item(title, tags=tags, values=(
                title, f"{count:,}", f"{pct:.1f}%", f"{left:,}",
            ))

    def _autosave(self):
        self.store.prune()
        self.store.save()
        if self.running:
            self.root.after(5000, self._autosave)

    def _autosync(self):
        # Push this device + pull all devices, every 15 min, when configured.
        if self.cfg.autosync:
            self._sync_now("auto")
        if self.running:
            self.root.after(SYNC_INTERVAL_MS, self._autosync)

    # -- tray --------------------------------------------------------------- #
    def _make_icon_image(self):
        img = Image.new("RGB", (64, 64), (28, 28, 30))
        d = ImageDraw.Draw(img)
        d.rectangle([10, 18, 54, 46], outline=(120, 200, 255), width=3)
        d.text((24, 26), "T", fill=(120, 200, 255))
        return img

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda: self.cmd_q.put(("show", None)), default=True),
            pystray.MenuItem("Push to Cloud", lambda: self.cmd_q.put(("push", None))),
            pystray.MenuItem("Quit", lambda: self.cmd_q.put(("quit", None))),
        )
        self.tray = pystray.Icon(APP_NAME, self._make_icon_image(), APP_DISPLAY_NAME, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # -- window state ------------------------------------------------------- #
    def hide_window(self):
        if HAVE_TRAY:
            self.root.withdraw()
            self.set_status("Running in the tray.")
        else:
            self.quit_app()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        self.running = False
        try:
            self.counter.stop()
        except Exception:
            pass
        try:
            self.store.save()
        except Exception:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.after(50, self.root.destroy)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------- #
def main():
    start_minimized = "--minimized" in sys.argv

    writable = ensure_writable(app_dir())
    # Genuine first run = no local data/config sitting next to the executable.
    first_run = not any(
        os.path.exists(os.path.join(app_dir(), f)) for f in ("data.json", "config.json")
    )

    store = Store()
    store.boot_check()
    store.save()

    counter = Counter(store)
    counter.start()

    cfg = Config()
    App(store, counter, cfg, start_minimized=start_minimized,
        first_run=first_run, writable=writable).run()


if __name__ == "__main__":
    main()
