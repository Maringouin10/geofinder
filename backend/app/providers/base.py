"""Contrat commun à toutes les sources d'imagerie de rue."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from itertools import islice
from typing import Callable, ClassVar, Iterable, Iterator, List, Optional

import requests

from ..geocode import City

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Erreur imputable à la source d'images (clé absente, API en panne…)."""


@dataclass
class ViewRef:
    """Une vue à télécharger : une image, sa position et son orientation.

    `place_id` regroupe les vues d'un même endroit. Chez Google c'est le
    `pano_id` ; chez les sources participatives, une cellule spatiale.
    `ref` porte ce qu'il faut pour récupérer l'image : un identifiant de
    panorama (Google) ou une URL directe (Mapillary, KartaView).
    """
    view_id: str
    place_id: str
    lat: float
    lng: float
    heading: int
    ref: str
    captured_at: Optional[str] = None


@dataclass
class CollectOptions:
    max_panos: int = 250
    headings: int = 4          # vues par lieu
    spacing_m: int = 120
    radius_km: float = 6.0


@dataclass
class CollectContext:
    """Permet au provider de remonter sa progression, d'être interrompu, et de
    ne pas s'éterniser : une source lente ne doit pas bloquer un job pour
    toujours."""
    progress: Callable[[int, str], None] = field(default=lambda found, note: None)
    cancelled: Callable[[], bool] = field(default=lambda: False)
    deadline: Optional[float] = None          # time.monotonic()

    def report(self, found: int, note: str) -> None:
        self.progress(found, note)

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def should_stop(self) -> bool:
        return self.cancelled() or self.expired()

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()
        if self.expired():
            raise TimedOut(
                "Budget de temps dépassé pendant la recherche d'images. "
                "La source est injoignable ou très lente ; réduis le rayon, "
                "ou augmente GEOFINDER_DISCOVERY_BUDGET."
            )


class Cancelled(RuntimeError):
    pass


class TimedOut(ProviderError):
    pass


def bounded_map(
    fn: Callable,
    items: Iterable,
    workers: int,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Iterator:
    """`Executor.map` en version interruptible.

    `Executor.map` soumet la totalité des tâches d'emblée : sortir de la boucle
    n'annule rien et la fermeture du pool attend chaque requête en file. Sur une
    grille de plusieurs centaines de points vers une API lente, cela bloque le
    job pendant des dizaines de minutes sans aucun signe extérieur.

    Ici la file reste courte, et l'arrêt annule tout ce qui n'a pas démarré.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    source = iter(items)
    queued = workers * 4
    try:
        pending = {pool.submit(fn, item) for item in islice(source, queued)}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
            if should_stop is not None and should_stop():
                break
            for item in islice(source, len(done)):
                pending.add(pool.submit(fn, item))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@dataclass
class ProviderStatus:
    name: str
    label: str
    ready: bool
    requires_key: bool
    reason: str = ""


class Provider(ABC):
    """Une source d'imagerie de rue.

    Deux familles cohabitent derrière cette interface :

    - *rendu à la demande* (Google) : on choisit un point et un cap, le serveur
      rend l'image ; la découverte consiste à sonder une grille ;
    - *photos existantes* (Mapillary, KartaView) : les clichés sont déjà là,
      chacun avec sa position et son cap propres ; la découverte consiste à
      interroger une emprise et à regrouper ce qui revient.
    """

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    requires_key: ClassVar[bool] = False
    attribution: ClassVar[str] = ""

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, self.label, True, self.requires_key)

    def session(self) -> requests.Session:
        from .. import config

        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        return s

    @abstractmethod
    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        """Retourne les vues à télécharger pour cette ville."""

    @abstractmethod
    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        """Télécharge l'image d'une vue."""


def cap_views_per_place(views: List[ViewRef], opts: CollectOptions) -> List[ViewRef]:
    """Borne le nombre de vues par lieu et le nombre de lieux.

    Donne la même sémantique à `max_panos` / `headings` quelle que soit la
    source, y compris celles qui ne laissent pas choisir l'orientation.
    """
    per_place: dict[str, List[ViewRef]] = {}
    for v in views:
        bucket = per_place.setdefault(v.place_id, [])
        if len(bucket) < opts.headings:
            bucket.append(v)
        if len(per_place) > opts.max_panos and v.place_id not in per_place:
            break

    out: List[ViewRef] = []
    for place_id in list(per_place)[: opts.max_panos]:
        out.extend(per_place[place_id])
    return out
