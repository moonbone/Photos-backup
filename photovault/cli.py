"""PhotoVault CLI. `photovault --help` for the full command list."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import Config, load_config

app = typer.Typer(
    name="photovault",
    help="Self-hosted photo backup with verified cloud storage and trust-before-delete.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

_state: dict = {}


def _config() -> Config:
    if "config" not in _state:
        _state["config"] = load_config(_state.get("config_path"))
    return _state["config"]


@app.callback()
def main(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.toml (default: ~/.config/photovault/config.toml)"
    ),
):
    _state["config_path"] = config


@app.command()
def init():
    """Create library directories and the catalog database."""
    from .db import init_db

    cfg = _config()
    init_db(cfg)
    typer.echo(f"Library initialized at {cfg.library_path}")
    typer.echo(f"Catalog: {cfg.db_path}")


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, help="Folder to ingest"),
    source: str = typer.Option(..., "--source", "-s", help="Source name (e.g. phone-a, dslr)"),
    link: bool = typer.Option(
        False,
        "--link",
        help="Hardlink originals into the library instead of copying — no extra disk "
        "space when migrating an existing collection on the same filesystem "
        "(falls back to copying across filesystems)",
    ),
):
    """Hash, extract metadata, thumbnail, and catalog everything under PATH."""
    from .db import open_session
    from .ingest import ingest_path

    cfg = _config()
    with open_session(cfg) as session:
        r = ingest_path(session, cfg, path, source, link=link)
    typer.echo(
        f"Ingested from {path} (source: {source}): "
        f"{r.files_new} new, {r.files_duplicate} duplicate, {r.files_failed} failed "
        f"of {r.files_seen} seen"
    )
    for f in r.failures[:20]:
        typer.echo(f"  FAILED {f}", err=True)
    raise typer.Exit(1 if r.files_failed else 0)


@app.command()
def sync():
    """Upload pending assets to cloud storage (server-verified), snapshot the catalog."""
    from .db import open_session
    from .sync import sync as do_sync

    cfg = _config()
    with open_session(cfg) as session:
        r = do_sync(session, cfg)
    typer.echo(f"Uploaded {r.uploaded}, failed {r.failed}, skipped {r.skipped}")
    typer.echo(f"Catalog snapshot: {r.snapshot_key}")
    for f in r.failures[:20]:
        typer.echo(f"  {f}", err=True)
    raise typer.Exit(1 if r.failed or r.skipped else 0)


@app.command()
def verify(
    remote: bool = typer.Option(True, "--remote/--no-remote"),
    sample: Optional[int] = typer.Option(None, help="Deep-verify sample size (default from config)"),
):
    """Reconcile the bucket against the catalog; deep-verify a rotating sample."""
    from .db import open_session
    from .verify import verify_remote

    cfg = _config()
    if not remote:
        typer.echo("Nothing to do (only --remote verification exists today)")
        raise typer.Exit(0)
    with open_session(cfg) as session:
        r = verify_remote(session, cfg, sample=sample)
    typer.echo(
        f"Checked {r.checked} remote copies: {r.promoted} promoted to verified, "
        f"{r.deep_verified} deep-verified"
    )
    if r.missing:
        typer.echo(f"⚠ MISSING/size-mismatch (re-queued for upload): {len(r.missing)}", err=True)
    if r.mismatched:
        typer.echo(f"⚠ HASH MISMATCH (re-queued for upload): {len(r.mismatched)}", err=True)
    if r.orphaned:
        typer.echo(f"Orphaned objects in bucket (not touched): {len(r.orphaned)}")
    typer.echo(f"Report: {r.report_path}")
    raise typer.Exit(0 if r.ok else 1)


@app.command("check-device")
def check_device_cmd(
    path: Path = typer.Argument(..., exists=True, help="Mounted device / folder to check"),
    source: Optional[str] = typer.Option(None, "--source", "-s"),
    out: Optional[Path] = typer.Option(None, "--out", help="Also copy the JSON report here"),
):
    """Classify every file on a device: safe to delete, or not (and why).

    Exit code 0 only if 100% of files are SAFE.
    """
    from .db import open_session
    from .device import REMEDY, check_device

    cfg = _config()
    with open_session(cfg) as session:
        check = check_device(session, cfg, path, source)
    t = check.totals
    typer.echo(f"Checked {t['files']} files on {path}:")
    typer.echo(f"  SAFE                {t['safe']}")
    typer.echo(f"  UPLOADED-UNVERIFIED {t['uploaded_unverified']}")
    typer.echo(f"  KNOWN-NOT-UPLOADED  {t['known_not_uploaded']}")
    typer.echo(f"  UNKNOWN             {t['unknown']}")
    for entry in check.files:
        if entry["class"] != "SAFE":
            typer.echo(f"  {entry['class']:20} {entry['path']} — {REMEDY[entry['class']]}")
    typer.echo(f"Report: {check.report_path}")
    if out:
        out.write_bytes(Path(check.report_path).read_bytes())
    if check.all_safe:
        typer.echo("✔ All files are safely backed up and verified — OK to delete from device.")
        raise typer.Exit(0)
    typer.echo("✘ NOT safe to wipe this device yet.", err=True)
    raise typer.Exit(1)


@app.command("prune-device")
def prune_device_cmd(
    path: Path = typer.Argument(..., exists=True),
    report: Path = typer.Option(..., "--report", help="A fresh check-device JSON report"),
    execute: bool = typer.Option(
        False, "--execute", help="Actually delete (default is dry-run)"
    ),
):
    """Delete SAFE files from a device, per a fresh check-device report.

    Dry-run by default. Each file is re-hashed immediately before deletion.
    """
    from .device import prune_device

    cfg = _config()
    r = prune_device(cfg, path, report, execute=execute)
    verb = "Deleted" if execute else "Would delete (dry-run)"
    typer.echo(f"{verb}: {len(r.deleted)} files")
    for p in r.skipped_changed:
        typer.echo(f"  SKIPPED (bytes changed since report): {p}")
    for p in r.skipped_missing:
        typer.echo(f"  SKIPPED (no longer on device): {p}")
    if r.log_path:
        typer.echo(f"Deletion log: {r.log_path}")
    if not execute:
        typer.echo("Re-run with --execute to actually delete.")


@app.command("build-browse")
def build_browse_cmd(
    clean: bool = typer.Option(
        False, "--clean", help="Wipe and rebuild the tree (it only holds links, never sole copies)"
    ),
):
    """(Re)build the human-browsable date/timestamp tree from the catalog.

    Layout: browse/<YYYY>/<YYYY-MM-DD>/<YYYYMMDD_HHMMSS>_<original-name>
    (formats configurable under [browse] in config.toml). Runs automatically
    during ingest; use this to backfill an existing library or after
    changing the formats.
    """
    from .browse import rebuild_browse
    from .db import open_session

    cfg = _config()
    with open_session(cfg) as session:
        r = rebuild_browse(session, cfg, clean=clean)
    typer.echo(f"Browse tree at {cfg.browse_dir}: {r.linked} linked, "
               f"{r.skipped_undated} undated skipped, {r.skipped_missing} missing skipped")
    for f in r.failures[:20]:
        typer.echo(f"  FAILED {f}", err=True)
    raise typer.Exit(1 if r.failures else 0)


@app.command()
def restore(
    dest: Path = typer.Option(..., "--dest", help="Destination folder"),
    source: Optional[str] = typer.Option(None, "--source", "-s"),
    date_from: Optional[str] = typer.Option(None, "--from", help="YYYY-MM-DD"),
    date_to: Optional[str] = typer.Option(None, "--to", help="YYYY-MM-DD"),
):
    """Download originals from the bucket (re-hashed on arrival)."""
    from .db import open_session
    from .restore import restore as do_restore

    cfg = _config()
    with open_session(cfg) as session:
        r = do_restore(session, cfg, dest, source=source, date_from=date_from, date_to=date_to)
    typer.echo(f"Restored {len(r.restored)} files to {dest}")
    if r.failed:
        typer.echo(f"⚠ FAILED (download error or hash mismatch): {len(r.failed)}", err=True)
        raise typer.Exit(1)


@app.command()
def rebuild(
    from_bucket: bool = typer.Option(False, "--from-bucket"),
    force: bool = typer.Option(False, "--force"),
):
    """Disaster recovery: restore the catalog from the newest cloud snapshot."""
    from .restore import rebuild_from_bucket

    if not from_bucket:
        typer.echo("Pass --from-bucket to restore the catalog from cloud snapshots")
        raise typer.Exit(1)
    cfg = _config()
    key = rebuild_from_bucket(cfg, force=force)
    typer.echo(f"Catalog restored from {key} to {cfg.db_path}")


@app.command()
def watch(
    interval: float = typer.Option(30.0, help="Seconds between staging-folder scans"),
    auto_sync: bool = typer.Option(True, "--auto-sync/--no-auto-sync"),
):
    """Daemon: continuously ingest configured staging folders (and sync)."""
    from .watch import watch as do_watch

    cfg = _config()
    if not cfg.sources:
        typer.echo("No [[sources]] configured", err=True)
        raise typer.Exit(1)
    typer.echo(f"Watching {len(cfg.sources)} staging folder(s), every {interval:.0f}s. Ctrl-C to stop.")
    do_watch(cfg, interval=interval, auto_sync=auto_sync)


@app.command()
def serve(
    host: Optional[str] = typer.Option(None),
    port: Optional[int] = typer.Option(None),
):
    """Run the web UI + API."""
    import uvicorn

    from .web.app import create_app

    cfg = _config()
    uvicorn.run(
        create_app(cfg),
        host=host or cfg.web.host,
        port=port or cfg.web.port,
    )


@app.command()
def status():
    """One-line library status."""
    from sqlalchemy import func, select

    from .db import open_session
    from .models import Asset

    cfg = _config()
    with open_session(cfg) as session:
        counts = dict(
            session.execute(
                select(Asset.backup_state, func.count()).group_by(Asset.backup_state)
            ).all()
        )
        total = session.scalar(select(func.coalesce(func.sum(Asset.size_bytes), 0)))
    typer.echo(
        f"{sum(counts.values())} assets ({total / 1e9:.2f} GB): "
        f"{counts.get('verified', 0)} verified, {counts.get('uploaded', 0)} uploaded, "
        f"{counts.get('new', 0)} pending upload"
    )


if __name__ == "__main__":
    app()
