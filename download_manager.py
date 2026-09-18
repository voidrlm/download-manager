"""Download manager GUI backed by aria2c's JSON-RPC interface."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, ttk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
SESSION_FILE = os.path.join(APP_DIR, "session.aria2")
RPC_PORT = 6809
RPC_URL = f"http://localhost:{RPC_PORT}/jsonrpc"
RPC_SECRET = "dlmgr"

DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "connections_per_server": 8,
    "split": 8,
    "max_concurrent": 3,
    "max_speed": "0",
    "continue_partial": True,
}

# ---- palette ----
BG        = "#12131a"   # window background
SURFACE   = "#1b1d27"   # cards / header
SURFACE2  = "#232634"   # hover / inputs
BORDER    = "#2c2f3f"
FG        = "#e8eaf2"
FG_DIM    = "#8b90a5"
ACCENT    = "#5b8cff"
GREEN     = "#4ade80"
RED       = "#f87171"
YELLOW    = "#facc15"

STATUS_COLORS = {
    "active":   ACCENT,
    "waiting":  YELLOW,
    "paused":   YELLOW,
    "complete": GREEN,
    "error":    RED,
    "removed":  FG_DIM,
}
STATUS_LABELS = {
    "active": "Downloading", "waiting": "Queued", "paused": "Paused",
    "complete": "Completed", "error": "Failed", "removed": "Cancelled",
}

VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".ogv", ".3gp",
}
# Bytes that must be on disk before an in-progress file is worth opening.
PLAYABLE_MIN_BYTES = 1024 * 1024


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def find_aria2c():
    path = shutil.which("aria2c")
    if path:
        return path
    winget_root = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages"
    )
    if os.path.isdir(winget_root):
        for root, _dirs, files in os.walk(winget_root):
            if "aria2c.exe" in files:
                return os.path.join(root, "aria2c.exe")
    return None


_player_cache = []


def find_player():
    """Path to a player that copes with partially downloaded files, or None."""
    if _player_cache:
        return _player_cache[0]
    candidates = ["vlc", "mpv", "mpc-hc64", "mpc-hc"]
    paths = [shutil.which(name) for name in candidates]
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        if base:
            paths.append(os.path.join(base, "VideoLAN", "VLC", "vlc.exe"))
            paths.append(os.path.join(base, "mpv", "mpv.exe"))
            paths.append(os.path.join(base, "MPC-HC", "mpc-hc64.exe"))
    for path in paths:
        if path and os.path.isfile(path):
            _player_cache.append(path)
            return path
    _player_cache.append(None)
    return None


def is_video_file(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_eta(secs):
    if secs >= 3600:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


class Aria2:
    """Thin JSON-RPC client for a locally spawned aria2c daemon."""

    def __init__(self):
        self.proc = None
        self.attached = False   # reusing a daemon left over from a previous run
        self._id = 0

    def start(self, settings):
        # a daemon from an earlier session may still be alive and downloading;
        # attach to it instead of failing on the busy RPC port
        try:
            self.call("aria2.getVersion")
            self.attached = True
            return
        except Exception:
            pass
        exe = find_aria2c()
        if not exe:
            raise RuntimeError(
                "aria2c.exe not found. Install it with:\n  winget install aria2.aria2"
            )
        args = [
            exe,
            "--enable-rpc",
            f"--rpc-listen-port={RPC_PORT}",
            f"--rpc-secret={RPC_SECRET}",
            "--rpc-listen-all=false",
            f"--dir={settings['download_dir']}",
            f"--max-connection-per-server={settings['connections_per_server']}",
            f"--split={settings['split']}",
            f"--max-concurrent-downloads={settings['max_concurrent']}",
            f"--continue={'true' if settings['continue_partial'] else 'false'}",
            "--file-allocation=none",
            "--auto-file-renaming=true",
            # unfinished downloads survive a restart: aria2 reloads them from the
            # session file and picks up where the .aria2 control files left off
            f"--save-session={SESSION_FILE}",
            "--save-session-interval=20",
            "--auto-save-interval=15",
        ]
        if os.path.isfile(SESSION_FILE):
            args.append(f"--input-file={SESSION_FILE}")
        if settings["max_speed"] not in ("", "0"):
            args.append(f"--max-overall-download-limit={settings['max_speed']}")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        for _ in range(40):
            try:
                self.call("aria2.getVersion")
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("aria2c exited immediately after launch.")
                time.sleep(0.25)
        raise RuntimeError("Could not connect to aria2c RPC.")

    def stop(self):
        if self.attached:
            try:
                self.call("aria2.saveSession")
                self.call("aria2.shutdown")
            except Exception:
                pass
            return
        if self.proc and self.proc.poll() is None:
            try:
                # saveSession first: a forced kill below would lose the queue
                self.call("aria2.saveSession")
            except Exception:
                pass
            try:
                self.call("aria2.shutdown")
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def call(self, method, *params):
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": str(self._id),
            "method": method,
            "params": [f"token:{RPC_SECRET}", *params],
        }).encode()
        req = urllib.request.Request(
            RPC_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "RPC error"))
        return data["result"]

    def apply_settings(self, settings):
        self.call("aria2.changeGlobalOption", {
            "max-concurrent-downloads": str(settings["max_concurrent"]),
            "max-overall-download-limit":
                settings["max_speed"] if settings["max_speed"] not in ("", "0") else "0",
            "dir": settings["download_dir"],
        })


class DownloadRow(ttk.Frame):
    """One download rendered as a card with a progress bar."""

    def __init__(self, parent, app, gid):
        super().__init__(parent, style="Card.TFrame", padding=(14, 10, 14, 12))
        self.app = app
        self.gid = gid
        self.status = ""
        self.url = ""
        self.filepath = ""
        self.playpath = ""     # video file to hand to the player
        self.playdone = 0      # bytes of that file already on disk

        top = ttk.Frame(self, style="Card.TFrame")
        top.pack(fill="x")
        self.name_lbl = ttk.Label(top, text="…", style="Name.TLabel", anchor="w")
        self.name_lbl.pack(side="left", fill="x", expand=True)
        self.status_lbl = ttk.Label(top, text="", style="Status.TLabel")
        self.status_lbl.pack(side="right")
        self.play_btn = ttk.Button(top, text="▶  Play", style="Play.TButton",
                                   command=self.play, takefocus=False)

        self.bar = ttk.Progressbar(self, style="Dl.Horizontal.TProgressbar",
                                   maximum=1000, value=0)
        self.bar.pack(fill="x", pady=(8, 6))

        bottom = ttk.Frame(self, style="Card.TFrame")
        bottom.pack(fill="x")
        self.info_lbl = ttk.Label(bottom, text="", style="Dim.TLabel", anchor="w")
        self.info_lbl.pack(side="left", fill="x", expand=True)
        self.pct_lbl = ttk.Label(bottom, text="", style="Pct.TLabel")
        self.pct_lbl.pack(side="right")

        for w in (self, top, bottom, self.name_lbl, self.status_lbl,
                  self.bar, self.info_lbl, self.pct_lbl):
            w.bind("<Button-3>", self._menu)
            w.bind("<Button-1>", lambda e: self.app.select_row(self))
            w.bind("<Double-Button-1>", self._double_click)

    def _double_click(self, _event):
        if self.can_play():
            self.play()

    def can_play(self):
        return bool(self.playpath) and (
            self.status == "complete" or self.playdone >= PLAYABLE_MIN_BYTES)

    def play(self):
        self.app.play_file(self.playpath, partial=self.status != "complete")

    def _menu(self, event):
        self.app.select_row(self)
        self.app.show_context_menu(self, event)

    def set_selected(self, on):
        style = "CardSel.TFrame" if on else "Card.TFrame"
        self.configure(style=style)
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                child.configure(style=style)

    def update_data(self, it):
        self.status = it.get("status", "?")
        total = int(it.get("totalLength", 0))
        done = int(it.get("completedLength", 0))
        speed = int(it.get("downloadSpeed", 0))

        files = it.get("files") or []
        if files:
            f0 = files[0]
            self.filepath = f0.get("path", "")
            uris = f0.get("uris") or [{}]
            self.url = uris[0].get("uri", self.url)

        name = ""
        bt = it.get("bittorrent") or {}
        if bt.get("info", {}).get("name"):
            name = bt["info"]["name"]
        elif self.filepath:
            name = os.path.basename(self.filepath)
        self.name_lbl.config(text=name or self.url or self.gid)

        # a torrent can hold several files: the biggest video is the one to play
        videos = [f for f in files if is_video_file(f.get("path", ""))]
        if videos:
            best = max(videos, key=lambda f: int(f.get("length", 0)))
            self.playpath = best.get("path", "")
            self.playdone = int(best.get("completedLength", 0))
        else:
            self.playpath = ""
            self.playdone = 0

        if self.can_play():
            if not self.play_btn.winfo_ismapped():
                self.play_btn.pack(side="right", padx=(0, 12))
        elif self.play_btn.winfo_ismapped():
            self.play_btn.pack_forget()

        color = STATUS_COLORS.get(self.status, FG_DIM)
        self.status_lbl.config(
            text=f"●  {STATUS_LABELS.get(self.status, self.status)}", foreground=color)

        if total:
            frac = done / total
            self.bar.config(value=int(frac * 1000))
            self.pct_lbl.config(text=f"{frac * 100:.1f}%")
        else:
            self.bar.config(value=0)
            self.pct_lbl.config(text="")

        parts = []
        if total:
            parts.append(f"{fmt_size(done)} / {fmt_size(total)}")
        elif done:
            parts.append(fmt_size(done))
        if self.status == "active" and speed:
            parts.append(f"{fmt_size(speed)}/s")
            if total and done < total:
                parts.append(f"ETA {fmt_eta((total - done) // speed)}")
        if self.status == "error" and it.get("errorMessage"):
            parts.append(it["errorMessage"][:80])
        self.info_lbl.config(text="   •   ".join(parts))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Download Manager")
        self.geometry("880x560")
        self.minsize(700, 420)
        self.configure(bg=BG)

        self.settings = load_settings()
        os.makedirs(self.settings["download_dir"], exist_ok=True)

        self.aria = Aria2()
        try:
            self.aria.start(self.settings)
        except RuntimeError as e:
            messagebox.showerror("aria2c error", str(e))
            self.destroy()
            sys.exit(1)

        self.rows = {}          # gid -> DownloadRow
        self.selected = None
        self._removed = set()   # gids cancelled locally; ignore if aria2 still reports them
        self._warned_partial = False

        self._init_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._polling = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---------- styling ----------

    def _init_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=BG, foreground=FG, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Header.TFrame", background=SURFACE)
        s.configure("Card.TFrame", background=SURFACE)
        s.configure("CardSel.TFrame", background=SURFACE2)

        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Title.TLabel", background=SURFACE, foreground=FG,
                    font=("Segoe UI Semibold", 14))
        s.configure("Name.TLabel", background=SURFACE, foreground=FG,
                    font=("Segoe UI Semibold", 11))
        s.configure("Status.TLabel", background=SURFACE, font=("Segoe UI", 9))
        s.configure("Dim.TLabel", background=SURFACE, foreground=FG_DIM,
                    font=("Segoe UI", 9))
        s.configure("Pct.TLabel", background=SURFACE, foreground=ACCENT,
                    font=("Segoe UI Semibold", 10))
        s.configure("SetLbl.TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("SetHint.TLabel", background=BG, foreground=FG_DIM, font=("Segoe UI", 9))
        s.configure("Bar.TLabel", background=SURFACE, foreground=FG_DIM, font=("Segoe UI", 9))
        s.configure("Saved.TLabel", background=BG, foreground=GREEN, font=("Segoe UI", 10))

        for sel_style in ("CardSel",):
            for lbl in ("Name", "Status", "Dim", "Pct"):
                pass  # labels keep SURFACE bg; acceptable subtle two-tone on selection

        s.configure("TButton", background=SURFACE2, foreground=FG, borderwidth=0,
                    focuscolor=SURFACE2, padding=(14, 7))
        s.map("TButton", background=[("active", "#2e3245")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Segoe UI Semibold", 10), padding=(18, 7))
        s.map("Accent.TButton", background=[("active", "#4a79e8")])
        s.configure("Play.TButton", background=SURFACE2, foreground=GREEN,
                    font=("Segoe UI Semibold", 9), borderwidth=0, padding=(10, 2))
        s.map("Play.TButton", background=[("active", "#2e3245")])
        s.configure("Nav.TButton", background=SURFACE, foreground=FG_DIM,
                    font=("Segoe UI", 10), padding=(14, 6))
        s.map("Nav.TButton", background=[("active", SURFACE2)])
        s.configure("NavOn.TButton", background=SURFACE2, foreground=FG,
                    font=("Segoe UI Semibold", 10), padding=(14, 6))
        s.map("NavOn.TButton", background=[("active", SURFACE2)])

        s.configure("TEntry", fieldbackground=SURFACE2, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER, padding=7)
        s.configure("TSpinbox", fieldbackground=SURFACE2, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER, arrowcolor=FG, padding=5)
        s.configure("TCheckbutton", background=BG, foreground=FG,
                    focuscolor=BG, indicatorcolor=SURFACE2)
        s.map("TCheckbutton", background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT)])

        s.configure("Dl.Horizontal.TProgressbar", troughcolor=SURFACE2,
                    background=ACCENT, bordercolor=SURFACE, lightcolor=ACCENT,
                    darkcolor=ACCENT, thickness=7)
        s.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG_DIM)
        s.map("Vertical.TScrollbar", background=[("active", "#343a50")])

    # ---------- UI ----------

    def _build_ui(self):
        # header
        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="⬇  Downloads", style="Title.TLabel").pack(side="left")
        self.nav_settings = ttk.Button(header, text="⚙  Settings", style="Nav.TButton",
                                       command=lambda: self.show_page("settings"))
        self.nav_settings.pack(side="right", padx=(6, 0))
        self.nav_downloads = ttk.Button(header, text="⬇  Downloads", style="NavOn.TButton",
                                        command=lambda: self.show_page("downloads"))
        self.nav_downloads.pack(side="right")

        self.pages = {}
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)

        # ----- downloads page -----
        dl = ttk.Frame(container, padding=(18, 14))
        self.pages["downloads"] = dl

        addbar = ttk.Frame(dl)
        addbar.pack(fill="x")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(addbar, textvariable=self.url_var, font=("Segoe UI", 11))
        url_entry.pack(side="left", fill="x", expand=True, ipady=3)
        url_entry.bind("<Return>", lambda e: self.add_download())
        self.url_entry = url_entry
        ttk.Button(addbar, text="＋  Add download", style="Accent.TButton",
                   command=self.add_download).pack(side="left", padx=(10, 0))

        # scrollable card list
        listwrap = ttk.Frame(dl)
        listwrap.pack(fill="both", expand=True, pady=(14, 0))
        self.canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(listwrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = ttk.Frame(self.canvas)
        self._list_win = self.canvas.create_window((0, 0), window=self.list_frame,
                                                   anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._list_win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        self.empty_lbl = ttk.Label(
            self.list_frame, text="\nNo downloads yet.\nPaste a link above and press Enter.",
            foreground=FG_DIM, background=BG, font=("Segoe UI", 11),
            anchor="center", justify="center")
        self.empty_lbl.pack(fill="x", pady=40)

        # status bar
        statusbar = ttk.Frame(self, style="Header.TFrame", padding=(18, 6))
        statusbar.pack(fill="x", side="bottom")
        self.global_lbl = ttk.Label(statusbar, text="Ready", style="Bar.TLabel")
        self.global_lbl.pack(side="left")
        open_btn = ttk.Label(statusbar, text="Open download folder", style="Bar.TLabel",
                             foreground=ACCENT, cursor="hand2")
        open_btn.pack(side="right")
        open_btn.bind("<Button-1>",
                      lambda e: os.startfile(self.settings["download_dir"]))

        # ----- settings page -----
        st = ttk.Frame(container, padding=(28, 24))
        self.pages["settings"] = st
        st.columnconfigure(1, weight=1)

        def row(r, label, hint=""):
            ttk.Label(st, text=label, style="SetLbl.TLabel").grid(
                row=r, column=0, sticky="w", pady=(10, 0), padx=(0, 24))
            if hint:
                ttk.Label(st, text=hint, style="SetHint.TLabel").grid(
                    row=r + 1, column=0, sticky="w")

        row(0, "Download folder")
        self.dir_var = tk.StringVar(value=self.settings["download_dir"])
        dirf = ttk.Frame(st)
        dirf.grid(row=0, column=1, sticky="ew", pady=(10, 0))
        dirf.columnconfigure(0, weight=1)
        ttk.Entry(dirf, textvariable=self.dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(dirf, text="Browse…", command=self.browse_dir).grid(
            row=0, column=1, padx=(8, 0))

        row(2, "Connections per server", "The \"threads\" per download (1–16)")
        self.conn_var = tk.IntVar(value=self.settings["connections_per_server"])
        ttk.Spinbox(st, from_=1, to=16, textvariable=self.conn_var, width=6).grid(
            row=2, column=1, sticky="w", pady=(10, 0))

        row(4, "Split", "Segments each file is divided into (1–32)")
        self.split_var = tk.IntVar(value=self.settings["split"])
        ttk.Spinbox(st, from_=1, to=32, textvariable=self.split_var, width=6).grid(
            row=4, column=1, sticky="w", pady=(10, 0))

        row(6, "Max simultaneous downloads")
        self.jobs_var = tk.IntVar(value=self.settings["max_concurrent"])
        ttk.Spinbox(st, from_=1, to=10, textvariable=self.jobs_var, width=6).grid(
            row=6, column=1, sticky="w", pady=(10, 0))

        row(8, "Speed limit", "0 = unlimited; e.g. 500K or 2M")
        self.speed_var = tk.StringVar(value=self.settings["max_speed"])
        ttk.Entry(st, textvariable=self.speed_var, width=10).grid(
            row=8, column=1, sticky="w", pady=(10, 0))

        self.cont_var = tk.BooleanVar(value=self.settings["continue_partial"])
        ttk.Checkbutton(st, text="Resume partially downloaded files",
                        variable=self.cont_var).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(18, 0))

        savef = ttk.Frame(st)
        savef.grid(row=11, column=0, columnspan=2, sticky="w", pady=(24, 0))
        ttk.Button(savef, text="Save settings", style="Accent.TButton",
                   command=self.save_settings_ui).pack(side="left")
        self.saved_lbl = ttk.Label(savef, text="", style="Saved.TLabel")
        self.saved_lbl.pack(side="left", padx=12)

        # context menu
        self.menu = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=FG,
                            activebackground=ACCENT, activeforeground="#ffffff",
                            bd=0, font=("Segoe UI", 10))

        self.show_page("downloads")

    def _on_wheel(self, event):
        if self.pages["downloads"].winfo_ismapped():
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def show_page(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.nav_downloads.configure(
            style="NavOn.TButton" if name == "downloads" else "Nav.TButton")
        self.nav_settings.configure(
            style="NavOn.TButton" if name == "settings" else "Nav.TButton")

    # ---------- selection & context menu ----------

    def select_row(self, row):
        if self.selected and self.selected is not row and self.selected.winfo_exists():
            self.selected.set_selected(False)
        self.selected = row
        row.set_selected(True)

    def show_context_menu(self, row, event):
        m = self.menu
        m.delete(0, "end")
        st = row.status
        if st == "active":
            m.add_command(label="⏸  Pause", command=lambda: self._rpc("aria2.pause", row.gid))
        if st in ("paused", "waiting"):
            m.add_command(label="▶  Resume", command=lambda: self._rpc("aria2.unpause", row.gid))
        if row.can_play():
            label = "▶  Play" if st == "complete" else "▶  Play while downloading"
            m.add_command(label=label, command=row.play)
        if st in ("active", "paused", "waiting"):
            m.add_command(label="✕  Cancel", command=lambda: self.cancel_row(row))
        if st == "complete" and row.filepath:
            m.add_command(label="📂  Open file",
                          command=lambda: self._open_path(row.filepath))
            m.add_command(label="📁  Show in folder",
                          command=lambda: self._show_in_folder(row.filepath))
        if st == "error" and row.url:
            m.add_command(label="↻  Retry", command=lambda: self.retry_row(row))
        if st in ("complete", "error", "removed"):
            m.add_command(label="🗑  Remove from list",
                          command=lambda: self.remove_row(row))
        if row.url:
            m.add_separator()
            m.add_command(label="🔗  Copy link",
                          command=lambda: self._copy(row.url))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _rpc(self, method, *args):
        try:
            self.aria.call(method, *args)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def play_file(self, path, partial=False):
        """Open a video in an external player; works on still-downloading files."""
        if not path or not os.path.exists(path):
            messagebox.showwarning("Not ready", "The file is not on disk yet.")
            return
        player = find_player()
        if player:
            try:
                subprocess.Popen([player, path])
                return
            except OSError:
                pass
        if partial and not self._warned_partial:
            self._warned_partial = True
            messagebox.showinfo(
                "Playing an incomplete file",
                "No VLC/mpv found, so the system default player will be used.\n\n"
                "Default players often refuse a file that is still downloading. "
                "Install VLC (winget install VideoLAN.VLC) for reliable "
                "watch-while-downloading playback.")
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Could not play", str(e))

    def _open_path(self, path):
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showwarning("Not found", "File no longer exists.")

    def _show_in_folder(self, path):
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(self.settings["download_dir"])

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    # ---------- actions ----------

    def add_download(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://", "ftp://", "magnet:")):
            messagebox.showwarning("Invalid link", "Enter an http(s), ftp or magnet link.")
            return
        opts = {
            "max-connection-per-server": str(self.settings["connections_per_server"]),
            "split": str(self.settings["split"]),
            "continue": "true" if self.settings["continue_partial"] else "false",
        }
        try:
            self.aria.call("aria2.addUri", [url], opts)
            self.url_var.set("")
        except Exception as e:
            messagebox.showerror("Error", f"Could not add download:\n{e}")

    def cancel_row(self, row):
        gid = row.gid
        self._removed.add(gid)
        try:
            self.aria.call("aria2.forceRemove", gid)
        except Exception:
            pass
        # purge the stopped/removed result so it doesn't linger in aria2's list
        try:
            self.aria.call("aria2.removeDownloadResult", gid)
        except Exception:
            pass
        self.remove_row(row, purge=False)

    def retry_row(self, row):
        url = row.url
        self.remove_row(row)
        self.url_var.set(url)
        self.add_download()

    def remove_row(self, row, purge=True):
        if purge:
            try:
                self.aria.call("aria2.removeDownloadResult", row.gid)
            except Exception:
                pass
        self._removed.add(row.gid)
        if self.selected is row:
            self.selected = None
        self.rows.pop(row.gid, None)
        row.destroy()
        self._refresh_empty()

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def save_settings_ui(self):
        try:
            self.settings = {
                "download_dir": self.dir_var.get().strip() or DEFAULT_SETTINGS["download_dir"],
                "connections_per_server": max(1, min(16, self.conn_var.get())),
                "split": max(1, min(32, self.split_var.get())),
                "max_concurrent": max(1, min(10, self.jobs_var.get())),
                "max_speed": self.speed_var.get().strip() or "0",
                "continue_partial": self.cont_var.get(),
            }
        except tk.TclError:
            messagebox.showwarning("Settings", "Please enter valid numbers.")
            return
        os.makedirs(self.settings["download_dir"], exist_ok=True)
        save_settings(self.settings)
        try:
            self.aria.apply_settings(self.settings)
        except Exception as e:
            messagebox.showwarning("Settings", f"Saved, but aria2c update failed:\n{e}")
            return
        self.saved_lbl.config(text="Saved ✓")
        self.after(2500, lambda: self.saved_lbl.config(text=""))

    # ---------- polling ----------

    def _poll_loop(self):
        keys = ["gid", "status", "totalLength", "completedLength",
                "downloadSpeed", "files", "bittorrent", "errorMessage"]
        while self._polling:
            try:
                active = self.aria.call("aria2.tellActive", keys)
                waiting = self.aria.call("aria2.tellWaiting", 0, 100, keys)
                stopped = self.aria.call("aria2.tellStopped", 0, 100, keys)
                self.after(0, self._update_rows, active + waiting + stopped)
            except Exception:
                pass
            time.sleep(1)

    def _update_rows(self, items):
        if not self.winfo_exists():
            return
        total_speed = 0
        n_active = 0
        for it in items:
            gid = it["gid"]
            if gid in self._removed:
                continue
            if it.get("status") == "active":
                n_active += 1
                total_speed += int(it.get("downloadSpeed", 0))
            row = self.rows.get(gid)
            if row is None:
                row = DownloadRow(self.list_frame, self, gid)
                row.pack(fill="x", pady=(0, 8))
                self.rows[gid] = row
            row.update_data(it)
        self._refresh_empty()
        if n_active:
            self.global_lbl.config(
                text=f"{n_active} active   •   {fmt_size(total_speed)}/s total")
        else:
            done = sum(1 for r in self.rows.values() if r.status == "complete")
            self.global_lbl.config(
                text=f"{done} completed" if done else "Ready")

    def _refresh_empty(self):
        if self.rows:
            self.empty_lbl.pack_forget()
        elif not self.empty_lbl.winfo_ismapped():
            self.empty_lbl.pack(fill="x", pady=40)

    def on_close(self):
        self._polling = False
        self.aria.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
