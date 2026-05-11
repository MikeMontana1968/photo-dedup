"""
Dedup-and-copy archive cleaner.

Walks a source root, computes SHA256 of every file, and copies UNIQUE
well-formed files to a destination root, preserving relative paths under
a source-tag subfolder. Hash DB is persisted in SQLite so subsequent
runs against other source drives dedup against everything seen so far.

Usage:
    python dedup_copy.py --source "E:\\Pictures" --dest "D:\\Pictures-Cleanup"

Re-running with the same source is safe and resumes where it left off.
"""

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile

# Pillow: don't bail on partial image errors during verify -- we want
# verify() to actually catch them, not raise on truncation.
ImageFile.LOAD_TRUNCATED_IMAGES = False

# --- file category sets (lowercase, with dot) ---

# Validated with Pillow.verify()+load(); copied if valid.
IMAGE_VALIDATE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png",
    ".gif",
    ".bmp", ".dib",
    ".tiff", ".tif",
    ".webp",
    ".ico",
    ".psd",
    ".thm",  # Canon thumbnail (JPEG)
}

# Image formats Pillow can't reliably validate -- copied as-is, no validation.
IMAGE_COPY_ONLY_EXTS = {
    ".heic", ".heif",
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng",
    ".orf", ".rw2", ".sr2", ".srf", ".pef", ".raf", ".rwl",
    ".nrw", ".x3f", ".kdc", ".dcr", ".mrw",
}

VIDEO_EXTS = {
    ".mp4", ".m4v",
    ".mov",
    ".avi",
    ".mkv",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg", ".mpeg", ".mpe",
    ".3gp", ".3g2",
    ".vob",
    ".mts", ".m2ts", ".ts",
    ".lrv",  # GoPro low-res proxy
    ".asf",
    ".rm", ".rmvb",
    ".divx",
    ".f4v",
    ".ogv",
}

AUDIO_EXTS = {
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg", ".oga",
    ".wma",
    ".aiff", ".aif",
    ".opus",
    ".amr",
    ".mka",
    ".ape",
    ".alac",
    ".mid", ".midi",
}

ALL_KEEP_EXTS = (
    IMAGE_VALIDATE_EXTS
    | IMAGE_COPY_ONLY_EXTS
    | VIDEO_EXTS
    | AUDIO_EXTS
)

CHUNK = 1024 * 1024  # 1 MiB read chunks
HEARTBEAT_EVERY = 100  # log every N files


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def safe_relpath(file_path, root):
    """Return relative path of file_path under root, with forward slashes preserved as on disk."""
    return os.path.relpath(file_path, root)


def sanitize_tag(s):
    bad = '<>:"/\\|?* '
    return "".join("_" if c in bad else c for c in s).strip("_") or "src"


def derive_source_tag(source_root):
    p = Path(source_root)
    # "E:\Pictures" -> "E_Pictures"
    drive = p.drive.replace(":", "")
    rest = "_".join(p.parts[1:]) if len(p.parts) > 1 else ""
    tag = f"{drive}_{rest}" if rest else drive
    return sanitize_tag(tag) or "src"


