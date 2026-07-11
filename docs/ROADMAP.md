# PhotoVault Roadmap

Phased milestones, each independently useful and shippable. Acceptance criteria are written so a milestone is "done" only when it can be demonstrated end-to-end.

**Status: M1–M5 are implemented and covered by the test suite** (unit tests for
every date pattern; sync/verify/device/restore/web exercised against a mocked
S3 via moto). Notes on scope deltas: uploads are single-request (up to 5 GB)
rather than multipart; the web UI is plain server-rendered templates;
`import-card` and active device pull remain future work (M6).

## M1 — Core ingest & catalog (CLI)

Scope: project scaffolding (pyproject, Typer CLI, SQLAlchemy models, config loading), `photovault ingest`, metadata extraction, capture-date resolution chain (EXIF → filename patterns → mtime, with `capture_ts_source` recorded), thumbnails/previews, dedup, `ingest_runs` accounting, local library storage.

Acceptance:
- Ingesting a mixed folder (JPEG, RAW+JPEG pair, video, a file with no EXIF but a `IMG_20240711_183000.jpg`-style name, a file with neither) produces correct `assets` rows with the right `capture_ts_source` for each.
- Re-ingesting the same folder, and ingesting the same files under a second `--source`, creates no duplicate assets — only new instances.
- Unit tests cover every built-in filename pattern plus the sanity-bounds rejection and `date_only` handling.

## M2 — Cloud sync

Scope: S3 client with pluggable endpoint, `photovault sync` with `ChecksumSHA256` uploads, multipart for large files, retry/backoff, resumability, backup-state transitions `new → uploaded`, catalog snapshots + `manifest.json` to the bucket.

Acceptance:
- Against a local MinIO container: full sync of a test library; killing sync mid-run and re-running completes with no duplicates and no missed assets.
- A deliberately corrupted upload (mismatched declared checksum) is rejected by the server and the asset remains `new`.
- The catalog snapshot appears in the bucket after every run.

## M3 — Integrity & safe delete (the safety milestone)

Scope: `photovault verify --remote` (listing reconcile + sampled deep verify), `verify_runs` audit rows, state promotion to `verified` and demotion on mismatch, `photovault check-device` with the four-class report (per [INTEGRITY.md](INTEGRITY.md)), `photovault prune-device` with freshness check, pre-delete re-hash, dry-run default, and deletion log.

Acceptance:
- Deleting an object from the MinIO bucket behind PhotoVault's back is detected by `verify`, demotes the asset, and re-sync restores it.
- `check-device` on a device containing one never-ingested file exits nonzero and lists it as UNKNOWN.
- `prune-device --execute` removes only SAFE files, skips a file modified after the report, and refuses a stale report.

After M3 the system delivers its core promise and can be used in earnest.

## M4 — Web UI

Scope: `photovault serve` — timeline view (day/month/year grouping, infinite scroll), source and camera filters, filter by `capture_ts_source` to review inferred dates, photo detail page (preview, full EXIF, sightings, backup history, date-provenance badge), status dashboard with per-source stats and warnings, token auth.

Acceptance:
- Browse a 10k-asset test library smoothly from a phone browser on the LAN; every photo shows its metadata and backup state; dashboard flags a source with pending uploads.

## M5 — Continuous operation

Scope: `photovault watch` daemon (stability window, per-source folders, periodic auto-sync), `POST /api/upload` endpoint (token auth, client-checksum echo) for PhotoSync/Shortcuts, scheduled verify documentation (cron/systemd), Docker + compose deployment.

Acceptance:
- A photo taken on a phone lands in the timeline with no manual steps (via sync tool or upload endpoint) and reaches `verified` after the scheduled verify.

## M6 — Extensions (prioritize as needed)

- Restore polish: `--original-layout`, restore sidecars, per-sha selection in the CLI.
- `photovault import-card` helper + active device pull (SD/MTP auto-import on mount).
- Multipart uploads with composed checksums for >5 GB videos.
- Optional client-side encryption (age/AES-GCM) with documented trade-offs.
- Map view from GPS metadata.
- Near-duplicate report (perceptual hash) — review tool only, never auto-delete.
- Full video metadata (video thumbnails already work where ffmpeg is installed).
- Second-bucket replication (guards against provider loss).
- Manual date-correction UI (stored as `capture_ts_source = manual` overrides).
