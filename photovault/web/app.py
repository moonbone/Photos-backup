"""Web UI + API: timeline, photo detail, dashboard, mobile upload endpoint."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..db import init_db
from ..hashing import sha256_file
from ..ingest import ingest_files, object_path
from ..models import (
    Asset,
    AssetInstance,
    IngestRun,
    RemoteCopy,
    Source,
    STATE_NEW,
    VerifyRun,
)
from ..thumbs import preview_path, thumb_path

PAGE_SIZE = 200

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="PhotoVault", docs_url=None, redoc_url=None)
    session_factory = init_db(config)
    app.state.config = config
    app.state.session_factory = session_factory

    def db() -> Session:
        return session_factory()

    # --- auth ----------------------------------------------------------
    @app.middleware("http")
    async def _token_auth(request: Request, call_next):
        token = config.web.token
        if token:
            supplied = (
                request.headers.get("x-photovault-token")
                or request.query_params.get("token")
                or request.cookies.get("pv_token")
            )
            if supplied != token:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = await call_next(request)
        if token and request.query_params.get("token") == token:
            response.set_cookie("pv_token", token, httponly=True)
        return response

    # --- helpers ---------------------------------------------------------
    def _filtered_assets(s: Session, request: Request):
        q = select(Asset)
        p = request.query_params
        if src_name := p.get("source"):
            src = s.scalar(select(Source).where(Source.name == src_name))
            if src:
                q = (
                    q.join(AssetInstance, AssetInstance.sha256 == Asset.sha256)
                    .where(AssetInstance.source_id == src.id)
                    .distinct()
                )
        if camera := p.get("camera"):
            q = q.where(Asset.camera_model == camera)
        if kind := p.get("kind"):
            q = q.where(Asset.kind == kind)
        if state := p.get("state"):
            q = q.where(Asset.backup_state == state)
        if ts_source := p.get("ts_source"):
            q = q.where(Asset.capture_ts_source == ts_source)
        return q

    # --- pages -----------------------------------------------------------
    @app.get("/")
    def timeline(request: Request, page: int = 0):
        with db() as s:
            q = _filtered_assets(s, request).order_by(Asset.capture_ts.desc())
            assets = list(s.scalars(q.limit(PAGE_SIZE).offset(page * PAGE_SIZE)))
            # group by local date for day headers
            groups: dict[str, list[Asset]] = {}
            for a in assets:
                groups.setdefault((a.capture_ts or "unknown")[:10], []).append(a)
            sources = list(s.scalars(select(Source).order_by(Source.name)))
            cameras = [
                c for (c,) in s.execute(
                    select(Asset.camera_model).where(Asset.camera_model.is_not(None)).distinct()
                )
            ]
        params = dict(request.query_params)
        params.pop("page", None)
        return _TEMPLATES.TemplateResponse(
            request,
            "timeline.html",
            {
                "groups": groups,
                "sources": sources,
                "cameras": sorted(cameras),
                "page": page,
                "has_next": len(assets) == PAGE_SIZE,
                "params": params,
                "query": request.query_params,
            },
        )

    @app.get("/photo/{sha}")
    def photo(request: Request, sha: str):
        with db() as s:
            asset = s.get(Asset, sha)
            if asset is None:
                raise HTTPException(404)
            instances = list(
                s.execute(
                    select(AssetInstance, Source.name)
                    .join(Source, Source.id == AssetInstance.source_id)
                    .where(AssetInstance.sha256 == sha)
                )
            )
            copies = list(s.scalars(select(RemoteCopy).where(RemoteCopy.sha256 == sha)))
            pair = []
            if asset.pair_group:
                pair = [
                    a
                    for a in s.scalars(
                        select(Asset).where(Asset.pair_group == asset.pair_group)
                    )
                    if a.sha256 != sha
                ]
            exif = json.loads(asset.exif_json or "{}")
        return _TEMPLATES.TemplateResponse(
            request,
            "photo.html",
            {"a": asset, "instances": instances, "copies": copies, "exif": exif, "pair": pair},
        )

    @app.get("/dashboard")
    def dashboard(request: Request):
        with db() as s:
            state_counts = dict(
                s.execute(select(Asset.backup_state, func.count()).group_by(Asset.backup_state)).all()
            )
            ts_counts = dict(
                s.execute(
                    select(Asset.capture_ts_source, func.count()).group_by(Asset.capture_ts_source)
                ).all()
            )
            total_bytes = s.scalar(select(func.coalesce(func.sum(Asset.size_bytes), 0)))
            per_source = s.execute(
                select(Source.name, func.count(func.distinct(AssetInstance.sha256)))
                .join(AssetInstance, AssetInstance.source_id == Source.id, isouter=True)
                .group_by(Source.name)
            ).all()
            last_ingests = list(
                s.execute(
                    select(IngestRun, Source.name)
                    .join(Source, Source.id == IngestRun.source_id)
                    .order_by(IngestRun.id.desc())
                    .limit(10)
                )
            )
            last_verifies = list(
                s.scalars(select(VerifyRun).order_by(VerifyRun.id.desc()).limit(10))
            )
            pending = state_counts.get(STATE_NEW, 0)
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "state_counts": state_counts,
                "ts_counts": ts_counts,
                "total_bytes": total_bytes,
                "per_source": per_source,
                "last_ingests": last_ingests,
                "last_verifies": last_verifies,
                "pending": pending,
            },
        )

    # --- media -----------------------------------------------------------
    def _serve(path: Path, media_type: str | None = None):
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type=media_type)

    @app.get("/thumb/{sha}")
    def thumb(sha: str):
        return _serve(thumb_path(config, sha), "image/jpeg")

    @app.get("/preview/{sha}")
    def preview(sha: str):
        return _serve(preview_path(config, sha), "image/jpeg")

    @app.get("/original/{sha}")
    def original(sha: str):
        with db() as s:
            asset = s.get(Asset, sha)
        if asset is None:
            raise HTTPException(404)
        return _serve(object_path(config, sha, asset.ext), asset.mime)

    # --- upload API (phase-2 mobile ingestion) ----------------------------
    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        source: str = Form(None),
        sha256: str = Form(None),
    ):
        source_name = source or config.web.upload_source
        staging = config.staging_root / source_name
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as tmp:
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
            tmp_path = Path(tmp.name)
        actual_sha = sha256_file(tmp_path)
        if sha256 and sha256.lower() != actual_sha:
            tmp_path.unlink()
            raise HTTPException(400, "checksum mismatch: upload corrupted in transit")
        final = staging / (file.filename or f"{actual_sha}.bin")
        if final.exists() and sha256_file(final) != actual_sha:
            final = staging / f"{actual_sha[:8]}-{file.filename}"
        tmp_path.replace(final)
        with db() as s:
            result = ingest_files(s, config, [final], source_name, root=staging)
        return {
            "sha256": actual_sha,
            "new": result.files_new == 1,
            "duplicate": result.files_duplicate == 1,
            "failed": result.files_failed == 1,
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
