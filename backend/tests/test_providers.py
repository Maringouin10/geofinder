"""Tests des sources d'imagerie : parsing des réponses d'API et sélection.

Le réseau est simulé — ces tests vérifient la traduction des réponses en
`ViewRef` et la robustesse aux champs manquants ou renommés, pas la
disponibilité réelle des services.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import providers  # noqa: E402
from backend.app.geocode import City  # noqa: E402
from backend.app.providers import CollectContext, CollectOptions  # noqa: E402
from backend.app.providers.base import cap_views_per_place  # noqa: E402
from backend.app.providers.kartaview import KartaViewProvider  # noqa: E402
from backend.app.providers.mapillary import MapillaryProvider, _to_view  # noqa: E402


CITY = City(
    query="Paris", display_name="Paris, France",
    lat=48.8566, lng=2.3522,
    south=48.85, north=48.87, west=2.34, east=2.37,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)
        self.content = b"\xff\xd8\xff"

    def json(self):
        if self._payload is None:
            raise ValueError("pas du JSON")
        return self._payload


class FakeSession:
    """Renvoie la même charge utile à chaque appel, en comptant les requêtes."""

    def __init__(self, payload, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.calls = []
        self.headers = {}
        self._text = text

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return FakeResponse(self.payload, self.status_code, self._text)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return FakeResponse(self.payload, self.status_code, self._text)


class RoutedSession:
    """Répond différemment selon l'URL, pour simuler des endpoints partiels."""

    def __init__(self, routes):
        self.routes = routes          # fragment d'URL -> (payload, status)
        self.calls = []
        self.headers = {}

    def _resolve(self, url):
        for fragment, (payload, status) in self.routes.items():
            if fragment in url:
                return FakeResponse(payload, status)
        return FakeResponse({"error": "not found"}, 404)

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return self._resolve(url)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self._resolve(url)


# --------------------------------------------------------------- Mapillary --

def mapillary_record(i: int, lat: float = 48.8566, lng: float = 2.3522) -> dict:
    return {
        "id": f"img{i}",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "computed_compass_angle": 271.4,
        "captured_at": 1600000000000,
        "thumb_1024_url": f"https://images.example/{i}.jpg",
    }


def test_mapillary_record_becomes_a_view():
    view = _to_view(mapillary_record(1))
    assert view is not None
    assert view.view_id == "img1"
    assert view.lat == pytest.approx(48.8566)
    assert view.lng == pytest.approx(2.3522)
    assert view.heading == 271                      # arrondi, borné à [0,360)
    assert view.ref == "https://images.example/1.jpg"


def test_mapillary_prefers_computed_geometry():
    record = mapillary_record(2)
    record["computed_geometry"] = {"type": "Point", "coordinates": [3.0, 45.0]}
    view = _to_view(record)
    # `computed_*` est la position raffinée par Mapillary : elle doit primer.
    assert (view.lat, view.lng) == (45.0, 3.0)


def test_mapillary_skips_unusable_records():
    assert _to_view({"id": "x"}) is None                                  # pas d'URL
    assert _to_view({"id": "x", "thumb_1024_url": "u"}) is None           # pas de position
    assert _to_view({"thumb_1024_url": "u", "geometry": {"coordinates": [1, 2]}}) is None
    assert _to_view({"id": "x", "thumb_1024_url": "u",
                     "geometry": {"coordinates": ["nan", None]}}) is None


def test_mapillary_falls_back_on_raw_compass_and_other_thumbs():
    record = {
        "id": "y",
        "geometry": {"type": "Point", "coordinates": [2.35, 48.85]},
        "compass_angle": 90.0,
        "thumb_256_url": "https://images.example/small.jpg",
    }
    view = _to_view(record)
    assert view.heading == 90
    assert view.ref.endswith("small.jpg")


def test_mapillary_requires_a_token(monkeypatch):
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "")
    provider = MapillaryProvider()
    assert provider.status().ready is False
    with pytest.raises(providers.ProviderError, match="MAPILLARY_ACCESS_TOKEN"):
        provider.discover(CITY, CollectOptions(), CollectContext())


def test_mapillary_discovers_and_groups_nearby_photos(monkeypatch):
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")
    # Trois clichés au même endroit + un à 500 m : deux lieux distincts.
    payload = {"data": [
        mapillary_record(1),
        mapillary_record(2),
        mapillary_record(3),
        mapillary_record(4, lat=48.8610, lng=2.3522),
    ]}
    session = FakeSession(payload)
    provider = MapillaryProvider()
    monkeypatch.setattr(provider, "session", lambda: session)

    views = provider.discover(CITY, CollectOptions(max_panos=50, headings=4), CollectContext())
    assert len({v.place_id for v in views}) == 2
    assert session.calls, "l'API doit être interrogée"
    assert "bbox" in session.calls[0][2]


