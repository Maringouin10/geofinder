"""Fixtures partagées : encodeur substitué, géocodage hors ligne, data dir jetable."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def tiny_descriptor(img: Image.Image) -> np.ndarray:
    """Descripteur de substitution à CLIP : vignette 16x16 en niveaux de gris,
    L2-normalisée. Suffisamment « visuel » pour que la logique de recherche soit
    réellement exercée, sans dépendre du téléchargement des poids du modèle.
    """
    arr = np.asarray(img.convert("L").resize((16, 16)), dtype=np.float32).ravel()
    arr -= arr.mean()
    norm = np.linalg.norm(arr)
    return arr / norm if norm > 1e-6 else np.zeros_like(arr)


@pytest.fixture()
def geofinder(tmp_path, monkeypatch):
    """Application prête à l'emploi, isolée dans un répertoire de données neuf."""
    from backend.app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CITIES_DIR", tmp_path / "cities")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PROVIDER", "demo")
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(config, "MAPILLARY_TOKEN", "")
    config.ensure_dirs()

    from backend.app import embedder, geocode, store

    monkeypatch.setattr(
        embedder, "embed_images",
        lambda images, batch_size=16: np.stack([tiny_descriptor(i) for i in images]).astype(np.float32),
    )
    monkeypatch.setattr(embedder, "embed_image", lambda image: tiny_descriptor(image))
    monkeypatch.setattr(embedder, "warmup", lambda: None)
    monkeypatch.setattr(embedder, "embedding_dim", lambda: 256)

    monkeypatch.setattr(
        geocode, "geocode_city",
        lambda name: geocode.City(
            query=name,
            display_name="Testville, Testland",
            lat=48.8566, lng=2.3522,
            south=48.85, north=48.87, west=2.34, east=2.37,
        ),
    )

    store._cache.clear()
    yield
    store._cache.clear()
