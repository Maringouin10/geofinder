"""Client Google Street View Static API (+ mode démo hors ligne)."""
from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from typing import Optional

import requests
from PIL import Image, ImageDraw

from . import config

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"


@dataclass
class Pano:
    pano_id: str
    lat: float
    lng: float
    date: Optional[str] = None


class StreetViewError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


# --------------------------------------------------------------------------- #
# Mode démo : aucun appel réseau, images synthétiques déterministes.
# --------------------------------------------------------------------------- #

def _demo_pano_id(lat: float, lng: float) -> str:
    return hashlib.sha1(f"{lat:.5f},{lng:.5f}".encode()).hexdigest()[:16]


def _demo_probe(lat: float, lng: float) -> Optional[Pano]:
    """Simule la disponibilité Street View : ~65 % des points sont couverts."""
    h = int(hashlib.sha1(f"cov{lat:.5f},{lng:.5f}".encode()).hexdigest(), 16)
    if h % 100 >= 65:
        return None
    # Snap léger, comme le ferait Google vers la route la plus proche.
    snapped_lat = round(lat + ((h >> 8) % 21 - 10) * 1e-5, 6)
    snapped_lng = round(lng + ((h >> 16) % 21 - 10) * 1e-5, 6)
    return Pano(_demo_pano_id(snapped_lat, snapped_lng), snapped_lat, snapped_lng)


def _demo_image(pano: Pano, heading: int, size: tuple[int, int]) -> bytes:
    seed = int(hashlib.sha1(f"{pano.pano_id}:{heading}".encode()).hexdigest(), 16)
    w, h = size
    img = Image.new("RGB", (w, h), (18, 22, 30))
    draw = ImageDraw.Draw(img)

    sky = ((seed >> 3) % 60 + 120, (seed >> 9) % 60 + 150, 210)
    for y in range(h // 2):
        k = y / max(h // 2, 1)
        draw.line([(0, y), (w, y)], fill=(int(sky[0] * (1 - 0.3 * k)), int(sky[1] * (1 - 0.25 * k)), int(sky[2] * (1 - 0.2 * k))))
    draw.rectangle([0, h // 2, w, h], fill=(70 + seed % 25, 68 + (seed >> 5) % 25, 66))

    # Façades pseudo-aléatoires mais stables pour un couple (pano, heading).
    x = 0
    i = 0
    while x < w:
        bw = 30 + ((seed >> (i % 20)) % 70)
        bh = 60 + ((seed >> (i % 13 + 3)) % (h // 2))
        color = (
            60 + ((seed >> (i % 11)) % 150),
            55 + ((seed >> (i % 7 + 2)) % 150),
            50 + ((seed >> (i % 5 + 4)) % 150),
        )
        draw.rectangle([x, h // 2 - bh, x + bw, h // 2], fill=color)
        for wy in range(h // 2 - bh + 8, h // 2 - 8, 18):
            for wx in range(x + 6, x + bw - 8, 16):
                draw.rectangle([wx, wy, wx + 8, wy + 10], fill=(230, 220, 160) if (wx + wy + i) % 3 else (40, 45, 60))
        x += bw + 4
        i += 1

    draw.text((10, h - 22), f"DEMO {pano.lat:.5f},{pano.lng:.5f} h={heading}", fill=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# API réelle
# --------------------------------------------------------------------------- #

def probe(lat: float, lng: float, radius_m: int = 60, session: Optional[requests.Session] = None) -> Optional[Pano]:
    """Retourne le panorama le plus proche, ou None s'il n'y a pas de couverture.

    L'endpoint metadata est gratuit chez Google : on l'utilise pour ne
    télécharger (et donc ne facturer) que des images réellement existantes.
    """
    if config.DEMO_MODE:
        return _demo_probe(lat, lng)

    sess = session or _session()
    params = {
        "location": f"{lat},{lng}",
        "radius": radius_m,
        "source": "outdoor",
        "key": config.GOOGLE_MAPS_API_KEY,
    }
    resp = sess.get(METADATA_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        raise StreetViewError(f"Street View metadata: {status} — {data.get('error_message', '')}".strip())
    return Pano(
        pano_id=data["pano_id"],
        lat=float(data["location"]["lat"]),
        lng=float(data["location"]["lng"]),
        date=data.get("date"),
    )


def fetch_image(pano: Pano, heading: int, session: Optional[requests.Session] = None) -> bytes:
    """Télécharge une vue du panorama pour un cap donné."""
    size = tuple(int(v) for v in config.STREETVIEW_SIZE.lower().split("x"))
    if config.DEMO_MODE:
        return _demo_image(pano, heading, size)  # type: ignore[arg-type]

    sess = session or _session()
    params = {
        "size": config.STREETVIEW_SIZE,
        "pano": pano.pano_id,
        "heading": heading,
        "pitch": 0,
        "fov": config.STREETVIEW_FOV,
        "return_error_code": "true",
        "key": config.GOOGLE_MAPS_API_KEY,
    }
    resp = sess.get(IMAGE_URL, params=params, timeout=30)
    if resp.status_code != 200:
        raise StreetViewError(f"Street View image HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def headings_for(count: int) -> list[int]:
    """Répartit `count` caps uniformément sur 360°."""
    count = max(1, min(int(count), 12))
    return [int(round(i * 360.0 / count)) % 360 for i in range(count)]


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance haversine en mètres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
