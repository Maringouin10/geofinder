"""Petits utilitaires géographiques partagés par les providers."""
from __future__ import annotations

import math

EARTH_M_PER_DEG = 111_320.0


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


def lng_scale(lat: float) -> float:
    """Mètres par degré de longitude à cette latitude (borné aux pôles)."""
    return EARTH_M_PER_DEG * max(math.cos(math.radians(lat)), 0.05)


def place_key(lat: float, lng: float, cell_m: float = 15.0) -> str:
    """Identifiant de « lieu » : les photos d'un même bout de trottoir tombent
    dans la même cellule.

    Les sources participatives (Mapillary, KartaView) livrent des séquences
    continues — des dizaines de clichés quasi identiques à quelques mètres
    d'intervalle. Sans regroupement, l'index serait saturé de doublons et
    `max_panos` ne voudrait plus rien dire.
    """
    dlat = cell_m / EARTH_M_PER_DEG
    dlng = cell_m / lng_scale(lat)
    return f"{int(round(lat / dlat))}_{int(round(lng / dlng))}"
