# PhotoVault Integrity & Safe-Delete Specification

This document specifies the guarantees behind the core promise: **you can know, with cryptographic confidence, that every photo on a device is safely backed up before you delete it.**

## 1. Principles

1. **Nothing is trusted without a hash.** Filenames, sizes, and timestamps are hints; only SHA-256 comparisons count as verification.
2. **Verification is layered and repeated.** An upload confirmed once is not "verified forever" — the system re-checks over time to catch silent corruption.
3. **The device check inspects reality, not the catalog's beliefs.** `check-device` hashes the bytes actually on the device at that moment, so files that were never ingested cannot be missed.
4. **Deletion is never a side effect.** It is a separate, explicit command, dry-run by default, driven by a fresh verification report.

## 2. The three verification layers

### Layer 1 — Upload-time verification (immediate)

Every upload includes the SHA-256 computed at ingest as the S3 `ChecksumSHA256`. The storage server independently recomputes the checksum of the bytes it received and **rejects the upload on mismatch**. Success promotes the asset `new → uploaded` and records `upload_checksum` in `remote_copies`.

This proves: *the bytes that arrived in the bucket are exactly the bytes that were hashed at ingest.*

### Layer 2 — Reconciliation + sampled deep verify (`photovault verify --remote`)

1. **Listing reconcile**: page through the full bucket listing under `originals/` and compare against `remote_copies`:
   - **Missing** — cataloged as uploaded but absent from the bucket → demote to `new`, re-queue, alert.
   - **Size mismatch** — object exists but wrong size → treat as missing.
   - **Orphaned** — object in the bucket with no catalog row → report (never auto-delete; likely a snapshot/manual artifact).
   - Objects present with matching size promote `uploaded → verified` (`last_verify_method = listing`) and refresh `last_verified_at`.
2. **Sampled deep verify** (`--sample N`, default 50): download N objects chosen preferentially by *oldest `last_verified_at`*, re-hash locally, compare to the catalog hash (which is also the storage key). Records `last_verify_method = deep`.

Every run writes a `verify_runs` row and a report file (human-readable + JSON).

### Layer 3 — Scheduled audit

A scheduled job (cron/systemd timer, documented in deployment) runs Layer 2 monthly. Because deep-verify sampling prefers the least-recently-verified objects, the whole library rotates through deep verification (~600 objects/year at default settings; the sample size is configurable to match library size so full rotation completes within a target period).

This catches: bit rot or provider-side loss *after* a successful upload, catalog/bucket drift, and accidental manual deletions in the bucket.

## 3. Device safety check — `photovault check-device`

```
photovault check-device /mnt/sdcard --source dslr [--out report.json]
```

Procedure:

1. Walk every media file on the mounted device/folder (same extension filter as ingest; `--all-files` widens it).
2. **Hash every file** — the actual bytes on the device now.
3. Classify each file:

| Class | Condition | Meaning |
|---|---|---|
| **SAFE** | hash in `assets` **and** `backup_state = verified` | Backed up and independently verified in the cloud — deletable |
| **UPLOADED-UNVERIFIED** | hash in `assets`, `backup_state = uploaded` | In the cloud with upload-time checksum, but no independent verify run yet — run `verify` first |
| **KNOWN-NOT-UPLOADED** | hash in `assets`, `backup_state = new` | Ingested but not yet in the cloud — run `sync` |
| **UNKNOWN** | hash not in `assets` | Never ingested — this is exactly the file you'd have lost; ingest it |

4. Output:
   - Human-readable summary: counts per class, and the full list of every non-SAFE file with its class and suggested remedy.
   - Machine-readable JSON report: device path, source, timestamp, per-file `{path, sha256, size, class}` — consumed by `prune-device`.
   - **Exit code 0 only if 100% of files are SAFE**; nonzero otherwise (scriptable: `check-device && echo "ok to wipe"`).

The intended workflow when not everything is SAFE: `ingest` the staging copy (or the device itself) → `sync` → `verify --remote` → re-run `check-device`. Each step moves classes toward SAFE.

## 4. Deletion — `photovault prune-device`

```
photovault prune-device /mnt/sdcard --report report.json [--dry-run|--execute]
```

Safety properties:

- Operates **only** on files listed as SAFE in the supplied report.
- The report must be **fresh** (default max age 24 h) and must match the device path; otherwise the command refuses.
- **Dry-run is the default** — it prints exactly what would be deleted and the space reclaimed. Actual deletion requires `--execute`.
- Immediately before deleting each file, it is **re-hashed**; if the bytes changed since the report (edited, re-synced), the file is skipped and reported.
- Files on the device that are not in the report (added after the check) are never touched.
- A deletion log (path, sha256, deleted_at) is written for audit.

PhotoVault never deletes from a device in any other code path.

## 5. What each guarantee does and does not cover

| Risk | Covered by |
|---|---|
| Upload corrupted in transit | Layer 1 server-side checksum |
| File never ingested at all | `check-device` hashes the device itself → UNKNOWN |
| Cloud object lost/corrupted after upload | Layer 2/3 reconcile + rotating deep verify |
| Catalog says uploaded but object missing | Layer 2 listing reconcile |
| File edited on device after backup | Pre-delete re-hash in `prune-device` (new bytes = not SAFE) |
| Host disk failure | Catalog snapshots in the bucket (`rebuild --from-bucket`) |
| Ransomware/accidental bucket deletion | *Mitigation only*: optional deny-delete IAM policy / object lock, documented per provider |
| Provider total loss | Out of scope for v1; a second-bucket replication target is a roadmap extension |

## 6. Report format (JSON)

```json
{
  "report_version": 1,
  "kind": "check-device",
  "device_path": "/mnt/sdcard",
  "source": "dslr",
  "generated_at": "2026-07-11T18:30:00+03:00",
  "catalog_snapshot": "catalog/snapshot-20260711T152000Z.db",
  "totals": {"files": 1240, "safe": 1238, "uploaded_unverified": 0, "known_not_uploaded": 1, "unknown": 1},
  "all_safe": false,
  "files": [
    {"path": "DCIM/100NIKON/DSC_0042.NEF", "sha256": "ab12…", "size": 24117248, "class": "SAFE"},
    {"path": "DCIM/100NIKON/DSC_0043.NEF", "sha256": "cd34…", "size": 24117248, "class": "UNKNOWN"}
  ]
}
```
