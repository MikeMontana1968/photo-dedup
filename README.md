# photo-dedup

A small set of Python scripts for consolidating decades of photos, videos,
and audio from multiple source drives (and Google Photos Takeout) into one
deduplicated, date-organized destination, then optionally tagging each file
with the city/state/country where it was taken.

Every script writes to a shared SQLite hash database, so files copied from
one source are recognised as duplicates when they appear in any later
source. Re-running any script is safe — already-processed files are
skipped.

## What's here

| Script | Purpose |
| --- | --- |
| `dedup_copy.py` | Walk a generic source root (e.g. `E:\Pictures`), copy unique well-formed media to a destination preserving relative paths under a source-tag folder. The bedrock script. |
| `dedup_takeout.py` | Same idea, but for an extracted Google Photos Takeout. Re-bins files by capture date (`YYYY\YYYY-Mmm-DD\filename`), converts HEIC to JPEG, copies the JSON sidecars alongside, and auto-extracts any nested zip bundles. |
| `run_takeout_pipeline.py` | End-to-end orchestrator: extracts three specific Takeout zips with a Windows long-path-aware unzipper, then invokes `dedup_takeout.run()`. Paths are hardcoded at the top of the file — edit them before reuse. |
| `geo_tag.py` | Walks the destination tree, reads GPS from JSON sidecar (preferred) or EXIF (fallback), looks up the nearest city via an offline GeoNames DB, and appends `(City, ST)` or `(City, Admin1, Country)` to the filename. Locations matching the configured "home" (Edison, NJ) are skipped. |
| `reorganize_e_pictures.py` | One-off helper for consolidating an earlier path-preserving import (e.g. `dedup_copy.py`'s `E_Pictures\` tree) into the date-based layout used by `dedup_takeout.py`. Walks the old folder, hashes each file, drops it if the hash is already canonical somewhere else, otherwise moves it to `YYYY\YYYY-Mmm-DD\` and records the new dest_path. No source-drive re-read required. |

## How it fits together

```
   source drives          Google Takeout (.zip)
        │                       │
        ▼                       ▼
   dedup_copy.py        run_takeout_pipeline.py
                              │
                              ▼
                       dedup_takeout.py
        │                       │
        └──────────┬────────────┘
                   ▼
       D:\Pictures-Cleanup\
       ├── E_Pictures\<original-relative-path>\...
       ├── 2024\2024-Aug-15\IMG_1234.jpg
       ├── 2024\2024-Aug-15\IMG_1234.jpg.json
       └── _dedup\
           ├── hashes.sqlite     ← shared content-hash DB
           ├── geocache.sqlite   ← shared lat/lon → city cache
           └── <source-tag>\     ← per-run reports
                   │
                   ▼
              geo_tag.py
              renames media + sidecar in place,
              appending location to the filename
```

## Setup

Python 3.10+ on Windows. Install dependencies:

```
python -m pip install pillow pillow-heif reverse_geocoder pycountry
```

- `pillow` — image decode / validate / save
- `pillow-heif` — HEIC/HEIF support (so iPhone photos can be converted to JPEG)
- `reverse_geocoder` — offline GeoNames cities, used by `geo_tag.py`
- `pycountry` — expands ISO country codes to full names (`FR` → `France`)

## Usage

### 1 — Import a generic source drive

```
python dedup_copy.py --source "E:\Pictures" --dest "D:\Pictures-Cleanup"
```

Files land at `D:\Pictures-Cleanup\E_Pictures\<original-relative-path>`.
Reports go to `D:\Pictures-Cleanup\_dedup\E_Pictures\`:

- `copied.csv` — every successfully imported file with hash and category
- `exceptions_duplicates.csv` — SHA256 collisions against the shared DB
- `exceptions_invalid_images.csv` — files that wouldn't open in Pillow
- `exceptions_unsupported.csv` — non-image / non-video / non-audio files
- `exceptions_errors.csv` — read/copy errors

### 2 — Import a Google Photos Takeout

Either run the pipeline (extract + dedup) end-to-end:

```
python run_takeout_pipeline.py
```

Edit the `ZIPS` / `EXTRACT_ROOT` / `DEST_ROOT` constants at the top first.
Uses a long-path-aware (`\\?\`) extractor so the Windows MAX_PATH=260 cap
doesn't trip on Google's lengthy `.supplemental-metadata.json` sidecars.

Or, if zips are already extracted:

```
python dedup_takeout.py --source "D:\GoogleTakeout\Takeout\Google Photos" `
                       --dest   "D:\Pictures-Cleanup"
```

Files land at `D:\Pictures-Cleanup\{YYYY}\{YYYY-Mmm-DD}\{filename}` based
on capture date. Sidecar JSON is copied beside each media file as
`<filename>.json`. HEIC photos are converted to JPEG (quality 95, EXIF
preserved, Orientation normalised to 1 so viewers don't double-rotate).

### 3 — Geo-tag files in the destination

```
python geo_tag.py --root "D:\Pictures-Cleanup"
python geo_tag.py --root "D:\Pictures-Cleanup" --dry-run   # preview only
```

Walks every media file under the destination root. Reads GPS from the
adjacent sidecar JSON first, falls back to EXIF GPSInfo on the image
itself. For files with usable GPS, looks up the nearest city via
`reverse_geocoder` (offline; ~150k GeoNames cities). Cache is keyed on
`(round(lat,2), round(lon,2))` — roughly 1.1 km buckets — so repeat hits
are free.

Locations matching Edison, NJ (the configured "home") are left alone.
Everything else is renamed:

- US:     `IMG_1234.jpg → IMG_1234 (Brooklyn, NY).jpg`
- non-US: `IMG_1234.jpg → IMG_1234 (Paris, Ile-de-France, France).jpg`

The matching `<filename>.json` sidecar is renamed in lock-step.

Reports go to `D:\Pictures-Cleanup\_dedup\geo\`:

- `geo_tagged.csv` — every rename with lat/lon, city, admin1, country
- `geo_skipped.csv` — Edison files and no-GPS files
- `geo_errors.csv` — rename/parse failures

## Design notes

**Shared hash DB.** All dedup scripts read and write the same
`D:\Pictures-Cleanup\_dedup\hashes.sqlite`. Once a SHA256 is recorded with
its first-seen dest path, every subsequent source that produces the same
content is logged as a duplicate and skipped. Order of imports doesn't
matter for the final set; the first source to provide a given byte
sequence wins the canonical copy.

**Idempotent / resumable.** Every script also maintains a `processed`
table keyed on `(source_path, size, mtime)`. A re-run skips files that
were already processed at the same size and mtime, so killing and
restarting a multi-hour run is cheap.

**Single-pass hash+write.** `dedup_takeout.py` streams each source file
through SHA256 *and* writes it to a staging path on the destination disk
in a single read. If the hash turns out to be a duplicate, the staging
file is deleted (rare). If unique, `os.replace()` moves it atomically
into the final per-date folder (free, since same NTFS volume). This was
specifically optimised for low-duplicate-rate runs.

**HEIC conversion.** When `pillow-heif` is installed, HEIC/HEIF files are
decoded and re-saved as JPEG (quality 95). EXIF survives the round-trip,
*including* an explicit Orientation normalisation to 1 — necessary
because `pillow-heif` applies HEIC's stored orientation transform during
decode, but the raw EXIF bytes still claim the original orientation.
Without the fix, viewers double-rotate.

**Long-path extraction.** Google Takeout sidecars can produce paths over
260 chars. `run_takeout_pipeline.py` extracts every member through the
`\\?\` Windows long-path prefix, bypassing MAX_PATH and trailing-space
quirks.

**Offline geocoding.** `geo_tag.py` uses `reverse_geocoder`, which loads
GeoNames cities (>1000 population) into an in-memory KD-tree. No network
calls, no API keys, no rate limits. Granularity is "nearest known city",
so very rural shots tag the next town over — fine for "tell me roughly
where this was."

**What's not preserved.** HEIC→JPEG conversion drops the ICC color
profile and any XMP metadata. The geocoding doesn't read GPS from video
container metadata (MP4/MOV) — only from sidecar JSON, which Takeout
provides.

## What lives where

Code (this repo):

```
photo-dedup/
├── dedup_copy.py
├── dedup_takeout.py
├── run_takeout_pipeline.py
├── geo_tag.py
├── requirements.txt
└── README.md
```

Data (separate, on your destination drive — e.g. `D:\Pictures-Cleanup\`):

```
D:\Pictures-Cleanup\
├── E_Pictures\...                  ← copied by dedup_copy.py
├── 2010\2010-Jul-04\...             ← copied by dedup_takeout.py
├── 2024\2024-Aug-15\...
├── _unknown_date\                   ← files with no resolvable capture date
└── _dedup\
    ├── hashes.sqlite                ← shared content hash DB
    ├── geocache.sqlite              ← geo lookup cache + geo_tagged state
    ├── E_Pictures\
    │   ├── copied.csv
    │   └── exceptions_*.csv
    ├── GoogleTakeout\
    │   ├── pipeline.log
    │   ├── copied.csv
    │   ├── exceptions_*.csv
    │   ├── zips_extracted.csv
    │   ├── _staging\                ← cleaned between runs
    │   └── _zip_extract\            ← preserved across runs (idempotent)
    └── geo\
        ├── geo_tagged.csv
        ├── geo_skipped.csv
        └── geo_errors.csv
```
