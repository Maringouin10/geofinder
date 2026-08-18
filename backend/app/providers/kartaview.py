"""KartaView (ex-OpenStreetCam) — imagerie de rue libre, sans clé d'API.

Couverture plus clairsemée que Mapillary, mais c'est la seule source qui
fonctionne sans inscription : utile pour démarrer immédiatement.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import requests

from ..geo import place_key
from ..geocode import City
from ..sampling import clamp_bbox
from .base import (
    CollectContext,
    CollectOptions,
    Provider,
    ProviderError,
    ViewRef,
    cap_views_per_place,
)

API_URL = "https://api.openstreetcam.org/2.0/photo/"
STORAGE_BASE = "https://storage.openstreetcam.org/"
PER_PAGE = 200
MAX_PAGES = 25


class KartaViewProvider(Provider):
    name = "kartaview"
    label = "KartaView (sans clé)"
    requires_key = False
    attribution = "Images © contributeurs KartaView (CC BY-SA)"

    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        south, north, west, east = clamp_bbox(city, opts.radius_km)
        session = self.session()

        views: List[ViewRef] = []
        places: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            ctx.check()
            batch = self._query(session, south, north, west, east, page)
            if not batch:
                break
            for record in batch:
                view = _to_view(record)
                if view is None:
                    continue
                views.append(view)
                places.add(view.place_id)
            ctx.report(len(places), f"KartaView : {len(places)}/{opts.max_panos} lieux (page {page})")
            if len(places) >= opts.max_panos or len(batch) < PER_PAGE:
                break

        if not views:
            raise ProviderError(
                "Aucune photo KartaView dans cette zone. La couverture y est peut-être "
                "inexistante — essaie Mapillary, qui couvre bien plus de territoire."
            )
        return cap_views_per_place(views, opts)

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        resp = session.get(view.ref, timeout=30)
        if resp.status_code != 200:
            raise ProviderError(f"Téléchargement KartaView HTTP {resp.status_code}")
        return resp.content

    def _query(self, session, south, north, west, east, page: int) -> Iterable[Dict[str, Any]]:
        params = {
            "bbTopLeft": f"{north},{west}",
            "bbBottomRight": f"{south},{east}",
            "itemsPerPage": PER_PAGE,
            "page": page,
        }
        try:
            resp = session.get(API_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            raise ProviderError(f"KartaView injoignable : {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"KartaView HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return []
        return _extract_records(payload)


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    """Récupère la liste de photos quelle que soit la forme de l'enveloppe.

    L'API a changé de structure entre ses versions ; on accepte les variantes
    plutôt que de casser sur un renommage d'enveloppe.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for candidate in (
        payload.get("result", {}).get("data") if isinstance(payload.get("result"), dict) else None,
        payload.get("data"),
        payload.get("currentPageItems"),
    ):
        if isinstance(candidate, list):
            return [r for r in candidate if isinstance(r, dict)]
    return []


def _pick(record: Dict[str, Any], *names: str) -> Optional[Any]:
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return None


def _to_view(record: Dict[str, Any]) -> Optional[ViewRef]:
    photo_id = _pick(record, "id", "photoId", "photo_id")
    url = _pick(record, "fileurlProc", "fileurl_proc", "fileurlLTh", "fileurlTh", "name")
    lat = _pick(record, "lat", "latitude")
    lng = _pick(record, "lng", "lon", "longitude")
    if photo_id is None or url is None or lat is None or lng is None:
        return None

    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None

    heading_raw = _pick(record, "heading", "compassAngle", "gpsCompass")
    try:
        heading = int(round(float(heading_raw))) % 360 if heading_raw is not None else 0
    except (TypeError, ValueError):
        heading = 0

    url = str(url)
    if not url.startswith("http"):
        url = STORAGE_BASE + url.lstrip("/")

    return ViewRef(
        view_id=str(photo_id),
        place_id=place_key(lat_f, lng_f),
        lat=lat_f,
        lng=lng_f,
        heading=heading,
        ref=url,
        captured_at=_pick(record, "shotDate", "dateAdded"),
    )