def test_mapillary_rejects_a_bad_token(monkeypatch):
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|wrong")
    provider = MapillaryProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession({}, status_code=401))
    with pytest.raises(providers.ProviderError, match="refusé"):
        provider.discover(CITY, CollectOptions(), CollectContext())


def test_mapillary_reports_empty_coverage(monkeypatch):
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")
    provider = MapillaryProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession({"data": []}))
    with pytest.raises(providers.ProviderError, match="Aucune photo"):
        provider.discover(CITY, CollectOptions(), CollectContext())


# --------------------------------------------------------------- KartaView --

NEARBY_PAYLOAD = {"currentPageItems": [
    {"id": "1", "lat": "48.8566", "lng": "2.3522", "heading": "90",
     "name": "files/photo/a.jpg"},
    {"id": "2", "lat": "48.8567", "lng": "2.3523", "heading": "",
     "lth_name": "files/photo/blth.jpg"},
]}

BBOX_PAYLOAD = {"result": {"data": [
    {"id": "9", "lat": "48.8570", "lng": "2.3530", "heading": "12",
     "fileurlProc": "https://cdn.example/9.jpg"},
]}}


def test_kartaview_parses_the_v1_envelope(monkeypatch):
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession(NEARBY_PAYLOAD))

    views = provider.discover(CITY, CollectOptions(max_panos=10, headings=4), CollectContext())
    assert len(views) == 2
    assert views[0].ref.startswith("https://storage.openstreetcam.org/files/photo/")
    assert views[1].heading == 0          # cap vide → 0 plutôt qu'une exception


def test_kartaview_falls_back_to_the_bbox_endpoint(monkeypatch):
    """Si la forme v1 échoue en 400, la v2 doit prendre le relais."""
    session = RoutedSession({
        "/1.0/list/nearby-photos/": (None, 400),
        "/2.0/photo/": (BBOX_PAYLOAD, 200),
    })
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: session)

    views = provider.discover(CITY, CollectOptions(max_panos=10, headings=4), CollectContext())
    assert len(views) == 1
    assert views[0].ref == "https://cdn.example/9.jpg"
    assert any("/2.0/photo/" in c[1] for c in session.calls)


def test_kartaview_error_quotes_the_api_response(monkeypatch):
    """Un 400 doit remonter le corps de la réponse, pas juste le code."""
    provider = KartaViewProvider()
    monkeypatch.setattr(
        provider, "session",
        lambda: FakeSession(None, status_code=400, text="missing parameter: bbTopLeft"),
    )
    with pytest.raises(providers.ProviderError) as err:
        provider.discover(CITY, CollectOptions(), CollectContext())

    message = str(err.value)
    assert "400" in message
    assert "missing parameter: bbTopLeft" in message      # le vrai motif
    assert "MAPILLARY_ACCESS_TOKEN" in message            # et quoi faire ensuite
    assert "openstreetcam.org" in message and "kartaview.org" in message


def test_kartaview_reports_an_application_level_error(monkeypatch):
    """L'API répond parfois 200 avec une erreur applicative dans le corps."""
    payload = {"status": {"apiCode": "660", "apiMessage": "Invalid request"}}
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession(payload))
    with pytest.raises(providers.ProviderError, match="Invalid request"):
        provider.discover(CITY, CollectOptions(), CollectContext())


def test_kartaview_deduplicates_photos_seen_from_several_probes(monkeypatch):
    """La grille sonde des points qui se recouvrent : une photo ne doit compter qu'une fois."""
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession(NEARBY_PAYLOAD))
    views = provider.discover(CITY, CollectOptions(max_panos=50, headings=4), CollectContext())
    assert len(views) == len({v.view_id for v in views}) == 2


def test_kartaview_needs_no_key():
    status = KartaViewProvider().status()
    assert status.ready is True and status.requires_key is False


# ----------------------------------------------------------------- registre --

def test_auto_prefers_mapillary_then_kartaview(monkeypatch):
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", "")
    assert providers.resolve_auto() == "mapillary"

    # Sans aucune clé, on retombe sur la source qui n'en demande pas.
    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "")
    assert providers.resolve_auto() == "kartaview"


def test_unknown_provider_is_rejected():
    with pytest.raises(providers.ProviderError, match="inconnue"):
        providers.get("streetview-de-mon-cousin")


def test_cap_limits_places_and_views_per_place():
    from backend.app.providers.base import ViewRef

    views = [ViewRef(f"v{i}", f"place{i // 10}", 1.0, 2.0, 0, "ref") for i in range(100)]
    capped = cap_views_per_place(views, CollectOptions(max_panos=4, headings=3))
    assert len({v.place_id for v in capped}) == 4
    assert len(capped) == 12


# ------------------------------------------------ interruption / budget --

