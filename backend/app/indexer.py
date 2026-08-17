"""Construction de l'index d'une ville : sondage Street View → images → embeddings."""
from __future__ import annotations

import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from PIL import Image

from . import config, embedder, geocode, sampling, store, streetview
from .streetview import Pano


@dataclass
class Job:
    id: str
    city: str
    slug: Optional[str] = None
    state: str = "pending"  # pending | running | done | error | cancelled
    step: str = "En attente…"
    progress: float = 0.0
    panos: int = 0
    views: int = 0
    error: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    cancel: bool = field(default=False, repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "city": self.city,
            "slug": self.slug,
            "state": self.state,
            "step": self.step,
            "progress": round(self.progress, 3),
            "panos": self.panos,
            "views": self.views,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_jobs: Dict[str, Job] = {}
_jobs_lock = threading.Lock()
_active_slugs: set[str] = set()


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def list_jobs() -> List[Job]:
    return sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)


def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job and job.state in ("pending", "running"):
        job.cancel = True
        job.step = "Annulation…"
        return True
    return False


class Cancelled(RuntimeError):
    pass


def start_index_job(
    city_name: str,
    max_panos: int = config.DEFAULT_MAX_PANOS,
    headings: int = 4,
    spacing_m: int = config.DEFAULT_SPACING_M,
    radius_km: float = 6.0,
) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], city=city_name)
    with _jobs_lock:
        _jobs[job.id] = job
    thread = threading.Thread(
        target=_run_job,
        args=(job, city_name, max_panos, headings, spacing_m, radius_km),
        daemon=True,
    )
    thread.start()
    return job


def _check(job: Job) -> None:
    if job.cancel:
        raise Cancelled()


def _run_job(job: Job, city_name: str, max_panos: int, headings: int, spacing_m: int, radius_km: float) -> None:
    slug = None
    try:
        job.state = "running"
        job.step = "Localisation de la ville…"
        job.progress = 0.02
        city = geocode.geocode_city(city_name)
        slug = city.slug
        job.slug = slug

        with _jobs_lock:
            if slug in _active_slugs:
                raise RuntimeError(f"Une indexation est déjà en cours pour « {city.display_name} »")
            _active_slugs.add(slug)

        _check(job)
        job.step = "Recherche des panoramas Street View…"
        job.progress = 0.05

        probe_points = sampling.grid_points(
            city, spacing_m=spacing_m, radius_km=radius_km, max_points=config.MAX_PROBES
        )
        panos = _collect_panos(job, probe_points, max_panos, spacing_m)
        if not panos:
            raise RuntimeError(
                "Aucun panorama Street View trouvé dans cette zone "
                "(couverture inexistante, ou clé API sans accès Street View Static)."
            )
        job.panos = len(panos)

        _check(job)
        job.step = f"Téléchargement des vues ({len(panos)} panoramas)…"
        heading_list = streetview.headings_for(headings)
        views = _download_views(job, slug, panos, heading_list)
        if not views:
            raise RuntimeError("Aucune image n'a pu être téléchargée.")
        job.views = len(views)

        _check(job)
        job.step = f"Calcul des empreintes visuelles ({len(views)} images)…"
        job.progress = 0.85
        embeddings = embedder.embed_images(_stream_images(job, slug, views))

        _check(job)
        job.step = "Enregistrement de l'index…"
        job.progress = 0.96
        index = store.CityIndex(
            slug=slug,
            name=city_name,
            display_name=city.display_name,
            lat=city.lat,
            lng=city.lng,
            demo=config.DEMO_MODE,
            created_at=datetime.now(timezone.utc).isoformat(),
            views=views,
        )
        store.save(index, embeddings)
        store._cache.pop(slug, None)

        job.state = "done"
        job.step = f"Index prêt : {len(panos)} panoramas, {len(views)} vues."
        job.progress = 1.0
    except Cancelled:
        job.state = "cancelled"
        job.step = "Indexation annulée."
        if slug:
            store.delete(slug)
    except Exception as exc:  # noqa: BLE001 - remonté tel quel à l'UI
        job.state = "error"
        job.error = str(exc)
        job.step = "Échec."
    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat()
        if slug:
            with _jobs_lock:
                _active_slugs.discard(slug)


