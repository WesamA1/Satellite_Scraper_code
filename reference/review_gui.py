# review_gui.py
# Manual review GUI for downloaded satellite images.
#
#   .venv\Scripts\python.exe review_gui.py                 (Windows)
#   .venv/bin/python review_gui.py                         (macOS/Linux/Ubuntu)
#   .venv/bin/python review_gui.py --dupes-first           # suspected duplicates first
#   (Linux/Ubuntu: tkinter needs `sudo apt install python3-tk`, not pip-installable)
#
# Shows one image at a time:
#   Right arrow / click right half / Yes button  -> KEEP (stays in place)
#   Left arrow  / click left half  / No button   -> REJECT (moved to rejected_images/)
#   Down arrow or D / Duplicate button           -> DUPLICATE (moved to duplicate_images/)
#   Z or Backspace                               -> undo last decision
#   Q or Escape                                  -> quit (progress is saved)
#
# Suspected duplicates are detected automatically with a perceptual hash
# (dHash): near-identical images within the same family, and exact matches
# across families. When the current image has suspects, an amber banner
# appears and the matching images are shown as thumbnails below it so you
# can compare before pressing D.
#
# Decisions are appended to manual_review_log.csv, so reviewed images are
# never shown again — quit and rerun any time. Rejected files go to
# rejected_images/<FAMILY>/ (same place the CLIP filter puts its rejects),
# duplicates go to duplicate_images/<FAMILY>/. Nothing is hard-deleted, so
# undo (Z) can always restore the last decision.

import argparse
import csv
import shutil
import sys
import time
import tkinter as tk
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageTk

# Windows consoles/pipes default to cp1252, which can't encode ⚠/✓/⧉.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT          = Path(__file__).resolve().parent
IMAGE_DIR     = ROOT / "satellite_images"
REJECT_DIR    = ROOT / "rejected_images"
DUPLICATE_DIR = ROOT / "duplicate_images"
LOG_FILE      = ROOT / "manual_review_log.csv"
HASH_CACHE    = ROOT / "image_hashes.csv"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}

KEEP_COLOR      = "#2e7d32"   # green flash on keep
REJECT_COLOR    = "#c62828"   # red flash on reject
DUPLICATE_COLOR = "#e65100"   # amber flash on duplicate
BG_COLOR        = "#1e1e1e"

# Max Hamming distance (out of 64 bits) for two same-family images to be
# flagged as suspected duplicates. Across families only exact matches are
# flagged, since sister programs legitimately share similar-looking photos.
MAX_HAMMING = 8

SUSPECT_THUMB_H = 130   # height of the suspect-comparison strip


# ── Perceptual hashing ────────────────────────────────────────────────────────

def dhash(path, hash_size=8):
    """64-bit difference hash of an image, or None if unreadable."""
    try:
        img = Image.open(path).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS)
    except Exception:
        return None
    px = list(img.getdata())
    w  = hash_size + 1
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            bits = (bits << 1) | (px[row * w + col] > px[row * w + col + 1])
    return bits


