# main.py
# Control panel for the whole satellite image dataset workflow.
#
#   .venv\Scripts\python.exe main.py   (Windows)
#   .venv/bin/python main.py           (macOS/Linux/Ubuntu)
#
# One window, the full pipeline in order:
#
#   1. Fetch catalog     satellite_scraper.py  (auto-chains variants.py after)
#   2. Assign variants   variants.py           (adds VARIANT_NAME column)
#   3. Scrape images     image_pipeline.py     (pick sources with checkboxes)
#   4. CLIP filter       sources/clip_filter.py
#   5. Manual review     review_gui.py         (opens its own window)
#   6. Coverage report   coverage_report.py
#   7. Build labels      build_labels.py       (image_labels.csv manifest)
#   8. Flatten images    flatten_images.py     (copies into flat_images/)
#
# Each step shows when it last ran (from its output file's mtime) and an
# amber "stale" marker when the dataset changed after that output was made.
# Steps run as subprocesses streaming into the log pane; Stop kills the
# whole process tree (needed on Windows, where terminate() would leave the
# scraper's per-source children running). All tools are resume-safe.

import csv as _csv
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

if sys.stdout:  # None under pythonw.exe (no console)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT      = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "satellite_images"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}

BG      = "#1e1e1e"
PANEL   = "#252526"
FG      = "#e0e0e0"
DIM     = "#9a9a9a"
ACCENT  = "#2e7d32"
RUNNING = "#e65100"
STALE   = "#ffb74d"
ERROR   = "#ef5350"
LOG_BG  = "#111111"
LOG_FG  = "#c8e6c9"

SOURCES = ["satnogs", "nasa_images", "gunters_space", "wikimedia",
           "wikipedia", "wikidata", "flickr"]

PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")
PREFIX_RE   = re.compile(r"^\[(\w+)\]")


