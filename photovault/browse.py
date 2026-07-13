"""Human-browsable local tree of all imported media (photos and videos).

Layout (both parts configurable via [browse] in config.toml):

    browse/<group_format of capture date>/<prefix_format>_<original-name>

Default: browse/2024/2024-07-11/20240711_183000_IMG_1234.jpg — so a
name-sorted folder listing is sorted by extracted capture time. Entries are
hardlinks to the content-addressed library objects (no extra disk space),
falling back to copies when the browse path is on another filesystem.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .hashing import sha256_file
from .ingest import object_path
from .models import Asset, AssetInstance


def _capture_dt(asset: Asset) -> datetime | None:
    if not asset.capture_ts:
        return None
    try:
        return datetime.strptime(asset.capture_ts, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# Filenames that already start with a timestamp prefix (e.g. re-ingesting a
# library previously organized this way, or migrating an existing collection
# that uses the same convention) would get a redundant second prefix.
# Strip a recognized leading timestamp — but only when something meaningful
# remains after it.
_EXISTING_PREFIX_RES = [
    re.compile(r"^\d{8}[_-]\d{6}[_\- ]+"),  # 20240711_183000_
    re.compile(r"^\d{4}-\d{2}-\d{2}[ _T]\d{2}[.\-]\d{2}[.\-]\d{2}[_\- ]+"),  # 2024-07-11 18.30.00_
]

_WINDOWS_UNSAFE = re.compile(r'[<>:"|?*]')


def _strip_existing_prefix(name: str) -> str:
    for pattern in _EXISTING_PREFIX_RES:
        m = pattern.match(name)
        if m and Path(name[m.end():]).stem:
            return name[m.end():]
    return name


def browse_target(config: Config, asset: Asset, original_filename: str) -> Path | None:
    dt = _capture_dt(asset)
    if dt is None:
        return None
    folder = config.browse_dir / dt.strftime(config.browse.group_format)
    base = _strip_existing_prefix(original_filename)
    name = f"{dt.strftime(config.browse.prefix_format)}_{base}"
    # keep names valid on Windows/NTFS regardless of configured formats
    name = _WINDOWS_UNSAFE.sub(".", name)
    return folder / name


def _place(src: Path, dest: Path, mode: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if mode == "symlink":
        tmp.symlink_to(src)
    elif mode == "copy":
        shutil.copy2(src, tmp)
    else:  # hardlink, with copy fallback across filesystems
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
    tmp.replace(dest)


def add_to_browse(config: Config, asset: Asset, original_filename: str) -> Path | None:
    """Link one asset into the browse tree. Returns the path (or None if disabled/undated)."""
    if not config.browse.enabled:
        return None
    dest = browse_target(config, asset, original_filename)
    if dest is None:
        return None
    src = object_path(config, asset.sha256, asset.ext)
    if not src.exists():
        return None
    if dest.exists():
        if os.path.samefile(src, dest) or sha256_file(dest) == asset.sha256:
            return dest  # already in place
        # same capture second + same name, different photo (e.g. burst): disambiguate
        dest = dest.with_name(f"{dest.stem}_{asset.sha256[:8]}{dest.suffix}")
        if dest.exists():
            return dest
    _place(src, dest, config.browse.link)
    return dest


@dataclass
class BrowseRebuild:
    linked: int = 0
    skipped_undated: int = 0
    skipped_missing: int = 0
    failures: list[str] = field(default_factory=list)


def rebuild_browse(session: Session, config: Config, clean: bool = False) -> BrowseRebuild:
    """(Re)build the whole browse tree from the catalog.

    Each asset appears once, named after its earliest-recorded instance.
    `clean` wipes the tree first (safe: it holds only links/copies of
    library objects, never the sole copy of anything).
    """
    result = BrowseRebuild()
    if clean and config.browse_dir.exists():
        shutil.rmtree(config.browse_dir)
    config.browse_dir.mkdir(parents=True, exist_ok=True)

    for asset in session.scalars(select(Asset).order_by(Asset.capture_ts)):
        inst = session.scalar(
            select(AssetInstance)
            .where(AssetInstance.sha256 == asset.sha256)
            .order_by(AssetInstance.id)
        )
        name = inst.original_filename if inst else f"{asset.sha256}.{asset.ext}"
        if _capture_dt(asset) is None:
            result.skipped_undated += 1
            continue
        if not object_path(config, asset.sha256, asset.ext).exists():
            result.skipped_missing += 1
            continue
        try:
            add_to_browse(config, asset, name)
            result.linked += 1
        except OSError as exc:
            result.failures.append(f"{asset.sha256}: {exc}")
    return result