def test_bounded_map_stops_without_draining_the_queue():
    """`Executor.map` soumet tout d'emblée : sortir de la boucle n'annule rien
    et la fermeture du pool attend chaque tâche en file. `bounded_map` doit
    rendre la main immédiatement."""
    import time as _time

    from backend.app.providers.base import bounded_map

    seen = []
    started = _time.monotonic()
    for result in bounded_map(
        lambda x: (_time.sleep(0.05), x)[1], range(400), 8, should_stop=lambda: len(seen) >= 10
    ):
        seen.append(result)
    elapsed = _time.monotonic() - started

    assert len(seen) >= 10
    # 400 tâches × 50 ms / 8 workers ≈ 2,5 s si la file n'est pas annulée.
    assert elapsed < 1.0, f"la file n'a pas été annulée ({elapsed:.2f}s)"


def test_discovery_gives_up_when_the_time_budget_is_exhausted(monkeypatch):
    """Une source qui traîne doit faire échouer le job, pas le figer."""
    import time as _time

    from backend.app import config
    from backend.app.providers.base import TimedOut

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")

    class SlowSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            _time.sleep(0.05)
            return super().get(url, params, timeout)

    provider = MapillaryProvider()
    monkeypatch.setattr(provider, "session", lambda: SlowSession({"data": []}))
    ctx = CollectContext(deadline=_time.monotonic() - 1)   # budget déjà dépassé

    with pytest.raises(TimedOut, match="Budget de temps"):
        provider.discover(CITY, CollectOptions(max_panos=1000), ctx)


def test_cancellation_beats_the_deadline():
    """Une annulation explicite reste distincte d'un dépassement de budget."""
    import time as _time

    from backend.app.providers.base import Cancelled

    ctx = CollectContext(cancelled=lambda: True, deadline=_time.monotonic() - 1)
    assert ctx.should_stop() is True
    with pytest.raises(Cancelled):
        ctx.check()


# ---------------------------------------------- robustesse réseau Mapillary --

class FlakySession(FakeSession):
    """Échoue sur les N premières requêtes, répond normalement ensuite."""

    def __init__(self, payload, failures, exc=None):
        super().__init__(payload)
        self.remaining_failures = failures
        self.exc = exc or __import__("requests").exceptions.ReadTimeout("read timeout=45")

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.exc
        return FakeResponse(self.payload, self.status_code)


def test_mapillary_survives_tiles_that_time_out(monkeypatch):
    """Une tuile en échec ne doit pas condamner l'indexation : il y en a des centaines."""
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")
    session = FlakySession({"data": [mapillary_record(1)]}, failures=5)
    provider = MapillaryProvider()
    monkeypatch.setattr(provider, "session", lambda: session)

    views = provider.discover(CITY, CollectOptions(max_panos=1, headings=4), CollectContext())
    assert views, "les tuiles saines doivent suffire"
    assert session.remaining_failures == 0


def test_mapillary_explains_a_total_timeout(monkeypatch):
    """Si toutes les tuiles échouent, l'erreur doit nommer le motif réseau —
    pas prétendre à tort que la zone n'est pas couverte."""
    from backend.app import config

    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "MLY|fake")
    provider = MapillaryProvider()
    monkeypatch.setattr(
        provider, "session", lambda: FlakySession({"data": []}, failures=10_000)
    )

    with pytest.raises(providers.ProviderError) as err:
        provider.discover(CITY, CollectOptions(max_panos=5), CollectContext())

    message = str(err.value)
    assert "ReadTimeout" in message                 # le vrai motif
    assert "GEOFINDER_DISCOVERY_TIMEOUT" in message  # et le levier pour le régler
    assert "Aucune photo Mapillary" not in message   # surtout pas un faux diagnostic


def test_mapillary_tiles_are_small_enough_to_answer():
    """Des tuiles de plusieurs kilomètres font expirer la requête côté serveur."""
    from backend.app.geo import distance_m
    from backend.app.providers.mapillary import TILE_M, _tiles
    from backend.app.sampling import clamp_bbox

    # Ville étendue : la bbox n'est plus le facteur limitant.
    big = City(query="Paris", display_name="Paris, France",
               lat=48.8566, lng=2.3522,
               south=48.75, north=48.95, west=2.20, east=2.50)

    tiles = _tiles(*clamp_bbox(big, 6.0), TILE_M)
    assert len(tiles) > 100, "une ville entière doit être découpée finement"

    # Chaque tuile doit rester petite : c'est l'emprise, pas leur nombre, qui
    # faisait expirer la requête côté Mapillary.
    for south, north, west, east in (tiles[0], tiles[len(tiles) // 2], tiles[-1]):
        assert distance_m(south, west, north, west) <= TILE_M + 50
        assert distance_m(south, west, south, east) <= TILE_M + 50


def test_session_retries_transient_failures():
    """429 et 5xx sont fréquents sur ces API : la session doit reprendre seule."""
    from backend.app.providers.demo import DemoProvider

    adapter = DemoProvider().session().get_adapter("https://example/")
    retry = adapter.max_retries
    assert retry.total >= 1
    assert 429 in retry.status_forcelist and 503 in retry.status_forcelist
    assert "POST" in retry.allowed_methods      # KartaView v1 poste
