"""GeoFinder — API HTTP + service du front."""
from __future__ import annotations

import io
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from . import config, embedder, indexer, matcher, providers, store

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

@asynccontextmanager
async def lifespan(_: FastAPI):
    # uvicorn ne configure que ses propres loggers : sans cela, tout ce que
    # journalise l'application (jobs, providers) n'atteint jamais docker logs.
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("backend").setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    config.ensure_dirs()
    # Le chargement de CLIP prend quelques secondes : on le fait en fond pour
    # que l'API réponde immédiatement.
    threading.Thread(target=embedder.warmup, daemon=True).start()
    yield


app = FastAPI(title="GeoFinder", version="1.0.0", lifespan=lifespan)


class IndexRequest(BaseModel):
    city: str = Field(min_length=2, max_length=120)
    provider: str = Field(default="auto")
    max_panos: int = Field(default=config.DEFAULT_MAX_PANOS, ge=10, le=3000)
    headings: int = Field(default=4, ge=1, le=8)
    spacing_m: int = Field(default=config.DEFAULT_SPACING_M, ge=20, le=1000)
    radius_km: float = Field(default=6.0, gt=0.1, le=40.0)


@app.get("/api/health")
def health() -> dict:
    active = providers.resolve_auto() if config.PROVIDER == "auto" else config.PROVIDER
    return {
        "status": "ok",
        "provider": active,
        "demo_mode": active == "demo",
        "providers": [
            {
                "name": s.name,
                "label": s.label,
                "ready": s.ready,
                "requires_key": s.requires_key,
                "reason": s.reason,
            }
            for s in providers.statuses()
        ],
        "model": f"{config.CLIP_MODEL}/{config.CLIP_PRETRAINED}",
        "cities": len(store.list_cities()),
    }


@app.get("/api/cities")
def list_cities() -> dict:
    cities = [
        {
            "slug": c.slug,
            "name": c.name,
            "display_name": c.display_name,
            "lat": c.lat,
            "lng": c.lng,
            "provider": c.provider,
            "demo": c.demo,
            "created_at": c.created_at,
            "views": len(c.views),
            "panos": c.pano_count,
        }
        for c in store.list_cities()
    ]
    return {"cities": cities}


@app.post("/api/cities")
def create_city_index(req: IndexRequest) -> dict:
    if req.provider not in ("auto", *providers.names()):
        raise HTTPException(400, f"Source d'images inconnue : « {req.provider} »")
    job = indexer.start_index_job(
        city_name=req.city.strip(),
        provider=req.provider,
        max_panos=req.max_panos,
        headings=req.headings,
        spacing_m=req.spacing_m,
        radius_km=req.radius_km,
    )
    indexer.prune_jobs()
    return job.as_dict()


@app.delete("/api/cities/{slug}")
def delete_city(slug: str) -> dict:
    if not store.delete(_safe_slug(slug)):
        raise HTTPException(404, "Ville inconnue")
    return {"deleted": slug}


@app.get("/api/cities/{slug}/images/{name}")
def city_image(slug: str, name: str) -> FileResponse:
    path = store.images_dir(_safe_slug(slug)) / _safe_name(name)
    if not path.exists():
        raise HTTPException(404, "Image introuvable")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/jobs")
def jobs() -> dict:
    return {"jobs": [j.as_dict() for j in indexer.list_jobs()[:20]]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = indexer.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Tâche inconnue")
    return job.as_dict()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict:
    if not indexer.cancel_job(job_id):
        raise HTTPException(409, "Tâche déjà terminée")
    return {"cancelled": job_id}


@app.post("/api/find")
async def find(
    file: UploadFile = File(...),
    city: Optional[str] = Form(default=None),
    top_k: int = Form(default=5),
    rerank: bool = Form(default=True),
) -> JSONResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Fichier vide")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image trop lourde (max 20 Mo)")

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            query = img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Format d'image non reconnu")

    slugs = matcher.resolve_slugs(city)
    if not slugs:
        raise HTTPException(
            409,
            "Aucune ville indexée. Indexe d'abord une ville avant de lancer une recherche.",
        )

    top_k = max(1, min(int(top_k), 20))
    try:
        matches, diag = matcher.search(query, slugs, top_k=top_k, rerank=rerank)
    except (OSError, RuntimeError) as exc:
        # Cas typique : poids du modèle indisponibles (cache vide + réseau coupé).
        raise HTTPException(503, f"Modèle de reconnaissance indisponible : {exc}") from exc
    if not matches:
        return JSONResponse({"matches": [], "best": None, "diagnostic": diag})

    best = matches[0]
    return JSONResponse(
        {
            "matches": [m.as_dict() for m in matches],
            "best": best.as_dict(),
            "diagnostic": diag,
        }
    )


def _safe_slug(slug: str) -> str:
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise HTTPException(400, "Identifiant de ville invalide")
    return slug


def _safe_name(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom de fichier invalide")
    return name


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