def _load_hash_cache():
    cache = {}
    if HASH_CACHE.exists():
        with open(HASH_CACHE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cache[row["path"]] = (int(row["mtime"]), int(row["size"]),
                                      int(row["hash"], 16))
    return cache


def compute_hashes(paths):
    """path -> 64-bit hash for every readable image, using a cache keyed on
    (path, mtime, size) so subsequent launches are instant."""
    cache   = _load_hash_cache()
    hashes  = {}
    new_rows = []
    n_computed = 0

    for i, p in enumerate(paths):
        stat = p.stat()
        key  = str(p.relative_to(ROOT))
        cached = cache.get(key)
        if cached and cached[0] == int(stat.st_mtime) and cached[1] == stat.st_size:
            hashes[p] = cached[2]
            continue
        h = dhash(p)
        n_computed += 1
        if n_computed % 500 == 0:
            print(f"  hashing... {i + 1}/{len(paths)}")
        if h is not None:
            hashes[p] = h
        cache[key] = (int(stat.st_mtime), stat.st_size, h if h is not None else 0)

    with open(HASH_CACHE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["path", "mtime", "size", "hash"])
        writer.writeheader()
        for key, (mtime, size, h) in cache.items():
            writer.writerow({"path": key, "mtime": mtime, "size": size,
                             "hash": format(h, "x")})
    if n_computed:
        print(f"  hashed {n_computed} new images (cache: {HASH_CACHE.name})")
    return hashes


def find_duplicate_suspects(hashes):
    """Return {path: [(other_path, hamming_distance), ...]} for suspects.

    Near-duplicates (distance <= MAX_HAMMING) are searched within each
    family; exact matches (distance 0) are also searched across families.
    """
    suspects = defaultdict(dict)

    # Exact matches anywhere
    by_hash = defaultdict(list)
    for p, h in hashes.items():
        by_hash[h].append(p)
    for group in by_hash.values():
        if len(group) > 1:
            for p in group:
                for q in group:
                    if p != q:
                        suspects[p][q] = 0

    # Near matches within a family
    by_family = defaultdict(list)
    for p, h in hashes.items():
        by_family[p.parent.name].append((p, h))
    for members in by_family.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                p, hp = members[i]
                q, hq = members[j]
                d = bin(hp ^ hq).count("1")
                if 0 < d <= MAX_HAMMING:
                    if d < suspects[p].get(q, 99):
                        suspects[p][q] = d
                        suspects[q][p] = d

    return {p: sorted(matches.items(), key=lambda kv: kv[1])
            for p, matches in suspects.items()}


# ── Decision log ──────────────────────────────────────────────────────────────

def load_reviewed():
    """Return the set of (family, filename) pairs already reviewed."""
    reviewed = set()
    if LOG_FILE.exists():
        with open(LOG_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                reviewed.add((row["family"], row["file"]))
    return reviewed


def append_log(family, filename, decision):
    new_file = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "family", "file", "decision"])
        if new_file:
            writer.writeheader()
        writer.writerow({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "family":    family,
            "file":      filename,
            "decision":  decision,
        })


def remove_last_log_entry():
    """Drop the last data row from the log (used by undo)."""
    if not LOG_FILE.exists():
        return
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > 1:
        lines.pop()
    LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── File operations ───────────────────────────────────────────────────────────

