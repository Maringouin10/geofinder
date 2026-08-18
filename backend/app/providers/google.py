"""Google Street View Static API.

Seule source à *rendre* les vues à la demande : on choisit un point et un cap,
le serveur produit l'image. La découverte consiste donc à sonder une grille via
l'endpoint `metadata` (gratuit), pour ne facturer que des images qui existent.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from .. import config, sampling
from ..geo import headings_for
from ..geocode import City
from .base import (
    CollectContext,
    CollectOptions,
    Provider,
    ProviderError,
    ProviderStatus,
    ViewRef,
)

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"


class GoogleProvider(Provider):
    name = "google"
    label = "Google Street View"
    requires_key = True
    attribution = "Panoramas © Google Street View"

    def status(self) -> ProviderStatus:
        if not config.GOOGLE_MAPS_API_KEY:
            return ProviderStatus(
                self.name, self.label, False, True,
                "GOOGLE_MAPS_API_KEY absent (API « Street View Static » à activer).",
            )
        return ProviderStatus(self.name, self.label, True, True)

    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        if not config.GOOGLE_MAPS_API_KEY:
            raise ProviderError(self.status().reason)

        points = sampling.grid_points(
            city, spacing_m=opts.spacing_m, radius_km=opts.radius_km, max_points=config.MAX_PROBES
        )
        session = self.session()
        radius = max(int(opts.spacing_m * 0.6), 30)
        found: Dict[str, tuple] = {}
        probed = 0

        with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
            for pano in pool.map(lambda p: self._safe_probe(p, radius, session), points):
                probed += 1
                if ctx.cancelled():
                    break
                if pano is not None and pano[0] not in found:
                    found[pano[0]] = pano
                ctx.report(
                    len(found),
                    f"Street View : {len(found)}/{opts.max_panos} panoramas ({probed} points sondés)",
                )
                if len(found) >= opts.max_panos:
                    break
        ctx.check()

        if not found:
            raise ProviderError(
                "Aucun panorama Street View dans cette zone (couverture inexistante, "
                "ou clé sans accès à l'API Street View Static)."
            )

        views: List[ViewRef] = []
        for pano_id, lat, lng, date in list(found.values())[: opts.max_panos]:
            for heading in headings_for(opts.headings):
                views.append(
                    ViewRef(
                        view_id=f"{pano_id}_{heading:03d}",
                        place_id=pano_id,
                        lat=lat,
                        lng=lng,
                        heading=heading,
                        ref=pano_id,
                        captured_at=date,
                    )
                )
        return views

    def _safe_probe(self, point, radius: int, session) -> Optional[tuple]:
        try:
            return self._probe(point[0], point[1], radius, session)
        except Exception:  # noqa: BLE001 - un point qui échoue ne tue pas le job
            return None

    def _probe(self, lat: float, lng: float, radius_m: int, session) -> Optional[tuple]:
        params = {
            "location": f"{lat},{lng}",
            "radius": radius_m,
            "source": "outdoor",
            "key": config.GOOGLE_MAPS_API_KEY,
        }
        resp = session.get(METADATA_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            raise ProviderError(
                f"Street View metadata : {status} — {data.get('error_message', '')}".strip()
            )
        return (
            data["pano_id"],
            float(data["location"]["lat"]),
            float(data["location"]["lng"]),
            data.get("date"),
        )

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        params = {
            "size": config.STREETVIEW_SIZE,
            "pano": view.ref,
            "heading": view.heading,
            "pitch": 0,
            "fov": config.STREETVIEW_FOV,
            "return_error_code": "true",
            "key": config.GOOGLE_MAPS_API_KEY,
        }
        resp = session.get(IMAGE_URL, params=params, timeout=30)
        if resp.status_code != 200:
            raise ProviderError(f"Street View image HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content