def kill_tree(proc):
    """Kill a subprocess and all of its children."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        proc.terminate()


def rel_time(mtime):
    if mtime is None:
        return "never"
    s = time.time() - mtime
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)}m ago"
    if s < 172800:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def mtime_of(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class Toolkit:
    def __init__(self, master):
        self.master = master
        self.proc   = None
        self.chain  = []      # queued follow-up tasks (arg lists)
        self.queue  = queue.Queue()
        self.buttons = []
        self.step_status = {}         # key -> ttk.Label
        self.progress_frac = {}       # source prefix -> fraction done

        master.title("Satellite Dataset Toolkit")
        master.geometry("1200x800")
        master.configure(bg=BG)
        master.minsize(950, 620)
        master.protocol("WM_DELETE_WINDOW", self.on_close)

        self._init_style()

        # ── Header ────────────────────────────────────────────────────
        header = tk.Frame(master, bg=BG)
        header.pack(fill=tk.X, padx=14, pady=(12, 4))
        tk.Label(header, text="Satellite Dataset Toolkit",
                 font=("Segoe UI", 18, "bold"), fg="white", bg=BG).pack(side=tk.LEFT)
        self.stats_label = tk.Label(header, text="computing stats...",
                                    font=("Segoe UI", 10), fg=DIM, bg=BG)
        self.stats_label.pack(side=tk.RIGHT)

        # ── Paned body: steps | log ───────────────────────────────────
        paned = tk.PanedWindow(master, orient=tk.HORIZONTAL, bg=BG,
                               sashwidth=6, sashrelief=tk.FLAT, bd=0)
        paned.pack(fill=tk.BOTH, expand=True, padx=14, pady=8)

        steps = tk.Frame(paned, bg=PANEL)
        paned.add(steps, minsize=430)

        self.add_step(steps, "fetch", "1", "Fetch catalog",
                      "Space-Track → satellites.csv (+ variants)",
                      chain=[["satellite_scraper.py"], ["variants.py"]])
        self.add_step(steps, "variants", "2", "Assign variants",
                      "Split families into hardware generations",
                      chain=[["variants.py"]])

        # Step 3 with source checkboxes
        f = tk.Frame(steps, bg=PANEL)
        f.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.add_step_button(f, "scrape", "3", "Scrape images",
                             "Run the selected sources in parallel",
                             self.run_scrape)
        grid = tk.Frame(steps, bg=PANEL)
        grid.pack(fill=tk.X, padx=26, pady=(0, 4))
        self.source_vars = {}
        for i, src in enumerate(SOURCES):
            var = tk.BooleanVar(value=(src != "flickr"))
            self.source_vars[src] = var
            tk.Checkbutton(grid, text=src, variable=var, font=("Segoe UI", 9),
                           fg=FG, bg=PANEL, selectcolor=BG,
                           activebackground=PANEL, activeforeground=FG
                           ).grid(row=i // 2, column=i % 2, sticky="w")

        self.add_step(steps, "clip", "4", "CLIP filter",
                      "Auto-reject non-satellite images",
                      chain=[[str(Path("sources") / "clip_filter.py")]])
        self.add_step(steps, "review", "5", "Manual review",
                      "Left=reject · Right=keep · D=duplicate",
                      chain=[["review_gui.py"]])
        self.add_step(steps, "coverage", "6", "Coverage report",
                      "Per-family × per-source coverage",
                      chain=[["coverage_report.py"]])
        self.add_step(steps, "labels", "7", "Build labels",
                      "image_labels.csv (family + variant tiers)",
                      chain=[["build_labels.py"]])
        self.add_step(steps, "flatten", "8", "Flatten images",
                      "Copy everything into flat_images/",
                      chain=[["flatten_images.py"]])

        # Flickr key row
        keyrow = tk.Frame(steps, bg=PANEL)
        keyrow.pack(fill=tk.X, padx=10, pady=(14, 10), side=tk.BOTTOM)
        tk.Label(keyrow, text="Flickr API key:", font=("Segoe UI", 9),
                 fg=DIM, bg=PANEL).pack(side=tk.LEFT)
        self.key_entry = tk.Entry(keyrow, show="•", width=24, bg="#333333",
                                  fg=FG, insertbackground=FG, relief=tk.FLAT)
        self.key_entry.pack(side=tk.LEFT, padx=6)
        ttk.Button(keyrow, text="Save", style="Small.TButton",
                   command=self.save_flickr_key).pack(side=tk.LEFT)
        self.key_status = tk.Label(keyrow, text="", font=("Segoe UI", 9),
                                   fg=ACCENT, bg=PANEL)
        self.key_status.pack(side=tk.LEFT, padx=6)
        self.update_key_status()

        # ── Right: status, progress, log ──────────────────────────────
        right = tk.Frame(paned, bg=BG)
        paned.add(right)

        bar = tk.Frame(right, bg=BG)
        bar.pack(fill=tk.X)
        self.status_label = tk.Label(bar, text="idle", font=("Segoe UI", 11, "bold"),
                                     fg=DIM, bg=BG)
        self.status_label.pack(side=tk.LEFT)
        ttk.Button(bar, text="Clear log", style="Small.TButton",
                   command=self.clear_log).pack(side=tk.RIGHT, padx=4)
        self.stop_btn = ttk.Button(bar, text="■ Stop", style="Stop.TButton",
                                   state=tk.DISABLED, command=self.stop_task)
        self.stop_btn.pack(side=tk.RIGHT, padx=4)

        prow = tk.Frame(right, bg=BG)
        prow.pack(fill=tk.X, pady=(4, 0))
        self.progress = ttk.Progressbar(prow, mode="determinate", maximum=1.0,
                                        style="Task.Horizontal.TProgressbar")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = tk.Label(prow, text="", font=("Segoe UI", 9),
                                       fg=DIM, bg=BG, width=12, anchor="e")
        self.progress_label.pack(side=tk.LEFT, padx=(6, 0))

        logframe = tk.Frame(right, bg=BG)
        logframe.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log = tk.Text(logframe, bg=LOG_BG, fg=LOG_FG, insertbackground=FG,
                           font=("Consolas", 9), state=tk.DISABLED, wrap="none")
        scroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.master.after(150, self.drain_queue)
        threading.Thread(target=self.refresh_stats, daemon=True).start()

    def _init_style(self):
        style = ttk.Style(self.master)
        style.theme_use("clam")
        style.configure("Step.TButton", font=("Segoe UI", 11, "bold"),
                        background="#333333", foreground="white",
                        borderwidth=0, focuscolor=PANEL, padding=(10, 6), anchor="w")
        style.map("Step.TButton",
                  background=[("active", ACCENT), ("disabled", "#2a2a2a")],
                  foreground=[("disabled", "#666666")])
        style.configure("Small.TButton", font=("Segoe UI", 9),
                        background="#444444", foreground="white",
                        borderwidth=0, focuscolor=BG, padding=(8, 3))
        style.map("Small.TButton", background=[("active", "#5a5a5a")])
        style.configure("Stop.TButton", font=("Segoe UI", 9, "bold"),
                        background="#c62828", foreground="white",
                        borderwidth=0, focuscolor=BG, padding=(8, 3))
        style.map("Stop.TButton",
                  background=[("active", "#e53935"), ("disabled", "#4a2626")])
        style.configure("Task.Horizontal.TProgressbar", troughcolor=PANEL,
                        background=ACCENT, borderwidth=0, thickness=8)

    # ── Step widgets ──────────────────────────────────────────────────

    def add_step(self, parent, key, num, title, subtitle, chain):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.add_step_button(f, key, num, title, subtitle,
                             lambda c=chain: self.run_chain(list(c)))

    def add_step_button(self, frame, key, num, title, subtitle, command):
        btn = ttk.Button(frame, text=f"{num}  {title}", width=20,
                         style="Step.TButton", command=command)
        btn.pack(side=tk.LEFT)
        col = tk.Frame(frame, bg=PANEL)
        col.pack(side=tk.LEFT, padx=8, fill=tk.X)
        tk.Label(col, text=subtitle, font=("Segoe UI", 9), fg=DIM, bg=PANEL,
                 anchor="w").pack(anchor="w")
        status = tk.Label(col, text="", font=("Segoe UI", 8), fg=DIM, bg=PANEL,
                          anchor="w")
        status.pack(anchor="w")
        self.buttons.append(btn)
        self.step_status[key] = status

    # ── Task execution ────────────────────────────────────────────────

    def run_scrape(self):
        picked = [s for s, v in self.source_vars.items() if v.get()]
        if not picked:
            self.append_log("! no sources selected\n")
            return
        args = ["image_pipeline.py"]
        if len(picked) < len(SOURCES):
            args += ["--only", ",".join(picked)]
        self.run_chain([args])

    def run_chain(self, chain):
        """Run a list of tasks back-to-back (next starts only on exit 0)."""
        if self.proc is not None:
            self.append_log("! a task is already running — stop it first\n")
            return
        self.chain = chain
        self.start_next_in_chain()

    def start_next_in_chain(self):
        script_args = self.chain.pop(0)
        cmd  = [sys.executable, "-u"] + script_args
        name = Path(script_args[0]).stem
        self.append_log(f"\n{'=' * 70}\n> {' '.join(script_args)}\n{'=' * 70}\n")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONUTF8": "1"})
        except Exception as e:
            self.append_log(f"! failed to start: {e}\n")
            self.proc = None
            self.chain = []
            return

        self.progress_frac = {}
        self.progress["value"] = 0
        self.progress_label.config(text="")
        self.status_label.config(text=f"running: {name}", fg=RUNNING)
        self.stop_btn.config(state=tk.NORMAL)
        for b in self.buttons:
            b.config(state=tk.DISABLED)
        threading.Thread(target=self.reader_thread, args=(self.proc,),
                         daemon=True).start()

    def reader_thread(self, proc):
        for line in proc.stdout:
            self.queue.put(line)
        proc.wait()
        self.queue.put(("__DONE__", proc.returncode))

    def drain_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    self.task_finished(item[1])
                else:
                    self.track_progress(item)
                    self.append_log(item)
        except queue.Empty:
            pass
        self.master.after(150, self.drain_queue)

    def track_progress(self, line):
        m = PROGRESS_RE.search(line)
        if not m:
            return
        n, total = int(m.group(1)), int(m.group(2))
        if total <= 0:
            return
        pm = PREFIX_RE.match(line)
        key = pm.group(1) if pm else "_"
        self.progress_frac[key] = n / total
        frac = sum(self.progress_frac.values()) / len(self.progress_frac)
        self.progress["value"] = frac
        self.progress_label.config(text=f"{frac * 100:.0f}%")

    def task_finished(self, code):
        self.proc = None
        if code == 0 and self.chain:
            self.append_log("\n-- ok, continuing --\n")
            self.start_next_in_chain()
            return
        if code == 0:
            self.append_log("\n-- finished --\n")
            self.status_label.config(text="idle", fg=DIM)
        else:
            self.append_log(f"\n-- FAILED (exit code {code}) --\n")
            self.status_label.config(text=f"failed (exit code {code})", fg=ERROR)
            self.chain = []
        self.stop_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0
        self.progress_label.config(text="")
        for b in self.buttons:
            b.config(state=tk.NORMAL)
        threading.Thread(target=self.refresh_stats, daemon=True).start()

    def stop_task(self):
        if self.proc is not None:
            self.append_log("\n-- stopping task tree (progress is saved) --\n")
            self.chain = []
            kill_tree(self.proc)

    def on_close(self):
        kill_tree(self.proc)
        self.master.destroy()

    # ── Flickr key ────────────────────────────────────────────────────

    def save_flickr_key(self):
        key = self.key_entry.get().strip()
        if key:
            (ROOT / ".flickr_api_key").write_text(key, encoding="utf-8")
            self.key_entry.delete(0, tk.END)
        self.update_key_status()

    def update_key_status(self):
        have = bool(os.environ.get("FLICKR_API_KEY", "").strip()
                    or (ROOT / ".flickr_api_key").exists())
        self.key_status.config(text="✓ key set" if have else "not set",
                               fg=ACCENT if have else DIM)

    # ── Log + stats ───────────────────────────────────────────────────

    def append_log(self, text):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        if int(self.log.index("end-1c").split(".")[0]) > 6000:
            self.log.delete("1.0", "2000.0")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def clear_log(self):
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)

    def refresh_stats(self):
        # Walk the dataset once: counts + newest image mtime
        n_images, covered, latest_img = 0, 0, 0
        if IMAGE_DIR.exists():
            for d in IMAGE_DIR.iterdir():
                if not d.is_dir():
                    continue
                n = 0
                for f in d.iterdir():
                    if (f.suffix.lower() in IMAGE_EXTENSIONS
                            and not f.name.startswith("._")):
                        n += 1
                        m = f.stat().st_mtime
                        if m > latest_img:
                            latest_img = m
                if n:
                    covered += 1
                    n_images += n

        n_families = 0
        has_variants = False
        csv_path = ROOT / "satellites.csv"
        if csv_path.exists():
            with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
                reader = _csv.DictReader(f)
                has_variants = "VARIANT_NAME" in (reader.fieldnames or [])
                n_families = len({r["FAMILY_NAME"] for r in reader
                                  if r.get("FAMILY_NAME", "").strip()})

        clip_log   = mtime_of(ROOT / "clip_filter_log.txt")
        review_log = mtime_of(ROOT / "manual_review_log.csv")
        # any of these means the dataset contents changed
        dataset_changed = max([m for m in (latest_img or None, clip_log, review_log)
                               if m is not None], default=None)

        def status(mtime, stale_after=None, missing_note=None):
            if mtime is None:
                return (missing_note or "never run", DIM)
            text = f"last run: {rel_time(mtime)}"
            if stale_after and mtime < stale_after:
                return (text + "  ·  stale", STALE)
            return (text, DIM)

        scrape_logs = [mtime_of(p) for p in ROOT.glob("no_images_found*.txt")]
        scrape_mt   = max([m for m in scrape_logs if m], default=latest_img or None)

        results = {
            "fetch":    status(mtime_of(csv_path)),
            "variants": (("column missing — run now!", ERROR) if not has_variants
                         else status(mtime_of(csv_path))),
            "scrape":   status(scrape_mt),
            "clip":     status(clip_log, stale_after=latest_img or None),
            "review":   status(review_log),
            "coverage": status(mtime_of(ROOT / "coverage_report.csv"),
                               stale_after=dataset_changed),
            "labels":   status(mtime_of(ROOT / "image_labels.csv"),
                               stale_after=dataset_changed),
            "flatten":  status(mtime_of(ROOT / "flat_images" / "flat_images_manifest.csv"),
                               stale_after=dataset_changed),
        }

        pct  = f" ({covered / n_families * 100:.0f}%)" if n_families else ""
        text = f"{n_images:,} images  ·  {covered}/{n_families} families covered{pct}"

        def apply():
            self.stats_label.config(text=text)
            for key, (msg, color) in results.items():
                if key in self.step_status:
                    self.step_status[key].config(text=msg, fg=color)
            self.update_key_status()
        self.master.after(0, apply)


if __name__ == "__main__":
    root = tk.Tk()
    app = Toolkit(root)
    root.mainloop()
