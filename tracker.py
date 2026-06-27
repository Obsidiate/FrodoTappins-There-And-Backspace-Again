#!/usr/bin/env python3
"""
FrodoTappins - a small, privacy-respecting activity counter for Windows.

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
import socket
import time
import threading
import subprocess
import tempfile
import datetime as dt
import urllib.request
import urllib.error

import math

import psutil
from pynput import keyboard

# The GUI is Qt (PySide6): a warm, near-dark "hobbity" wood theme with an
# animated, self-generating vine border that grows into / retracts from the
# window as it is resized. The counting/storage/sync backend below is pure
# Python and UI-agnostic.
from PySide6.QtCore import Qt, QTimer, QPointF, QEvent, Signal, QObject
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QBrush, QFont, QIcon, QPixmap,
    QImage, QRadialGradient, QLinearGradient, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QCheckBox, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QScrollArea, QMessageBox,
    QSystemTrayIcon, QMenu, QAbstractItemView, QToolButton, QSizePolicy,
)

# System-tray support is built into Qt; pystray/Pillow are no longer needed.
HAVE_TRAY = True

# Internal/short name: used for filesystem paths (Startup shortcut) and
# non-visible code.
APP_NAME = "FrodoTappins"
# Full display name: shown in the window title, tray tooltip, and dialogs.
APP_DISPLAY_NAME = "FrodoTappins - There and Backspace Again"
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
        probe = os.path.join(path, ".frodotappins_write_test")
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

    @property
    def cloud_expanded(self) -> bool:
        # Whether the collapsible Cloud section starts expanded. Remembered
        # across launches. Defaults expanded.
        return bool(self.data.get("cloud_expanded", True))


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
# GUI  --  PySide6 (Qt). A warm, near-dark "hobbity" wood theme with a living,
# self-generating vine border that animates (grows / retracts) as the window is
# resized. Tables are rendered as aged-parchment "pages" set into the dark wood.
# --------------------------------------------------------------------------- #
SYNC_INTERVAL_MS = 15 * 60 * 1000  # auto-sync cadence: 15 minutes
INITIAL_SYNC_MS = 8 * 1000         # first auto-sync shortly after launch

# -- palette: warm, near-dark, woody ---------------------------------------- #
WOOD_DEEP   = "#241710"   # window background, deepest wood
WOOD_DARK   = "#2e1f14"   # panel wood
WOOD_MID    = "#3b2918"   # raised wood (headers, buttons)
WOOD_LIGHT  = "#4a3420"   # button hover / borders
AMBER       = "#e0b25a"   # warm headings / accents
AMBER_DIM   = "#b8893f"
PARCHMENT   = "#efe2c4"   # aged-paper table background
PARCHMENT_2 = "#e6d6b3"   # parchment alternate row
INK         = "#3a2a17"   # dark-brown "ink" text on parchment
INK_SOFT    = "#6a5536"
DONE_GREEN  = "#3f6b2f"   # "you've passed this milestone" green
SELF_GREEN  = "#2f5d27"

# vine / wreath colours (shared with the border widget)
STEM_DARK   = QColor("#241608")   # woody rope shadow
STEM_MID    = QColor("#46331e")   # rope body
STEM_LIGHT  = QColor("#5e4628")   # lit fibres
IVY_GREENS  = [QColor("#4f6a39"), QColor("#5e7d44"), QColor("#6f8c4a"),
               QColor("#43562f")]
MOTE_CORE   = QColor(255, 226, 150)   # warm gold mote
MOTE_GLOW   = QColor(255, 196, 90)
MOTE_CORE_B = QColor(190, 224, 255)   # faint blue mote
MOTE_GLOW_B = QColor(120, 175, 255)
FRAME_WOOD  = QColor("#5a3f27")   # title-bar underline / wood accents


def _lerp(a, b, t):
    return a + (b - a) * t


def _noise(seed, x):
    v = math.sin((seed * 374761 + x * 668265263) * 1e-7) * 43758.5453
    return v - math.floor(v)


# Tones for the burl texture: a warm mid-brown base with brighter grain so the
# wood reads clearly as wood (not near-black) while parchment pages and amber
# text stay legible on top.
WOOD_BASE_TEX = QColor("#5a4026")   # warm mid-brown base
WOOD_GRAIN_LIGHT = QColor("#8a6438")
WOOD_GRAIN_DARK = QColor("#3a2614")


def make_burl(w, h, seed=7):
    """A procedural burl / knotty wood texture, drawn once per size and cached.

    Warped concentric grain rings flow around a few knot centres on a warm
    mid-brown base; soft knot cores and a light vignette add depth without
    going murky. No image assets -- generated entirely with QPainter so the
    app stays a single portable file.
    """
    img = QImage(max(w, 1), max(h, 1), QImage.Format_RGB32)
    qp = QPainter(img)
    qp.setRenderHint(QPainter.Antialiasing, True)
    qp.fillRect(0, 0, w, h, WOOD_BASE_TEX)

    knots = []
    for k in range(4):
        kx = _noise(seed, k * 3 + 1) * w
        ky = _noise(seed, k * 3 + 2) * h
        knots.append((kx, ky, 0.6 + _noise(seed, k * 3 + 3) * 0.8))

    qp.setBrush(Qt.NoBrush)
    for (kx, ky, scale) in knots:
        max_r = max(w, h) * (0.5 + scale * 0.4)
        ring = 0
        r = 5.0
        while r < max_r:
            ring += 1
            t = 0.5 + 0.5 * math.sin(ring * 0.9)
            c = QColor(
                int(_lerp(WOOD_GRAIN_DARK.red(),   WOOD_GRAIN_LIGHT.red(),   t)),
                int(_lerp(WOOD_GRAIN_DARK.green(), WOOD_GRAIN_LIGHT.green(), t)),
                int(_lerp(WOOD_GRAIN_DARK.blue(),  WOOD_GRAIN_LIGHT.blue(),  t)),
            )
            c.setAlpha(60)
            qp.setPen(QPen(c, 1.6))
            pts = []
            steps = 60
            for s in range(steps + 1):
                ang = (s / steps) * math.tau
                wob = (1.0 + 0.18 * math.sin(ang * 3 + ring * 0.5 + kx * 0.01)
                           + 0.10 * math.sin(ang * 7 - ky * 0.01))
                rr = r * wob
                pts.append(QPointF(kx + math.cos(ang) * rr,
                                   ky + math.sin(ang) * rr * 0.82))
            qp.drawPolyline(pts)
            r += 3.0 + _noise(seed, ring) * 3.5

    for (kx, ky, scale) in knots:
        rad = 9 + scale * 10
        g = QRadialGradient(kx, ky, rad)
        core = QColor(58, 38, 20); core.setAlpha(150)
        g.setColorAt(0.0, core)
        edge = QColor(58, 38, 20); edge.setAlpha(0)
        g.setColorAt(1.0, edge)
        qp.setBrush(QBrush(g)); qp.setPen(Qt.NoPen)
        qp.drawEllipse(QPointF(kx, ky), rad, rad * 0.85)

    g = QRadialGradient(w / 2, h / 2, max(w, h) * 0.8)
    g.setColorAt(0.0, QColor(0, 0, 0, 0))
    g.setColorAt(1.0, QColor(0, 0, 0, 38))
    qp.setBrush(QBrush(g)); qp.setPen(Qt.NoPen)
    qp.drawRect(0, 0, w, h)
    qp.end()
    return img


# --------------------------------------------------------------------------- #
# The living vine border: a woven wreath of gnarled, intertwining vines that
# follow the window perimeter (constrained to a band), with sparse ivy leaves
# and golden + faint-blue "magic motes" drifting along the vines. The outer
# band is darkened (a vignette) so the vines read as the window's border,
# emerging from shadow rather than sitting on a flat box.
#
# Performance: the vines + vignette are STATIC once grown, so they are rendered
# once into a cached pixmap; each animation frame just blits that pixmap and
# draws the handful of moving motes. The timer also pauses whenever the widget
# is hidden (window minimised / hidden to tray), so it costs nothing in the
# background. Resizing rebuilds geometry but carries growth over, so the wreath
# grows into / retracts from the new shape rather than snapping.
# --------------------------------------------------------------------------- #
def _wreath_noise(seed, n):
    x = math.sin((seed * 928371 + n * 2654435761) * 0.0001) * 43758.5453
    return x - math.floor(x)


def _perimeter_path(w, h, inset, radius, samples=240):
    """Sample points evenly around a rounded rectangle centred in the band,
    `inset` in from the window edges. Returns [(QPointF, cumulative_dist)]."""
    x0, y0, x1, y1 = inset, inset, w - inset, h - inset
    r = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    if x1 <= x0 or y1 <= y0:
        return [(QPointF(inset, inset), 0.0)], 1.0
    path = QPainterPath()
    path.moveTo(x0 + r, y0)
    path.lineTo(x1 - r, y0); path.arcTo(x1 - 2*r, y0, 2*r, 2*r, 90, -90)
    path.lineTo(x1, y1 - r); path.arcTo(x1 - 2*r, y1 - 2*r, 2*r, 2*r, 0, -90)
    path.lineTo(x0 + r, y1); path.arcTo(x0, y1 - 2*r, 2*r, 2*r, -90, -90)
    path.lineTo(x0, y0 + r); path.arcTo(x0, y0, 2*r, 2*r, 180, -90)
    path.closeSubpath()
    total = path.length() or 1.0
    pts = []
    for i in range(samples + 1):
        pt = path.pointAtPercent(i / samples)
        pts.append((QPointF(pt.x(), pt.y()), (i / samples) * total))
    return pts, total


class _RingVine:
    """One vine following the perimeter path, offset across the band by layered
    sines so it weaves in/out and crosses siblings. `gnarl` draws it as a thick
    knotty rope (variable width + twisting fibres); otherwise a thin wiry vine."""

    def __init__(self, base_pts, band, seed, start_frac=0.0, coverage=1.0,
                 width=6.0, amp=0.7, leafy=1.0, gnarl=False):
        self.seed = seed
        self.band = band
        self.width = width
        self.amp = amp
        self.leafy = leafy
        self.gnarl = gnarl
        self.grown = 0.0
        self.target = 0.0
        self.poly = []     # (QPointF, dist_along_vine)
        self.leaves = []   # (QPointF, dist, size, color, angle)
        self.path = []     # centreline (== poly) for motes
        self.total_len = 1.0
        self._build(base_pts, start_frac, coverage)

    @staticmethod
    def _normal(base_pts, i):
        n = len(base_pts)
        p0 = base_pts[(i - 1) % n][0]; p1 = base_pts[(i + 1) % n][0]
        tx, ty = p1.x() - p0.x(), p1.y() - p0.y()
        m = math.hypot(tx, ty) or 1.0
        return -ty / m, tx / m

    def _build(self, base_pts, start_frac, coverage):
        n = len(base_pts)
        if n < 2:
            return
        count = max(8, int(coverage * n))
        start_i = int(start_frac * n)
        # window centre, to orient the weave so it leans OUTWARD (toward the
        # edge) and rarely crosses inward over the content.
        cx = sum(p.x() for p, _d in base_pts) / n
        cy = sum(p.y() for p, _d in base_pts) / n
        d_acc = 0.0
        prev = None
        for k in range(count + 1):
            i = (start_i + k) % n
            bp = base_pts[i][0]
            nx, ny = self._normal(base_pts, i)
            # ensure (nx, ny) points outward (away from centre)
            if (nx * (bp.x() - cx) + ny * (bp.y() - cy)) < 0:
                nx, ny = -nx, -ny
            phase = self.seed * 1.7
            s = (math.sin(k * 0.12 + phase) * 0.6
                 + math.sin(k * 0.31 + phase * 1.3) * 0.3
                 + (_wreath_noise(self.seed, k) - 0.5) * 0.25)
            # bias outward: shift the weave toward the edge so the inner extent
            # stays clear of content (range roughly [-0.25, +1.0] * half-band).
            off = (s * 0.62 + 0.38) * self.amp * (self.band * 0.5)
            p = QPointF(bp.x() + nx * off, bp.y() + ny * off)
            if prev is not None:
                d_acc += math.hypot(p.x() - prev.x(), p.y() - prev.y())
            self.poly.append((p, d_acc))
            if _wreath_noise(self.seed, k * 3 + 1) < 0.22 * self.leafy:
                side = 1 if (k % 2 == 0) else -1
                ang = math.atan2(ny, nx) + side * (0.3 + _wreath_noise(self.seed, k) * 0.6)
                size = _lerp(6.0, 11.0, _wreath_noise(self.seed, k * 3 + 2))
                col = IVY_GREENS[int(_wreath_noise(self.seed, k * 3 + 3) * len(IVY_GREENS)) % len(IVY_GREENS)]
                self.leaves.append((p, d_acc, size, col, ang))
            prev = p
        self.total_len = max(d_acc, 1.0)
        self.path = self.poly

    def set_target(self, t):
        self.target = max(0.0, min(1.0, t))

    def step(self, dt):
        self.grown += (self.target - self.grown) * min(1.0, dt * 2.2)
        if abs(self.target - self.grown) < 0.001:
            self.grown = self.target

    def draw(self, qp):
        reveal = self.grown * self.total_len
        revealed = [(p, d) for (p, d) in self.poly if d <= reveal]
        if len(revealed) >= 2:
            if self.gnarl:
                self._draw_gnarled(qp, revealed)
            else:
                self._draw_simple(qp, [p for (p, d) in revealed])
        for (pos, dist, size, col, a) in self.leaves:
            if dist > reveal:
                continue
            gi = min(1.0, (reveal - dist) / 24.0)
            self._ivy(qp, pos, size * gi, col, a)

    def _draw_simple(self, qp, pts):
        path = QPainterPath(); path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        qp.setPen(QPen(STEM_DARK, self.width + 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.drawPath(path)
        qp.setPen(QPen(STEM_MID, self.width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.drawPath(path)
        qp.setPen(QPen(STEM_LIGHT, max(1.0, self.width * 0.34), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.drawPath(path)

    def _draw_gnarled(self, qp, revealed):
        n = len(revealed)
        if n < 2:
            return
        info = []
        for i in range(n):
            p, d = revealed[i]
            p0 = revealed[max(0, i - 1)][0]; p1 = revealed[min(n - 1, i + 1)][0]
            tx, ty = p1.x() - p0.x(), p1.y() - p0.y()
            m = math.hypot(tx, ty) or 1.0
            nx, ny = -ty / m, tx / m
            knot = (0.78 + 0.34 * math.sin(d * 0.05 + self.seed)
                         + 0.16 * math.sin(d * 0.13 + self.seed * 2))
            hw = (self.width * 0.5) * max(0.5, knot)
            info.append((p, nx, ny, hw))
        outline = QPainterPath()
        outline.moveTo(QPointF(info[0][0].x() + info[0][1] * info[0][3],
                               info[0][0].y() + info[0][2] * info[0][3]))
        for (p, nx, ny, hw) in info[1:]:
            outline.lineTo(QPointF(p.x() + nx * hw, p.y() + ny * hw))
        for (p, nx, ny, hw) in reversed(info):
            outline.lineTo(QPointF(p.x() - nx * hw, p.y() - ny * hw))
        outline.closeSubpath()
        qp.setPen(QPen(STEM_DARK, 2.0)); qp.setBrush(QBrush(STEM_DARK))
        qp.drawPath(outline)
        body = QPainterPath(); body.moveTo(info[0][0])
        for (p, nx, ny, hw) in info[1:]:
            body.lineTo(p)
        qp.setPen(QPen(STEM_MID, self.width * 0.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.setBrush(Qt.NoBrush); qp.drawPath(body)
        for phase in (0.0, math.pi):
            sp = QPainterPath()
            for i, (p, nx, ny, hw) in enumerate(info):
                off = math.sin(i * 0.6 + phase) * hw * 0.5
                q = QPointF(p.x() + nx * off, p.y() + ny * off)
                if i == 0:
                    sp.moveTo(q)
                else:
                    sp.lineTo(q)
            qp.setPen(QPen(STEM_LIGHT, max(1.0, self.width * 0.16),
                           Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            qp.drawPath(sp)

    def _ivy(self, qp, pos, size, col, a):
        if size < 1.0:
            return
        qp.save(); qp.translate(pos); qp.rotate(math.degrees(a))
        s = size
        path = QPainterPath(); path.moveTo(0, 0)
        path.cubicTo(0.5*s, -0.2*s, 0.9*s, -0.5*s, 0.7*s, -0.9*s)
        path.cubicTo(1.1*s, -0.7*s, 1.5*s, -0.6*s, 1.6*s, 0.0)
        path.cubicTo(1.5*s, 0.6*s, 1.1*s, 0.7*s, 0.7*s, 0.9*s)
        path.cubicTo(0.9*s, 0.5*s, 0.5*s, 0.2*s, 0, 0)
        qp.setBrush(QBrush(col)); qp.setPen(QPen(col.darker(150), 1.0))
        qp.drawPath(path)
        qp.setPen(QPen(col.darker(130), 0.9)); qp.drawLine(QPointF(0, 0), QPointF(1.3*s, 0))
        qp.restore()


class _Mote:
    """A magic spark loafing among the vines. Rather than circulating uniformly,
    each mote wanders: it eases slowly along its vine (forward OR backward, at
    its own gentle speed) while also bobbing in a small free-floating orbit, and
    some barely travel at all (mostly hovering). The mix reads as ambient
    drifting fireflies, not a conveyor belt."""

    def __init__(self, vine, anchor, seed, blue=False):
        self.vine = vine
        self.seed = seed
        self.phase = (seed % 100) / 100.0
        self.core = MOTE_CORE_B if blue else MOTE_CORE
        self.glow = MOTE_GLOW_B if blue else MOTE_GLOW
        # base position along the vine (0..1 of revealed length) and how far it
        # drifts from there. ~1/3 are near-stationary "hoverers".
        self.anchor = anchor
        r = _wreath_noise(seed, 11)
        if r < 0.34:
            self.span = _lerp(0.0, 0.015, _wreath_noise(seed, 12))   # hover
            self.drift_spd = _lerp(0.02, 0.06, _wreath_noise(seed, 13))
        else:
            self.span = _lerp(0.04, 0.13, _wreath_noise(seed, 12))   # wander
            self.drift_spd = _lerp(0.05, 0.16, _wreath_noise(seed, 13))
        # direction of the slow along-path sway (some forward, some back)
        self.dir = 1.0 if _wreath_noise(seed, 14) < 0.5 else -1.0
        # free-floating bob: small elliptical orbit, own frequencies
        self.bob = _lerp(2.0, 6.0, _wreath_noise(seed, 15))          # px
        self.fx = _lerp(0.25, 0.6, _wreath_noise(seed, 16))          # Hz-ish
        self.fy = _lerp(0.20, 0.5, _wreath_noise(seed, 17))

    def _at(self, frac):
        if len(self.vine.path) < 2:
            return None
        total = self.vine.total_len
        grown = self.vine.grown
        if grown <= 0.001:
            return None
        d = max(0.0, min(frac, 1.0)) * grown * total
        for (p, dist) in self.vine.path:
            if dist >= d:
                return p
        return self.vine.path[-1][0]

    def pos_at(self, clock):
        # eased along-path sway around the anchor + a small floating bob
        sway = math.sin(clock * self.drift_spd * math.tau + self.phase * 6.28)
        frac = self.anchor + self.dir * self.span * sway
        base = self._at(frac)
        if base is None:
            return None
        bx = math.sin(clock * self.fx * math.tau + self.phase * 5.0) * self.bob
        by = math.cos(clock * self.fy * math.tau + self.phase * 7.0) * self.bob
        return QPointF(base.x() + bx, base.y() + by)

    def trail_at(self, clock, n=4, gap=0.05):
        # short trail = recent positions (a touch in the past)
        out = []
        for i in range(1, n + 1):
            p = self.pos_at(clock - i * gap)
            if p is not None:
                out.append(p)
        return out


class VineBorder(QWidget):
    """Transparent, click-through overlay drawn on top of the content: the woven
    vine wreath + drifting motes. See the module comment above for the perf
    model (cached static layer, paused when hidden)."""

    BAND = 95

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.vines = []
        self.motes = []
        self._clock = 0.0
        self._static = None          # cached pixmap of vignette + vines
        self._static_dirty = True
        self.DT = 1 / 60.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)        # ~60 fps

    # The host window calls pause()/resume() on minimise / hide-to-tray, since a
    # child widget doesn't reliably get hideEvent on window-minimise.
    def pause(self):
        if self._timer.isActive():
            self._timer.stop()

    def resume(self):
        if not self._timer.isActive():
            self._timer.start(16)

    def hideEvent(self, e):
        self.pause()
        super().hideEvent(e)

    def showEvent(self, e):
        self.resume()
        super().showEvent(e)

    def _tick(self):
        self._clock += self.DT
        growing = False
        for v in self.vines:
            before = v.grown
            v.step(self.DT)
            if abs(v.grown - before) > 1e-5:
                growing = True
        if growing:
            self._static_dirty = True
        # motes derive their position from self._clock directly (ambient wander),
        # so there's no per-mote state to advance here.
        self.update()

    def _rebuild(self):
        w, h = self.width(), self.height()
        self.vines = []
        self.motes = []
        band = self.BAND
        inset = band * 0.42        # ring sits in the OUTER part of the band
        base_pts, _total = _perimeter_path(w, h, inset, 46)
        seed = 1
        # 2 bold gnarled ropes as backbone, thinner wiry vines woven around them
        loop_specs = [
            dict(width=13.0, amp=0.62, leafy=0.5, gnarl=True),
            dict(width=11.0, amp=0.85, leafy=0.6, gnarl=True),
            dict(width=5.0,  amp=1.0,  leafy=1.0),
            dict(width=4.0,  amp=1.1,  leafy=1.1),
            dict(width=5.5,  amp=0.55, leafy=0.7),
        ]
        for spec in loop_specs:
            v = _RingVine(base_pts, band, seed, start_frac=_wreath_noise(seed, 1),
                          coverage=1.0, **spec)
            v.set_target(1.0); self.vines.append(v); seed += 1
        # partial wander vines for extra gnarl
        for _k in range(4):
            v = _RingVine(base_pts, band, seed, start_frac=_wreath_noise(seed, 2),
                          coverage=_lerp(0.25, 0.5, _wreath_noise(seed, 3)),
                          width=4.0, amp=1.2, leafy=1.2)
            v.set_target(1.0); self.vines.append(v); seed += 1
        # Motes scattered among the vines at varied anchor points; ~1 in 3 a
        # faint blue one. Each wanders independently (see _Mote), so the field
        # feels ambient rather than circulating.
        for v in self.vines[:6]:
            for k in range(2):
                ms = seed * 7 + k
                blue = (_wreath_noise(ms, 5) < 0.34)
                anchor = _wreath_noise(ms, 9)        # spread along the vine
                self.motes.append(_Mote(v, anchor, ms, blue=blue))
                seed += 1
        self._static_dirty = True

    def resizeEvent(self, e):
        prev = [v.grown for v in self.vines]
        self._rebuild()
        for v, g in zip(self.vines, prev):
            v.grown = g
        self._static = None
        self._static_dirty = True
        super().resizeEvent(e)

    def _vignette(self, qp):
        w, h = self.width(), self.height()
        band = int(self.BAND * 1.15)
        dark = QColor(0, 0, 0, 165)
        clear = QColor(0, 0, 0, 0)
        g = QLinearGradient(0, 0, 0, band); g.setColorAt(0, dark); g.setColorAt(1, clear)
        qp.fillRect(0, 0, w, band, QBrush(g))
        g = QLinearGradient(0, h, 0, h - band); g.setColorAt(0, dark); g.setColorAt(1, clear)
        qp.fillRect(0, h - band, w, band, QBrush(g))
        g = QLinearGradient(0, 0, band, 0); g.setColorAt(0, dark); g.setColorAt(1, clear)
        qp.fillRect(0, 0, band, h, QBrush(g))
        g = QLinearGradient(w, 0, w - band, 0); g.setColorAt(0, dark); g.setColorAt(1, clear)
        qp.fillRect(w - band, 0, band, h, QBrush(g))

    def _rebuild_static(self):
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h); pm.fill(Qt.transparent)
        qp = QPainter(pm); qp.setRenderHint(QPainter.Antialiasing, True)
        self._vignette(qp)
        for v in self.vines:
            v.draw(qp)
        qp.end()
        self._static = pm
        self._static_dirty = False

    def paintEvent(self, e):
        if self._static is None or self._static_dirty:
            self._rebuild_static()
        qp = QPainter(self)
        qp.drawPixmap(0, 0, self._static)
        qp.setRenderHint(QPainter.Antialiasing, True)
        for m in self.motes:
            p = m.pos_at(self._clock)
            if p is None:
                continue
            # slow, gentle twinkle (each mote on its own phase)
            tw = 0.5 + 0.5 * math.sin(self._clock * 1.3 + m.phase * 6.28)
            self._mote(qp, p, tw, m.trail_at(self._clock), m.core, m.glow)
        qp.end()

    def _mote(self, qp, p, tw, trail, core_col, glow_col):
        for i, tp in enumerate(trail):
            f = (i + 1) / (len(trail) + 1)
            tr = (2.0 + 4.0 * tw) * f
            gc = QColor(glow_col); gc.setAlpha(int(60 * tw * f))
            qp.setPen(Qt.NoPen); qp.setBrush(QBrush(gc)); qp.drawEllipse(tp, tr, tr)
        R = 12 * tw + 4
        g = QRadialGradient(p, R)
        gc = QColor(glow_col); gc.setAlpha(int(160 * tw)); g.setColorAt(0.0, gc)
        ge = QColor(glow_col); ge.setAlpha(0); g.setColorAt(1.0, ge)
        qp.setPen(Qt.NoPen); qp.setBrush(QBrush(g)); qp.drawEllipse(p, R, R)
        core = QColor(core_col); core.setAlpha(255)
        qp.setBrush(QBrush(core)); qp.drawEllipse(p, 2.4 * tw + 0.8, 2.4 * tw + 0.8)


# --------------------------------------------------------------------------- #
# The wood panel that paints the burl texture as the window background. The
# texture is regenerated (and cached) only when the size actually changes, so
# resizing stays cheap.
# --------------------------------------------------------------------------- #
class WoodPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tex = None
        self._tex_size = None

    def _ensure_tex(self):
        size = (self.width(), self.height())
        if size != self._tex_size and size[0] > 0 and size[1] > 0:
            self._tex = make_burl(size[0], size[1])
            self._tex_size = size

    def paintEvent(self, e):
        self._ensure_tex()
        if self._tex is not None:
            QPainter(self).drawImage(0, 0, self._tex)


# --------------------------------------------------------------------------- #
# A collapsible section: a clickable wood header with a disclosure triangle and
# a body widget that hides/shows. Used to fold the Cloud block away.
# --------------------------------------------------------------------------- #
class CollapsibleSection(QWidget):
    def __init__(self, title, expanded=True, parent=None):
        super().__init__(parent)
        self._expanded = expanded
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(6)

        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setText(title)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.clicked.connect(self._toggle)
        v.addWidget(self.header)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        self.body.setVisible(expanded)
        v.addWidget(self.body)

    def addWidget(self, w):
        self.body_layout.addWidget(w)

    def addLayout(self, l):
        self.body_layout.addLayout(l)

    def _toggle(self):
        self._expanded = self.header.isChecked()
        self.header.setArrowType(Qt.DownArrow if self._expanded else Qt.RightArrow)
        self.body.setVisible(self._expanded)


# --------------------------------------------------------------------------- #
# Custom wood title bar. The window is frameless (no grey OS chrome), so this
# bar carries the icon + title, the minimise / close buttons, and window-drag.
# --------------------------------------------------------------------------- #
class TitleBar(QWidget):
    def __init__(self, win, title):
        super().__init__(win)
        self._win = win
        self._drag = None
        self.setObjectName("titlebar")
        self.setFixedHeight(34)
        h = QHBoxLayout(self); h.setContentsMargins(10, 0, 6, 0); h.setSpacing(8)

        self.icon = QLabel(); self.icon.setObjectName("titleicon")
        self.icon.setPixmap(win._app_icon().pixmap(18, 18))
        h.addWidget(self.icon)

        self.label = QLabel(title); self.label.setObjectName("titletext")
        h.addWidget(self.label)
        h.addStretch(1)

        self.btn_min = QToolButton(); self.btn_min.setObjectName("winbtn")
        self.btn_min.setText("–"); self.btn_min.setToolTip("Minimise")
        self.btn_min.clicked.connect(win.showMinimized)
        h.addWidget(self.btn_min)

        self.btn_close = QToolButton(); self.btn_close.setObjectName("winbtnclose")
        self.btn_close.setText("✕"); self.btn_close.setToolTip("Close to tray")
        self.btn_close.clicked.connect(win.close)
        h.addWidget(self.btn_close)

    # drag the (frameless) window by its title bar
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._win.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, e):
        # darker wood strip with a 1px amber-ish underline
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor(WOOD_DEEP))
        qp.setPen(QPen(FRAME_WOOD, 1))
        qp.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        qp.end()


# --------------------------------------------------------------------------- #
# A thin QObject that lets background threads (tray, sync workers) marshal work
# onto the Qt GUI thread via signals -- the Qt-native replacement for the old
# Tkinter command queue.
# --------------------------------------------------------------------------- #
class _Bridge(QObject):
    status = Signal(str)
    cloud_totals = Signal(object)
    synced = Signal()
    sync_done = Signal()
    show = Signal()
    push = Signal()
    quit = Signal()


# Column model shared by the device + cloud tables.
_METRIC_COLS = ("keystrokes", "words", "deletions", "alt_tabs", "power_cycles")
_METRIC_HDRS = {"keystrokes": "Keys", "words": "Words", "deletions": "Del",
                "alt_tabs": "Alt-Tab", "power_cycles": "Cycles"}


_CHECK_PNG = None  # cached path to the generated checkmark image


def _checkmark_image_path():
    """Paint a bold checkmark once and cache it as a PNG, so the *checked* state
    of every QCheckBox shows an unmistakable tick (not just a colour change).
    The ink-coloured tick sits on the amber :checked fill for clear contrast.
    Generated at runtime, so there is no image asset to ship."""
    global _CHECK_PNG
    if _CHECK_PNG and os.path.exists(_CHECK_PNG):
        return _CHECK_PNG

    s = 28  # paint big and let Qt scale down for a crisp anti-aliased edge
    img = QImage(s, s, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.moveTo(s * 0.20, s * 0.52)
    path.lineTo(s * 0.42, s * 0.74)
    path.lineTo(s * 0.80, s * 0.26)
    pen = QPen(QColor(INK), s * 0.16)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPath(path)
    p.end()

    out = os.path.join(tempfile.gettempdir(), "frodotappins_check.png")
    img.save(out, "PNG")
    _CHECK_PNG = out
    return out


def _qss():
    """The warm dark-wood stylesheet. Tables stay light (parchment) as readable
    'pages'; everything around them is wood and amber."""
    return f"""
    /* root + page are transparent so the burl WoodPanel shows through */
    QMainWindow {{ background: {WOOD_DEEP}; }}
    QWidget#root, QWidget#page {{ background: transparent; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QLabel {{ color: {AMBER}; background: transparent; }}
    QLabel#dim {{ color: {AMBER_DIM}; }}
    QLabel#path {{ color: {INK_SOFT}; }}
    QLabel#heading {{ color: {AMBER}; font-size: 12pt; font-weight: bold; }}
    QLabel#sub {{ color: {AMBER_DIM}; font-size: 8pt; }}
    QLabel#status {{
        color: {PARCHMENT}; background: {WOOD_DEEP};
        border-top: 1px solid {FRAME_WOOD.name()}; padding: 4px 8px;
    }}
    /* custom title bar */
    QLabel#titletext {{ color: {AMBER}; font-weight: bold; }}
    QToolButton#winbtn, QToolButton#winbtnclose {{
        color: {AMBER}; background: transparent; border: none;
        border-radius: 4px; min-width: 26px; min-height: 22px; font-size: 12pt;
    }}
    QToolButton#winbtn:hover {{ background: {WOOD_MID}; color: {PARCHMENT}; }}
    QToolButton#winbtnclose:hover {{ background: #7a2f22; color: {PARCHMENT}; }}
    /* collapsible section header */
    QToolButton#sectionHeader {{
        color: {AMBER}; background: {WOOD_DARK}; border: 1px solid {WOOD_MID};
        border-radius: 6px; padding: 6px 8px; font-size: 12pt; font-weight: bold;
        text-align: left;
    }}
    QToolButton#sectionHeader:hover {{ background: {WOOD_MID}; }}
    /* cards: translucent dark so the burl shows faintly but text stays readable */
    QFrame#card {{
        background: rgba(20, 13, 8, 170); border: 1px solid {WOOD_MID};
        border-radius: 8px;
    }}
    QFrame[hline="true"] {{ background: {WOOD_MID}; max-height: 1px; border: none; }}
    QLineEdit {{
        background: {PARCHMENT}; color: {INK}; border: 1px solid {WOOD_MID};
        border-radius: 4px; padding: 4px 6px; selection-background-color: {AMBER_DIM};
    }}
    QPushButton {{
        background: {WOOD_MID}; color: {AMBER}; border: 1px solid {WOOD_LIGHT};
        border-radius: 5px; padding: 5px 12px;
    }}
    QPushButton:hover {{ background: {WOOD_LIGHT}; color: {PARCHMENT}; }}
    QPushButton:pressed {{ background: {WOOD_DARK}; }}
    QCheckBox {{ color: {AMBER}; background: transparent; spacing: 6px; }}
    QCheckBox::indicator {{
        width: 17px; height: 17px; border: 1px solid {WOOD_LIGHT};
        border-radius: 3px; background: {WOOD_DEEP};
    }}
    QCheckBox::indicator:hover {{ border: 1px solid {AMBER}; }}
    QCheckBox::indicator:checked {{
        background: {AMBER}; border: 1px solid {AMBER};
        image: url("{_checkmark_image_path().replace(chr(92), '/')}");
    }}
    QTreeWidget {{
        background: {PARCHMENT}; alternate-background-color: {PARCHMENT_2};
        color: {INK}; border: 1px solid {WOOD_MID}; border-radius: 6px;
        outline: 0; gridline-color: #d8c69e;
    }}
    QTreeWidget::item {{ padding: 3px 2px; border: none; }}
    QHeaderView::section {{
        background: {WOOD_MID}; color: {AMBER}; padding: 5px 6px;
        border: none; border-right: 1px solid {WOOD_DARK}; font-weight: bold;
    }}
    QScrollBar:vertical {{ background: {WOOD_DEEP}; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {WOOD_MID}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {WOOD_LIGHT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {WOOD_DARK}; color: {PARCHMENT}; border: 1px solid {AMBER_DIM}; }}
    """


class App(QMainWindow):
    WINDOW_ROWS = ("Last hour", "Last day", "Last week", "Last month",
                   "Last year", "All-time")

    def __init__(self, store, counter, cfg, start_minimized=False,
                 first_run=False, writable=True):
        super().__init__()
        self.store = store
        self.counter = counter
        self.cfg = cfg
        self.running = True
        self._sync_in_flight = False
        self.tray = None
        self._first_run = first_run
        self._writable = writable

        self.bridge = _Bridge()
        self.bridge.status.connect(self.set_status)
        self.bridge.cloud_totals.connect(self._render_cloud)
        self.bridge.synced.connect(self._on_synced)
        self.bridge.sync_done.connect(lambda: setattr(self, "_sync_in_flight", False))
        self.bridge.show.connect(self.show_window)
        self.bridge.push.connect(self.on_push)
        self.bridge.quit.connect(self.quit_app)

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(self._app_icon())
        # Frameless: we draw our own wood title bar instead of the grey OS one.
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.resize(580, 1040)
        self.setMinimumSize(540, 720)
        self.setStyleSheet(_qss())
        self._build_ui()

        # The vine border floats over the content (click-through). Z-order:
        # content < border < title bar, so the app name + window controls stay
        # visible above the vines, and the border is geometry-offset to start
        # just below the title bar.
        self.border = VineBorder(self)
        self._position_border()
        self.border.raise_()
        self.titlebar.raise_()

        # App-wide event filter drives frameless edge/corner resizing; it must
        # see mouse moves/presses before child widgets consume them.
        self.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)

        if HAVE_TRAY:
            self._start_tray()

        # periodic loops, all on the Qt event loop
        self._tick();             self._tick_timer = self._every(1000, self._tick)
        self._autosave();         self._save_timer = self._every(5000, self._autosave)
        QTimer.singleShot(INITIAL_SYNC_MS, self._autosync)

        if start_minimized and HAVE_TRAY:
            QTimer.singleShot(0, self.hide)
        else:
            self.show()

        if not writable:
            QTimer.singleShot(300, self._warn_not_writable)
        elif first_run:
            QTimer.singleShot(300, self._show_welcome)

    # -- small helpers ------------------------------------------------------ #
    def _every(self, ms, fn):
        t = QTimer(self)
        t.timeout.connect(fn)
        t.start(ms)
        return t

    def _app_icon(self):
        pm = QPixmap(64, 64)
        pm.fill(QColor(WOOD_DEEP))
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.setPen(QPen(QColor(AMBER), 3))
        qp.drawRoundedRect(10, 16, 44, 32, 6, 6)
        f = QFont("Georgia", 20, QFont.Bold)
        qp.setFont(f)
        qp.setPen(QColor(AMBER))
        qp.drawText(pm.rect(), Qt.AlignCenter, "T")
        qp.end()
        return QIcon(pm)

    def _heading(self, text):
        lab = QLabel(text); lab.setObjectName("heading")
        return lab

    def _hline(self):
        ln = QFrame(); ln.setProperty("hline", True); ln.setFixedHeight(1)
        return ln

    def _make_metric_tree(self, first_col, height_rows):
        tv = QTreeWidget()
        tv.setColumnCount(1 + len(_METRIC_COLS))
        tv.setHeaderLabels([first_col] + [_METRIC_HDRS[c] for c in _METRIC_COLS])
        tv.setRootIsDecorated(False)
        tv.setAlternatingRowColors(True)
        tv.setSelectionMode(QAbstractItemView.NoSelection)
        tv.setFocusPolicy(Qt.NoFocus)
        tv.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        hdr = tv.header()
        hdr.setStretchLastSection(False)
        # The name column flexes with the window; the five metric columns keep a
        # fixed width so their numbers are never clipped at narrow sizes.
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 1 + len(_METRIC_COLS)):
            hdr.setSectionResizeMode(i, QHeaderView.Fixed)
            tv.setColumnWidth(i, 62)
            tv.headerItem().setTextAlignment(i, Qt.AlignRight | Qt.AlignVCenter)
        row_h = 24
        tv.setFixedHeight(height_rows * row_h + 30)
        return tv

    # -- layout ------------------------------------------------------------- #
    def _build_ui(self):
        # The central panel paints the burl-wood texture; everything else is
        # transparent and sits on top of it.
        root = WoodPanel(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        # custom wood title bar (replaces the grey OS title bar)
        self.titlebar = TitleBar(self, APP_DISPLAY_NAME)
        outer.addWidget(self.titlebar)

        # scrollable content, inset so vines have a clear band around the edges
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        page = QWidget(); page.setObjectName("page")
        scroll.setWidget(page)
        col = QVBoxLayout(page)
        # Content is inset clear of the ~95px woven-vine band so the wreath
        # frames the page without overlapping the tables.
        col.setContentsMargins(58, 64, 58, 48)
        col.setSpacing(8)

        # days-since header
        self.since_lbl = QLabel(""); self.since_lbl.setStyleSheet(
            f"color:{AMBER}; font-size:11pt; font-weight:bold;")
        col.addWidget(self.since_lbl)

        # This device
        col.addWidget(self._heading("This device"))
        self.tree = self._make_metric_tree("Window", len(self.WINDOW_ROWS))
        for name in self.WINDOW_ROWS:
            it = QTreeWidgetItem([name] + ["0"] * len(_METRIC_COLS))
            for i in range(1, 1 + len(_METRIC_COLS)):
                it.setTextAlignment(i, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
        self._device_items = {self.tree.topLevelItem(i).text(0): self.tree.topLevelItem(i)
                              for i in range(self.tree.topLevelItemCount())}
        col.addWidget(self.tree)

        col.addWidget(self._hline())

        # Words vs Tolkien  --  promoted to sit right under This device
        col.addWidget(self._heading("Words vs. Tolkien (all-time)"))
        self.books_tree = QTreeWidget()
        self.books_tree.setColumnCount(4)
        self.books_tree.setHeaderLabels(["Work", "Book total", "% written", "Words to go"])
        self.books_tree.setRootIsDecorated(False)
        self.books_tree.setAlternatingRowColors(True)
        self.books_tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.books_tree.setFocusPolicy(Qt.NoFocus)
        self.books_tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.books_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        bh = self.books_tree.header()
        bh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            bh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            self.books_tree.headerItem().setTextAlignment(i, Qt.AlignRight | Qt.AlignVCenter)
        self._book_items = {}
        for title, count, is_agg in BOOK_WORD_COUNTS:
            it = QTreeWidgetItem([title, f"{count:,}", "0.0%", f"{count:,}"])
            for i in (1, 2, 3):
                it.setTextAlignment(i, Qt.AlignRight | Qt.AlignVCenter)
            if is_agg:
                f = it.font(0); f.setBold(True)
                for c in range(4):
                    it.setFont(c, f)
            self.books_tree.addTopLevelItem(it)
            self._book_items[title] = it
        self.books_tree.setFixedHeight(len(BOOK_WORD_COUNTS) * 24 + 30)
        col.addWidget(self.books_tree)

        col.addWidget(self._hline())

        # Cloud - all devices  --  collapsible
        cloud = CollapsibleSection("Cloud — all devices", expanded=self.cfg.cloud_expanded)
        self.cloud_section = cloud
        cloud.header.clicked.connect(self._save_cloud_expanded)
        col.addWidget(cloud)

        self.cloud_tree = self._make_metric_tree("Device", 3)
        self.cloud_tree.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        placeholder = QTreeWidgetItem(["Not synced yet"] + [""] * len(_METRIC_COLS))
        self.cloud_tree.addTopLevelItem(placeholder)
        cloud.addWidget(self.cloud_tree)

        self.sync_lbl = QLabel("Last cloud sync: never"); self.sync_lbl.setObjectName("dim")
        cloud.addWidget(self.sync_lbl)

        # Cloud config card
        card = QFrame(); card.setObjectName("card")
        cg = QGridLayout(card); cg.setContentsMargins(12, 10, 12, 12); cg.setSpacing(6)
        cg.addWidget(QLabel("Worker URL"), 0, 0, 1, 2)
        self.e_url = QLineEdit(self.cfg.data.get("endpoint", ""))
        cg.addWidget(self.e_url, 1, 0, 1, 2)
        cg.addWidget(QLabel("API key"), 2, 0, 1, 2)
        self.e_key = QLineEdit(self.cfg.data.get("api_key", ""))
        self.e_key.setEchoMode(QLineEdit.Password)
        cg.addWidget(self.e_key, 3, 0, 1, 2)
        save_btn = QPushButton("Save"); save_btn.clicked.connect(self.save_cfg)
        cg.addWidget(save_btn, 4, 0, alignment=Qt.AlignLeft)
        cloud.addWidget(card)

        # push / refresh row + autosync
        bf = QHBoxLayout()
        push_btn = QPushButton("Push to Cloud"); push_btn.clicked.connect(self.on_push)
        refresh_btn = QPushButton("Refresh total"); refresh_btn.clicked.connect(self.on_refresh)
        bf.addWidget(push_btn); bf.addWidget(refresh_btn); bf.addStretch(1)
        cloud.addLayout(bf)

        self.autosync_chk = QCheckBox("Auto-sync to cloud every 15 min")
        self.autosync_chk.setChecked(self.cfg.autosync)
        self.autosync_chk.toggled.connect(self.toggle_autosync)
        cloud.addWidget(self.autosync_chk)

        col.addWidget(self._hline())

        # install / data location
        loc = QLabel("This app and its data live here (delete the folder to uninstall):")
        loc.setObjectName("dim"); loc.setWordWrap(True)
        col.addWidget(loc)
        self.path_lbl = QLabel(app_dir()); self.path_lbl.setObjectName("path")
        self.path_lbl.setWordWrap(True)
        col.addWidget(self.path_lbl)

        row = QHBoxLayout()
        open_btn = QPushButton("Open folder"); open_btn.clicked.connect(self.open_folder)
        self.autostart_chk = QCheckBox("Start at login")
        self.autostart_chk.setChecked(autostart_enabled())
        self.autostart_chk.toggled.connect(self.toggle_autostart)
        row.addWidget(open_btn); row.addWidget(self.autostart_chk); row.addStretch(1)
        col.addLayout(row)

        warn = QLabel("Moving this folder breaks auto-start — re-tick “Start at login” to fix it.")
        warn.setObjectName("sub"); warn.setWordWrap(True)
        col.addWidget(warn)
        col.addStretch(1)

        # status bar (outside the scroll area, full width)
        self.status = QLabel("Counting…"); self.status.setObjectName("status")
        outer.addWidget(self.status)

    # -- vine border tracks the window size --------------------------------- #
    def _position_border(self):
        # Border covers everything below the title bar, so the wreath frames the
        # content area while the title bar (name + controls) stays clear on top.
        top = self.titlebar.height() if hasattr(self, "titlebar") else 0
        self.border.setGeometry(0, top, self.width(), self.height() - top)

    def resizeEvent(self, e):
        if hasattr(self, "border"):
            self._position_border()
            self.border.raise_()
            self.titlebar.raise_()
        super().resizeEvent(e)

    # -- frameless window resize ------------------------------------------- #
    # We use Qt's startSystemResize() (the supported way to resize a frameless
    # window), driven by an application-wide event filter so that mouse moves /
    # presses in the outer edge band are caught even though child widgets (the
    # scroll area, tables) would otherwise consume them. The cursor shape is set
    # on hover; a press in the band starts the native resize drag.
    _RESIZE_MARGIN = 8

    def _edges_at(self, pos):
        """Return the Qt.Edges under a window-local point, or None if not in
        the resize band."""
        m = self._RESIZE_MARGIN
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()
        edges = Qt.Edges()
        if x <= m:           edges |= Qt.LeftEdge
        if x >= w - m:       edges |= Qt.RightEdge
        if y <= m:           edges |= Qt.TopEdge
        if y >= h - m:       edges |= Qt.BottomEdge
        return edges if edges else None

    @staticmethod
    def _cursor_for(edges):
        horizontal = (Qt.LeftEdge | Qt.RightEdge)
        vertical = (Qt.TopEdge | Qt.BottomEdge)
        tl_br = (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge)
        tr_bl = (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge)
        if edges in tl_br:   return Qt.SizeFDiagCursor
        if edges in tr_bl:   return Qt.SizeBDiagCursor
        if edges & horizontal and not (edges & vertical):  return Qt.SizeHorCursor
        if edges & vertical and not (edges & horizontal):  return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.MouseMove, QEvent.MouseButtonPress,
                  QEvent.HoverMove) and not self.isMaximized():
            # global position -> this window's local coords
            gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") \
                else QCursor.pos()
            local = self.mapFromGlobal(gp)
            if self.rect().contains(local):
                edges = self._edges_at(local)
                if et in (QEvent.MouseMove, QEvent.HoverMove):
                    if edges:
                        self.setCursor(self._cursor_for(edges))
                    elif self.cursor().shape() != Qt.ArrowCursor:
                        self.unsetCursor()
                elif et == QEvent.MouseButtonPress and edges:
                    if event.button() == Qt.LeftButton:
                        wh = self.windowHandle()
                        if wh is not None:
                            wh.startSystemResize(edges)
                            return True  # consume so children don't react
        return super().eventFilter(obj, event)

    # -- status / misc ------------------------------------------------------ #
    def set_status(self, msg):
        self.status.setText(msg)

    def open_folder(self):
        try:
            os.startfile(app_dir())
        except Exception as e:
            self.set_status(f"Couldn't open folder: {e}")

    def _show_welcome(self):
        self.show_window()
        QMessageBox.information(
            self, APP_DISPLAY_NAME,
            "This tool saves all data in the folder you've installed it in. "
            "To uninstall, just delete everything.\n\n"
            "This tool runs offline and standalone, but can tally totals across "
            "multiple devices leveraging a free Cloudflare account. See the README "
            "file for instructions.",
        )

    def _warn_not_writable(self):
        self.show_window()
        QMessageBox.warning(
            self, APP_DISPLAY_NAME,
            "FrodoTappins can't save data in its current folder:\n\n"
            f"{app_dir()}\n\n"
            "It needs to live somewhere writable — a normal folder, your Desktop, "
            "or a USB drive, but not Program Files or a read-only location. Move the "
            "whole folder somewhere writable and start it again.",
        )

    def save_cfg(self):
        self.cfg.data["endpoint"] = self.e_url.text().strip()
        self.cfg.data["api_key"] = self.e_key.text().strip()
        self.cfg.save()
        self.set_status("Settings saved.")

    def toggle_autostart(self, checked):
        try:
            set_autostart(checked)
            self.set_status("Autostart " + ("enabled." if checked else "disabled."))
        except Exception as e:
            QMessageBox.critical(self, APP_DISPLAY_NAME, f"Could not change autostart:\n{e}")
            self.autostart_chk.blockSignals(True)
            self.autostart_chk.setChecked(autostart_enabled())
            self.autostart_chk.blockSignals(False)

    # -- cloud sync (threads marshal back via self.bridge) ------------------ #
    def on_push(self):
        self._sync_now("manual")

    def on_refresh(self):
        if not (self.cfg.endpoint and self.cfg.api_key):
            self.set_status("Set the Worker URL and API key first.")
            return
        self.set_status("Fetching cloud totals…")

        def work():
            try:
                totals = fetch_cloud_totals(self.cfg)
                self.bridge.cloud_totals.emit(totals)
                self.bridge.status.emit("Cloud totals updated.")
            except urllib.error.HTTPError as e:
                self.bridge.status.emit(f"Fetch failed: HTTP {e.code}")
            except Exception as e:
                self.bridge.status.emit(f"Fetch failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _sync_now(self, reason="manual"):
        if not (self.cfg.endpoint and self.cfg.api_key):
            if reason == "manual":
                self.set_status("Set the Worker URL and API key first.")
            return
        if self._sync_in_flight:
            if reason == "manual":
                self.set_status("Sync already in progress…")
            return
        self._sync_in_flight = True
        self.set_status("Syncing…")

        def work():
            try:
                self.store.save()
                push_to_cloud(self.store, self.cfg)
                totals = fetch_cloud_totals(self.cfg)
                self.bridge.cloud_totals.emit(totals)
                self.bridge.synced.emit()
            except urllib.error.HTTPError as e:
                self.bridge.status.emit(f"Sync failed: HTTP {e.code}")
            except Exception as e:
                self.bridge.status.emit(f"Sync failed: {e}")
            finally:
                self.bridge.sync_done.emit()

        threading.Thread(target=work, daemon=True).start()

    def _on_synced(self):
        now = dt.datetime.now().strftime("%H:%M:%S")
        self.sync_lbl.setText(f"Last cloud sync: {now}")
        self.set_status("Synced.")

    def _save_cloud_expanded(self):
        # Persist the Cloud section's open/closed state across launches.
        self.cfg.data["cloud_expanded"] = bool(self.cloud_section._expanded)
        self.cfg.save()

    def _render_cloud(self, totals):
        g = totals.get("global", {}) or {}
        devices = totals.get("devices", []) or []
        my_id = self.store.identity()[0]
        tv = self.cloud_tree
        tv.clear()

        def fmt(v):
            try:
                return f"{int(v):,}"
            except Exception:
                return "0"

        for d in devices:
            name = d.get("device_name") or (d.get("device_id", "") or "")[:8]
            is_self = d.get("device_id") == my_id
            if is_self:
                name += "  (this device)"
            it = QTreeWidgetItem([
                name, fmt(d.get("keystrokes")), fmt(d.get("words")),
                fmt(d.get("deletions")), fmt(d.get("alt_tabs")),
                fmt(d.get("power_cycles")),
            ])
            for i in range(1, 1 + len(_METRIC_COLS)):
                it.setTextAlignment(i, Qt.AlignRight | Qt.AlignVCenter)
            if is_self:
                for c in range(1 + len(_METRIC_COLS)):
                    it.setForeground(c, QColor(SELF_GREEN))
            tv.addTopLevelItem(it)

        count = totals.get("device_count", len(devices))
        total_it = QTreeWidgetItem([
            f"Cloud total ({count} device{'s' if count != 1 else ''})",
            fmt(g.get("keystrokes")), fmt(g.get("words")),
            fmt(g.get("deletions")), fmt(g.get("alt_tabs")),
            fmt(g.get("power_cycles")),
        ])
        f = total_it.font(0); f.setBold(True)
        for c in range(1 + len(_METRIC_COLS)):
            total_it.setFont(c, f)
            total_it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
        total_it.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
        tv.addTopLevelItem(total_it)
        rows = min(max(len(devices) + 1, 1), 6)
        tv.setFixedHeight(rows * 24 + 30)

    def toggle_autosync(self, checked):
        self.cfg.data["autosync"] = bool(checked)
        self.cfg.save()
        if checked:
            self.set_status("Auto-sync on (every 15 min).")
            self._sync_now("manual")
        else:
            self.set_status("Auto-sync off.")

    # -- periodic loops ----------------------------------------------------- #
    def _tick(self):
        n = self.store.days_since()
        self.since_lbl.setText(
            f"Tracking since {self.store.start_date()}  ·  {n} day{'s' if n != 1 else ''}")
        bd = self.store.breakdown()
        for name in self.WINDOW_ROWS:
            b = bd[name]
            it = self._device_items[name]
            it.setText(1, f"{b['keystrokes']:,}")
            it.setText(2, f"{b['words']:,}")
            it.setText(3, f"{b['deletions']:,}")
            it.setText(4, f"{b['alt_tabs']:,}")
            it.setText(5, f"{b['power_cycles']:,}")
        self._update_books()

    def _update_books(self):
        typed = self.store.totals().get("words", 0)
        for title, count, is_agg in BOOK_WORD_COUNTS:
            pct = (typed / count * 100) if count else 0.0
            left = max(0, count - typed)
            it = self._book_items[title]
            it.setText(1, f"{count:,}")
            it.setText(2, f"{pct:.1f}%")
            it.setText(3, f"{left:,}")
            done = typed >= count
            colr = QColor(DONE_GREEN) if done else QColor(INK)
            for c in range(4):
                it.setForeground(c, colr)

    def _autosave(self):
        self.store.prune()
        self.store.save()

    def _autosync(self):
        if self.cfg.autosync:
            self._sync_now("auto")
        if self.running:
            QTimer.singleShot(SYNC_INTERVAL_MS, self._autosync)

    # -- tray --------------------------------------------------------------- #
    def _start_tray(self):
        self.tray = QSystemTrayIcon(self._app_icon(), self)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu()
        act_open = menu.addAction("Open")
        act_push = menu.addAction("Push to Cloud")
        menu.addSeparator()
        act_quit = menu.addAction("Quit")
        act_open.triggered.connect(self.show_window)
        act_push.triggered.connect(self.on_push)
        act_quit.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    # -- window state ------------------------------------------------------- #
    def changeEvent(self, e):
        # Pause the vine animation while minimised so it uses no CPU in the
        # background; resume when restored. (Hide-to-tray is handled by the
        # border's own hideEvent/showEvent.)
        if e.type() == QEvent.WindowStateChange and hasattr(self, "border"):
            if self.isMinimized():
                self.border.pause()
            else:
                self.border.resume()
        super().changeEvent(e)

    def closeEvent(self, e):
        # Closing the window hides to tray (matching the old Tk behaviour);
        # quitting happens only via the tray menu or quit_app().
        if HAVE_TRAY and self.running:
            e.ignore()
            self.hide()
            self.set_status("Running in the tray.")
        else:
            e.accept()
            self.quit_app()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

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
                self.tray.hide()
            except Exception:
                pass
        QApplication.instance().quit()

    def run(self):
        # kept for call-site compatibility; the real loop runs in main()
        self.show_window()


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

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    # Don't quit when the window is hidden to tray (only quit_app() should exit).
    qapp.setQuitOnLastWindowClosed(False)
    win = App(store, counter, cfg, start_minimized=start_minimized,
              first_run=first_run, writable=writable)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
