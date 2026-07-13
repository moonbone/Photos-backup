# PhotoVault Architecture

This document is the detailed technical design. For the one-page overview see the [README](../README.md); for the integrity/safe-delete specification see [INTEGRITY.md](INTEGRITY.md).

## 1. Overview

PhotoVault runs as a single Python application on an always-on host (NAS, home server, or PC). It has four cooperating parts sharing one SQLite catalog:

1. **Ingest pipeline** — turns files in staging folders (or uploads) into cataloged assets.
2. **Cloud sync engine** — pushes assets to S3-compatible storage with cryptographic verification.
3. **Integrity/verification subsystem** — proves the backup is complete and uncorrupted over time.
4. **Web UI + API** — browsing, metadata, and status dashboard.

A Typer-based CLI (`photovault`) drives everything; `photovault serve` runs the FastAPI web app; `photovault watch` runs the ingest daemon.

## 2. Photo identity and deduplication

The **SHA-256 hash of the file's bytes** is the canonical identity of an asset.

- The same photo synced from two phones, or present on both an SD card and a laptop, is stored **once** in the cloud and cataloged once, with every sighting recorded.
- Integrity verification anywhere in the system is "re-hash and compare" — no trust in filenames, sizes, or timestamps.
- Edited copies (re-encoded, cropped, stripped EXIF) hash differently and are treated as distinct assets by design. Near-duplicate detection (perceptual hashing) is a roadmap extension, not part of identity.

## 3. Ingest pipeline

Entry points: `photovault ingest <dir> --source <name>` (one-shot) and `photovault watch` (daemon watching configured staging folders, each mapped to a source).

Per-file processing:

1. **Stability check** (daemon mode): skip files still being written (size unchanged for a settle window).
2. **Hash**: stream SHA-256.
3. **Dedup**: if the hash exists in `assets`, record only a new `asset_instances` row (this source also has it) and stop.
4. **Metadata extraction**: ExifTool (invoked in batches of 32 files per process for throughput; Pillow fallback when exiftool is absent) → capture datetime, camera make/model, lens, GPS, dimensions, orientation, full tag dump stored as JSON.
5. **Capture-date resolution** (see §3.1).
6. **Derivatives**: thumbnail (~400px) and preview (~2048px) JPEGs via Pillow, with EXIF orientation applied. RAW files use the embedded JPEG preview extracted by ExifTool.
7. **Catalog write**: `assets` row + `asset_instances` row + counters on the `ingest_runs` row. The asset enters backup state `new`.
8. **Original file retention**: the original is copied into a local library area (`library/<sha[0:2]>/<sha>.<ext>`) so staging folders can be emptied independently of upload progress. A config option allows "staging is the library" for hosts with tight disk.
9. **Browse tree**: each new asset (photos and videos alike) is also hardlinked into a human-browsable tree — `browse/<group_format>/<prefix_format>_<original-name>`, default `browse/2024/2024-07-11/20240711_183000_IMG_1234.jpg` — so a name-sorted folder listing is sorted by extracted capture time. Hardlinks cost no extra disk space (copy fallback across filesystems; `link = "copy"|"symlink"` configurable). Deduplicated: one entry per asset, named after its earliest-seen instance; same-second/same-name collisions get a short hash suffix; date-only assets sort to the top of their day (`..._000000_`). `photovault build-browse [--clean]` backfills an existing catalog or re-lays the tree after a format change. The tree holds only links, never the sole copy of anything — it is regenerable at any time.

Special handling:

- **RAW+JPEG pairs** (e.g. `DSC_0042.NEF` + `DSC_0042.JPG`): both are independent assets (different bytes), linked by a `pair_group` on their instances so the UI can show them as one shot.
- **Sidecars** (`.xmp`): stored alongside and uploaded with the owning asset; not independent assets.
- **Videos**: ingested and backed up as opaque assets (hash, size, mtime/filename date, thumbnail from first frame via ffmpeg if available); full video metadata is a roadmap item.
- **Live Photos / motion photos**: the paired video is kept and pair-grouped like RAW+JPEG.

### 3.1 Capture-date resolution chain

Every asset gets a `capture_ts` and a `capture_ts_source` recording how it was determined:

| Priority | Source | `capture_ts_source` | Details |
|---|---|---|---|
| 1 | EXIF | `exif` | `DateTimeOriginal`, falling back to `CreateDate` / QuickTime `CreationDate` for videos. Timezone: use EXIF `OffsetTimeOriginal` when present, else configured home timezone. |
| 2 | **Filename inference** | `filename` | Match against a library of well-known patterns (below). |
| 3 | File mtime | `mtime` | Last resort; visibly flagged in UI and reports. |

Built-in filename patterns (user-extensible via config, first match wins):

- `IMG_20240711_183000.jpg`, `VID_20240711_183000.mp4` — Android camera
- `PXL_20240711_183000123.jpg` — Google Pixel
- `IMG-20240711-WA0001.jpg` — WhatsApp
- `Screenshot_2024-07-11-18-30-00*.png` and `Screenshot 2024-07-11 at 18.30.00.png`
- `signal-2024-07-11-183000.jpg` — Signal
- `2024-07-11 18.30.00.jpg` — various sync tools
- Generic extractor: any `YYYYMMDD` or `YYYY-MM-DD` (optionally followed by `HHMMSS` / `HH-MM-SS` / `HH.MM.SS`) token in the filename.

