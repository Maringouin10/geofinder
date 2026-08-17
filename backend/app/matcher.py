"""Recherche : embedding CLIP + revérification géométrique ORB/RANSAC."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import cv2
import numpy as np
from PIL import Image

from . import config, embedder, store


@dataclass
class Match:
    pano_id: str
    lat: float
    lng: float
    heading: int
    file: str
    slug: str
    city: str
    similarity: float          # cosinus CLIP brut
    inliers: int               # points d'intérêt vérifiés géométriquement
    score: float               # score combiné final

    def as_dict(self) -> dict:
        return {
            "pano_id": self.pano_id,
            "lat": self.lat,
            "lng": self.lng,
            "heading": self.heading,
            "thumb": f"/api/cities/{self.slug}/images/{self.file}",
            "city": self.city,
            "slug": self.slug,
            "similarity": round(self.similarity, 4),
            "inliers": self.inliers,
            "score": round(self.score, 4),
            "streetview_url": (
                "https://www.google.com/maps/@?api=1&map_action=pano"
                f"&viewpoint={self.lat},{self.lng}&heading={self.heading}"
            ),
        }


def _to_cv(img: Image.Image, max_side: int = 640) -> np.ndarray:
    """PIL → niveaux de gris OpenCV, redimensionné pour borner le coût d'ORB."""
    arr = np.array(img.convert("L"))
    h, w = arr.shape
    scale = max_side / float(max(h, w))
    if scale < 1.0:
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return arr


def _orb_inliers(query_gray: np.ndarray, cand_gray: np.ndarray) -> int:
    """Nombre de correspondances locales validées par une homographie RANSAC.

    C'est ce qui distingue « deux rues qui se ressemblent » de « la même rue » :
    CLIP capture la scène globale, ORB confirme la géométrie réelle.
    """
    orb = cv2.ORB_create(nfeatures=1200)
    kp_a, des_a = orb.detectAndCompute(query_gray, None)
    kp_b, des_b = orb.detectAndCompute(cand_gray, None)
    if des_a is None or des_b is None or len(kp_a) < 8 or len(kp_b) < 8:
        return 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    good = []
    for pair in bf.knnMatch(des_a, des_b, k=2):
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            good.append(pair[0])
    if len(good) < 8:
        return 0

    src = np.float32([kp_a[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_b[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if mask is None:
        return 0
    return int(mask.sum())


def search(
    image: Image.Image,
    slugs: List[str],
    top_k: int = 5,
    rerank: bool = True,
) -> tuple[List[Match], dict]:
    """Cherche l'image parmi les index demandés. Retourne (matches, diagnostic)."""
    query_vec = embedder.embed_image(image)

    scored: List[tuple[float, store.View, store.CityIndex]] = []
    total_views = 0
    for slug in slugs:
        index = store.load(slug, with_embeddings=True)
        if index is None or index.embeddings is None or len(index.views) == 0:
            continue
        sims = index.embeddings @ query_vec  # vecteurs L2-normalisés → cosinus
        total_views += len(index.views)
        take = min(config.RERANK_CANDIDATES, len(sims))
        for i in np.argpartition(-sims, take - 1)[:take]:
            scored.append((float(sims[i]), index.views[i], index))

    if not scored:
        return [], {"views_searched": 0, "candidates": 0}

    scored.sort(key=lambda t: t[0], reverse=True)
    candidates = scored[: config.RERANK_CANDIDATES]

    query_gray = _to_cv(image) if rerank else None
    sims = np.array([c[0] for c in candidates], dtype=np.float32)
    lo, hi = float(sims.min()), float(sims.max())
    span = max(hi - lo, 1e-6)

    matches: List[Match] = []
    for (sim, view, index) in candidates:
        inliers = 0
        if rerank and query_gray is not None:
            path = store.images_dir(index.slug) / view.file
            if path.exists():
                with Image.open(path) as cand:
                    inliers = _orb_inliers(query_gray, _to_cv(cand))
        clip_n = (sim - lo) / span
        orb_n = min(inliers / 40.0, 1.0)
        score = config.CLIP_WEIGHT * clip_n + (config.ORB_WEIGHT * orb_n if rerank else 0.0)
        matches.append(
            Match(
                pano_id=view.pano_id,
                lat=view.lat,
                lng=view.lng,
                heading=view.heading,
                file=view.file,
                slug=index.slug,
                city=index.display_name,
                similarity=sim,
                inliers=inliers,
                score=score,
            )
        )

    best_per_pano: Dict[str, Match] = {}
    for m in matches:
        current = best_per_pano.get(m.pano_id)
        if current is None or m.score > current.score:
            best_per_pano[m.pano_id] = m

    ranked = sorted(best_per_pano.values(), key=lambda m: m.score, reverse=True)[:top_k]
    diagnostic = {
        "views_searched": total_views,
        "candidates": len(candidates),
        "reranked": rerank,
        "confidence": _confidence(ranked),
    }
    return ranked, diagnostic


def _confidence(ranked: List[Match]) -> float:
    """Confiance heuristique : similarité absolue du premier, corrigée par sa
    marge sur les suivants et par la vérification géométrique."""
    if not ranked:
        return 0.0
    top = ranked[0]
    base = max(0.0, min((top.similarity - 0.55) / 0.35, 1.0))
    if len(ranked) > 1:
        margin = top.similarity - float(np.mean([m.similarity for m in ranked[1:]]))
        base *= max(0.35, min(1.0, 0.5 + margin * 8))
    if top.inliers >= 15:
        base = min(1.0, base + 0.25)
    elif top.inliers >= 8:
        base = min(1.0, base + 0.10)
    return round(base, 3)


def resolve_slugs(requested: Optional[str]) -> List[str]:
    """`None` ou "all" → toutes les villes indexées."""
    available = [c.slug for c in store.list_cities()]
    if not requested or requested == "all":
        return available
    return [s for s in [requested] if s in available]
