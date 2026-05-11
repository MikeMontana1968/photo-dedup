"""
Geo-tag media in D:\\Pictures-Cleanup\\ by reverse-geocoding GPS and
appending the location to the filename.

GPS sources (preferred -> fallback):
  1. Adjacent JSON sidecar (Takeout-imported files have <name>.json)
     -- reads geoData.{latitude,longitude}, else geoDataExif.*
  2. EXIF GPSInfo on the image itself

For each file with GPS:
  - Look up nearest city via reverse_geocoder (offline GeoNames DB)
  - Cache the result, keyed on (round(lat,2), round(lon,2)) ~ 1.1 km buckets
  - If the location is Edison, NJ, US -> no rename
  - Else rename media + matching sidecar to: <stem> (Suffix).<ext>
       US:     "City, ST"            e.g. "Brooklyn, NY"
       non-US: "City, Admin1, Country"  e.g. "Paris, Ile-de-France, France"

State is recorded in geocache.sqlite so re-runs are idempotent.

Usage:
    python geo_tag.py --root D:\\Pictures-Cleanup           # do it for real
    python geo_tag.py --root D:\\Pictures-Cleanup --dry-run # plan only
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

import reverse_geocoder as rg
import pycountry


# Media we'll consider. JSON sidecars are picked up alongside, not directly walked.
MEDIA_EXTS = {
    # Pillow-readable images
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".heic", ".heif",
    # Videos -- GPS only via sidecar
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".webm",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".mts", ".m2ts",
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".webm",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".mts", ".m2ts",
}

SKIP_DIRS = {"_dedup"}

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "American Samoa": "AS", "Guam": "GU",
    "Northern Mariana Islands": "MP", "U.S. Virgin Islands": "VI",
}

WINDOWS_BAD = re.compile(r'[<>:"/\\|?*]')
# Detect filenames we've already geo-tagged: stem ends with " (X, Y[, Z])"
ALREADY_TAGGED = re.compile(r'.+ \(([^()]+,[^()]+)\)$')


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def open_geocache(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS geo_cache (
            lat_key REAL NOT NULL,
            lon_key REAL NOT NULL,
            city TEXT,
            admin1 TEXT,
            cc TEXT,
            PRIMARY KEY (lat_key, lon_key)
        );
        CREATE TABLE IF NOT EXISTS geo_tagged (
            final_path TEXT PRIMARY KEY,
            has_gps INTEGER NOT NULL,
            lat REAL,
            lon REAL,
            city TEXT,
            admin1 TEXT,
            cc TEXT,
            renamed INTEGER NOT NULL,
            processed_at TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def sanitize(s):
    return WINDOWS_BAD.sub("_", s or "").strip()


def country_full_name(cc):
    if not cc:
        return None
    try:
        c = pycountry.countries.get(alpha_2=cc.upper())
        if c is not None:
            return getattr(c, "common_name", c.name)
    except Exception:
        pass
    return cc


def find_sidecar(media_path):
    """Sidecar convention used by dedup_takeout.py: <media>.json"""
    p = Path(media_path)
    cand = p.parent / (p.name + ".json")
    return cand if cand.exists() else None


def parse_sidecar_gps(sidecar_path):
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    for key in ("geoData", "geoDataExif"):
        g = data.get(key) or {}
        lat = g.get("latitude")
        lon = g.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            continue
        # Sentinel zeroes -- treat as no data
        if abs(lat_f) < 1e-9 and abs(lon_f) < 1e-9:
            continue
        return lat_f, lon_f
    return None


def _dms_to_decimal(dms, ref):
    try:
        d, m, s = dms
        v = float(d) + float(m) / 60.0 + float(s) / 3600.0
        if str(ref).upper() in ("S", "W"):
            v = -v
        return v
    except Exception:
        return None


def parse_exif_gps(path):
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            try:
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            except Exception:
                gps = None
            if not gps:
                return None
            lat_dms = gps.get(2)
            lat_ref = gps.get(1)
            lon_dms = gps.get(4)
            lon_ref = gps.get(3)
            if not (lat_dms and lon_dms and lat_ref and lon_ref):
                return None
            lat = _dms_to_decimal(lat_dms, lat_ref)
            lon = _dms_to_decimal(lon_dms, lon_ref)
            if lat is None or lon is None:
                return None
            if abs(lat) < 1e-9 and abs(lon) < 1e-9:
                return None
            return lat, lon
    except Exception:
        return None


def lookup_geo(conn, lat, lon):
    """Return ((city, admin1, cc), was_miss).
    Bucketed to 2 decimals (~1.1 km)."""
    lat_key = round(lat, 2)
    lon_key = round(lon, 2)
    cur = conn.cursor()
    cur.execute("SELECT city, admin1, cc FROM geo_cache WHERE lat_key=? AND lon_key=?",
                (lat_key, lon_key))
    row = cur.fetchone()
    if row is not None:
        return row, False
    results = rg.search([(lat_key, lon_key)], mode=1)
    if not results:
        return None, True
    r = results[0]
    triple = (r.get("name") or "", r.get("admin1") or "", r.get("cc") or "")
    cur.execute("INSERT OR REPLACE INTO geo_cache VALUES (?,?,?,?,?)",
                (lat_key, lon_key, triple[0], triple[1], triple[2]))
    conn.commit()
    return triple, True


def is_edison_nj(city, admin1, cc):
    return cc == "US" and admin1 == "New Jersey" and city == "Edison"


def build_suffix(city, admin1, cc):
    c = sanitize(city)
    if cc == "US":
        ab = US_STATES.get(admin1, sanitize(admin1))
        return f"{c}, {ab}"
    parts = [c]
    a = sanitize(admin1)
    if a:
        parts.append(a)
    country = sanitize(country_full_name(cc) or "")
    if country:
        parts.append(country)
    return ", ".join(parts)


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


def rename_pair(media_path, new_name):
    """Rename media + matching .json sidecar."""
    media_p = Path(media_path)
    sidecar_p = find_sidecar(media_path)
    new_media = collision_free(media_p.parent / new_name)
    os.rename(media_p, new_media)
    new_sidecar = None
    if sidecar_p is not None:
        new_sidecar = collision_free(new_media.parent / (new_media.name + ".json"))
        os.rename(sidecar_p, new_sidecar)
    return new_media, new_sidecar


def open_csv(path, header):
    new = not path.exists() or path.stat().st_size == 0
    fh = open(path, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(fh)
    if new:
        w.writerow(header)
        fh.flush()
    return fh, w


def run(root, dry_run=False, limit=None):
    root = Path(root).resolve()
    dedup_dir = root / "_dedup"
    rep_dir = dedup_dir / "geo"
    rep_dir.mkdir(parents=True, exist_ok=True)

    cache_path = dedup_dir / "geocache.sqlite"
    conn = open_geocache(cache_path)
    cur = conn.cursor()

    fh_tagged, w_tagged = open_csv(rep_dir / "geo_tagged.csv",
        ["old_path", "new_path", "lat", "lon", "city", "admin1", "cc", "country", "logged_at"])
    fh_skipped, w_skipped = open_csv(rep_dir / "geo_skipped.csv",
        ["path", "reason", "city", "admin1", "cc", "logged_at"])
    fh_errors, w_errors = open_csv(rep_dir / "geo_errors.csv",
        ["path", "error", "logged_at"])

    plog = open(rep_dir / "geo.log", "a", encoding="utf-8", buffering=1)

    def pmsg(msg):
        line = f"[{now_iso()}] {msg}"
        plog.write(line + "\n")
        print(line, flush=True)

    pmsg(f"=== geo-tag start: root={root} dry_run={dry_run} ===")

    n_seen = n_tagged = n_edison = n_no_gps = n_already = n_err = 0
    n_resume = n_hit = n_miss = 0

    t0 = time.time()
    last_hb = t0
    last_hb_n = 0

    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

            for fname in filenames:
                if limit is not None and n_seen >= limit:
                    pmsg(f"hit limit={limit}")
                    raise StopIteration

                ext = os.path.splitext(fname)[1].lower()
                if ext not in MEDIA_EXTS:
                    continue

                fpath = os.path.join(dirpath, fname)
                n_seen += 1

                now = time.time()
                if n_seen % 200 == 0 or (now - last_hb) > 30:
                    rate = (n_seen - last_hb_n) / max(0.001, now - last_hb)
                    pmsg(f"seen={n_seen} tagged={n_tagged} edison={n_edison} "
                         f"no_gps={n_no_gps} already={n_already} err={n_err} "
                         f"resume={n_resume} cache hit/miss={n_hit}/{n_miss} "
                         f"rate={rate:.1f} f/s")
                    last_hb = now
                    last_hb_n = n_seen

                cur.execute("SELECT 1 FROM geo_tagged WHERE final_path=?", (fpath,))
                if cur.fetchone() is not None:
                    n_resume += 1
                    continue

                stem = Path(fname).stem
                if ALREADY_TAGGED.match(stem):
                    n_already += 1
                    cur.execute("INSERT OR REPLACE INTO geo_tagged VALUES (?,?,?,?,?,?,?,?,?)",
                                (fpath, 0, None, None, None, None, None, 0, now_iso()))
                    conn.commit()
                    continue

                # GPS lookup: sidecar first, EXIF fallback (only for images)
                gps = None
                sidecar = find_sidecar(fpath)
                if sidecar is not None:
                    gps = parse_sidecar_gps(sidecar)
                if gps is None and ext not in VIDEO_EXTS:
                    gps = parse_exif_gps(fpath)

                if gps is None:
                    n_no_gps += 1
                    w_skipped.writerow([fpath, "no_gps", "", "", "", now_iso()])
                    cur.execute("INSERT OR REPLACE INTO geo_tagged VALUES (?,?,?,?,?,?,?,?,?)",
                                (fpath, 0, None, None, None, None, None, 0, now_iso()))
                    conn.commit()
                    continue

                lat, lon = gps
                try:
                    triple, was_miss = lookup_geo(conn, lat, lon)
                except Exception as e:
                    n_err += 1
                    w_errors.writerow([fpath, f"geocode: {e}", now_iso()])
                    continue
                if was_miss:
                    n_miss += 1
                else:
                    n_hit += 1
                if triple is None:
                    n_no_gps += 1
                    w_skipped.writerow([fpath, "no_geocode_result", "", "", "", now_iso()])
                    cur.execute("INSERT OR REPLACE INTO geo_tagged VALUES (?,?,?,?,?,?,?,?,?)",
                                (fpath, 1, lat, lon, None, None, None, 0, now_iso()))
                    conn.commit()
                    continue

                city, admin1, cc = triple

                if is_edison_nj(city, admin1, cc):
                    n_edison += 1
                    w_skipped.writerow([fpath, "edison_skip", city, admin1, cc, now_iso()])
                    cur.execute("INSERT OR REPLACE INTO geo_tagged VALUES (?,?,?,?,?,?,?,?,?)",
                                (fpath, 1, lat, lon, city, admin1, cc, 0, now_iso()))
                    conn.commit()
                    continue

                suffix = build_suffix(city, admin1, cc)
                new_name = f"{stem} ({suffix}){ext}"
                planned_new_path = str(Path(dirpath) / new_name)

                if dry_run:
                    w_tagged.writerow([fpath, planned_new_path, lat, lon,
                                       city, admin1, cc, country_full_name(cc) or cc, "DRY_RUN"])
                    n_tagged += 1
                    continue

                try:
                    new_media, _ = rename_pair(fpath, new_name)
                except Exception as e:
                    n_err += 1
                    w_errors.writerow([fpath, f"rename: {e}", now_iso()])
                    continue

                final_path = str(new_media)
                cur.execute("INSERT OR REPLACE INTO geo_tagged VALUES (?,?,?,?,?,?,?,?,?)",
                            (final_path, 1, lat, lon, city, admin1, cc, 1, now_iso()))
                conn.commit()
                w_tagged.writerow([fpath, final_path, lat, lon, city, admin1, cc,
                                   country_full_name(cc) or cc, now_iso()])
                n_tagged += 1

    except StopIteration:
        pass
    except KeyboardInterrupt:
        pmsg("interrupted")
    except Exception:
        pmsg("UNHANDLED:\n" + traceback.format_exc())
        raise
    finally:
        elapsed = time.time() - t0
        pmsg(f"=== geo-tag end: seen={n_seen} tagged={n_tagged} edison={n_edison} "
             f"no_gps={n_no_gps} already={n_already} err={n_err} resume={n_resume} "
             f"cache hit/miss={n_hit}/{n_miss} elapsed={elapsed:.1f}s ===")
        for fh in (fh_tagged, fh_skipped, fh_errors):
            fh.close()
        plog.close()
        conn.commit()
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=r"D:\Pictures-Cleanup")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan renames without touching files")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    run(args.root, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
