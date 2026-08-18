"""Persistance des index de villes (métadonnées + embeddings + vignettes)."""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from . import config


@dataclass
class View:
    """Une vue = un panorama Street View regardé dans une direction donnée."""
    idx: int
    pano_id: str
    lat: float
    lng: float
    heading: int
    file: str


@dataclass
class CityIndex:
    slug: str
    name: str
    display_name: str
    lat: float
    lng: float
    provider: str
    created_at: str
    views: List[View]
    embeddings: Optional[np.ndarray] = None

    @property
    def pano_count(self) -> int:
        return len({v.pano_id for v in self.views})

    @property
    def demo(self) -> bool:
        return self.provider == "demo"


def city_dir(slug: str) -> Path:
    return config.CITIES_DIR / slug


def images_dir(slug: str) -> Path:
    return city_dir(slug) / "images"


def _meta_path(slug: str) -> Path:
    return city_dir(slug) / "index.json"


def _emb_path(slug: str) -> Path:
    return city_dir(slug) / "embeddings.npy"


def save(index: CityIndex, embeddings: np.ndarray) -> None:
    d = city_dir(index.slug)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "slug": index.slug,
        "name": index.name,
        "display_name": index.display_name,
        "lat": index.lat,
        "lng": index.lng,
        "provider": index.provider,
        "created_at": index.created_at,
        "views": [asdict(v) for v in index.views],
    }
    _meta_path(index.slug).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    np.save(_emb_path(index.slug), embeddings.astype(np.float32))


_cache: Dict[str, CityIndex] = {}


def load(slug: str, with_embeddings: bool = True) -> Optional[CityIndex]:
    if not _meta_path(slug).exists():
        _cache.pop(slug, None)
        return None

    cached = _cache.get(slug)
    if cached is not None and (cached.embeddings is not None or not with_embeddings):
        return cached

    meta = json.loads(_meta_path(slug).read_text(encoding="utf-8"))
    index = CityIndex(
        slug=meta["slug"],
        name=meta["name"],
        display_name=meta["display_name"],
        lat=meta["lat"],
        lng=meta["lng"],
        provider=meta.get("provider", "demo"),
        created_at=meta["created_at"],
        views=[View(**v) for v in meta["views"]],
    )
    if with_embeddings and _emb_path(slug).exists():
        index.embeddings = np.load(_emb_path(slug))
    _cache[slug] = index
    return index


def list_cities() -> List[CityIndex]:
    if not config.CITIES_DIR.exists():
        return []
    out = []
    for path in sorted(config.CITIES_DIR.iterdir()):
        if path.is_dir():
            idx = load(path.name, with_embeddings=False)
            if idx is not None:
                out.append(idx)
    return out


def delete(slug: str) -> bool:
    d = city_dir(slug)
    _cache.pop(slug, None)
    if d.exists():
        shutil.rmtree(d)
        return True
    return False
