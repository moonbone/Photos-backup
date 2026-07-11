"""Every built-in filename pattern, sanity bounds, and the resolution chain."""
from __future__ import annotations

from datetime import datetime

import pytest

from photovault.dates import (
    infer_date_from_filename,
    parse_exif_datetime,
    resolve_capture_date,
)


@pytest.mark.parametrize(
    "filename,expected,date_only",
    [
        ("IMG_20240711_183000.jpg", datetime(2024, 7, 11, 18, 30, 0), False),
        ("VID_20240711_183000.mp4", datetime(2024, 7, 11, 18, 30, 0), False),
        ("PXL_20240711_183000123.jpg", datetime(2024, 7, 11, 18, 30, 0), False),
        ("IMG-20240711-WA0001.jpg", datetime(2024, 7, 11), True),
        ("VID-20240711-WA0042.mp4", datetime(2024, 7, 11), True),
        ("Screenshot_2024-07-11-18-30-00.png", datetime(2024, 7, 11, 18, 30, 0), False),
        ("Screenshot 2024-07-11 at 18.30.00.png", datetime(2024, 7, 11, 18, 30, 0), False),
        ("signal-2024-07-11-183000.jpg", datetime(2024, 7, 11, 18, 30, 0), False),
        ("2024-07-11 18.30.00.jpg", datetime(2024, 7, 11, 18, 30, 0), False),
        ("20240711_183000.jpg", datetime(2024, 7, 11, 18, 30, 0), False),
        ("20240711.jpg", datetime(2024, 7, 11), True),
        ("photo-2024-07-11.jpg", datetime(2024, 7, 11), True),
    ],
)
def test_builtin_patterns(filename, expected, date_only):
    result = infer_date_from_filename(filename)
    assert result is not None, filename
    dt, is_date_only = result
    assert dt == expected
    assert is_date_only == date_only


@pytest.mark.parametrize(
    "filename",
    [
        "DSC_0042.NEF",  # no date in name
        "IMG_19700101_000000.jpg",  # before 1990 -> rejected
        "IMG_29990101_000000.jpg",  # future -> rejected
        "IMG_20241399_183000.jpg",  # month 13 day 99 -> invalid
        "random-12345678.jpg",  # 1234-56-78 invalid date
    ],
)
def test_rejected_filenames(filename):
    assert infer_date_from_filename(filename) is None


def test_extra_patterns_win():
    extra = [r"holiday(?P<D>\d{2})(?P<M>\d{2})(?P<Y>\d{4})"]
    result = infer_date_from_filename("holiday25122023.jpg", extra_patterns=extra)
    assert result == (datetime(2023, 12, 25), True)


def test_parse_exif_datetime():
    assert parse_exif_datetime("2024:07:11 18:30:00") == datetime(2024, 7, 11, 18, 30, 0)
    assert parse_exif_datetime("2024-07-11T18:30:00") == datetime(2024, 7, 11, 18, 30, 0)
    assert parse_exif_datetime("2024:07:11 18:30:00.123+03:00") == datetime(2024, 7, 11, 18, 30)
    assert parse_exif_datetime("0000:00:00 00:00:00") is None
    assert parse_exif_datetime("") is None


def test_chain_prefers_exif():
    r = resolve_capture_date(
        {"DateTimeOriginal": "2023:01:05 10:00:00", "OffsetTimeOriginal": "+02:00"},
        "IMG_20240711_183000.jpg",  # filename says something else
        file_mtime=0,
    )
    assert r.source == "exif"
    assert r.iso == "2023-01-05T10:00:00"
    assert r.tz_offset == "+02:00"


def test_chain_filename_when_no_exif():
    r = resolve_capture_date({}, "IMG_20240711_183000.jpg", file_mtime=0)
    assert r.source == "filename"
    assert r.iso == "2024-07-11T18:30:00"
    assert not r.date_only


def test_chain_mtime_last_resort():
    from datetime import timezone

    # 2024-07-11 15:30:00 UTC == 18:30 in Asia/Jerusalem (UTC+3 in summer)
    mtime = datetime(2024, 7, 11, 15, 30, tzinfo=timezone.utc).timestamp()
    r = resolve_capture_date({}, "DSC_0042.NEF", file_mtime=mtime, home_timezone="Asia/Jerusalem")
    assert r.source == "mtime"
    assert r.iso == "2024-07-11T18:30:00"
    assert r.tz_offset == "+03:00"


def test_camera_with_unset_clock_falls_through():
    r = resolve_capture_date(
        {"DateTimeOriginal": "1980:01:01 00:00:00"}, "IMG_20240711_183000.jpg", file_mtime=0
    )
    assert r.source == "filename"
    assert r.iso == "2024-07-11T18:30:00"
