"""Contrat commun à toutes les sources d'imagerie de rue."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, List, Optional

import requests

from ..geocode import City


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
    """Permet au provider de remonter sa progression et d'être interrompu."""
    progress: Callable[[int, str], None] = field(default=lambda found, note: None)
    cancelled: Callable[[], bool] = field(default=lambda: False)

    def report(self, found: int, note: str) -> None:
        self.progress(found, note)

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()


class Cancelled(RuntimeError):
    pass


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
