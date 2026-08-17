"""Tests des briques pures (aucun réseau, aucun modèle chargé)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.geocode import City, slugify  # noqa: E402
from backend.app.sampling import clamp_bbox, grid_points  # noqa: E402
from backend.app.streetview import distance_m, headings_for  # noqa: E402


def make_city(**kw) -> City:
    base = dict(
        query="Paris",
        display_name="Paris, Île-de-France, France",
        lat=48.8566,
        lng=2.3522,
        south=48.8156,
        north=48.9021,
        west=2.2242,
        east=2.4699,
    )
    base.update(kw)
    return City(**base)


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Saint-Étienne") == "saint-etienne"
    assert slugify("Paris, Île-de-France") == "paris-ile-de-france"
    assert slugify("!!!") == "ville"


def test_city_slug_uses_first_component():
    assert make_city().slug == "paris"


def test_headings_are_evenly_spread():
    assert headings_for(4) == [0, 90, 180, 270]
    assert headings_for(1) == [0]
    assert headings_for(0) == [0]          # borné à 1
    assert len(headings_for(99)) == 12     # borné à 12


def test_distance_m_is_haversine():
    # Paris → Lyon, ~392 km
    d = distance_m(48.8566, 2.3522, 45.7640, 4.8357)
    assert 385_000 < d < 400_000
    assert distance_m(48.8566, 2.3522, 48.8566, 2.3522) == 0.0


def test_clamp_bbox_shrinks_large_boxes():
    city = make_city(south=40.0, north=55.0, west=-5.0, east=12.0)
    south, north, west, east = clamp_bbox(city, radius_km=5)
    assert north - south < 0.12          # ~10 km de haut
    assert south < city.lat < north
    assert west < city.lng < east


def test_clamp_bbox_falls_back_when_center_outside_bbox():
    # bbox incohérente avec le centre : on doit retomber sur le carré centré.
    city = make_city(south=10.0, north=11.0, west=10.0, east=11.0)
    south, north, west, east = clamp_bbox(city, radius_km=3)
    assert south < city.lat < north
    assert west < city.lng < east


def test_grid_points_respects_max_and_stays_in_bbox():
    city = make_city()
    pts = grid_points(city, spacing_m=100, radius_km=5, max_points=200)
    assert 0 < len(pts) <= 200
    south, north, west, east = clamp_bbox(city, 5)
    for lat, lng in pts:
        assert south - 1e-6 <= lat <= north + 1e-3
        assert west - 1e-6 <= lng <= east + 1e-3


def test_grid_points_is_deterministic():
    city = make_city()
    a = grid_points(city, spacing_m=150, radius_km=4, max_points=50)
    b = grid_points(city, spacing_m=150, radius_km=4, max_points=50)
    assert a == b
