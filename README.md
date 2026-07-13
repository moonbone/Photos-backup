# PhotoVault

A self-hosted photo backup system that keeps a **full, verified copy of all your photos in cloud storage**, collected from every device you own — phones, DSLR SD cards, laptops — and lets you browse the entire library **by date or by source** with complete metadata.

Its defining feature is **trust before deletion**: PhotoVault can prove that every photo on a device is safely backed up and verified in the cloud *before* you delete anything from that device.

## Feature summary

- **Multi-source ingestion** — watched staging folders per device (fed by SD-card copies, Syncthing, PhotoSync, USB dumps), and later a direct mobile upload endpoint.
- **Content-addressed backup** — every photo is identified by its SHA-256 hash. The same photo arriving from two devices is stored once; integrity checking is "re-hash and compare" everywhere.
- **Full metadata catalog** — capture date/time, camera and lens, GPS, dimensions, and the complete EXIF blob, extracted with ExifTool. When EXIF dates are missing, the capture date is inferred from well-known filename patterns (with the inference clearly flagged), falling back to file mtime as a last resort.
- **Verified cloud storage** — uploads to any S3-compatible backend (AWS S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO) with server-side SHA-256 checksum verification on receipt, plus ongoing reconciliation and sample re-download audits.
- **Safe-delete workflow** — `photovault check-device` hashes what is *actually on the device right now* and classifies every file; nothing is deletable until it is cataloged **and** its cloud copy is verified. Deletion is a separate, explicit, dry-run-by-default command.
- **Self-hosted web UI** — timeline browsing by day/month/year, filters by source and camera, per-photo metadata panel, and a backup-status dashboard. Runs on a NAS, home server, or any always-on PC; usable from phone or desktop browsers.
- **Self-describing backup** — the catalog database is snapshotted to the cloud after every run, so the entire library can be rebuilt from the bucket alone if the host machine dies.
- **Human-browsable local tree** — alongside the content-addressed library, every imported photo and video is hardlinked (no extra disk space) into `browse/2024/2024-07-11/20240711_183000_IMG_1234.jpg`: grouped by capture date, filename prefixed with the capture timestamp so sorting by name is sorting by capture time. Folder grouping and prefix formats are configurable.
- **Restore** — pull originals back by source and date range, re-hashed on arrival, with original filenames and folder structure reconstructed.

## Architecture at a glance

```
 Sources                     PhotoVault host (NAS / home server)              Cloud (S3-compatible)
┌──────────────┐   sync/    ┌──────────────────────────────────────┐        ┌─────────────────────┐
│ Phone A      │──copy/────▶│ staging/phone-a/   ┐                 │        │ originals/ab/<sha>  │
│ Phone B      │  upload    │ staging/phone-b/   ├─▶ Ingest ─┐     │ upload │ thumbs/ab/<sha>.jpg │
│ DSLR SD card │──USB──────▶│ staging/dslr/      ┘  (hash,   │     │──────▶ │ catalog/snapshots/  │
└──────────────┘            │                       exif,    ▼     │ +sha256│ manifest.json       │
                            │                       dedup)  Catalog│ verify └─────────────────────┘
                            │                              (SQLite)│
                            │  CLI (typer)  ◀──────────────┘  ▲    │
                            │  Web UI + API (FastAPI) ─────────┘   │
                            └──────────────────────────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design, [docs/INTEGRITY.md](docs/INTEGRITY.md) for the verification and safe-delete specification, [docs/ROADMAP.md](docs/ROADMAP.md) for build milestones, and [docs/DECISIONS.md](docs/DECISIONS.md) for the architectural decision records.

## CLI overview

| Command | Purpose |
|---|---|
| `photovault ingest <dir> --source <name>` | Hash, extract metadata, thumbnail, and catalog everything in a staging folder |
| `photovault watch` | Daemon mode: continuously ingest configured staging folders |
| `photovault sync` | Upload pending assets to cloud storage with checksum verification; snapshot the catalog |
| `photovault verify --remote [--sample N]` | Reconcile catalog vs. bucket; deep-verify a random sample by re-download + re-hash |
| `photovault check-device <path> --source <name>` | Classify every file on a device as SAFE / UPLOADED-UNVERIFIED / KNOWN-NOT-UPLOADED / UNKNOWN |
| `photovault prune-device <path> --report <json>` | Delete only SAFE files from a fresh report (dry-run by default) |
| `photovault restore --source X --from DATE --to DATE --dest <dir>` | Download originals, re-hash on arrival, restore names/structure |
| `photovault build-browse [--clean]` | (Re)build the human-browsable date/timestamp tree from the catalog |
| `photovault serve` | Run the web UI + API |

## FAQ

**How much does the cloud storage cost?**
With Backblaze B2 or Cloudflare R2, roughly **$5–6 per TB per month**. A 500 GB library costs about $3/month. AWS S3 is pricier but offers lifecycle tiering to Glacier for cold storage.

**Why S3-compatible instead of Google Drive / Dropbox?**
The S3 API supports server-side checksum verification on upload (`ChecksumSHA256`), efficient bulk listing for reconciliation, and is offered by many competing vendors — so PhotoVault stays vendor-independent and the integrity guarantees stay cryptographic rather than hopeful.

**When is it safe to delete photos from my phone or SD card?**
Only when `photovault check-device` reports **100% SAFE** — meaning every file currently on the device has been ingested *and* its cloud copy has passed checksum verification. The check hashes the actual bytes on the device at that moment, so nothing can slip through.

**What if my server dies?**
The catalog is snapshotted to the bucket after every sync. Point a fresh PhotoVault install at the bucket and it rebuilds the full library, metadata and all.

## Stack

Python 3.12 · Typer (CLI) · FastAPI (API/web) · SQLAlchemy + SQLite (catalog) · boto3 (S3) · ExifTool via PyExifTool (metadata) · Pillow (thumbnails)

## Quick start

```bash
# host needs Python 3.11+ and exiftool (apt install libimage-exiftool-perl)
pip install .

cp config.example.toml ~/.config/photovault/config.toml   # edit: library path, bucket, sources
export PHOTOVAULT_S3_KEY=... PHOTOVAULT_S3_SECRET=...

photovault init
photovault ingest ~/staging/phone-a --source phone-a   # catalog + thumbnail everything
photovault sync                                        # upload with checksum verification
photovault verify --remote                             # independent verification pass
photovault check-device /mnt/sdcard                    # is this device safe to wipe?
photovault serve                                       # browse at http://localhost:8420
```

Or with Docker: put `config.toml` in `./config/`, then `docker compose up -d`
(runs the web UI plus the `watch` ingest/sync daemon).

The safe-deletion loop:

```bash
photovault check-device /mnt/sdcard --out report.json  # exit 0 only if 100% SAFE
photovault prune-device /mnt/sdcard --report report.json            # dry-run
photovault prune-device /mnt/sdcard --report report.json --execute  # actually delete
```

## Status

Implemented: ingest (multi-source, dedup, EXIF + filename-date inference,
thumbnails), verified cloud sync with catalog snapshots, remote verification
with rotating deep checks, check-device/prune-device, restore, catalog rebuild
from bucket, web UI (timeline/filters/detail/dashboard), watch daemon, and the
mobile upload API. Test suite: `pip install -e ".[dev]" && pytest`.
See [docs/ROADMAP.md](docs/ROADMAP.md) for what's next (M6 extensions).
