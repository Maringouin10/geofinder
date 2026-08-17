"""Configuration centrale de GeoFinder (lue depuis l'environnement)."""
from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


DATA_DIR = Path(os.getenv("GEOFINDER_DATA", "/data"))
CITIES_DIR = DATA_DIR / "cities"
UPLOADS_DIR = DATA_DIR / "uploads"

GOOGLE_MAPS_API_KEY = (os.getenv("GOOGLE_MAPS_API_KEY", "") or "").strip()
# Sans clé API on bascule sur un jeu d'images synthétiques : le pipeline complet
# reste testable, mais les résultats ne sont évidemment pas de vraies positions.
DEMO_MODE = not GOOGLE_MAPS_API_KEY

# Modèle de reconnaissance d'image
CLIP_MODEL = os.getenv("GEOFINDER_CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED = os.getenv("GEOFINDER_CLIP_PRETRAINED", "laion2b_s34b_b79k")

# Collecte Street View
STREETVIEW_SIZE = os.getenv("GEOFINDER_SV_SIZE", "512x512")
STREETVIEW_FOV = _int("GEOFINDER_SV_FOV", 90)
DEFAULT_HEADINGS = [0, 90, 180, 270]
DEFAULT_MAX_PANOS = _int("GEOFINDER_MAX_PANOS", 250)
DEFAULT_SPACING_M = _int("GEOFINDER_SPACING_M", 120)
MAX_PROBES = _int("GEOFINDER_MAX_PROBES", 6000)
FETCH_WORKERS = _int("GEOFINDER_WORKERS", 8)

# Recherche
RERANK_CANDIDATES = _int("GEOFINDER_RERANK", 40)
CLIP_WEIGHT = float(os.getenv("GEOFINDER_CLIP_WEIGHT", "0.75"))
ORB_WEIGHT = 1.0 - CLIP_WEIGHT

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "GeoFinder/1.0 (image-based geolocation)"


def ensure_dirs() -> None:
    CITIES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
