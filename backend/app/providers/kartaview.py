"""KartaView (ex-OpenStreetCam) — imagerie de rue libre, sans clé d'API.

Le service a changé de nom, d'hôte et de version d'API au fil du temps, et
plusieurs formes coexistent encore dans la nature. Plutôt que de parier sur
une seule, ce provider les **sonde** au démarrage et garde celle qui répond.
Si aucune ne répond, l'erreur remontée cite ce que chaque tentative a dit,
pour que le diagnostic soit immédiat.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .. import config, sampling
from ..geo import place_key
from ..geocode import City
from .base import (
    CollectContext,
    CollectOptions,
    Provider,
    ProviderError,
    ViewRef,
    cap_views_per_place,
)

# Les deux noms d'hôte du service (l'ancien redirige encore, mais pas partout).
HOSTS = ("https://api.openstreetcam.org", "https://api.kartaview.org")

# Base de stockage des fichiers quand l'API renvoie un chemin relatif.
STORAGE_BASES = ("https://storage.openstreetcam.org/", "https://storage.kartaview.org/")

NEARBY_RADIUS_M = 250
PER_PAGE = 200
MAX_PAGES = 25


@dataclass(frozen=True)
class Endpoint:
    """Un couple (hôte, forme d'API) qui a effectivement répondu."""
    host: str
    kind: str  # "nearby" (v1, POST autour d'un point) | "bbox" (v2, GET sur une emprise)

    def __str__(self) -> str:
        return f"{self.host} [{self.kind}]"


class KartaViewProvider(Provider):
    name = "kartaview"
    label = "KartaView (sans clé)"
    requires_key = False
    attribution = "Images © contributeurs KartaView (CC BY-SA)"

    # ------------------------------------------------------------------ #

    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        session = self.session()
        endpoint = self._select_endpoint(city, session, ctx)
        ctx.report(0, f"KartaView : {endpoint.kind} sur {endpoint.host}…")

        if endpoint.kind == "nearby":
            views = self._collect_nearby(city, opts, ctx, session, endpoint)
        else:
            views = self._collect_bbox(city, opts, ctx, session, endpoint)

        if not views:
            raise ProviderError(
                f"KartaView a répondu ({endpoint}) mais ne couvre pas cette zone. "
                "Essaie un rayon plus grand, ou Mapillary qui couvre bien plus de territoire."
            )
        return cap_views_per_place(views, opts)

    def _select_endpoint(self, city: City, session, ctx: CollectContext) -> Endpoint:
        """Essaie chaque forme d'API connue et garde la première qui renvoie des photos."""
        attempts: List[str] = []
        for host in HOSTS:
            for kind in ("nearby", "bbox"):
                ctx.check()
                candidate = Endpoint(host, kind)
                ctx.report(0, f"KartaView : test de {candidate}…")
                try:
                    records = self._fetch_page(session, candidate, city, page=1, probe=True)
                except ProviderError as exc:
                    attempts.append(f"  • {candidate} → {exc}")
                    continue
                if records:
                    return candidate
                attempts.append(f"  • {candidate} → répond, mais 0 photo au centre de la ville")

        raise ProviderError(
            "Aucun point d'accès KartaView n'a fonctionné :\n"
            + "\n".join(attempts)
            + "\n\nUtilise plutôt Mapillary : le jeton est gratuit et immédiat "
              "(mapillary.com → Developers → Register application), et la couverture "
              "est bien meilleure. Renseigne MAPILLARY_ACCESS_TOKEN puis relance."
        )

    # --------------------------------------------------------- collecte --

    def _collect_nearby(self, city, opts, ctx, session, endpoint) -> List[ViewRef]:
        """v1 : une requête par point d'une grille couvrant la ville."""
        points = sampling.grid_points(
            city,
            spacing_m=max(NEARBY_RADIUS_M * 2, opts.spacing_m),
            radius_km=opts.radius_km,
            max_points=config.MAX_PROBES,
        )
        views: List[ViewRef] = []
        places: set[str] = set()
        seen: set[str] = set()
        done = 0

        with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
            for records in pool.map(
                lambda p: self._safe_nearby(session, endpoint, p[0], p[1]), points
            ):
                done += 1
                if ctx.cancelled():
                    break
                for view in _to_views(records, seen):
                    views.append(view)
                    places.add(view.place_id)
                ctx.report(
                    len(places),
                    f"KartaView : {len(places)}/{opts.max_panos} lieux ({done} points sondés)",
                )
                if len(places) >= opts.max_panos:
                    break
        ctx.check()
        return views

    def _collect_bbox(self, city, opts, ctx, session, endpoint) -> List[ViewRef]:
        """v2 : pagination sur l'emprise de la ville."""
        views: List[ViewRef] = []
        places: set[str] = set()
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            ctx.check()
            records = self._fetch_page(session, endpoint, city, page=page, radius_km=opts.radius_km)
            if not records:
                break
            for view in _to_views(records, seen):
                views.append(view)
                places.add(view.place_id)
            ctx.report(len(places), f"KartaView : {len(places)}/{opts.max_panos} lieux (page {page})")
            if len(places) >= opts.max_panos or len(records) < PER_PAGE:
                break
        return views

    def _safe_nearby(self, session, endpoint, lat, lng) -> List[Dict[str, Any]]:
        try:
            return self._call_nearby(session, endpoint.host, lat, lng)
        except Exception:  # noqa: BLE001 - un point qui échoue ne tue pas le job
            return []

    # ----------------------------------------------------------- réseau --

    def _fetch_page(self, session, endpoint: Endpoint, city: City, page: int,
                    radius_km: float = 1.0, probe: bool = False) -> List[Dict[str, Any]]:
        if endpoint.kind == "nearby":
            return self._call_nearby(session, endpoint.host, city.lat, city.lng)
        south, north, west, east = sampling.clamp_bbox(city, 1.0 if probe else radius_km)
        return self._call_bbox(session, endpoint.host, south, north, west, east, page)

    def _call_nearby(self, session, host: str, lat: float, lng: float) -> List[Dict[str, Any]]:
        """v1 : POST formulaire, photos autour d'un point."""
        url = f"{host}/1.0/list/nearby-photos/"
        try:
            resp = session.post(
                url,
                data={"lat": f"{lat}", "lng": f"{lng}", "radius": NEARBY_RADIUS_M},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"injoignable ({exc})") from exc
        return _records_or_raise(resp)

    def _call_bbox(self, session, host: str, south, north, west, east, page: int) -> List[Dict[str, Any]]:
        """v2 : GET sur une emprise rectangulaire."""
        url = f"{host}/2.0/photo/"
        params = {
            "bbTopLeft": f"{north},{west}",
            "bbBottomRight": f"{south},{east}",
            "itemsPerPage": PER_PAGE,
            "page": page,
        }
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            raise ProviderError(f"injoignable ({exc})") from exc
        return _records_or_raise(resp)

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        resp = session.get(view.ref, timeout=30)
        if resp.status_code != 200:
            raise ProviderError(f"Téléchargement KartaView HTTP {resp.status_code}")
        return resp.content


# --------------------------------------------------------------- parsing --

def _records_or_raise(resp) -> List[Dict[str, Any]]:
    """Extrait la liste de photos, ou lève une erreur qui cite la réponse.

    Un HTTP 400 ne dit rien en soi : c'est le corps de la réponse qui nomme le
    paramètre fautif, donc on le fait remonter jusqu'à l'utilisateur.
    """
    if resp.status_code != 200:
        raise ProviderError(f"HTTP {resp.status_code} — {_snippet(resp)}")
    try:
        payload = resp.json()
    except ValueError:
        raise ProviderError(f"réponse non-JSON — {_snippet(resp)}") from None

    records = _extract_records(payload)
    if not records and isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, dict):
            api_code = status.get("apiCode")
            message = status.get("apiMessage") or status.get("httpMessage")
            # L'API répond parfois 200 avec une erreur applicative dans le corps.
            if message and str(api_code) not in ("600", "None"):
                raise ProviderError(f"erreur API {api_code} — {message}")
    return records


