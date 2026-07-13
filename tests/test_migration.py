"""Migrating an existing library: --link ingest, prefix stripping, portability."""
from __future__ import annotations

import os

from sqlalchemy import select

from photovault.browse import _strip_existing_prefix
from photovault.ingest import ingest_path, object_path
from photovault.models import Asset

from .conftest import make_jpeg


def test_link_ingest_shares_inode(config, session, tmp_path):
    """--link migration: library object is the same file, no extra disk space."""
    existing = tmp_path / "existing-library"
    src = make_jpeg(existing / "2024" / "IMG_20240711_183000.jpg")

    r = ingest_path(session, config, existing, "legacy", link=True)
    assert r.files_new == 1
    asset = session.scalar(select(Asset))
    obj = object_path(config, asset.sha256, asset.ext)
    assert os.path.samefile(src, obj)
    assert src.stat().st_nlink >= 2


def test_copy_ingest_does_not_share_inode(config, session, tmp_path):
    existing = tmp_path / "existing-library"
    src = make_jpeg(existing / "IMG_20240711_183000.jpg")
    ingest_path(session, config, existing, "legacy", link=False)
    asset = session.scalar(select(Asset))
    obj = object_path(config, asset.sha256, asset.ext)
    assert not os.path.samefile(src, obj)


def test_no_double_prefix_for_already_prefixed_names(config, session, tmp_path):
    """A library already organized as <timestamp>_<name> migrates cleanly."""
    existing = tmp_path / "existing-library"
    make_jpeg(existing / "2024" / "2024-07-11" / "20240711_183000_IMG_1234.jpg", (1, 1, 1))
    make_jpeg(existing / "2023" / "2023-01-05 10.00.00_holiday.jpg", (2, 2, 2))

    ingest_path(session, config, existing, "legacy")

    assert (
        config.browse_dir / "2024" / "2024-07-11" / "20240711_183000_IMG_1234.jpg"
    ).exists()  # not 20240711_183000_20240711_183000_IMG_1234.jpg
    assert (
        config.browse_dir / "2023" / "2023-01-05" / "20230105_100000_holiday.jpg"
    ).exists()  # dashed/dotted prefix also recognized and normalized


def test_prefix_stripping_rules():
    assert _strip_existing_prefix("20240711_183000_IMG_1234.jpg") == "IMG_1234.jpg"
    assert _strip_existing_prefix("20240711-183000 beach.jpg") == "beach.jpg"
    assert _strip_existing_prefix("2024-07-11 18.30.00_x.jpg") == "x.jpg"
    # nothing meaningful after the prefix -> keep as-is
    assert _strip_existing_prefix("20240711_183000.jpg") == "20240711_183000.jpg"
    # normal camera names untouched
    assert _strip_existing_prefix("IMG_20240711_183000.jpg") == "IMG_20240711_183000.jpg"
    assert _strip_existing_prefix("DSC_0042.NEF") == "DSC_0042.NEF"


def test_browse_names_windows_safe(config, session, tmp_path):
    """Configured formats with characters invalid on NTFS get sanitized."""
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    config.browse.prefix_format = "%Y-%m-%d %H:%M:%S"  # colons: invalid on Windows

    ingest_path(session, config, staging, "phone-a")
    names = [p.name for p in (config.browse_dir / "2024" / "2024-07-11").iterdir()]
    assert names == ["2024-07-11 18.30.00_IMG_20240711_183000.jpg"]


def test_migration_flow_end_to_end(config, session, storage, tmp_path):
    """Migrate an existing collection, back it up, and prove it safe to retire."""
    from photovault.device import check_device
    from photovault.sync import sync
    from photovault.verify import verify_remote

    existing = tmp_path / "my-old-photos"
    make_jpeg(existing / "2019 vacation" / "beach.jpg", (0, 0, 1),
              exif_dt="2019:08:20 16:45:00")
    make_jpeg(existing / "phone dump" / "IMG_20220301_121500.jpg", (0, 1, 0))

    ingest_path(session, config, existing, "legacy-library", link=True)
    sync(session, config, storage)
    verify_remote(session, config, storage)

    check = check_device(session, config, existing)
    assert check.all_safe  # the old library is now fully backed up + verified

    # and it browses by capture date, not by the old folder mess
    assert (config.browse_dir / "2019" / "2019-08-20" / "20190820_164500_beach.jpg").exists()
    assert (
        config.browse_dir / "2022" / "2022-03-01" / "20220301_121500_IMG_20220301_121500.jpg"
    ).exists()
