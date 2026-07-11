"""Continuous operation: poll staging folders, ingest stable files, auto-sync.

A file is ingested only after its (size, mtime) is unchanged across two
consecutive scans — the stability window that keeps half-written files out.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import Config
from .db import open_session
from .ingest import ingest_files, iter_media_files


def watch(
    config: Config,
    interval: float = 30.0,
    auto_sync: bool = True,
    max_cycles: int | None = None,
    log=print,
) -> None:
    session = open_session(config)
    pending: dict[Path, tuple[int, float]] = {}  # candidates awaiting stability
    processed: dict[Path, tuple[int, float]] = {}  # already ingested (this process)
    cycles = 0

    while True:
        uploaded_any = False
        for src in config.sources:
            root = Path(src.staging)
            if not root.exists():
                continue
            stable: list[Path] = []
            for f in iter_media_files(root):
                st = f.stat()
                sig = (st.st_size, st.st_mtime)
                if processed.get(f) == sig:
                    continue
                if pending.get(f) == sig:
                    stable.append(f)
                else:
                    pending[f] = sig  # first sighting (or still changing): wait a cycle
            if stable:
                result = ingest_files(session, config, stable, src.name, root=root)
                log(
                    f"[watch] {src.name}: ingested {result.files_new} new, "
                    f"{result.files_duplicate} duplicate, {result.files_failed} failed"
                )
                for f in stable:
                    processed[f] = pending.pop(f, (0, 0.0))
                uploaded_any = uploaded_any or result.files_new > 0

        if auto_sync and uploaded_any:
            from .sync import sync

            try:
                s = sync(session, config)
                log(f"[watch] sync: {s.uploaded} uploaded, {s.failed} failed")
            except Exception as exc:
                log(f"[watch] sync failed: {exc}")

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        time.sleep(interval)