def _snippet(resp, limit: int = 300) -> str:
    try:
        text = resp.text.strip().replace("\n", " ")
    except Exception:  # noqa: BLE001
        return "(corps illisible)"
    return text[:limit] or "(corps vide)"


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    """Récupère la liste de photos quelle que soit la forme de l'enveloppe."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    result = payload.get("result")
    for candidate in (
        result.get("data") if isinstance(result, dict) else None,
        payload.get("currentPageItems"),   # v1
        payload.get("data"),
        payload.get("photos"),
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


def _to_views(records: List[Dict[str, Any]], seen: set[str]) -> List[ViewRef]:
    out = []
    for record in records:
        view = _to_view(record)
        if view is not None and view.view_id not in seen:
            seen.add(view.view_id)
            out.append(view)
    return out


def _to_view(record: Dict[str, Any]) -> Optional[ViewRef]:
    photo_id = _pick(record, "id", "photoId", "photo_id")
    # v2 : fileurlProc/fileurlLTh — v1 : name/lth_name/th_name
    url = _pick(
        record,
        "fileurlProc", "fileurl_proc", "fileurlLTh", "fileurlTh",
        "name", "lth_name", "th_name",
    )
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
        url = STORAGE_BASES[0] + url.lstrip("/")

    return ViewRef(
        view_id=str(photo_id),
        place_id=place_key(lat_f, lng_f),
        lat=lat_f,
        lng=lng_f,
        heading=heading,
        ref=url,
        captured_at=_pick(record, "shotDate", "dateAdded", "date_added"),
    )