def _collect_panos(job: Job, points, max_panos: int, spacing_m: int) -> List[Pano]:
    """Sonde la grille et déduplique les panoramas trouvés."""
    found: Dict[str, Pano] = {}
    probed = 0
    radius = max(int(spacing_m * 0.6), 30)
    session = streetview._session()

    with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
        for pano in pool.map(lambda p: _safe_probe(p, radius, session), points):
            probed += 1
            if job.cancel:
                break
            if pano is not None and pano.pano_id not in found:
                found[pano.pano_id] = pano
                job.panos = len(found)
            job.progress = 0.05 + 0.25 * min(len(found) / max(max_panos, 1), 1.0)
            job.step = f"Recherche des panoramas… {len(found)}/{max_panos} ({probed} points sondés)"
            if len(found) >= max_panos:
                break

    _check(job)
    return list(found.values())[:max_panos]


def _safe_probe(point, radius: int, session) -> Optional[Pano]:
    try:
        return streetview.probe(point[0], point[1], radius_m=radius, session=session)
    except Exception:  # noqa: BLE001 - un point qui échoue ne doit pas tuer le job
        return None


def _download_views(job: Job, slug: str, panos: List[Pano], headings: List[int]) -> List[store.View]:
    """Télécharge chaque couple (panorama, cap) et écrit l'image sur disque.

    Les images ne sont pas conservées en mémoire : sur une grosse ville l'index
    dépasse le millier de vues, elles sont relues par lots au moment d'encoder.
    """
    out_dir = store.images_dir(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(p, h) for p in panos for h in headings]
    total = len(tasks)
    session = streetview._session()
    results: List[Optional[tuple]] = [None] * total
    done = 0
    lock = threading.Lock()

    def work(i_task):
        nonlocal done
        i, (pano, heading) = i_task
        if job.cancel:
            return
        try:
            raw = streetview.fetch_image(pano, heading, session=session)
            name = f"{pano.pano_id}_{heading:03d}.jpg"
            with Image.open(io.BytesIO(raw)) as img:
                img.convert("RGB").save(out_dir / name, format="JPEG", quality=88)
            results[i] = (pano, heading, name)
        except Exception:  # noqa: BLE001 - une vue manquante ne doit pas tuer le job
            results[i] = None
        finally:
            with lock:
                done += 1
                job.progress = 0.30 + 0.55 * (done / max(total, 1))
                job.step = f"Téléchargement des vues… {done}/{total}"

    with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
        list(pool.map(work, enumerate(tasks)))

    _check(job)

    views: List[store.View] = []
    for res in results:
        if res is None:
            continue
        pano, heading, name = res
        views.append(
            store.View(
                idx=len(views),
                pano_id=pano.pano_id,
                lat=pano.lat,
                lng=pano.lng,
                heading=heading,
                file=name,
            )
        )
    return views


def _stream_images(job: Job, slug: str, views: List[store.View]):
    """Relit les vues depuis le disque, une par une, pour l'encodage CLIP."""
    d = store.images_dir(slug)
    for i, view in enumerate(views):
        _check(job)
        with Image.open(d / view.file) as img:
            yield img.convert("RGB")
        if i % 25 == 0:
            job.progress = 0.85 + 0.10 * (i / max(len(views), 1))
            job.step = f"Calcul des empreintes visuelles… {i}/{len(views)}"


def prune_jobs(keep: int = 50) -> None:
    with _jobs_lock:
        if len(_jobs) <= keep:
            return
        for job in sorted(_jobs.values(), key=lambda j: j.started_at)[:-keep]:
            if job.state in ("done", "error", "cancelled"):
                _jobs.pop(job.id, None)


__all__ = ["Job", "start_index_job", "get_job", "list_jobs", "cancel_job", "prune_jobs"]
