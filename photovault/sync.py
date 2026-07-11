"""Cloud sync engine: upload pending assets, then snapshot the catalog.

State machine (docs/ARCHITECTURE.md §6): new -> uploaded (server checksum OK).
Interrupted syncs are harmless — state lives in the catalog and uploads are
idempotent (same content-addressed key, same bytes).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Config
from .ingest import object_path, sidecar_path
from .models import Asset, RemoteCopy, STATE_NEW, STATE_UPLOADED, utcnow_iso
from .storage import ChecksumMismatch, StorageClient
from .thumbs import preview_path, thumb_path

_MAX_ATTEMPTS = 4


@dataclass
class SyncResult:
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    snapshot_key: str | None = None


def pending_assets(session: Session) -> list[Asset]:
    return list(
        session.scalars(
            select(Asset).where(Asset.backup_state == STATE_NEW).order_by(Asset.capture_ts)
        )
    )


def sync(session: Session, config: Config, storage: StorageClient | None = None) -> SyncResult:
    storage = storage or StorageClient(config)
    result = SyncResult()

    for asset in pending_assets(session):
        local = object_path(config, asset.sha256, asset.ext)
        if not local.exists():
            result.skipped += 1
            result.failures.append(f"{asset.sha256}: original missing from library")
            continue
        try:
            checksum = _upload_with_retry(storage, local, asset.storage_key, asset.sha256)
        except Exception as exc:
            result.failed += 1
            result.failures.append(f"{asset.sha256}: {exc}")
            continue  # stays `new`; sync never silently drops work

        # best-effort derivative + sidecar uploads (not part of the guarantee)
        for path, key in (
            (thumb_path(config, asset.sha256), f"thumbs/{asset.sha256[:2]}/{asset.sha256}.jpg"),
            (preview_path(config, asset.sha256), f"previews/{asset.sha256[:2]}/{asset.sha256}.jpg"),
            (sidecar_path(config, asset.sha256), asset.sidecar_key),
        ):
            if path.exists():
                try:
                    storage.put_bytes(path.read_bytes(), key, "image/jpeg")
                except Exception:
                    pass

        session.merge(
            RemoteCopy(
                sha256=asset.sha256,
                bucket=storage.bucket,
                storage_key=asset.storage_key,
                size_bytes=asset.size_bytes,
                upload_checksum=checksum,
                uploaded_at=utcnow_iso(),
            )
        )
        asset.backup_state = STATE_UPLOADED
        session.commit()
        result.uploaded += 1

    result.snapshot_key = snapshot_catalog(session, config, storage)
    return result


def _upload_with_retry(storage: StorageClient, path: Path, key: str, sha: str) -> str:
    delay = 2.0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return storage.put_file(path, key, sha)
        except ChecksumMismatch:
            raise  # local corruption is permanent; retrying can't help
        except Exception:
            if attempt == _MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def snapshot_catalog(session: Session, config: Config, storage: StorageClient) -> str:
    """Upload a consistent SQLite backup + JSON manifest. Makes the bucket self-describing."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_key = f"catalog/snapshot-{ts}.db"

    with tempfile.TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(str(config.db_path))
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        storage.put_bytes(backup_path.read_bytes(), snapshot_key, "application/x-sqlite3")

    counts = dict(
        session.execute(
            select(Asset.backup_state, func.count()).group_by(Asset.backup_state)
        ).all()
    )
    objects = {
        a.sha256: {"key": a.storage_key, "size": a.size_bytes}
        for a in session.scalars(select(Asset).where(Asset.backup_state != STATE_NEW))
    }
    manifest = {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "snapshot_key": snapshot_key,
        "counts": counts,
        "objects": objects,
    }
    storage.put_bytes(
        json.dumps(manifest, indent=1).encode(), "catalog/manifest.json", "application/json"
    )
    return snapshot_key