All inferred dates pass **sanity bounds**: reject results before 1990 or in the future; a rejected inference falls through to the next rung. Date-only matches (no time component) are stored with time `00:00:00` and a `date_only` flag so the UI can sort them to the top of the day rather than pretending a precise time.

The web UI badges non-EXIF dates (e.g. "date inferred from filename") and offers a filter for `capture_ts_source != exif` so questionable dates can be reviewed and manually corrected (a correction is stored as an override, never mutating the original EXIF blob).

## 4. Catalog data model

SQLite via SQLAlchemy, WAL mode. The catalog is the source of truth for *what exists* and *what is verified*.

```
assets
  sha256            TEXT PRIMARY KEY
  size_bytes        INTEGER NOT NULL
  mime              TEXT NOT NULL
  kind              TEXT NOT NULL        -- photo | video | raw | other
  capture_ts        TEXT                 -- ISO-8601 with offset
  capture_ts_source TEXT NOT NULL        -- exif | filename | mtime | manual
  date_only         INTEGER DEFAULT 0
  camera_make       TEXT
  camera_model      TEXT
  lens              TEXT
  gps_lat           REAL
  gps_lon           REAL
  width             INTEGER
  height            INTEGER
  exif_json         TEXT                 -- full ExifTool dump
  backup_state      TEXT NOT NULL        -- new | uploaded | verified  (see §6)
  thumb_state       TEXT NOT NULL        -- pending | done | failed
  pair_group        TEXT                 -- links RAW+JPEG / live-photo pairs
  created_at        TEXT NOT NULL

asset_instances                          -- every place an asset was seen
  id                INTEGER PRIMARY KEY
  sha256            TEXT REFERENCES assets
  source_id         INTEGER REFERENCES sources
  original_path     TEXT NOT NULL        -- path relative to the staging/device root
  original_filename TEXT NOT NULL
  file_mtime        TEXT
  seen_at           TEXT NOT NULL
  ingest_run_id     INTEGER REFERENCES ingest_runs

remote_copies
  sha256            TEXT REFERENCES assets
  bucket            TEXT NOT NULL
  storage_key       TEXT NOT NULL
  size_bytes        INTEGER NOT NULL
  upload_checksum   TEXT NOT NULL        -- ChecksumSHA256 confirmed by the server
  uploaded_at       TEXT NOT NULL
  last_verified_at  TEXT
  last_verify_method TEXT                -- listing | deep
  PRIMARY KEY (sha256, bucket)

sources
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, staging_path TEXT, created_at TEXT

ingest_runs
  id, source_id, started_at, finished_at, files_seen, files_new, files_duplicate, files_failed, notes

verify_runs
  id, started_at, finished_at, kind (upload|listing|deep|device),
  objects_checked, mismatches, missing, orphaned, report_path
```

Indexes on `assets(capture_ts)`, `assets(backup_state)`, `asset_instances(source_id, seen_at)`, `asset_instances(sha256)`.

### Catalog snapshots

After every `sync` and `verify` run, the catalog is snapshotted to the bucket:

- `catalog/snapshot-<utc-ts>.db` — `sqlite3 .backup` copy (last N retained by lifecycle rule).
- `catalog/manifest.json` — compact JSON: schema version, snapshot key, counts per state, and the full `{sha256 → storage_key, size}` map, so a bare-bones recovery doesn't even need SQLite.

`photovault rebuild --from-bucket` bootstraps a fresh host from the newest snapshot.

## 5. Cloud storage layout

S3-compatible, configured with endpoint URL + bucket + credentials (works with AWS S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO).

```
originals/<sha[0:2]>/<sha256>.<ext>     -- the photo bytes, content-addressed
sidecars/<sha[0:2]>/<sha256>.xmp        -- sidecar of the owning asset
thumbs/<sha[0:2]>/<sha256>.jpg          -- 400px thumbnail
previews/<sha[0:2]>/<sha256>.jpg        -- 2048px preview
catalog/snapshot-<ts>.db
catalog/manifest.json
```

- Content-addressing makes uploads idempotent and reconciliation trivial (the key *is* the expected hash).
- Two-character sharding keeps listings efficient under prefix-based pagination.
- Recommended bucket settings (documented per provider): versioning or object lock off by default, lifecycle rule pruning old catalog snapshots, deny-delete IAM policy as an optional ransomware guard.
- **Optional client-side encryption** (age or AES-GCM per object, key file held by the user) is a documented extension — off by default because it breaks presigned-URL browsing and complicates restore.

## 6. Backup state machine

Each asset's `backup_state` moves strictly forward:

```
new ──sync: upload + server ChecksumSHA256 OK──▶ uploaded ──verify: listing/deep check OK──▶ verified
                                                     │
                                                     └──verify finds mismatch──▶ new (re-queued, alert raised)
```