def scan_images(reviewed):
    """All unreviewed images, grouped so families are reviewed together."""
    items = []
    if not IMAGE_DIR.exists():
        return items
    for family_dir in sorted(IMAGE_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        for f in sorted(family_dir.iterdir()):
            if (f.suffix.lower() in IMAGE_EXTENSIONS
                    and not f.name.startswith("._")
                    and (family_dir.name, f.name) not in reviewed):
                items.append(f)
    return items


def scan_all_images():
    """Every image currently in the dataset (reviewed or not) — the hash /
    duplicate index covers all of them so kept images can still be matched."""
    items = []
    if not IMAGE_DIR.exists():
        return items
    for family_dir in sorted(IMAGE_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        for f in sorted(family_dir.iterdir()):
            if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith("._"):
                items.append(f)
    return items


def move_out(path, dest_root):
    """Move an image to dest_root/<family>/. Returns the new path."""
    dest = dest_root / path.parent.name / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():  # name collision (e.g. with a CLIP-filter reject)
        dest = dest.with_name(f"{dest.stem}_manual{dest.suffix}")
    shutil.move(str(path), str(dest))
    return dest


def reject_image(path):
    return move_out(path, REJECT_DIR)


def duplicate_image(path):
    return move_out(path, DUPLICATE_DIR)


def unreject_image(moved_path, original_path):
    """Move a rejected/duplicate image back where it came from (undo)."""
    original_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(moved_path), str(original_path))


# ── GUI ───────────────────────────────────────────────────────────────────────

class ReviewApp:
    def __init__(self, master, images, suspects=None):
        self.master   = master
        self.images   = images
        self.suspects = suspects or {}
        self.index    = 0
        self.kept     = 0
        self.dropped  = 0
        self.dupes    = 0
        # history of (original_path, decision, moved_path_or_None) for undo
        self.history = []
        self._pil_image   = None
        self._tk_image    = None
        self._suspect_tks = []
        self._resize_job  = None

        master.title("Satellite Image Review")
        master.geometry("1100x850")
        master.configure(bg=BG_COLOR)
        master.minsize(600, 500)

        self.family_label = tk.Label(master, font=("Segoe UI", 20, "bold"),
                                     fg="white", bg=BG_COLOR)
        self.family_label.pack(pady=(10, 0))

        self.info_label = tk.Label(master, font=("Segoe UI", 10),
                                   fg="#aaaaaa", bg=BG_COLOR)
        self.info_label.pack()

        self.dup_label = tk.Label(master, font=("Segoe UI", 11, "bold"),
                                  fg="#ffb74d", bg=BG_COLOR)
        self.dup_label.pack()

        self.canvas = tk.Canvas(master, bg=BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        button_bar = tk.Frame(master, bg=BG_COLOR)
        button_bar.pack(pady=(0, 12))
        tk.Button(button_bar, text="✗  No  (←)", width=13, font=("Segoe UI", 12),
                  bg=REJECT_COLOR, fg="white", relief=tk.FLAT,
                  command=lambda: self.decide("reject")).pack(side=tk.LEFT, padx=6)
        tk.Button(button_bar, text="⧉  Duplicate  (D / ↓)", width=18, font=("Segoe UI", 12),
                  bg=DUPLICATE_COLOR, fg="white", relief=tk.FLAT,
                  command=lambda: self.decide("duplicate")).pack(side=tk.LEFT, padx=6)
        tk.Button(button_bar, text="↩  Undo  (Z)", width=11, font=("Segoe UI", 12),
                  bg="#444444", fg="white", relief=tk.FLAT,
                  command=self.undo).pack(side=tk.LEFT, padx=6)
        tk.Button(button_bar, text="✓  Yes  (→)", width=13, font=("Segoe UI", 12),
                  bg=KEEP_COLOR, fg="white", relief=tk.FLAT,
                  command=lambda: self.decide("keep")).pack(side=tk.LEFT, padx=6)

        master.bind("<Left>",      lambda e: self.decide("reject"))
        master.bind("<Right>",     lambda e: self.decide("keep"))
        master.bind("<Down>",      lambda e: self.decide("duplicate"))
        master.bind("<d>",         lambda e: self.decide("duplicate"))
        master.bind("<z>",         lambda e: self.undo())
        master.bind("<BackSpace>", lambda e: self.undo())
        master.bind("<q>",         lambda e: master.destroy())
        master.bind("<Escape>",    lambda e: master.destroy())
        self.canvas.bind("<Button-1>",  self.on_click)
        self.canvas.bind("<Configure>", self.on_resize)

        self.show_current()

    # ── display ──────────────────────────────────────────────────────────

    def current_suspects(self):
        """Suspect matches for the current image that still exist on disk."""
        if self.index >= len(self.images):
            return []
        path = self.images[self.index]
        return [(p, d) for p, d in self.suspects.get(path, []) if p.exists()][:3]

    def show_current(self):
        self.canvas.delete("all")

        if self.index >= len(self.images):
            self.family_label.config(text="All done!")
            self.info_label.config(
                text=f"Session: {self.kept} kept, {self.dropped} rejected, "
                     f"{self.dupes} duplicates — press Q to quit")
            self.dup_label.config(text="")
            self._pil_image = None
            return

        path = self.images[self.index]
        try:
            self._pil_image = Image.open(path).convert("RGB")
        except Exception as e:
            # Unreadable file — log it as rejected and move on
            print(f"Could not open {path}: {e} — rejecting")
            self.decide("reject")
            return

        self.family_label.config(text=path.parent.name)
        self.info_label.config(
            text=f"{path.name}   ·   {self.index + 1} / {len(self.images)}   ·   "
                 f"session: {self.kept} kept / {self.dropped} rejected / "
                 f"{self.dupes} dupes")

        matches = self.current_suspects()
        if matches:
            names = ",  ".join(
                f"{p.parent.name}/{p.name}" + (f" (d={d})" if d else " (exact)")
                for p, d in matches)
            self.dup_label.config(text=f"⚠ suspected duplicate of: {names}")
        else:
            self.dup_label.config(text="")

        self.render_image()

    def render_image(self):
        if self._pil_image is None:
            return
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        self.canvas.delete("all")
        self._suspect_tks = []

        matches  = self.current_suspects()
        strip_h  = (SUSPECT_THUMB_H + 30) if matches else 0
        main_h   = max(ch - strip_h, 50)

        img = self._pil_image.copy()
        img.thumbnail((cw, main_h), Image.LANCZOS)
        self._tk_image = ImageTk.PhotoImage(img)
        self.canvas.create_image(cw // 2, main_h // 2, image=self._tk_image)

        # Suspect comparison strip along the bottom
        x = 10
        for p, d in matches:
            try:
                thumb = Image.open(p).convert("RGB")
            except Exception:
                continue
            thumb.thumbnail((220, SUSPECT_THUMB_H), Image.LANCZOS)
            tk_thumb = ImageTk.PhotoImage(thumb)
            self._suspect_tks.append(tk_thumb)
            y = ch - strip_h + 22
            label = f"{p.name[:32]}" + (f"  d={d}" if d else "  exact")
            self.canvas.create_text(x, y - 12, anchor="w", fill="#ffb74d",
                                    font=("Segoe UI", 9), text=label)
            self.canvas.create_image(x, y, anchor="nw", image=tk_thumb)
            x += tk_thumb.width() + 16

    def on_resize(self, _event):
        # Debounce: only re-render after resizing pauses
        if self._resize_job:
            self.master.after_cancel(self._resize_job)
        self._resize_job = self.master.after(100, self.render_image)

    def flash(self, color):
        self.canvas.configure(bg=color)
        self.master.after(120, lambda: self.canvas.configure(bg=BG_COLOR))

    # ── decisions ────────────────────────────────────────────────────────

    def on_click(self, event):
        if self.index >= len(self.images):
            return
        self.decide("keep" if event.x >= self.canvas.winfo_width() / 2 else "reject")

    def decide(self, decision):
        if self.index >= len(self.images):
            return
        path = self.images[self.index]

        if decision == "keep":
            moved = None
            self.kept += 1
            self.flash(KEEP_COLOR)
        else:
            dest_root = REJECT_DIR if decision == "reject" else DUPLICATE_DIR
            try:
                moved = move_out(path, dest_root)
            except Exception as e:
                print(f"Could not move {path}: {e}")
                return
            if decision == "reject":
                self.dropped += 1
                self.flash(REJECT_COLOR)
            else:
                self.dupes += 1
                self.flash(DUPLICATE_COLOR)

        append_log(path.parent.name, path.name, decision)
        self.history.append((path, decision, moved))
        self.index += 1
        self.show_current()

    def undo(self):
        if not self.history:
            return
        path, decision, moved = self.history.pop()
        if moved is not None:
            try:
                unreject_image(moved, path)
            except Exception as e:
                print(f"Could not undo move for {path}: {e}")
                return
        if decision == "keep":
            self.kept -= 1
        elif decision == "reject":
            self.dropped -= 1
        else:
            self.dupes -= 1
        remove_last_log_entry()
        self.index -= 1
        self.show_current()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual satellite image review.")
    parser.add_argument("--dupes-first", action="store_true",
                        help="show suspected duplicates before everything else")
    args = parser.parse_args()

    reviewed = load_reviewed()
    images   = scan_images(reviewed)

    print(f"{len(reviewed)} images already reviewed, {len(images)} to go")
    if not images:
        print("Nothing left to review!")
        raise SystemExit(0)

    print("Indexing duplicates (perceptual hash)...")
    hashes   = compute_hashes(scan_all_images())
    suspects = find_duplicate_suspects(hashes)
    flagged  = sum(1 for p in images if p in suspects)
    print(f"{flagged} of {len(images)} unreviewed images are suspected duplicates")

    if args.dupes_first:
        images.sort(key=lambda p: (p not in suspects, str(p)))

    root = tk.Tk()
    app  = ReviewApp(root, images, suspects)
    root.mainloop()

    print(f"Session finished: {app.kept} kept, {app.dropped} rejected, "
          f"{app.dupes} duplicates")
    print(f"Decisions logged to {LOG_FILE.name}")
