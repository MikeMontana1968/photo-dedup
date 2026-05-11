"""
Google Takeout dedup-and-import.

Reads an extracted Google Photos Takeout directory, rebins each photo/video
into  D:\\Pictures-Cleanup\\{YYYY}\\{YYYY-Mmm-DD}\\{filename}  by capture
date, and dedups against the SHA256 hash DB shared with dedup_copy.py
(D:\\Pictures-Cleanup\\_dedup\\hashes.sqlite).

Behavior:
  * Capture-date priority:  sidecar JSON photoTakenTime  ->  EXIF
    DateTimeOriginal  ->  file mtime.
  * HEIC / HEIF photos are decoded and saved as JPEG (quality 95, EXIF
    preserved).  Dedup hashes the ORIGINAL HEIC bytes so re-running on the
    same archive doesn't re-import.
  * JSON sidecars are copied alongside their photo as <dest-filename>.json
    so the metadata travels with the file.
  * .zip files in the source are extracted to a tracked extract directory
    and their members are processed as if they were normal files.
  * Files we can't date go to D:\\Pictures-Cleanup\\_unknown_date\\.

Optimization: assumes most source files will be UNIQUE (low dedup hit
rate). Source is read exactly once, streamed simultaneously into the
SHA256 hasher and into a staging file on the destination disk; the
staged file is then either validated/renamed-into-place or discarded.
This avoids re-reading the source for hash, validate, and copy steps.

Usage:
    python dedup_takeout.py --source "D:\\GoogleTakeout\\Takeout\\Google Photos" \\
                            --dest   "D:\\Pictures-Cleanup"

Re-runs are safe and resume where they left off.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile, ExifTags

ImageFile.LOAD_TRUNCATED_IMAGES = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_CONVERT_AVAILABLE = True
except ImportError:
    HEIC_CONVERT_AVAILABLE = False


IMAGE_VALIDATE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".gif",
    ".bmp", ".dib",
    ".tiff", ".tif",
    ".webp", ".ico", ".psd", ".thm",
}

IMAGE_CONVERT_HEIC_EXTS = {".heic", ".heif"}

IMAGE_COPY_ONLY_EXTS = {
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng",
    ".orf", ".rw2", ".sr2", ".srf", ".pef", ".raf", ".rwl",
    ".nrw", ".x3f", ".kdc", ".dcr", ".mrw",
}

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".mpe", ".3gp", ".3g2", ".vob",
    ".mts", ".m2ts", ".ts", ".lrv", ".asf", ".rm", ".rmvb",
    ".divx", ".f4v", ".ogv",
}

AUDIO_EXTS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac",
    ".ogg", ".oga", ".wma", ".aiff", ".aif",
    ".opus", ".amr", ".mka", ".ape", ".alac",
    ".mid", ".midi",
}

CHUNK = 1024 * 1024
HEARTBEAT_EVERY = 100
JPEG_QUALITY = 95
EXIF_DATETIME_ORIGINAL = 36867


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


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


def hash_and_write_streaming(src_path, staging_path):
    """
    Stream src into staging while computing SHA256. Returns hash.
    Reads the source exactly once.
    """
    h = hashlib.sha256()
    with open(src_path, "rb") as fin, open(staging_path, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            fout.write(chunk)
    return h.hexdigest()


def validate_image(path):
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im2:
            im2.load()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


_EXIF_ORIENTATION_TAG = 0x0112  # 274


def _normalize_exif_orientation(exif_bytes):
    """
    pillow-heif rotates HEIC pixels during decode but leaves the original EXIF
    Orientation tag intact. Embedding those bytes verbatim in a JPEG would
    cause viewers to double-rotate. This rewrites Orientation to 1 (normal)
    while preserving all other EXIF tags. Falls back to the original bytes
    if parsing fails.
    """
    if not exif_bytes:
        return exif_bytes
    try:
        e = Image.Exif()
        e.load(exif_bytes)
        if e.get(_EXIF_ORIENTATION_TAG, 1) != 1:
            e[_EXIF_ORIENTATION_TAG] = 1
            return e.tobytes()
        return exif_bytes
    except Exception:
        return exif_bytes


def convert_heic_to_jpeg(staging_heic, dest_jpeg):
    """
    Decode HEIC (already on local disk) and write JPEG. Combines validation
    with conversion -- a decode failure is treated as an invalid image.
    Preserves EXIF (DateTimeOriginal, GPS, camera, lens, ISO, exposure...) and
    normalizes Orientation to 1 so viewers don't double-rotate. ICC profile
    and XMP are not preserved.
    Returns (ok, error_message).
    """
    try:
        with Image.open(staging_heic) as im:
            im.load()
            exif_bytes = _normalize_exif_orientation(im.info.get("exif"))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            kwargs = {"format": "JPEG", "quality": JPEG_QUALITY, "optimize": True}
            if exif_bytes:
                kwargs["exif"] = exif_bytes
            im.save(dest_jpeg, **kwargs)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class CsvLog:
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
    if e in IMAGE_CONVERT_HEIC_EXTS:
        return "image_convert_heic" if HEIC_CONVERT_AVAILABLE else "image_copy_only"
    if e in IMAGE_COPY_ONLY_EXTS:
        return "image_copy_only"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    return "other"


def collision_free_dest(dest_path):
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


_SIDECAR_SUFFIXES = (
    ".supplemental-metadata.json",
    ".json",
)

_ALBUM_METADATA_NAMES = {
    "metadata.json",
    "user-generated-memory-titles.json",
    "shared_album_comments.json",
    "print-subscriptions.json",
}


def find_sidecar(media_path):
    p = Path(media_path)
    parent = p.parent
    base = p.name
    for suf in _SIDECAR_SUFFIXES:
        cand = parent / (base + suf)
        if cand.exists():
            return cand
    if len(base) > 30:
        prefix = base[:30]
        try:
            for sib in parent.iterdir():
                sn = sib.name
                if not sn.startswith(prefix):
                    continue
                for suf in _SIDECAR_SUFFIXES:
                    if sn.endswith(suf):
                        return sib
        except OSError:
            pass
    return None


def parse_sidecar_date(sidecar_path):
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    pt = data.get("photoTakenTime") or {}
    ts = pt.get("timestamp")
    if ts is None:
        ct = data.get("creationTime") or {}
        ts = ct.get("timestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError):
        return None


_EXIF_DATE_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


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


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def date_to_path_parts(dt):
    return f"{dt.year:04d}", f"{dt.year:04d}-{_MONTH_ABBR[dt.month-1]}-{dt.day:02d}"


def is_sidecar_or_album_meta(name_lower):
    if name_lower in _ALBUM_METADATA_NAMES:
        return True
    if name_lower.endswith(".json"):
        return True
    return False


def safe_extract_zip(zf, dest):
    dest_resolved = dest.resolve()
    members = zf.infolist()
    for member in members:
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise RuntimeError(f"unsafe member path in zip: {member.filename!r}")
    zf.extractall(dest)
    return len(members)


def _silent_unlink(p):
    try:
        Path(p).unlink()
    except OSError:
        pass


def run(source_root, dest_root, source_tag="GoogleTakeout", limit=None):
    source_root = str(Path(source_root).resolve())
    dest_root = Path(dest_root).resolve()
    dedup_dir = dest_root / "_dedup"
    dedup_dir.mkdir(parents=True, exist_ok=True)

    db_path = dedup_dir / "hashes.sqlite"
    progress_log = dedup_dir / f"progress_{source_tag}.log"

    rep_dir = dedup_dir / source_tag
    rep_dir.mkdir(parents=True, exist_ok=True)

    extract_root = rep_dir / "_zip_extract"
    extract_root.mkdir(parents=True, exist_ok=True)

    staging_dir = rep_dir / "_staging"
    # Wipe any leftover staging files from a previous aborted run
    if staging_dir.exists():
        for f in staging_dir.iterdir():
            _silent_unlink(f)
    else:
        staging_dir.mkdir(parents=True)

    log_dup = CsvLog(rep_dir / "exceptions_duplicates.csv",
                     ["source_path", "size", "hash", "duplicate_of_dest", "duplicate_of_source", "logged_at"])
    log_invalid = CsvLog(rep_dir / "exceptions_invalid_images.csv",
                         ["source_path", "size", "ext", "error", "logged_at"])
    log_unsupported = CsvLog(rep_dir / "exceptions_unsupported.csv",
                             ["source_path", "size", "ext", "logged_at"])
    log_errors = CsvLog(rep_dir / "exceptions_errors.csv",
                        ["source_path", "size", "error", "logged_at"])
    log_no_date = CsvLog(rep_dir / "exceptions_no_date.csv",
                         ["source_path", "size", "reason", "logged_at"])
    log_zips = CsvLog(rep_dir / "zips_extracted.csv",
                      ["zip_path", "extract_dir", "members", "logged_at"])
    log_copied = CsvLog(rep_dir / "copied.csv",
                        ["source_path", "dest_path", "size", "hash",
                         "category", "date_source", "capture_date",
                         "sidecar_copied", "logged_at"])

    plog = open(progress_log, "a", encoding="utf-8", buffering=1)

    def pmsg(msg):
        line = f"[{now_iso()}] {msg}"
        plog.write(line + "\n")
        print(line, flush=True)

    pmsg(f"=== takeout run start: source={source_root} dest={dest_root} tag={source_tag} ===")
    pmsg(f"HEIC conversion: {'enabled (pillow-heif)' if HEIC_CONVERT_AVAILABLE else 'DISABLED -- HEIC will be copied as-is'}")
    pmsg(f"staging dir: {staging_dir}")

    conn = open_db(db_path)
    cur = conn.cursor()

    n_seen = 0
    n_skipped_resume = 0
    n_copied = 0
    n_dup = 0
    n_invalid = 0
    n_unsupported = 0
    n_error = 0
    n_no_date = 0
    n_sidecars_skipped = 0
    n_sidecars_copied = 0
    n_zips_extracted = 0
    n_zips_failed = 0
    n_heic_converted = 0
    bytes_copied = 0
    date_source_counts = {"sidecar": 0, "exif": 0, "mtime": 0}

    walk_queue = [Path(source_root)]
    staging_counter = 0
    pid = os.getpid()

    t0 = time.time()
    last_heartbeat = t0
    heartbeat_files_at_last = 0

    try:
        while walk_queue:
            cur_root = walk_queue.pop(0)

            for dirpath, dirnames, filenames in os.walk(cur_root, followlinks=False):
                for fname in filenames:
                    if limit is not None and n_seen >= limit:
                        pmsg(f"hit limit={limit}")
                        raise StopIteration

                    src_path = os.path.join(dirpath, fname)
                    fname_l = fname.lower()
                    n_seen += 1

                    now = time.time()
                    if n_seen % HEARTBEAT_EVERY == 0 or (now - last_heartbeat) > 30:
                        rate = (n_seen - heartbeat_files_at_last) / max(0.001, now - last_heartbeat)
                        pmsg(f"seen={n_seen} copied={n_copied} dup={n_dup} invalid={n_invalid} "
                             f"unsupported={n_unsupported} error={n_error} no_date={n_no_date} "
                             f"heic_conv={n_heic_converted} zips={n_zips_extracted} "
                             f"side_copied={n_sidecars_copied} resume={n_skipped_resume} "
                             f"bytes_copied={bytes_copied} rate={rate:.1f} files/s")
                        last_heartbeat = now
                        heartbeat_files_at_last = n_seen

                    # --- Cheap early skips (no source read) ---

                    if is_sidecar_or_album_meta(fname_l):
                        n_sidecars_skipped += 1
                        continue

                    if fname_l.endswith(".zip"):
                        ext_id = hashlib.sha1(src_path.encode("utf-8", "ignore")).hexdigest()[:16]
                        ext_dir = extract_root / ext_id
                        if not ext_dir.exists():
                            try:
                                ext_dir.mkdir(parents=True)
                                with zipfile.ZipFile(src_path) as zf:
                                    n_members = safe_extract_zip(zf, ext_dir)
                                log_zips.write([src_path, str(ext_dir), n_members, now_iso()])
                                pmsg(f"extracted zip ({n_members} members): {src_path}")
                                n_zips_extracted += 1
                            except Exception as e:
                                n_zips_failed += 1
                                n_error += 1
                                log_errors.write([src_path, "", f"zip extract failed: {e}", now_iso()])
                                shutil.rmtree(ext_dir, ignore_errors=True)
                                continue
                        else:
                            n_zips_extracted += 1
                        walk_queue.append(ext_dir)
                        continue

                    try:
                        st = os.stat(src_path, follow_symlinks=False)
                    except OSError as e:
                        n_error += 1
                        log_errors.write([src_path, "", f"stat failed: {e}", now_iso()])
                        continue

                    size = st.st_size
                    mtime = st.st_mtime

                    if not os.path.isfile(src_path) or os.path.islink(src_path):
                        continue

                    cur.execute(
                        "SELECT status FROM processed WHERE source_path=? AND size=? AND mtime=?",
                        (src_path, size, mtime),
                    )
                    if cur.fetchone() is not None:
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

                    # --- Sidecar lookup + capture-date (read-cheap if sidecar present) ---

                    sidecar = find_sidecar(src_path)
                    sidecar_date = parse_sidecar_date(sidecar) if sidecar else None

                    # --- Single-pass: source -> staging, computing hash inline ---

                    staging_counter += 1
                    staging_path = staging_dir / f"{pid}_{staging_counter}.tmp"

                    try:
                        h = hash_and_write_streaming(src_path, staging_path)
                    except OSError as e:
                        n_error += 1
                        log_errors.write([src_path, size, f"stream hash/write failed: {e}", now_iso()])
                        cur.execute(
                            "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                            (src_path, size, mtime, None, "error", str(e), now_iso()),
                        )
                        conn.commit()
                        _silent_unlink(staging_path)
                        continue

                    # --- Dedup check ---

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
                        _silent_unlink(staging_path)
                        continue

                    # --- Validation (decoded from staging on local disk) ---

                    if cat == "image_validate":
                        ok, detail = validate_image(staging_path)
                        if not ok:
                            log_invalid.write([src_path, size, ext, detail, now_iso()])
                            cur.execute(
                                "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                                (src_path, size, mtime, h, "invalid_image", detail, now_iso()),
                            )
                            conn.commit()
                            n_invalid += 1
                            _silent_unlink(staging_path)
                            continue

                    # --- Resolve capture date (sidecar > EXIF > mtime) ---

                    if sidecar_date is not None:
                        dt, dsrc = sidecar_date, "sidecar"
                    elif cat in ("image_validate", "image_convert_heic"):
                        d_exif = parse_exif_date(staging_path)
                        if d_exif is not None:
                            dt, dsrc = d_exif, "exif"
                        else:
                            try:
                                dt, dsrc = datetime.fromtimestamp(mtime), "mtime"
                            except (OSError, ValueError):
                                dt, dsrc = None, ""
                    else:
                        try:
                            dt, dsrc = datetime.fromtimestamp(mtime), "mtime"
                        except (OSError, ValueError):
                            dt, dsrc = None, ""

                    if dt is None:
                        log_no_date.write([src_path, size, "no_date_resolvable", now_iso()])
                        n_no_date += 1
                        dest_dir = dest_root / "_unknown_date"
                        capture_str = ""
                        dsrc = "none"
                    else:
                        year_part, day_part = date_to_path_parts(dt)
                        dest_dir = dest_root / year_part / day_part
                        capture_str = dt.isoformat(timespec="seconds")
                        date_source_counts[dsrc] = date_source_counts.get(dsrc, 0) + 1

                    # --- Compute final destination filename ---

                    if cat == "image_convert_heic":
                        dest_name = Path(fname).stem + ".jpg"
                    else:
                        dest_name = fname

                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_path = collision_free_dest(dest_dir / dest_name)

                    # --- Materialize the destination ---

                    if cat == "image_convert_heic":
                        # Convert validates AND writes; staging holds the HEIC source
                        ok, detail = convert_heic_to_jpeg(staging_path, dest_path)
                        _silent_unlink(staging_path)
                        if not ok:
                            log_invalid.write([src_path, size, ext, detail, now_iso()])
                            cur.execute(
                                "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                                (src_path, size, mtime, h, "invalid_image", detail, now_iso()),
                            )
                            conn.commit()
                            n_invalid += 1
                            try:
                                if dest_path.exists():
                                    dest_path.unlink()
                            except OSError:
                                pass
                            continue
                        # Preserve original mtime on the converted JPEG
                        try:
                            os.utime(dest_path, (st.st_atime, st.st_mtime))
                        except OSError:
                            pass
                        n_heic_converted += 1
                    else:
                        # Move staging -> final (atomic on same volume)
                        try:
                            os.replace(staging_path, dest_path)
                        except OSError as e:
                            n_error += 1
                            log_errors.write([src_path, size, f"rename failed: {e}", now_iso()])
                            cur.execute(
                                "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                                (src_path, size, mtime, h, "error", f"rename failed: {e}", now_iso()),
                            )
                            conn.commit()
                            _silent_unlink(staging_path)
                            continue
                        # Restore original mtime that os.replace doesn't preserve
                        try:
                            os.utime(dest_path, (st.st_atime, st.st_mtime))
                        except OSError:
                            pass

                    # --- Copy sidecar JSON alongside the final media ---

                    sidecar_copied_str = ""
                    if sidecar is not None:
                        sidecar_dest = collision_free_dest(
                            dest_path.parent / (dest_path.name + ".json")
                        )
                        try:
                            shutil.copy2(sidecar, sidecar_dest)
                            n_sidecars_copied += 1
                            sidecar_copied_str = str(sidecar_dest)
                        except OSError as e:
                            log_errors.write([str(sidecar), 0, f"sidecar copy failed: {e}", now_iso()])

                    cur.execute(
                        "INSERT OR IGNORE INTO hashes VALUES (?,?,?,?,?)",
                        (h, src_path, str(dest_path), size, now_iso()),
                    )
                    cur.execute(
                        "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?,?)",
                        (src_path, size, mtime, h, "copied", str(dest_path), now_iso()),
                    )
                    conn.commit()
                    log_copied.write([src_path, str(dest_path), size, h, cat,
                                      dsrc, capture_str, sidecar_copied_str, now_iso()])
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
        # Best-effort staging cleanup
        try:
            for f in staging_dir.iterdir():
                _silent_unlink(f)
        except OSError:
            pass

        elapsed = time.time() - t0
        pmsg(f"=== takeout run end: seen={n_seen} copied={n_copied} dup={n_dup} "
             f"invalid={n_invalid} unsupported={n_unsupported} error={n_error} "
             f"no_date={n_no_date} heic_converted={n_heic_converted} "
             f"zips_extracted={n_zips_extracted} zips_failed={n_zips_failed} "
             f"sidecars_copied={n_sidecars_copied} sidecars_skipped={n_sidecars_skipped} "
             f"resume_skip={n_skipped_resume} bytes_copied={bytes_copied} "
             f"date_sources={date_source_counts} elapsed={elapsed:.1f}s ===")
        for lg in (log_dup, log_invalid, log_unsupported, log_errors,
                   log_no_date, log_zips, log_copied):
            lg.close()
        conn.commit()
        conn.close()
        plog.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True,
                   help="Path to extracted Takeout 'Google Photos' directory")
    p.add_argument("--dest", required=True,
                   help="Destination root (e.g. D:\\Pictures-Cleanup)")
    p.add_argument("--source-tag", default="GoogleTakeout")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    run(args.source, args.dest, source_tag=args.source_tag, limit=args.limit)


if __name__ == "__main__":
    main()
