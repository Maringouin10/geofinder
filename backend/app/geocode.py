"""Résolution d'un nom de ville en boîte englobante via Nominatim (OSM)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import requests

from . import config


class GeocodeError(RuntimeError):
    pass


@dataclass
class City:
    query: str
    display_name: str
    lat: float
    lng: float
    south: float
    north: float
    west: float
    east: float

    @property
    def slug(self) -> str:
        return slugify(self.display_name.split(",")[0] or self.query)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "ville"


def geocode_city(name: str) -> City:
    """Cherche la ville et retourne son centre + sa bounding box."""
    params = {"q": name, "format": "json", "limit": 1, "addressdetails": 0}
    try:
        resp = requests.get(
            config.NOMINATIM_URL,
            params=params,
            headers={"User-Agent": config.USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:  # réseau / DNS / timeout
        raise GeocodeError(f"Géocodage impossible ({exc})") from exc

    if not results:
        raise GeocodeError(f"Aucune ville trouvée pour « {name} »")

    hit = results[0]
    south, north, west, east = (float(v) for v in hit["boundingbox"])
    return City(
        query=name,
        display_name=hit.get("display_name", name),
        lat=float(hit["lat"]),
        lng=float(hit["lon"]),
        south=south,
        north=north,
        west=west,
        east=east,
    )