- `new`: cataloged locally, not yet (successfully) uploaded.
- `uploaded`: the server confirmed the SHA-256 checksum of the received bytes at upload time.
- `verified`: a subsequent, independent verify run confirmed the object (listing reconcile at minimum; deep verifies record `last_verify_method = deep`).

Only `verified` counts toward SAFE in the device-deletion workflow (see INTEGRITY.md). A mismatch discovered later demotes the asset to `new`, re-queues the upload, and raises a dashboard alert — this should effectively never happen, which is exactly why it must be loud when it does.

## 7. Cloud sync engine

`photovault sync` (also runnable on a schedule / at the end of `watch` cycles):

1. Select assets in state `new` (originals first, then derivatives), oldest capture date first.
2. Upload with boto3, passing `ChecksumSHA256` computed at ingest. Two checks guard every upload: the file is **re-hashed immediately before upload** (refusing to send bytes that no longer match the catalog — catches local bit-rot between ingest and sync), and the server recomputes the checksum of the received bytes and **rejects the upload on mismatch**. Single-request uploads support objects up to 5 GB, ample for photos and RAW files; multipart with composed checksums is a future extension for very large videos.
3. On success: write `remote_copies`, promote to `uploaded`.
4. Retries with exponential backoff + jitter; a file that fails repeatedly is marked in the run notes and left in `new` — sync never silently drops work.
5. Finish by snapshotting the catalog (§4) and printing/logging a run summary.

Interrupted syncs are harmless: state lives in the catalog, uploads are idempotent (same key, same bytes), so re-running continues where it left off.

## 8. Web UI + API

FastAPI serving both the JSON API and the UI (server-rendered Jinja templates, dependency-free CSS; small enough to swap for React later if wanted).

Views:

- **Timeline** — grouped by day within month/year headers; infinite scroll over `assets ORDER BY capture_ts DESC`. Filters: source, camera model, kind (photo/raw/video), `capture_ts_source` (to review inferred dates), backup state.
- **By source** — the same grid scoped to a source, plus per-source stats.
- **Photo detail** — preview (local file or presigned S3 URL), complete EXIF table, capture-date provenance badge, every device/path it was seen on, backup history (uploaded/verified timestamps), pair-group siblings.
- **Dashboard** — counts per state and per source, storage used, last sync/verify runs, and prominent warnings: assets stuck in `new`, sources not ingested recently, verify mismatches.

API notes: token-authenticated (single-user); thumbnails always served from local disk for speed; originals streamed via presigned URLs when no local copy is retained.

## 9. Device ingestion options (phased)

1. **Phase 1 — staging folders** (no custom device code): Syncthing or PhotoSync pushes phone camera rolls into `staging/<source>/`; DSLR cards are copied in manually or via a `photovault import-card` helper that copies then triggers ingest.
2. **Phase 2 — direct upload endpoint**: `POST /api/upload` (token auth, multipart body, client-supplied SHA-256 echoed back for confirmation) so phones upload with PhotoSync/iOS Shortcuts with no computer in the middle; received files flow through the same ingest pipeline.
3. **Phase 3 — active pull** (optional): udev/mount detection of SD cards and MTP devices, auto-import on connect.

## 10. Restore

`photovault restore [--source X] [--from DATE] [--to DATE] --dest <dir>`:

- Downloads originals, **re-hashes each file on arrival** and fails loudly on mismatch.
- Reconstructs `original_filename` under a capture-date `YYYY/MM/` layout (restoring the recorded `original_path` layout is a roadmap extension).
- `photovault rebuild --from-bucket` restores the *catalog* itself from the newest snapshot (§4).

## 11. Configuration

Single TOML file (`~/.config/photovault/config.toml` or `--config`):

```toml
[storage]
endpoint = "https://s3.us-west-000.backblazeb2.com"
bucket   = "my-photovault"
# credentials via env vars PHOTOVAULT_S3_KEY / PHOTOVAULT_S3_SECRET

[library]
path = "/data/photovault/library"
home_timezone = "Asia/Jerusalem"

[[sources]]
name = "phone-a"
staging = "/data/photovault/staging/phone-a"

[[sources]]
name = "dslr"
staging = "/data/photovault/staging/dslr"

[ingest]
extra_filename_date_patterns = []   # user-extensible regexes with named groups

[verify]
sample_size = 50
```

Deployment: `pip install` for direct use; a Dockerfile + compose file (app + volume mounts for staging/library) for NAS deployment.

## 12. Failure semantics summary

| Failure | Behavior |
|---|---|
| Upload checksum mismatch | Server rejects; retry; asset stays `new` |
| Network outage mid-sync | Resume on next run; idempotent keys |
| Host disk dies | Rebuild from `catalog/` snapshot in bucket |
| Cloud object corrupted/missing later | Verify run demotes asset, re-uploads, raises alert |
| File still being written during watch | Settle-window check defers it |
| Duplicate file from second device | New `asset_instances` row only; no re-upload |
| Ingest crash mid-run | Per-file transactions; re-run re-processes only unfinished files |
