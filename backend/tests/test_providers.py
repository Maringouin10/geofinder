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
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.content = b"\xff\xd8\xff"

    def json(self):
        return self._payload


class FakeSession:
    """Renvoie la même charge utile à chaque appel, en comptant les requêtes."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.payload, self.status_code)


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
    assert "bbox" in session.calls[0][1]


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

def test_kartaview_parses_the_documented_envelope(monkeypatch):
    payload = {"result": {"data": [
        {"id": 1, "lat": "48.8566", "lng": "2.3522", "heading": "90",
         "fileurlProc": "files/photo/a.jpg"},
        {"id": 2, "lat": "48.8567", "lng": "2.3523", "heading": "",
         "fileurlProc": "https://cdn.example/b.jpg"},
    ]}}
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession(payload))

    views = provider.discover(CITY, CollectOptions(max_panos=10, headings=4), CollectContext())
    assert len(views) == 2
    # URL relative complétée, URL absolue laissée intacte.
    assert views[0].ref.startswith("https://storage.openstreetcam.org/files/photo/")
    assert views[1].ref == "https://cdn.example/b.jpg"
    assert views[1].heading == 0          # cap vide → 0 plutôt qu'une exception


def test_kartaview_needs_no_key():
    status = KartaViewProvider().status()
    assert status.ready is True and status.requires_key is False


def test_kartaview_reports_empty_coverage(monkeypatch):
    provider = KartaViewProvider()
    monkeypatch.setattr(provider, "session", lambda: FakeSession({"result": {"data": []}}))
    with pytest.raises(providers.ProviderError, match="Aucune photo"):
        provider.discover(CITY, CollectOptions(), CollectContext())


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
