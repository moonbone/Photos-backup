"""Integrity Layer 2: bucket reconciliation + sampled deep verification.

See docs/INTEGRITY.md §2. Promotes uploaded -> verified; demotes and
re-queues anything missing or corrupted (which should never happen — hence
it is loud when it does).
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .hashing import sha256_file
from .models import (
    Asset,
    RemoteCopy,
    STATE_NEW,
    STATE_UPLOADED,
    STATE_VERIFIED,
    VerifyRun,
    utcnow_iso,
)
from .storage import StorageClient


@dataclass
class VerifyResult:
    run_id: int
    checked: int = 0
    promoted: int = 0
    missing: list[str] = field(default_factory=list)  # sha256s demoted (gone/size-mismatch)
    mismatched: list[str] = field(default_factory=list)  # sha256s demoted (deep hash mismatch)
    orphaned: list[str] = field(default_factory=list)  # bucket keys with no catalog row
    deep_verified: int = 0
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched


def verify_remote(
    session: Session,
    config: Config,
    storage: StorageClient | None = None,
    sample: int | None = None,
) -> VerifyResult:
    storage = storage or StorageClient(config)
    run = VerifyRun(kind="listing")
    session.add(run)
    session.commit()
    result = VerifyResult(run_id=run.id)

    # --- listing reconcile -------------------------------------------
    listing: dict[str, int] = dict(storage.list_all("originals/"))
    copies = list(
        session.scalars(select(RemoteCopy).where(RemoteCopy.bucket == storage.bucket))
    )
    known_keys = set()
    for copy in copies:
        known_keys.add(copy.storage_key)
        asset = session.get(Asset, copy.sha256)
        result.checked += 1
        remote_size = listing.get(copy.storage_key)
        if remote_size is None or remote_size != copy.size_bytes:
            _demote(session, asset, copy)
            result.missing.append(copy.sha256)
            continue
        if asset.backup_state == STATE_UPLOADED:
            asset.backup_state = STATE_VERIFIED
            result.promoted += 1
        copy.last_verified_at = utcnow_iso()
        if copy.last_verify_method != "deep":
            copy.last_verify_method = "listing"
    session.commit()

    result.orphaned = sorted(set(listing) - known_keys)

    # --- sampled deep verify ------------------------------------------
    n = sample if sample is not None else config.verify.sample_size
    if n:
        deep_targets = list(
            session.scalars(
                select(RemoteCopy)
                .join(Asset, Asset.sha256 == RemoteCopy.sha256)
                .where(
                    RemoteCopy.bucket == storage.bucket,
                    Asset.backup_state == STATE_VERIFIED,
                )
                .order_by(RemoteCopy.last_verify_method == "deep", RemoteCopy.last_verified_at)
                .limit(n)
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            for copy in deep_targets:
                dest = Path(tmp) / "obj"
                try:
                    storage.download(copy.storage_key, dest)
                    actual = sha256_file(dest)
                except Exception:
                    actual = None
                if actual != copy.sha256:
                    asset = session.get(Asset, copy.sha256)
                    _demote(session, asset, copy)
                    result.mismatched.append(copy.sha256)
                else:
                    copy.last_verified_at = utcnow_iso()
                    copy.last_verify_method = "deep"
                    result.deep_verified += 1
                dest.unlink(missing_ok=True)
        session.commit()

    # --- report ---------------------------------------------------------
    report = {
        "report_version": 1,
        "kind": "verify-remote",
        "generated_at": utcnow_iso(),
        "bucket": storage.bucket,
        "checked": result.checked,
        "promoted": result.promoted,
        "deep_verified": result.deep_verified,
        "missing": result.missing,
        "mismatched": result.mismatched,
        "orphaned": result.orphaned,
        "ok": result.ok,
    }
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = config.reports_dir / f"verify-{utcnow_iso().replace(':', '')}.json"
    report_file.write_text(json.dumps(report, indent=1))
    result.report_path = str(report_file)

    run.finished_at = utcnow_iso()
    run.objects_checked = result.checked + result.deep_verified
    run.missing = len(result.missing)
    run.mismatches = len(result.mismatched)
    run.orphaned = len(result.orphaned)
    run.report_path = str(report_file)
    session.commit()
    return result


def _demote(session: Session, asset: Asset | None, copy: RemoteCopy) -> None:
    """Missing/corrupt remote object: re-queue the asset, drop the remote record."""
    if asset is not None:
        asset.backup_state = STATE_NEW
    session.delete(copy)
