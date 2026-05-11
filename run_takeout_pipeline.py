"""
End-to-end pipeline: extract Takeout zips, then run the dedup importer.

Runs as a long-lived background process; logs to
D:\\Pictures-Cleanup\\_dedup\\GoogleTakeout\\pipeline.log so progress can be
watched while the script is detached.
"""

import os
import shutil
import sys
import time
import zipfile
import traceback
from datetime import datetime
from pathlib import Path

# Ensure we can import the dedup module from the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dedup_takeout

ZIPS = [
    r"C:\Users\mikem\Downloads\takeout-20260510T154058Z-3-001.zip",
    r"C:\Users\mikem\Downloads\takeout-20260510T154058Z-3-002.zip",
    r"C:\Users\mikem\Downloads\takeout-20260510T154058Z-3-003.zip",
]
EXTRACT_ROOT = Path(r"D:\GoogleTakeout")
DEST_ROOT = Path(r"D:\Pictures-Cleanup")
SOURCE_TAG = "GoogleTakeout"

LOG_DIR = DEST_ROOT / "_dedup" / SOURCE_TAG
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "pipeline.log"


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _long_path(p):
    """Return Windows long-path-prefixed string for an absolute path.
    Bypasses MAX_PATH (260 chars) and trailing-space dir-name issues that
    affect Takeout sidecar names like '<long-stem>.supplemental-metadata.json'.
    """
    s = os.path.abspath(str(p))
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


def _extract_member(zf, member, extract_root_abs):
    """Extract a single zip member to a long-path-aware destination."""
    # zip names use forward slashes; convert to backslashes for Windows
    rel = member.filename.replace("/", "\\")
    # zipfile already strips drive letters and leading slashes when needed,
    # but be defensive against absolute or path-traversal entries.
    if rel.startswith("\\") or ":" in rel or ".." in rel.split("\\"):
        raise RuntimeError(f"unsafe zip member path: {member.filename!r}")

    target_native = os.path.join(extract_root_abs, rel)
    target_long = _long_path(target_native)

    if member.is_dir():
        os.makedirs(target_long, exist_ok=True)
        return 0

    parent = os.path.dirname(target_long)
    if parent:
        os.makedirs(parent, exist_ok=True)

    bytes_written = 0
    with zf.open(member, "r") as src, open(target_long, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            bytes_written += len(chunk)
    # Preserve mtime when zip provides one
    try:
        ts = time.mktime(member.date_time + (0, 0, -1))
        os.utime(target_long, (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass
    return bytes_written


def extract_one(zip_path, extract_root):
    """Extract a Takeout zip into extract_root, returning member count."""
    zp = Path(zip_path)
    if not zp.exists():
        raise FileNotFoundError(f"missing zip: {zip_path}")
    extract_root.mkdir(parents=True, exist_ok=True)
    extract_root_abs = os.path.abspath(str(extract_root))

    failed_members = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        n = len(members)
        log(f"  {zp.name}: {n} members, extracting...")
        t0 = time.time()
        last = t0
        for i, member in enumerate(members, 1):
            try:
                _extract_member(zf, member, extract_root_abs)
            except Exception as e:
                failed_members += 1
                log(f"    member failed ({type(e).__name__}): {member.filename!r}: {e}")
            now = time.time()
            if now - last > 30 or i == n:
                pct = 100.0 * i / n
                log(f"    progress: {i}/{n} ({pct:.1f}%) elapsed={now - t0:.0f}s "
                    f"failed={failed_members}")
                last = now
    if failed_members:
        log(f"  {zp.name}: {failed_members} member(s) failed during extraction")
    return n - failed_members


def find_google_photos_root(extract_root):
    """Locate the 'Google Photos' subfolder within the extracted tree."""
    candidates = [
        extract_root / "Takeout" / "Google Photos",
        extract_root / "Google Photos",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # Fallback: walk for any "Google Photos" directory
    for dirpath, dirnames, _ in os.walk(extract_root):
        for d in dirnames:
            if d == "Google Photos":
                return Path(dirpath) / d
    return None


def main():
    log("=" * 70)
    log("PIPELINE START")
    log(f"  zips: {len(ZIPS)}")
    log(f"  extract root: {EXTRACT_ROOT}")
    log(f"  dedup dest:   {DEST_ROOT}")

    # --- Phase 1: extract ---
    log("phase 1: extract")
    extract_t0 = time.time()
    extracted_counts = []
    for zp in ZIPS:
        try:
            n = extract_one(zp, EXTRACT_ROOT)
            extracted_counts.append((zp, n, None))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log(f"  FAILED to extract {zp}: {err}")
            extracted_counts.append((zp, 0, err))
    extract_elapsed = time.time() - extract_t0
    log(f"phase 1 complete: {extract_elapsed:.0f}s, "
        f"{sum(n for _, n, e in extracted_counts if e is None)} total members extracted, "
        f"{sum(1 for _, _, e in extracted_counts if e is not None)} zips failed")

    gp_root = find_google_photos_root(EXTRACT_ROOT)
    if gp_root is None:
        log("ERROR: could not find 'Google Photos' folder under extract root")
        log("Listing extract root contents:")
        try:
            for p in sorted(EXTRACT_ROOT.iterdir()):
                log(f"  {p}")
        except Exception as e:
            log(f"  (listing failed: {e})")
        sys.exit(1)
    log(f"google photos root: {gp_root}")

    # --- Phase 2: dedup ---
    log("phase 2: dedup_takeout")
    dedup_t0 = time.time()
    try:
        dedup_takeout.run(str(gp_root), str(DEST_ROOT), source_tag=SOURCE_TAG)
        log(f"phase 2 complete: {time.time() - dedup_t0:.0f}s")
    except Exception:
        log("phase 2 FAILED:\n" + traceback.format_exc())
        raise

    log("PIPELINE END")
    log("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("UNHANDLED:\n" + traceback.format_exc())
        sys.exit(1)
