"""Restore originals from the bucket; rebuild the catalog from a snapshot."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .hashing import sha256_file
from .models import Asset, AssetInstance, Source
from .storage import StorageClient


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # hash mismatch or download error


def restore(
    session: Session,
    config: Config,
    dest: Path,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    shas: list[str] | None = None,
    storage: StorageClient | None = None,
) -> RestoreResult:
    storage = storage or StorageClient(config)
    dest = Path(dest)
    result = RestoreResult()

    q = select(Asset).order_by(Asset.capture_ts)
    if shas:
        q = q.where(Asset.sha256.in_(shas))
    if date_from:
        q = q.where(Asset.capture_ts >= date_from)
    if date_to:
        q = q.where(Asset.capture_ts <= date_to + "T99")  # inclusive end date
    if source:
        src = session.scalar(select(Source).where(Source.name == source))
        if src is None:
            raise ValueError(f"Unknown source: {source}")
        q = q.join(AssetInstance, AssetInstance.sha256 == Asset.sha256).where(
            AssetInstance.source_id == src.id
        ).distinct()

    for asset in session.scalars(q):
        inst = session.scalar(
            select(AssetInstance).where(AssetInstance.sha256 == asset.sha256)
        )
        name = inst.original_filename if inst else f"{asset.sha256}.{asset.ext}"
        ym = (asset.capture_ts or "0000-00")[:7].replace("-", "/")
        out = dest / ym / name
        n = 1
        while out.exists() and sha256_file(out) != asset.sha256:
            out = out.with_name(f"{out.stem}-{n}{out.suffix}")  # name collision, different photo
            n += 1
        try:
            storage.download(asset.storage_key, out)
            if sha256_file(out) != asset.sha256:  # re-hash on arrival, fail loudly
                out.unlink(missing_ok=True)
                result.failed.append(asset.sha256)
                continue
        except Exception:
            result.failed.append(asset.sha256)
            continue
        result.restored.append(str(out))
    return result


def rebuild_from_bucket(config: Config, storage: StorageClient | None = None, force: bool = False) -> str:
    """Bootstrap a fresh host: download the newest catalog snapshot from the bucket."""
    storage = storage or StorageClient(config)
    if config.db_path.exists() and not force:
        raise FileExistsError(
            f"Catalog already exists at {config.db_path}; pass --force to overwrite"
        )
    snapshots = sorted(
        k for k, _ in storage.list_all("catalog/snapshot-")
        if re.fullmatch(r"catalog/snapshot-\d{8}T\d{6}Z\.db", k)
    )
    if not snapshots:
        raise FileNotFoundError("No catalog snapshots found in bucket")
    newest = snapshots[-1]
    config.ensure_dirs()
    storage.download(newest, config.db_path)
    return newest
