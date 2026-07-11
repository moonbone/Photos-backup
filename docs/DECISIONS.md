# Architecture Decision Records

Lightweight ADRs for choices made during design. Each is reversible; revisiting one means updating this file and the affected docs.

## ADR-1: S3-compatible object storage, pluggable endpoint

**Decision**: Target the S3 API generically (endpoint + bucket + credentials) rather than a specific vendor or a consumer cloud (Google Drive/Dropbox).

**Rationale**: One boto3 integration covers AWS S3, Backblaze B2, Cloudflare R2, Wasabi, and self-hosted MinIO (also used for tests). The S3 API provides `ChecksumSHA256` server-side verification on upload — load-bearing for the integrity model — plus efficient paged listings for reconciliation. Cheapest durable options run ~$5–6/TB/month. Consumer-cloud APIs are quota-limited and lack checksum guarantees.

**Consequences**: Users need an S3-style account/bucket (slightly more setup than a consumer cloud); provider-specific setup guides mitigate.

## ADR-2: Python 3.12 stack

**Decision**: Python with Typer (CLI), FastAPI (API/web), SQLAlchemy + SQLite (catalog), boto3 (S3), PyExifTool (metadata), Pillow (thumbnails).

**Rationale**: ExifTool is the gold standard for metadata across camera vendors and Python wraps it well; development speed matters more than raw throughput at personal-library scale (hashing and upload are I/O-bound anyway). Alternatives considered: Go (nicer single-binary deploy, weaker metadata ecosystem), TypeScript (one language with the UI, weaker CLI/data ergonomics).

**Consequences**: Deployment needs a Python runtime or the provided Docker image; ExifTool is a runtime dependency.

## ADR-3: SHA-256 content hash as canonical photo identity

**Decision**: An asset *is* its SHA-256; dedup, cloud keys, and all verification derive from it.

**Rationale**: Cross-device dedup for free; idempotent uploads; verification anywhere is "re-hash and compare"; the storage key itself encodes the expected content.

**Consequences**: Edited/re-encoded copies are distinct assets (correct for backup; near-duplicate *reporting* is a roadmap extension). Metadata edits must be stored as catalog overrides, never by rewriting the file.

## ADR-4: SQLite catalog with cloud snapshots

**Decision**: Single-file SQLite (WAL) as the source of truth, snapshotted to the bucket after every sync/verify along with a JSON manifest.

**Rationale**: Single-user, single-host workload; zero-ops; trivially snapshottable. The cloud snapshot makes the backup self-describing — a dead host is recovered from the bucket alone.

**Consequences**: Not multi-writer; the web UI and daemon share one process/host. Fine for the intended deployment.

## ADR-5: Trust-before-delete; the system never deletes from devices implicitly

**Decision**: Device deletion is a separate `prune-device` command, driven by a fresh `check-device` report, dry-run by default, re-hashing each file immediately before deletion. `check-device` classifies by hashing the *actual device contents*, and only independently `verified` assets count as SAFE.

**Rationale**: The product's core promise is confidence before deletion; any implicit or convenience deletion path undermines it.

**Consequences**: Deleting from a device is a deliberate two-step. That friction is the feature.

## ADR-6: Capture-date resolution chain with provenance

**Decision**: Capture date resolves EXIF → filename-pattern inference → file mtime, recording `capture_ts_source` on every asset; inferred dates are flagged in the UI and pattern list is user-extensible. (Added at user request.)

**Rationale**: Many real-world files (WhatsApp saves, screenshots, exports) lack EXIF but encode their date in the filename; mtime is often wrong after copies. Provenance keeps inferred dates honest and reviewable, with sanity bounds (1990–now) preventing garbage matches.

**Consequences**: A pattern library to maintain; date-only matches need explicit `date_only` handling in sorting.

## ADR-7: Self-hosted web UI, server-rendered + htmx first

**Decision**: FastAPI serving Jinja + htmx for the browse UI, rather than a SPA or native apps.

**Rationale**: Meets the browse-by-date/source-with-metadata requirement with minimal moving parts; works from phone browsers; a React SPA or native app can replace it later without touching the API/catalog.

**Consequences**: Less app-like polish initially; no offline mobile browsing.

## ADR-8: Staging folders first, direct upload endpoint second

**Decision**: Phase 1 ingestion is watched staging folders fed by existing sync tools (Syncthing/PhotoSync/manual SD copy); a direct authenticated upload endpoint comes in M5; active device pull (SD/MTP detection) is optional later.

**Rationale**: Staging folders deliver full multi-source coverage on day one with zero custom device code, and every later ingestion path reuses the same pipeline.

**Consequences**: Until M5, phones need a sync tool configured (well-documented, one-time setup).
