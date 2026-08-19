"""Mapillary — imagerie de rue participative, API v4 (graph.mapillary.com).

Alternative principale à Google Street View : le jeton d'accès est gratuit et
immédiat (mapillary.com → Developers → Register application), la couverture
mondiale est large, et les photos sont livrées avec leur position ET leur cap
réels — pas besoin de « rendre » une vue, elle existe déjà.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

import requests

from .. import config
from ..geo import place_key
from ..geocode import City
from ..sampling import clamp_bbox
from .base import (
    CollectContext,
    bounded_map,
    CollectOptions,
    Provider,
    ProviderError,
    ProviderStatus,
    ViewRef,
    cap_views_per_place,
)

GRAPH_URL = "https://graph.mapillary.com/images"

# Une emprise large coûte très cher côté serveur : sur une ville entière la
# requête dépasse la minute et finit en read timeout. On interroge donc de
# petites tuiles, en parallèle, et on s'arrête dès qu'on a assez de lieux.
TILE_M = 400
PER_TILE_LIMIT = 100
MAX_TILES = 600

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

        tiles = _tiles(*clamp_bbox(city, opts.radius_km), TILE_M)
        random.Random(1337).shuffle(tiles)
        tiles = tiles[:MAX_TILES]

        session = self.session()
        views: List[ViewRef] = []
        places: set[str] = set()
        seen: set[str] = set()
        target = opts.max_panos
        done = 0
        errors: List[str] = []

        def enough() -> bool:
            return len(places) >= target or ctx.should_stop()

        for records, error in bounded_map(
            lambda tile: self._query_tile(session, tile),
            tiles,
            config.FETCH_WORKERS,
            should_stop=enough,
        ):
            done += 1
            if error:
                errors.append(error)
            for record in records:
                view = _to_view(record)
                if view is None or view.view_id in seen:
                    continue
                seen.add(view.view_id)
                views.append(view)
                places.add(view.place_id)
            ctx.report(
                len(places),
                f"Mapillary : {len(places)}/{target} lieux ({done}/{len(tiles)} tuiles)",
            )
        ctx.check()

        if not views:
            if errors:
                # Toutes les tuiles ont échoué : c'est un problème de liaison,
                # pas une absence de couverture. On cite le motif.
                raise ProviderError(
                    f"Mapillary n'a répondu à aucune des {done} tuiles interrogées.\n"
                    f"Premier motif : {errors[0]}\n"
                    "Si c'est un dépassement de délai, réduis radius_km ou augmente "
                    "GEOFINDER_DISCOVERY_TIMEOUT."
                )
            raise ProviderError(
                "Aucune photo Mapillary dans cette zone. Essaie un rayon plus grand "
                "ou une ville mieux couverte."
            )
        return cap_views_per_place(views, opts)

    def _query_tile(
        self, session: requests.Session, tile: Tuple[float, float, float, float]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Interroge une tuile. Retourne (photos, motif d'échec).

        Une tuile qui échoue ne condamne pas l'indexation — il y en a des
        centaines. Seul un jeton refusé est fatal : réessayer n'y changerait rien.
        """
        south, north, west, east = tile
        params = {
            "fields": FIELDS,
            "bbox": f"{west},{south},{east},{north}",
            "limit": PER_TILE_LIMIT,
        }
        try:
            resp = session.get(
                GRAPH_URL, params=params, timeout=config.timeouts(config.DISCOVERY_TIMEOUT)
            )
        except requests.RequestException as exc:
            return [], f"{type(exc).__name__} — {exc}"

        if resp.status_code in (401, 403):
            raise ProviderError(
                f"Mapillary a refusé le jeton (HTTP {resp.status_code}). "
                "Vérifie MAPILLARY_ACCESS_TOKEN : il doit commencer par « MLY| » "
                f"et provenir de mapillary.com → Developers. Réponse : {resp.text[:200]}"
            )
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code} — {resp.text[:200]}"
        try:
            payload = resp.json()
        except ValueError:
            return [], "réponse non-JSON"
        data = payload.get("data")
        return (data if isinstance(data, list) else []), None

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        resp = session.get(view.ref, timeout=config.timeouts(config.DOWNLOAD_TIMEOUT))
        if resp.status_code != 200:
            raise ProviderError(f"Téléchargement Mapillary HTTP {resp.status_code}")
        return resp.content


def _tiles(south: float, north: float, west: float, east: float,
           tile_m: float) -> List[Tuple[float, float, float, float]]:
    """Découpe l'emprise en tuiles d'environ `tile_m` de côté."""
    from ..geo import EARTH_M_PER_DEG, lng_scale

    mid_lat = (south + north) / 2
    dlat = tile_m / EARTH_M_PER_DEG
    dlng = tile_m / lng_scale(mid_lat)
    rows = max(int((north - south) / dlat) + 1, 1)
    cols = max(int((east - west) / dlng) + 1, 1)
    return [
        (
            south + i * dlat,
            min(south + (i + 1) * dlat, north),
            west + j * dlng,
            min(west + (j + 1) * dlng, east),
        )
        for i in range(rows)
        for j in range(cols)
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
