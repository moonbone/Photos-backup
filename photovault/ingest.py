"""Ingest pipeline: hash -> dedup -> metadata -> date resolution -> derivatives -> catalog.

See docs/ARCHITECTURE.md §3. Originals are copied into the local library
(objects/<sha[0:2]>/<sha>.<ext>) so staging folders can be emptied
independently of upload progress.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .dates import resolve_capture_date
from .hashing import sha256_file
from .metadata import (
    MEDIA_EXTS,
    SIDECAR_EXTS,
    core_fields,
    exif_json_dump,
    extract_batch,
    kind_for_ext,
)
from .models import Asset, AssetInstance, IngestRun, Source, utcnow_iso
from .thumbs import make_derivatives

_BATCH = 32


@dataclass
class IngestResult:
    run_id: int
    files_seen: int = 0
    files_new: int = 0
    files_duplicate: int = 0
    files_failed: int = 0
    failures: list[str] = field(default_factory=list)
    new_shas: list[str] = field(default_factory=list)


def get_or_create_source(session: Session, name: str, staging_path: str | None = None) -> Source:
    source = session.scalar(select(Source).where(Source.name == name))
    if source is None:
        source = Source(name=name, staging_path=staging_path)
        session.add(source)
        session.commit()
    return source


def object_path(config: Config, sha256: str, ext: str) -> Path:
    return config.objects_dir / sha256[:2] / f"{sha256}.{ext}"


def sidecar_path(config: Config, sha256: str) -> Path:
    return config.objects_dir / sha256[:2] / f"{sha256}.xmp"


def iter_media_files(root: Path) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower().lstrip(".") in MEDIA_EXTS:
            files.append(p)
    return files


def find_sidecars(root: Path) -> dict[str, Path]:
    """Map of lowercased stem-path -> sidecar file (DSC_0042.xmp or DSC_0042.NEF.xmp)."""
    out: dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower().lstrip(".") in SIDECAR_EXTS:
            out[str(p.with_suffix("")).lower()] = p
    return out


def ingest_path(
    session: Session, config: Config, root: Path, source_name: str
) -> IngestResult:
    """One-shot ingest of every media file under `root` for the given source."""
    root = Path(root)
    files = iter_media_files(root)
    return ingest_files(session, config, files, source_name, root=root)


def ingest_files(
    session: Session,
    config: Config,
    files: list[Path],
    source_name: str,
    root: Path | None = None,
) -> IngestResult:
    config.ensure_dirs()
    src_cfg = config.source(source_name)
    source = get_or_create_source(
        session, source_name, staging_path=src_cfg.staging if src_cfg else None
    )
    run = IngestRun(source_id=source.id)
    session.add(run)
    session.commit()
    result = IngestResult(run_id=run.id)

    sidecars = find_sidecars(root) if root else {}
    hashes_seen: set[str] = set()  # dedup within this run, across batches

    for batch_start in range(0, len(files), _BATCH):
        batch = files[batch_start : batch_start + _BATCH]
        hashes: dict[Path, str] = {}
        for path in batch:
            result.files_seen += 1
            try:
                hashes[path] = sha256_file(path)
            except Exception as exc:
                result.files_failed += 1
                result.failures.append(f"{path}: {exc}")
        new_files = [
            p for p, sha in hashes.items()
            if sha not in hashes_seen and session.get(Asset, sha) is None
        ]
        hashes_seen.update(hashes.values())
        metadata = extract_batch(new_files)
        for path, sha in hashes.items():
            try:
                rel = str(path.relative_to(root)) if root else path.name
                if path not in new_files or session.get(Asset, sha) is not None:
                    _record_instance(session, sha, source.id, rel, path, run.id)
                    result.files_duplicate += 1
                    continue
                _ingest_new(
                    session, config, path, sha, rel, source, run.id,
                    metadata.get(path, {}), sidecars, root,
                )
                result.files_new += 1
                result.new_shas.append(sha)
            except Exception as exc:  # per-file isolation: one bad file never kills a run
                session.rollback()
                result.files_failed += 1
                result.failures.append(f"{path}: {exc}")
        session.commit()

    run.finished_at = utcnow_iso()
    run.files_seen = result.files_seen
    run.files_new = result.files_new
    run.files_duplicate = result.files_duplicate
    run.files_failed = result.files_failed
    if result.failures:
        run.notes = "\n".join(result.failures[:100])
    session.commit()
    return result


def _record_instance(
    session: Session, sha: str, source_id: int, rel: str, path: Path, run_id: int
) -> None:
    exists = session.scalar(
        select(AssetInstance).where(
            AssetInstance.sha256 == sha,
            AssetInstance.source_id == source_id,
            AssetInstance.original_path == rel,
        )
    )
    if exists:
        return
    session.add(
        AssetInstance(
            sha256=sha,
            source_id=source_id,
            original_path=rel,
            original_filename=path.name,
            file_mtime=_mtime_iso(path),
            ingest_run_id=run_id,
        )
    )
    session.commit()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _ingest_new(
    session: Session,
    config: Config,
    path: Path,
    sha: str,
    rel: str,
    source: Source,
    run_id: int,
    meta: dict,
    sidecars: dict[str, Path],
    root: Path | None,
) -> None:
    ext = path.suffix.lower().lstrip(".")
    kind = kind_for_ext(ext)
    stat = path.stat()

    resolved = resolve_capture_date(
        meta,
        path.name,
        stat.st_mtime,
        home_timezone=config.home_timezone,
        extra_patterns=config.ingest.extra_filename_date_patterns,
    )
    fields = core_fields(meta)

    # copy the original into the content-addressed library
    dest = object_path(config, sha, ext)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copy2(path, tmp)
        tmp.rename(dest)

    # sidecar (DSC_0042.xmp or DSC_0042.NEF.xmp next to the file)
    has_sidecar = False
    for cand_key in (str(path.with_suffix("")).lower(), str(path).lower()):
        sc = sidecars.get(cand_key)
        if sc:
            shutil.copy2(sc, sidecar_path(config, sha))
            has_sidecar = True
            break

    # RAW+JPEG / live-photo pairing: same directory + stem within this source
    pair = f"{source.name}/{Path(rel).with_suffix('')}".lower()

    thumb_state = make_derivatives(config, dest, sha, kind)

    asset = Asset(
        sha256=sha,
        size_bytes=stat.st_size,
        ext=ext,
        kind=kind,
        capture_ts=resolved.iso,
        capture_tz=resolved.tz_offset,
        capture_ts_source=resolved.source,
        date_only=resolved.date_only,
        exif_json=exif_json_dump(meta),
        thumb_state=thumb_state,
        has_sidecar=has_sidecar,
        pair_group=pair,
        **fields,
    )
    session.add(asset)
    session.add(
        AssetInstance(
            sha256=sha,
            source_id=source.id,
            original_path=rel,
            original_filename=path.name,
            file_mtime=_mtime_iso(path),
            ingest_run_id=run_id,
        )
    )
    session.commit()
