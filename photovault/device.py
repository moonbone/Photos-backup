"""Trust-before-delete: check-device and prune-device.

See docs/INTEGRITY.md §3–4. check-device hashes the bytes actually on the
device *now* — it inspects reality, not the catalog's beliefs. prune-device
deletes only SAFE entries from a fresh report, re-hashing each file
immediately before deletion. PhotoVault never deletes from a device in any
other code path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .config import Config
from .hashing import sha256_file
from .ingest import iter_media_files
from .models import Asset, STATE_NEW, STATE_UPLOADED, STATE_VERIFIED, VerifyRun, utcnow_iso

CLASS_SAFE = "SAFE"
CLASS_UPLOADED_UNVERIFIED = "UPLOADED-UNVERIFIED"
CLASS_KNOWN_NOT_UPLOADED = "KNOWN-NOT-UPLOADED"
CLASS_UNKNOWN = "UNKNOWN"

REMEDY = {
    CLASS_UPLOADED_UNVERIFIED: "run `photovault verify --remote`",
    CLASS_KNOWN_NOT_UPLOADED: "run `photovault sync`",
    CLASS_UNKNOWN: "ingest this file (`photovault ingest`)",
}

REPORT_MAX_AGE_HOURS = 24


@dataclass
class DeviceCheck:
    device_path: str
    source: str | None
    generated_at: str
    files: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    report_path: str | None = None

    @property
    def all_safe(self) -> bool:
        return self.totals.get("files", 0) == self.totals.get("safe", 0)


def _classify(asset: Asset | None) -> str:
    if asset is None:
        return CLASS_UNKNOWN
    return {
        STATE_VERIFIED: CLASS_SAFE,
        STATE_UPLOADED: CLASS_UPLOADED_UNVERIFIED,
        STATE_NEW: CLASS_KNOWN_NOT_UPLOADED,
    }.get(asset.backup_state, CLASS_UNKNOWN)


def check_device(
    session: Session, config: Config, device_path: Path, source: str | None = None
) -> DeviceCheck:
    device_path = Path(device_path).resolve()
    check = DeviceCheck(
        device_path=str(device_path), source=source, generated_at=utcnow_iso()
    )
    counts = {"files": 0, "safe": 0, "uploaded_unverified": 0, "known_not_uploaded": 0, "unknown": 0}

    for f in iter_media_files(device_path):
        sha = sha256_file(f)
        cls = _classify(session.get(Asset, sha))
        counts["files"] += 1
        counts[cls.lower().replace("-", "_")] += 1
        check.files.append(
            {
                "path": str(f.relative_to(device_path)),
                "sha256": sha,
                "size": f.stat().st_size,
                "class": cls,
            }
        )
    check.totals = counts

    config.reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = config.reports_dir / f"check-device-{check.generated_at.replace(':', '')}.json"
    report_file.write_text(
        json.dumps(
            {
                "report_version": 1,
                "kind": "check-device",
                "device_path": check.device_path,
                "source": source,
                "generated_at": check.generated_at,
                "totals": counts,
                "all_safe": check.all_safe,
                "files": check.files,
            },
            indent=1,
        )
    )
    check.report_path = str(report_file)

    session.add(
        VerifyRun(
            kind="device",
            finished_at=utcnow_iso(),
            objects_checked=counts["files"],
            missing=counts["files"] - counts["safe"],
            report_path=str(report_file),
        )
    )
    session.commit()
    return check


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    skipped_changed: list[str] = field(default_factory=list)  # bytes changed since report
    skipped_missing: list[str] = field(default_factory=list)  # no longer on device
    dry_run: bool = True
    log_path: str | None = None


def prune_device(
    config: Config,
    device_path: Path,
    report_path: Path,
    execute: bool = False,
    max_age_hours: int = REPORT_MAX_AGE_HOURS,
) -> PruneResult:
    device_path = Path(device_path).resolve()
    report = json.loads(Path(report_path).read_text())

    if report.get("kind") != "check-device":
        raise ValueError("Not a check-device report")
    if Path(report["device_path"]).resolve() != device_path:
        raise ValueError(
            f"Report was generated for {report['device_path']}, not {device_path}"
        )
    generated = datetime.strptime(report["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if datetime.now(timezone.utc) - generated > timedelta(hours=max_age_hours):
        raise ValueError(
            f"Report is older than {max_age_hours}h — re-run `photovault check-device` first"
        )

    result = PruneResult(dry_run=not execute)
    log_entries = []
    for entry in report["files"]:
        if entry["class"] != CLASS_SAFE:
            continue  # only SAFE files are ever eligible
        f = device_path / entry["path"]
        if not f.exists():
            result.skipped_missing.append(entry["path"])
            continue
        # re-hash immediately before deletion: edited/re-synced bytes are not SAFE
        if sha256_file(f) != entry["sha256"]:
            result.skipped_changed.append(entry["path"])
            continue
        if execute:
            f.unlink()
            log_entries.append(
                {"path": entry["path"], "sha256": entry["sha256"], "deleted_at": utcnow_iso()}
            )
        result.deleted.append(entry["path"])

    if execute and log_entries:
        config.reports_dir.mkdir(parents=True, exist_ok=True)
        log_file = config.reports_dir / "deletions.jsonl"
        with open(log_file, "a") as fh:
            for e in log_entries:
                fh.write(json.dumps(e) + "\n")
        result.log_path = str(log_file)
    return result
