"""Source synthétique : aucun réseau, aucune clé.

Sert à valider le pipeline complet (indexation, recherche, carte) hors ligne.
Les positions retournées ne correspondent à rien de réel.
"""
from __future__ import annotations

import hashlib
import io
from typing import List, Optional

import requests
from PIL import Image, ImageDraw

from .. import config, sampling
from ..geo import headings_for
from ..geocode import City
from .base import CollectContext, CollectOptions, Provider, ProviderError, ViewRef


class DemoProvider(Provider):
    name = "demo"
    label = "Démo (images synthétiques)"
    requires_key = False
    attribution = "Images synthétiques — aucune valeur géographique"

    def discover(self, city: City, opts: CollectOptions, ctx: CollectContext) -> List[ViewRef]:
        points = sampling.grid_points(
            city, spacing_m=opts.spacing_m, radius_km=opts.radius_km, max_points=config.MAX_PROBES
        )
        views: List[ViewRef] = []
        places = 0

        for lat, lng in points:
            ctx.check()
            pano = _fake_pano(lat, lng)
            if pano is None:
                continue
            pano_id, plat, plng = pano
            for heading in headings_for(opts.headings):
                views.append(
                    ViewRef(
                        view_id=f"{pano_id}_{heading:03d}",
                        place_id=pano_id,
                        lat=plat,
                        lng=plng,
                        heading=heading,
                        ref=pano_id,
                    )
                )
            places += 1
            ctx.report(places, f"Démo : {places}/{opts.max_panos} lieux")
            if places >= opts.max_panos:
                break

        if not views:
            raise ProviderError("Zone trop petite pour générer un index de démonstration.")
        return views

    def fetch(self, view: ViewRef, session: requests.Session) -> bytes:
        size = tuple(int(v) for v in config.STREETVIEW_SIZE.lower().split("x"))
        return _render(view, size)


def _fake_pano(lat: float, lng: float) -> Optional[tuple]:
    """Simule la couverture : ~65 % des points sondés ont une « image »."""
    h = int(hashlib.sha1(f"cov{lat:.5f},{lng:.5f}".encode()).hexdigest(), 16)
    if h % 100 >= 65:
        return None
    slat = round(lat + ((h >> 8) % 21 - 10) * 1e-5, 6)
    slng = round(lng + ((h >> 16) % 21 - 10) * 1e-5, 6)
    return hashlib.sha1(f"{slat:.5f},{slng:.5f}".encode()).hexdigest()[:16], slat, slng


def _render(view: ViewRef, size: tuple) -> bytes:
    """Rue factice, déterministe pour un couple (lieu, cap) donné."""
    seed = int(hashlib.sha1(view.view_id.encode()).hexdigest(), 16)
    w, h = size
    img = Image.new("RGB", (w, h), (18, 22, 30))
    draw = ImageDraw.Draw(img)

    sky = ((seed >> 3) % 60 + 120, (seed >> 9) % 60 + 150, 210)
    for y in range(h // 2):
        k = y / max(h // 2, 1)
        draw.line(
            [(0, y), (w, y)],
            fill=(int(sky[0] * (1 - 0.30 * k)), int(sky[1] * (1 - 0.25 * k)), int(sky[2] * (1 - 0.20 * k))),
        )
    draw.rectangle([0, h // 2, w, h], fill=(70 + seed % 25, 68 + (seed >> 5) % 25, 66))

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
                draw.rectangle(
                    [wx, wy, wx + 8, wy + 10],
                    fill=(230, 220, 160) if (wx + wy + i) % 3 else (40, 45, 60),
                )
        x += bw + 4
        i += 1

    draw.text((10, h - 22), f"DEMO {view.lat:.5f},{view.lng:.5f} h={view.heading}", fill=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
