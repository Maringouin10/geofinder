"""Mapillary — imagerie de rue participative, API v4 (graph.mapillary.com).

Alternative principale à Google Street View : le jeton d'accès est gratuit et
immédiat (mapillary.com → Developers → Register application), la couverture
mondiale est large, et les photos sont livrées avec leur position ET leur cap
réels — pas besoin de « rendre » une vue, elle existe déjà.
"""
from __future__ import annotations

import random
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .. import config
from ..geo import place_key
from ..geocode import City
from ..sampling import clamp_bbox
from .base import (
    CollectContext,
    CollectOptions,
    Provider,
    ProviderError,
    ProviderStatus,
    ViewRef,
    cap_views_per_place,
)

GRAPH_URL = "https://graph.mapillary.com/images"

# L'API plafonne à 2000 résultats par appel et ne pagine pas sur une emprise :
# on découpe donc la ville en tuiles et on interroge tuile par tuile.
TILE_GRID = 4
PER_TILE_LIMIT = 500

FIELDS = ",".join([
    "id",
    "geometry",
    "computed_geometry",
    "compass_angle",
    "computed_compass_angle",
    "captured_at",
    "is_pano",
    "thumb_1024_url",
])


class MapillaryProvider(Provider):
    name = "mapillary"
    label = "Mapillary"
    requires_key = True
    attribution = "Images © contributeurs Mapillary (CC BY-SA)"

    def status(self) -> ProviderStatus:
        token = config.MAPILLARY_TOKEN
        if not token:
            return ProviderStatus(
                self.name, self.label, False, True,
                "MAPILLARY_ACCESS_TOKEN absent — jeton gratuit sur mapillary.com "
                "(Developers → Register application).",
            )
        return ProviderStatus(self.name, self.label, True, True)

    def session(self) -> requests.Session:
        s = super().session()
        s.headers["Authorization"] = f"OAuth {config.MAPILLARY_TOKEN}"
        return s

    # ------------------------------------------------------------------ #

    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        if not config.MAPILLARY_TOKEN:
            raise ProviderError(self.status().reason)

        south, north, west, east = clamp_bbox(city, opts.radius_km)
        tiles = _tiles(south, north, west, east, TILE_GRID)
        random.Random(1337).shuffle(tiles)

        session = self.session()
        views: List[ViewRef] = []
        places: set[str] = set()
        target = opts.max_panos

        for i, tile in enumerate(tiles):
            ctx.check()
            for record in self._query_tile(session, tile):
                view = _to_view(record)
                if view is None:
                    continue
                views.append(view)
                places.add(view.place_id)
            ctx.report(
                len(places),
                f"Mapillary : {len(places)}/{target} lieux ({i + 1}/{len(tiles)} tuiles)",
            )
            if len(places) >= target or ctx.should_stop():
                break

        if not views:
            raise ProviderError(
                "Aucune photo Mapillary dans cette zone. Essaie un rayon plus grand, "
                "une ville mieux couverte, ou vérifie le jeton d'accès."
            )
        return cap_views_per_place(views, opts)

    def _query_tile(self, session: requests.Session, tile: Tuple[float, float, float, float]) -> Iterable[Dict[str, Any]]:
        south, north, west, east = tile
        params = {
            "fields": FIELDS,
            "bbox": f"{west},{south},{east},{north}",
            "limit": PER_TILE_LIMIT,
        }
        try:
            resp = session.get(GRAPH_URL, params=params, timeout=config.DISCOVERY_TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"Mapillary injoignable : {exc}") from exc

        if resp.status_code in (401, 403):
            raise ProviderError(
                "Mapillary a refusé le jeton (HTTP "
                f"{resp.status_code}). Vérifie MAPILLARY_ACCESS_TOKEN."
            )
        if resp.status_code != 200:
            # Une tuile qui échoue ne doit pas condamner toute l'indexation.
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        resp = session.get(view.ref, timeout=config.DOWNLOAD_TIMEOUT)
        if resp.status_code != 200:
            raise ProviderError(f"Téléchargement Mapillary HTTP {resp.status_code}")
        return resp.content


def _tiles(south: float, north: float, west: float, east: float, n: int) -> List[Tuple[float, float, float, float]]:
    dlat = (north - south) / n
    dlng = (east - west) / n
    return [
        (south + i * dlat, south + (i + 1) * dlat, west + j * dlng, west + (j + 1) * dlng)
        for i in range(n)
        for j in range(n)
    ]


def _to_view(record: Dict[str, Any]) -> Optional[ViewRef]:
    """Convertit un enregistrement de l'API en vue, en tolérant les champs absents.

    `computed_*` est la version raffinée par Mapillary (structure-from-motion) ;
    elle est plus précise que la mesure GPS/boussole brute quand elle existe.
    """
    image_id = record.get("id")
    url = record.get("thumb_1024_url") or record.get("thumb_2048_url") or record.get("thumb_256_url")
    if not image_id or not url:
        return None

    coords = _coords(record.get("computed_geometry")) or _coords(record.get("geometry"))
    if coords is None:
        return None
    lng, lat = coords

    angle = record.get("computed_compass_angle")
    if angle is None:
        angle = record.get("compass_angle")
    heading = int(round(float(angle))) % 360 if angle is not None else 0

    captured = record.get("captured_at")
    return ViewRef(
        view_id=str(image_id),
        place_id=place_key(lat, lng),
        lat=lat,
        lng=lng,
        heading=heading,
        ref=str(url),
        captured_at=str(captured) if captured is not None else None,
    )


def _coords(geometry: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        return float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