def open_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hashes (
            hash TEXT PRIMARY KEY,
            first_source_path TEXT NOT NULL,
            dest_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed (
            source_path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            hash TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            processed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_processed_status ON processed(status);
    """)
    conn.commit()
    return conn


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_image(path):
    """Returns (ok, detail). Uses Pillow verify() then a fresh load() to be sure."""
    try:
        with Image.open(path) as im:
            im.verify()  # checks structure
        # verify() leaves the image unusable -- reopen for actual decode
        with Image.open(path) as im2:
            im2.load()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class CsvLog:
    """Append-only CSV writer with header on first open, line-buffered."""

    def __init__(self, path, header):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = open(self.path, "a", newline="", encoding="utf-8", buffering=1)
        self._w = csv.writer(self._fh)
        if new:
            self._w.writerow(header)
            self._fh.flush()

    def write(self, row):
        self._w.writerow(row)

    def close(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


def categorize(ext):
    e = ext.lower()
    if e in IMAGE_VALIDATE_EXTS:
        return "image_validate"
    if e in IMAGE_COPY_ONLY_EXTS:
        return "image_copy_only"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    return "other"


def collision_free_dest(dest_path):
    """If dest_path exists, append _1, _2, ... before the extension."""
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    i = 1
    while True:
        cand = parent / f"{stem}__{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def run(source_root, dest_root, source_tag=None, limit=None):
    source_root = str(Path(source_root).resolve())
    dest_root = Path(dest_root).resolve()
    dedup_dir = dest_root / "_dedup"
    dedup_dir.mkdir(parents=True, exist_ok=True)

    if not source_tag:
        source_tag = derive_source_tag(source_root)
    dest_tag_root = dest_root / source_tag

    db_path = dedup_dir / "hashes.sqlite"
    progress_log = dedup_dir / "progress.log"

    # Per-source-tag report files (so each drive's run gets its own reports)
    rep_dir = dedup_dir / source_tag
    rep_dir.mkdir(parents=True, exist_ok=True)

    log_dup = CsvLog(rep_dir / "exceptions_duplicates.csv",
                     ["source_path", "size", "hash", "duplicate_of_dest", "duplicate_of_source", "logged_at"])
    log_invalid = CsvLog(rep_dir / "exceptions_invalid_images.csv",
                         ["source_path", "size", "ext", "error", "logged_at"])
    log_unsupported = CsvLog(rep_dir / "exceptions_unsupported.csv",
                             ["source_path", "size", "ext", "logged_at"])
    log_errors = CsvLog(rep_dir / "exceptions_errors.csv",
                        ["source_path", "size", "error", "logged_at"])
    log_copied = CsvLog(rep_dir / "copied.csv",
                        ["source_path", "dest_path", "size", "hash", "category", "logged_at"])

    plog = open(progress_log, "a", encoding="utf-8", buffering=1)

    def pmsg(msg):
        line = f"[{now_iso()}] {msg}"
        plog.write(line + "\n")
        print(line, flush=True)

    pmsg(f"=== run start: source={source_root} dest={dest_root} tag={source_tag} ===")

    conn = open_db(db_path)
    cur = conn.cursor()

    # Counters
    n_seen = 0
    n_skipped_resume = 0
    n_copied = 0
    n_dup = 0
    n_invalid = 0
    n_unsupported = 0
    n_error = 0
    bytes_copied = 0

    t0 = time.time()
    last_heartbeat = t0
    heartbeat_files_at_last = 0

    try:
        for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
            # Skip Windows recycle bin if it's somehow inside the source
            dirnames[:] = [d for d in dirnames if d.lower() not in {"$recycle.bin", "system volume information"}]
            for fname in filenames:
                if limit is not None and n_seen >= limit:
                    pmsg(f"hit limit={limit}, stopping enumeration")
                    raise StopIteration
                src_path = os.path.join(dirpath, fname)
                n_seen += 1

                # Periodic heartbeat
                now = time.time()
                if n_seen % HEARTBEAT_EVERY == 0 or (now - last_heartbeat) > 30:
                    rate = (n_seen - heartbeat_files_at_last) / max(0.001, now - last_heartbeat)
                    pmsg(f"seen={n_seen} copied={n_copied} dup={n_dup} invalid={n_invalid} "
                         f"unsupported={n_unsupported} error={n_error} resume_skip={n_skipped_resume} "
                         f"bytes_copied={bytes_copied} rate={rate:.1f} files/s")
                    last_heartbeat = now
                    heartbeat_files_at_last = n_seen

                try:
                    st = os.stat(src_path, follow_symlinks=False)
                except OSError as e:
                    n_error += 1
                    log_errors.write([src_path, "", f"stat failed: {e}", now_iso()])
                    continue

                size = st.st_size
                mtime = st.st_mtime

                # Skip symlinks / non-regular files
                if not os.path.isfile(src_path) or os.path.islink(src_path):
                    continue

                # Resume check: already processed with same size+mtime?
                cur.execute(
                    "SELECT status FROM processed WHERE source_path=? AND size=? AND mtime=?",
                    (src_path, size, mtime),
                )
                row = cur.fetchone()
                if row is not None:
                    n_skipped_resume += 1
                    continue

                ext = os.path.splitext(fname)[1]
                cat = categorize(ext)

                if cat == "other":
                    log_unsupported.write([src_path, size, ext, now_iso()])
                    cur.execute(
                        "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                        (src_path, size, mtime, None, "not_supported", ext, now_iso()),
                    )
                    conn.commit()
                    n_unsupported += 1
                    continue

                # Compute hash
                try:
                    h = hash_file(src_path)
                except OSError as e:
                    n_error += 1
                    log_errors.write([src_path, size, f"hash read failed: {e}", now_iso()])
                    cur.execute(
                        "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                        (src_path, size, mtime, None, "error", str(e), now_iso()),
                    )
                    conn.commit()
                    continue

                # Dedup check across ALL prior runs
                cur.execute(
                    "SELECT first_source_path, dest_path FROM hashes WHERE hash=?",
                    (h,),
                )
                hit = cur.fetchone()
                if hit is not None:
                    first_source, first_dest = hit
                    log_dup.write([src_path, size, h, first_dest, first_source, now_iso()])
                    cur.execute(
                        "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                        (src_path, size, mtime, h, "duplicate", first_dest, now_iso()),
                    )
                    conn.commit()
                    n_dup += 1
                    continue

                # Validate if it's a Pillow-supported image
                if cat == "image_validate":
                    ok, detail = validate_image(src_path)
                    if not ok:
                        log_invalid.write([src_path, size, ext, detail, now_iso()])
                        cur.execute(
                            "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                            (src_path, size, mtime, h, "invalid_image", detail, now_iso()),
                        )
                        conn.commit()
                        n_invalid += 1
                        continue

                # Compute destination path
                rel = safe_relpath(src_path, source_root)
                dest_path = dest_tag_root / rel
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path = collision_free_dest(dest_path)

                # Copy
                try:
                    shutil.copy2(src_path, dest_path)
                except OSError as e:
                    n_error += 1
                    log_errors.write([src_path, size, f"copy failed: {e}", now_iso()])
                    cur.execute(
                        "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                        (src_path, size, mtime, h, "error", f"copy failed: {e}", now_iso()),
                    )
                    conn.commit()
                    # Try to clean up partial
                    try:
                        if dest_path.exists():
                            dest_path.unlink()
                    except OSError:
                        pass
                    continue

                # Record success
                cur.execute(
                    "INSERT OR IGNORE INTO hashes VALUES (?,?,?,?,?)",
                    (h, src_path, str(dest_path), size, now_iso()),
                )
                cur.execute(
                    "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                    (src_path, size, mtime, h, "copied", str(dest_path), now_iso()),
                )
                conn.commit()
                log_copied.write([src_path, str(dest_path), size, h, cat, now_iso()])
                n_copied += 1
                bytes_copied += size

    except StopIteration:
        pass
    except KeyboardInterrupt:
        pmsg("interrupted by user")
    except Exception:
        pmsg("UNHANDLED EXCEPTION:\n" + traceback.format_exc())
        raise
    finally:
        elapsed = time.time() - t0
        pmsg(f"=== run end: seen={n_seen} copied={n_copied} dup={n_dup} "
             f"invalid={n_invalid} unsupported={n_unsupported} error={n_error} "
             f"resume_skip={n_skipped_resume} bytes_copied={bytes_copied} "
             f"elapsed={elapsed:.1f}s ===")
        for lg in (log_dup, log_invalid, log_unsupported, log_errors, log_copied):
            lg.close()
        conn.commit()
        conn.close()
        plog.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--source-tag", default=None,
                   help="Subfolder name under dest (default: derived from source path)")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N files (for smoke testing)")
    args = p.parse_args()
    run(args.source, args.dest, source_tag=args.source_tag, limit=args.limit)


if __name__ == "__main__":
    main()
