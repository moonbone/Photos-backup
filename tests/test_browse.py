"""Browse tree: date grouping, timestamp prefixes, name-sort == time-sort."""
from __future__ import annotations

import os

from sqlalchemy import select

from photovault.browse import rebuild_browse
from photovault.ingest import ingest_path
from photovault.models import Asset

from .conftest import make_jpeg


def test_browse_layout_and_sort_order(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    # deliberately ingested out of chronological order, mixed date sources
    make_jpeg(staging / "zebra.jpg", (1, 1, 1), exif_dt="2024:07:11 09:05:00")
    make_jpeg(staging / "IMG_20240711_183000.jpg", (2, 2, 2))  # filename date
    make_jpeg(staging / "alpha.jpg", (3, 3, 3), exif_dt="2024:07:11 14:00:00")
    (staging / "VID_20240711_120000.mp4").write_bytes(b"fake video bytes")  # videos included

    ingest_path(session, config, staging, "phone-a")

    day = config.browse_dir / "2024" / "2024-07-11"
    names = sorted(os.listdir(day))
    assert names == [
        "20240711_090500_zebra.jpg",
        "20240711_120000_VID_20240711_120000.mp4",
        "20240711_140000_alpha.jpg",
        "20240711_183000_IMG_20240711_183000.jpg",
    ]  # name order == capture-time order, regardless of original names


def test_browse_uses_hardlinks(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    ingest_path(session, config, staging, "phone-a")

    entry = next((config.browse_dir / "2024" / "2024-07-11").iterdir())
    from photovault.ingest import object_path

    asset = session.scalar(select(Asset))
    assert os.path.samefile(entry, object_path(config, asset.sha256, asset.ext))


def test_browse_date_only_sorts_first(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG-20240711-WA0001.jpg", (1, 1, 1))  # date only -> 00:00:00
    make_jpeg(staging / "IMG_20240711_090000.jpg", (2, 2, 2))
    ingest_path(session, config, staging, "phone-a")

    names = sorted(os.listdir(config.browse_dir / "2024" / "2024-07-11"))
    assert names[0] == "20240711_000000_IMG-20240711-WA0001.jpg"


def test_browse_collision_same_second(config, session, tmp_path):
    """Two different photos, same capture second, same filename -> both survive."""
    s1 = tmp_path / "staging" / "phone-a" / "burst"
    s2 = tmp_path / "staging" / "dslr"
    make_jpeg(s1 / "IMG_20240711_183000.jpg", (1, 1, 1))
    make_jpeg(s2 / "IMG_20240711_183000.jpg", (2, 2, 2))  # different bytes, same name+second
    ingest_path(session, config, s1, "phone-a")
    ingest_path(session, config, s2, "dslr")

    names = os.listdir(config.browse_dir / "2024" / "2024-07-11")
    assert len(names) == 2
    assert len({n for n in names}) == 2


def test_browse_dedup_across_sources(config, session, tmp_path):
    """The same photo seen on two devices appears once in the browse tree."""
    p1 = make_jpeg(tmp_path / "staging" / "phone-a" / "IMG_20240711_183000.jpg", (1, 2, 3))
    p2 = tmp_path / "staging" / "dslr" / "same_photo.jpg"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_bytes(p1.read_bytes())
    ingest_path(session, config, p1.parent, "phone-a")
    ingest_path(session, config, p2.parent, "dslr")

    names = os.listdir(config.browse_dir / "2024" / "2024-07-11")
    assert names == ["20240711_183000_IMG_20240711_183000.jpg"]


def test_rebuild_browse(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg", (1, 1, 1))
    make_jpeg(staging / "IMG_20230101_120000.jpg", (2, 2, 2))
    ingest_path(session, config, staging, "phone-a")

    import shutil

    shutil.rmtree(config.browse_dir)
    r = rebuild_browse(session, config)
    assert r.linked == 2
    assert (config.browse_dir / "2024" / "2024-07-11" / "20240711_183000_IMG_20240711_183000.jpg").exists()
    assert (config.browse_dir / "2023" / "2023-01-01" / "20230101_120000_IMG_20230101_120000.jpg").exists()

    # idempotent
    r2 = rebuild_browse(session, config)
    assert r2.linked == 2 and not r2.failures


def test_rebuild_with_custom_formats(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    ingest_path(session, config, staging, "phone-a")

    config.browse.group_format = "%Y-%m"
    config.browse.prefix_format = "%Y-%m-%d %H.%M.%S"
    rebuild_browse(session, config, clean=True)
    assert (config.browse_dir / "2024-07" / "2024-07-11 18.30.00_IMG_20240711_183000.jpg").exists()
    assert not (config.browse_dir / "2024" / "2024-07-11").exists()  # clean wiped old layout


def test_browse_disabled(config, session, tmp_path):
    config.browse.enabled = False
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    ingest_path(session, config, staging, "phone-a")
    assert not config.browse_dir.exists() or not any(config.browse_dir.rglob("*"))
