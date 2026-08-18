"""Construction de l'index d'une ville : découverte → images → empreintes."""
from __future__ import annotations

import io
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from PIL import Image

from . import config, embedder, geocode, providers, store
from .providers import Cancelled, CollectContext, CollectOptions, ViewRef
from .providers.base import bounded_map

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    city: str
    provider: str
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

    def announce(self, step: str, progress: Optional[float] = None) -> None:
        """Change l'étape courante et la journalise.

        Sans cela, un job bloqué est totalement muet dans `docker logs` : c'est
        exactement ce qui rend une indexation qui piétine impossible à diagnostiquer.
        """
        self.step = step
        if progress is not None:
            self.progress = progress
        log.info("[job %s] %s", self.id, step)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "city": self.city,
            "provider": self.provider,
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


def start_index_job(
    city_name: str,
    provider: str = "auto",
    max_panos: int = config.DEFAULT_MAX_PANOS,
    headings: int = 4,
    spacing_m: int = config.DEFAULT_SPACING_M,
    radius_km: float = 6.0,
) -> Job:
    resolved = providers.resolve_auto() if provider in (None, "", "auto") else provider
    job = Job(id=uuid.uuid4().hex[:12], city=city_name, provider=resolved)
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(
        target=_run_job,
        args=(job, city_name, resolved, CollectOptions(max_panos, headings, spacing_m, radius_km)),
        daemon=True,
    ).start()
    return job


def _check(job: Job) -> None:
    if job.cancel:
        raise Cancelled()


def _run_job(job: Job, city_name: str, provider_name: str, opts: CollectOptions) -> None:
    slug = None
    started = time.monotonic()
    try:
        job.state = "running"
        provider = providers.get(provider_name)
        log.info("[job %s] démarrage — ville=%r source=%s max_panos=%d rayon=%.1fkm",
                 job.id, city_name, provider_name, opts.max_panos, opts.radius_km)

        job.announce("Localisation de la ville…", 0.02)
        city = geocode.geocode_city(city_name)
        slug = city.slug
        job.slug = slug

        with _jobs_lock:
            if slug in _active_slugs:
                raise RuntimeError(f"Une indexation est déjà en cours pour « {city.display_name} »")
            _active_slugs.add(slug)

        _check(job)
        job.announce(f"Recherche d'images ({provider.label})…", 0.05)

        last_log = [0.0]

        def on_progress(found: int, note: str) -> None:
            job.panos = found
            job.progress = 0.05 + 0.25 * min(found / max(opts.max_panos, 1), 1.0)
            job.step = note
            # La découverte émet un point de progression par requête : on
            # journalise au rythme humain, sinon les logs deviennent illisibles.
            now = time.monotonic()
            if now - last_log[0] >= 5.0:
                last_log[0] = now
                log.info("[job %s] %s", job.id, note)

        ctx = CollectContext(
            progress=on_progress,
            cancelled=lambda: job.cancel,
            deadline=time.monotonic() + config.DISCOVERY_BUDGET_S,
        )
        refs = provider.discover(city, opts, ctx)
        job.panos = len({r.place_id for r in refs})

        _check(job)
        job.announce(f"Téléchargement de {len(refs)} images…")
        views = _download(job, provider, slug, refs)
        if not views:
            raise RuntimeError(
                "Aucune image n'a pu être téléchargée depuis "
                f"{provider.label} (source injoignable ou images retirées)."
            )
        job.views = len(views)

        _check(job)
        job.announce(f"Calcul des empreintes visuelles ({len(views)} images)…", 0.85)
        embeddings = embedder.embed_images(_stream_images(job, slug, views))

        _check(job)
        job.announce("Enregistrement de l'index…", 0.96)
        index = store.CityIndex(
            slug=slug,
            name=city_name,
            display_name=city.display_name,
            lat=city.lat,
            lng=city.lng,
            provider=provider.name,
            created_at=datetime.now(timezone.utc).isoformat(),
            views=views,
        )
        store.save(index, embeddings)
        store._cache.pop(slug, None)

        job.state = "done"
        job.announce(
            f"Index prêt : {job.panos} lieux, {len(views)} vues ({provider.label}) "
            f"en {time.monotonic() - started:.0f}s.",
            1.0,
        )
    except Cancelled:
        job.state = "cancelled"
        job.announce("Indexation annulée.")
        if slug:
            store.delete(slug)
    except Exception as exc:  # noqa: BLE001 - remonté tel quel à l'UI
        job.state = "error"
        job.error = str(exc)
        job.step = "Échec."
        # `job.error` part vers l'UI ; la trace complète va dans les logs du
        # conteneur, seule source exploitable quand l'UI n'est pas regardée.
        log.error("[job %s] échec après %.0fs : %s",
                  job.id, time.monotonic() - started, exc, exc_info=True)
    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat()
        if slug:
            with _jobs_lock:
                _active_slugs.discard(slug)


def _download(job: Job, provider, slug: str, refs: List[ViewRef]) -> List[store.View]:
    """Télécharge chaque vue et l'écrit sur disque.

    Les images ne sont pas gardées en mémoire : sur une grosse ville l'index
    dépasse le millier de vues, elles sont relues par lots pour l'encodage.
    """
    out_dir = store.images_dir(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(refs)
    session = provider.session()
    results: List[Optional[tuple]] = [None] * total
    done = 0
    lock = threading.Lock()

    def work(item):
        nonlocal done
        i, ref = item
        if job.cancel:
            return
        try:
            raw = provider.fetch(ref, session)
            name = f"{_safe_stem(ref.view_id)}.jpg"
            with Image.open(io.BytesIO(raw)) as img:
                img.convert("RGB").save(out_dir / name, format="JPEG", quality=88)
            results[i] = (ref, name)
        except Exception:  # noqa: BLE001 - une vue manquante ne tue pas le job
            results[i] = None
        finally:
            with lock:
                done += 1
                job.progress = 0.30 + 0.55 * (done / max(total, 1))
                job.step = f"Téléchargement des images… {done}/{total}"
                if done % 50 == 0 or done == total:
                    log.info("[job %s] images %d/%d", job.id, done, total)

    for _ in bounded_map(work, enumerate(refs), config.FETCH_WORKERS,
                         should_stop=lambda: job.cancel):
        pass

    _check(job)
    failed = sum(1 for r in results if r is None)
    if failed:
        log.warning("[job %s] %d/%d images non récupérées", job.id, failed, total)

    views: List[store.View] = []
    for res in results:
        if res is None:
            continue
        ref, name = res
        views.append(
            store.View(
                idx=len(views),
                pano_id=ref.place_id,
                lat=ref.lat,
                lng=ref.lng,
                heading=ref.heading,
                file=name,
            )
        )
    return views


def _safe_stem(view_id: str) -> str:
    """Un identifiant de vue devient un nom de fichier : il vient d'une API
    tierce, donc on n'y laisse passer que de l'alphanumérique."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in view_id)
    return cleaned[:96] or "view"


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
