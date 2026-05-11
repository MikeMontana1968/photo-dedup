"""
Reorganize D:\\Pictures-Cleanup\\E_Pictures\\ in place.

For each file:
  - Hash it.
  - If its hash already exists in hashes.sqlite (because the canonical copy
    sits in a date folder or in the Takeout import), delete this E_Pictures
    copy as redundant.
  - Otherwise, determine the capture date (EXIF -> mtime fallback) and
    MOVE the file to YYYY\\YYYY-Mmm-DD\\filename. Add the new dest_path
    to the hash DB.

Empty subdirectories under E_Pictures\\ are removed at the end so the
tree collapses naturally once consolidation is complete.
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile, ExifTags

ImageFile.LOAD_TRUNCATED_IMAGES = False

EXIF_DATETIME_ORIGINAL = 36867
CHUNK = 1024 * 1024
HEARTBEAT_EVERY = 200

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_EXIF_DATE_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")

IMAGE_EXIF_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".gif", ".bmp", ".dib",
    ".tiff", ".tif", ".webp", ".psd", ".thm",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def parse_exif_date(path):
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            val = exif.get(EXIF_DATETIME_ORIGINAL)
            if not val and hasattr(ExifTags, "IFD"):
                sub = exif.get_ifd(ExifTags.IFD.Exif)
                if sub:
                    val = sub.get(EXIF_DATETIME_ORIGINAL)
            if not val:
                return None
            m = _EXIF_DATE_RE.match(str(val).strip())
            if not m:
                return None
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, s)
    except Exception:
        return None


def date_to_path_parts(dt):
    return f"{dt.year:04d}", f"{dt.year:04d}-{_MONTH_ABBR[dt.month-1]}-{dt.day:02d}"


def collision_free(p):
    if not p.exists():
        return p
    stem, suffix, parent = p.stem, p.suffix, p.parent
    i = 1
    while True:
        cand = parent / f"{stem}__{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def open_csv(path, header):
    new = not path.exists() or path.stat().st_size == 0
    fh = open(path, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(fh)
    if new:
        w.writerow(header)
        fh.flush()
    return fh, w


def run(src_root, dest_root, dry_run=False):
    src_root = Path(src_root).resolve()
    dest_root = Path(dest_root).resolve()
    dedup_dir = dest_root / "_dedup"
    rep_dir = dedup_dir / "reorganize_E_Pictures"
    rep_dir.mkdir(parents=True, exist_ok=True)

    db_path = dedup_dir / "hashes.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()

    fh_moved, w_moved = open_csv(rep_dir / "moved.csv",
        ["old_path", "new_path", "size", "hash", "date_source", "capture_date", "logged_at"])
    fh_dropped, w_dropped = open_csv(rep_dir / "dropped.csv",
        ["path", "size", "hash", "duplicate_of", "logged_at"])
    fh_errors, w_errors = open_csv(rep_dir / "errors.csv",
        ["path", "error", "logged_at"])
    fh_no_date, w_no_date = open_csv(rep_dir / "no_date.csv",
        ["path", "size", "logged_at"])

    plog = open(rep_dir / "reorganize.log", "a", encoding="utf-8", buffering=1)

    def pmsg(msg):
        line = f"[{now_iso()}] {msg}"
        plog.write(line + "\n")
        print(line, flush=True)

    pmsg(f"=== reorganize start: src={src_root} dest={dest_root} dry_run={dry_run} ===")

    n_seen = n_moved = n_dropped = n_err = n_no_date = 0
    bytes_moved = bytes_dropped = 0

    t0 = time.time()
    last_hb = t0
    last_hb_n = 0

    try:
        # topdown=False so empty parent dirs can be cleaned up after their children
        for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False, topdown=False):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                n_seen += 1

                now = time.time()
                if n_seen % HEARTBEAT_EVERY == 0 or (now - last_hb) > 30:
                    rate = (n_seen - last_hb_n) / max(0.001, now - last_hb)
                    pmsg(f"seen={n_seen} moved={n_moved} dropped={n_dropped} "
                         f"err={n_err} no_date={n_no_date} "
                         f"bytes_moved={bytes_moved} rate={rate:.1f} f/s")
                    last_hb = now
                    last_hb_n = n_seen

                try:
                    size = os.path.getsize(fpath)
                except OSError as e:
                    n_err += 1
                    w_errors.writerow([fpath, f"stat: {e}", now_iso()])
                    continue

                try:
                    h = hash_file(fpath)
                except OSError as e:
                    n_err += 1
                    w_errors.writerow([fpath, f"hash: {e}", now_iso()])
                    continue

                cur.execute("SELECT first_source_path, dest_path FROM hashes WHERE hash=?", (h,))
                row = cur.fetchone()
                if row is not None:
                    _, canonical_dest = row
                    if not dry_run:
                        try:
                            os.remove(fpath)
                        except OSError as e:
                            n_err += 1
                            w_errors.writerow([fpath, f"delete: {e}", now_iso()])
                            continue
                    w_dropped.writerow([fpath, size, h, canonical_dest, now_iso()])
                    n_dropped += 1
                    bytes_dropped += size
                    continue

                ext = os.path.splitext(fname)[1].lower()
                dt = None
                dsrc = ""
                if ext in IMAGE_EXIF_EXTS:
                    dt = parse_exif_date(fpath)
                    if dt is not None:
                        dsrc = "exif"
                if dt is None:
                    try:
                        dt = datetime.fromtimestamp(os.path.getmtime(fpath))
                        dsrc = "mtime"
                    except (OSError, ValueError):
                        pass

                if dt is None:
                    dest_dir = dest_root / "_unknown_date"
                    dsrc = "none"
                    w_no_date.writerow([fpath, size, now_iso()])
                    n_no_date += 1
                else:
                    year_part, day_part = date_to_path_parts(dt)
                    dest_dir = dest_root / year_part / day_part

                dest_path = collision_free(dest_dir / fname)

                if not dry_run:
                    try:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        os.replace(fpath, dest_path)
                    except OSError as e:
                        n_err += 1
                        w_errors.writerow([fpath, f"move: {e}", now_iso()])
                        continue
                    cur.execute("INSERT OR IGNORE INTO hashes VALUES (?,?,?,?,?)",
                                (h, fpath, str(dest_path), size, now_iso()))
                    conn.commit()

                w_moved.writerow([fpath, str(dest_path), size, h, dsrc,
                                  dt.isoformat(timespec="seconds") if dt else "",
                                  now_iso()])
                n_moved += 1
                bytes_moved += size

            if not dry_run and Path(dirpath) != src_root:
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass  # not empty -- leftover files we didn't touch

        if not dry_run:
            try:
                src_root.rmdir()
                pmsg(f"removed empty {src_root}")
            except OSError:
                pmsg(f"{src_root} not empty -- left in place (check residual files)")

    except KeyboardInterrupt:
        pmsg("interrupted")
    except Exception:
        pmsg("UNHANDLED:\n" + traceback.format_exc())
        raise
    finally:
        elapsed = time.time() - t0
        pmsg(f"=== reorganize end: seen={n_seen} moved={n_moved} dropped={n_dropped} "
             f"err={n_err} no_date={n_no_date} bytes_moved={bytes_moved} "
             f"bytes_dropped={bytes_dropped} elapsed={elapsed:.1f}s ===")
        for fh in (fh_moved, fh_dropped, fh_errors, fh_no_date):
            fh.close()
        plog.close()
        conn.commit()
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=r"D:\Pictures-Cleanup\E_Pictures")
    p.add_argument("--dest", default=r"D:\Pictures-Cleanup")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(args.src, args.dest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
