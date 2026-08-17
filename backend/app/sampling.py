"""Échantillonnage de points de sondage à l'intérieur d'une ville."""
from __future__ import annotations

import math
import random
from typing import List, Tuple

from .geocode import City

EARTH_M_PER_DEG = 111_320.0


def clamp_bbox(city: City, radius_km: float) -> Tuple[float, float, float, float]:
    """Restreint la bbox OSM à un carré de `radius_km` autour du centre.

    Les bounding boxes administratives peuvent couvrir des dizaines de km ;
    sans bornage, la grille exploserait en nombre de sondages.
    """
    dlat = (radius_km * 1000.0) / EARTH_M_PER_DEG
    dlng = (radius_km * 1000.0) / (EARTH_M_PER_DEG * max(math.cos(math.radians(city.lat)), 0.05))
    south = max(city.south, city.lat - dlat)
    north = min(city.north, city.lat + dlat)
    west = max(city.west, city.lng - dlng)
    east = min(city.east, city.lng + dlng)
    # Garde-fou : si l'intersection est dégénérée, on reprend le carré centré.
    if north <= south or east <= west:
        south, north = city.lat - dlat, city.lat + dlat
        west, east = city.lng - dlng, city.lng + dlng
    return south, north, west, east


def grid_points(
    city: City,
    spacing_m: int,
    radius_km: float,
    max_points: int,
    seed: int = 1337,
) -> List[Tuple[float, float]]:
    """Grille régulière sur la bbox, élargie tant que le nombre de points dépasse
    `max_points`, puis mélangée pour obtenir une couverture homogène si l'on
    s'arrête avant la fin."""
    south, north, west, east = clamp_bbox(city, radius_km)
    spacing = float(max(spacing_m, 10))

    for _ in range(24):
        lat_step = spacing / EARTH_M_PER_DEG
        lng_step = spacing / (EARTH_M_PER_DEG * max(math.cos(math.radians(city.lat)), 0.05))
        n_lat = max(int((north - south) / lat_step) + 1, 1)
        n_lng = max(int((east - west) / lng_step) + 1, 1)
        if n_lat * n_lng <= max_points:
            break
        spacing *= 1.35
    else:  # pragma: no cover - sécurité
        n_lat = n_lng = int(math.sqrt(max_points)) or 1
        lat_step = (north - south) / n_lat
        lng_step = (east - west) / n_lng

    points = [
        (south + i * lat_step, west + j * lng_step)
        for i in range(n_lat)
        for j in range(n_lng)
    ]
    random.Random(seed).shuffle(points)
    return points[:max_points]
