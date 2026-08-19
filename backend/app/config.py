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

# --- Sources d'imagerie -------------------------------------------------- #
# « auto » choisit la première source utilisable : Mapillary, puis KartaView
# (aucune clé requise), puis Google, puis la démo hors ligne.
PROVIDER = (os.getenv("GEOFINDER_PROVIDER", "auto") or "auto").strip().lower()
MAPILLARY_TOKEN = (os.getenv("MAPILLARY_ACCESS_TOKEN", "") or "").strip()
GOOGLE_MAPS_API_KEY = (os.getenv("GOOGLE_MAPS_API_KEY", "") or "").strip()

# --- Modèle de reconnaissance d'image ------------------------------------ #
# ViT-B/16 découpe l'image en patches deux fois plus fins que ViT-B/32 : il
# distingue bien mieux deux façades qui se ressemblent de loin. Le surcoût est
# supportable — l'encodage reste une poignée de minutes pour un millier de vues.
CLIP_MODEL = os.getenv("GEOFINDER_CLIP_MODEL", "ViT-B-16")
CLIP_PRETRAINED = os.getenv("GEOFINDER_CLIP_PRETRAINED", "laion2b_s34b_b88k")


def model_id() -> str:
    return f"{CLIP_MODEL}/{CLIP_PRETRAINED}"

# --- Collecte ------------------------------------------------------------ #
STREETVIEW_SIZE = os.getenv("GEOFINDER_SV_SIZE", "512x512")
STREETVIEW_FOV = _int("GEOFINDER_SV_FOV", 90)
DEFAULT_MAX_PANOS = _int("GEOFINDER_MAX_PANOS", 250)
DEFAULT_SPACING_M = _int("GEOFINDER_SPACING_M", 120)
MAX_PROBES = _int("GEOFINDER_MAX_PROBES", 6000)
FETCH_WORKERS = _int("GEOFINDER_WORKERS", 8)
# Connexion et lecture ont des timeouts séparés : un hôte injoignable doit
# échouer en quelques secondes, alors qu'une requête acceptée mais coûteuse
# (Mapillary sur une grande emprise) mérite qu'on l'attende.
CONNECT_TIMEOUT = _int("GEOFINDER_CONNECT_TIMEOUT", 8)
DISCOVERY_TIMEOUT = _int("GEOFINDER_DISCOVERY_TIMEOUT", 45)
DOWNLOAD_TIMEOUT = _int("GEOFINDER_DOWNLOAD_TIMEOUT", 30)
HTTP_RETRIES = _int("GEOFINDER_HTTP_RETRIES", 2)


def timeouts(read: int) -> tuple[int, int]:
    """Couple (connexion, lecture) attendu par requests."""
    return CONNECT_TIMEOUT, read
DISCOVERY_BUDGET_S = _int("GEOFINDER_DISCOVERY_BUDGET", 300)
LOG_LEVEL = (os.getenv("GEOFINDER_LOG_LEVEL", "INFO") or "INFO").upper()

# --- Recherche ----------------------------------------------------------- #
RERANK_CANDIDATES = _int("GEOFINDER_RERANK", 48)
CLIP_WEIGHT = float(os.getenv("GEOFINDER_CLIP_WEIGHT", "0.6"))
GEOMETRY_WEIGHT = float(os.getenv("GEOFINDER_GEOMETRY_WEIGHT", "0.4"))

# Échelle ABSOLUE de la similarité cosinus. Une normalisation relative au lot
# de candidats donnerait toujours 1,0 au premier, même quand rien ne
# correspond : c'est ainsi qu'on désigne un lieu au hasard avec assurance.
SIM_FLOOR = float(os.getenv("GEOFINDER_SIM_FLOOR", "0.55"))
SIM_CEIL = float(os.getenv("GEOFINDER_SIM_CEIL", "0.90"))

# Nombre d'inliers géométriques valant une certitude.
INLIER_TARGET = _int("GEOFINDER_INLIER_TARGET", 25)

# Vote de voisinage : un vrai lieu est confirmé par les vues alentour ;
# un faux positif est isolé.
CONSENSUS_RADIUS_M = _int("GEOFINDER_CONSENSUS_RADIUS_M", 150)
CONSENSUS_WEIGHT = float(os.getenv("GEOFINDER_CONSENSUS_WEIGHT", "0.35"))

# Deux résultats distants de moins de ça décrivent le même endroit : la marge
# doit se mesurer contre un concurrent réellement ailleurs.
RIVAL_SEPARATION_M = _int("GEOFINDER_RIVAL_SEPARATION_M", 250)

# En dessous, GeoFinder annonce qu'il ne sait pas plutôt que de trancher.
MIN_CONFIDENCE = float(os.getenv("GEOFINDER_MIN_CONFIDENCE", "0.35"))

# Recadrages du cliché requête : une photo de touriste est plus large qu'une
# vue de rue, zoomer rapproche les deux cadrages.
QUERY_CROPS = [float(x) for x in os.getenv("GEOFINDER_QUERY_CROPS", "1.0,0.7,0.5").split(",")]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "GeoFinder/1.1 (image-based geolocation)"


def ensure_dirs() -> None:
    CITIES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
