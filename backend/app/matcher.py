"""Recherche : récupération CLIP, vérification géométrique, vote de voisinage.

Trois garde-fous distinguent « le meilleur du lot » de « le bon endroit » :

1. la similarité est jugée sur une **échelle absolue**, jamais relative au lot,
   sans quoi le premier candidat obtient toujours la note maximale même quand
   rien ne correspond ;
2. les correspondances locales sont validées par une **matrice fondamentale**,
   qui est le bon modèle pour une scène de rue avec de la profondeur ;
3. un **vote de voisinage** favorise les lieux confirmés par plusieurs vues
   alentour, car un faux positif est presque toujours isolé.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from . import config, embedder, store
from .geo import distance_m

log = logging.getLogger(__name__)


@dataclass
class Match:
    pano_id: str
    lat: float
    lng: float
    heading: int
    file: str
    slug: str
    city: str
    provider: str
    similarity: float          # cosinus CLIP brut, échelle absolue
    inliers: int               # points vérifiés géométriquement
    score: float               # score combiné, après vote de voisinage
    support: int = 0           # vues voisines qui corroborent ce lieu

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
            "support": self.support,
            "score": round(self.score, 4),
            "provider": self.provider,
            "source_url": source_url(self.provider, self.lat, self.lng, self.heading),
        }


def source_url(provider: str, lat: float, lng: float, heading: int) -> str:
    """Lien « voir sur place » vers la source d'origine de l'image."""
    if provider == "google":
        return (
            "https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat},{lng}&heading={heading}"
        )
    if provider == "mapillary":
        return f"https://www.mapillary.com/app/?lat={lat}&lng={lng}&z=17"
    if provider == "kartaview":
        return f"https://kartaview.org/map/@{lat},{lng},17z"
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=18/{lat}/{lng}"


# --------------------------------------------------------------- requête --

def query_crops(image: Image.Image) -> List[Image.Image]:
    """Le cliché requête, plus quelques recadrages centrés.

    Une photo prise au téléphone couvre un champ bien plus large qu'une vue de
    rue de la base ; recadrer rapproche les deux cadrages et rattrape une part
    des échecs dus au seul écart de focale.
    """
    crops: List[Image.Image] = []
    w, h = image.size
    for frac in config.QUERY_CROPS:
        if frac >= 0.999:
            crops.append(image)
            continue
        cw, ch = max(int(w * frac), 32), max(int(h * frac), 32)
        left, top = (w - cw) // 2, (h - ch) // 2
        crops.append(image.crop((left, top, left + cw, top + ch)))
    return crops or [image]


# ----------------------------------------------- vérification géométrique --

def _to_cv(img: Image.Image, max_side: int = 720) -> np.ndarray:
    arr = np.array(img.convert("L"))
    h, w = arr.shape
    scale = max_side / float(max(h, w))
    if scale < 1.0:
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return arr


def _detector():
    """SIFT si disponible, ORB sinon.

    SIFT encaisse bien mieux les écarts d'échelle, d'éclairage et de saison
    entre une photo de touriste et une vue de rue prise un autre jour ; son
    brevet a expiré, il est dans OpenCV standard.
    """
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=1500), cv2.NORM_L2
    return cv2.ORB_create(nfeatures=1500), cv2.NORM_HAMMING


def geometric_inliers(query_gray: np.ndarray, cand_gray: np.ndarray) -> int:
    """Correspondances survivant à une matrice fondamentale estimée par RANSAC.

    Une homographie suppose une scène plane ou une rotation pure — faux dans une
    rue, où les plans ont des profondeurs différentes. La matrice fondamentale
    n'exige, elle, qu'une géométrie épipolaire cohérente : c'est le bon modèle,
    et il rejette les appariements fortuits entre deux endroits distincts.
    """
    detector, norm = _detector()
    kp_a, des_a = detector.detectAndCompute(query_gray, None)
    kp_b, des_b = detector.detectAndCompute(cand_gray, None)
    if des_a is None or des_b is None or len(kp_a) < 8 or len(kp_b) < 8:
        return 0

    matcher = cv2.BFMatcher(norm)
    forward = _ratio_test(matcher, des_a, des_b)
    if len(forward) < 8:
        return 0
    # Vérification croisée : on ne garde que les paires qui se choisissent
    # mutuellement, ce qui élimine l'essentiel des appariements parasites.
    backward = {b: a for a, b in _ratio_test(matcher, des_b, des_a)}
    mutual = [(a, b) for a, b in forward if backward.get(b) == a]
    if len(mutual) < 8:
        return 0

    src = np.float32([kp_a[a].pt for a, _ in mutual]).reshape(-1, 1, 2)
    dst = np.float32([kp_b[b].pt for _, b in mutual]).reshape(-1, 1, 2)
    try:
        _, mask = cv2.findFundamentalMat(src, dst, cv2.FM_RANSAC, 3.0, 0.99)
    except cv2.error:
        return 0
    return int(mask.sum()) if mask is not None else 0


def _ratio_test(matcher, des_a, des_b, ratio: float = 0.75) -> List[Tuple[int, int]]:
    pairs: List[Tuple[int, int]] = []
    for candidates in matcher.knnMatch(des_a, des_b, k=2):
        if len(candidates) == 2 and candidates[0].distance < ratio * candidates[1].distance:
            pairs.append((candidates[0].queryIdx, candidates[0].trainIdx))
    return pairs


# ------------------------------------------------------------- recherche --

def normalize_similarity(sim: float) -> float:
    """Cosinus → [0, 1] sur une échelle **absolue**, identique d'une requête à
    l'autre. C'est ce qui permet de dire « rien ne correspond »."""
    span = max(config.SIM_CEIL - config.SIM_FLOOR, 1e-6)
    return float(np.clip((sim - config.SIM_FLOOR) / span, 0.0, 1.0))


def search(
    image: Image.Image,
    slugs: List[str],
    top_k: int = 5,
    rerank: bool = True,
) -> tuple[List[Match], dict]:
    """Cherche l'image parmi les index demandés. Retourne (matches, diagnostic)."""
    crops = query_crops(image)
    query_vecs = embedder.embed_images(crops)

    scored: List[tuple[float, store.View, store.CityIndex]] = []
    total_views = 0
    skipped: List[str] = []

    for slug in slugs:
        index = store.load(slug, with_embeddings=True)
        if index is None or index.embeddings is None or len(index.views) == 0:
            continue
        if index.model and index.model != config.model_id():
            # Deux modèles produisent des vecteurs incomparables : les mélanger
            # donnerait un classement arbitraire, avec l'apparence du sérieux.
            skipped.append(f"{index.display_name} (indexé avec {index.model})")
            continue

        # Meilleur recadrage pour chaque vue : la requête est jugée sous son
        # cadrage le plus favorable, pas sous un cadrage arbitraire.
        sims = (index.embeddings @ query_vecs.T).max(axis=1)
        total_views += len(index.views)
        take = min(config.RERANK_CANDIDATES, len(sims))
        for i in np.argpartition(-sims, take - 1)[:take]:
            scored.append((float(sims[i]), index.views[i], index))

    if not scored:
        return [], {
            "views_searched": 0,
            "candidates": 0,
            "confidence": 0.0,
            "uncertain": True,
            "skipped_indexes": skipped,
        }

    scored.sort(key=lambda t: t[0], reverse=True)
    candidates = scored[: config.RERANK_CANDIDATES]

    query_gray = _to_cv(image) if rerank else None
    matches: List[Match] = []
    for sim, view, index in candidates:
        inliers = 0
        if query_gray is not None:
            path = store.images_dir(index.slug) / view.file
            if path.exists():
                with Image.open(path) as cand:
                    inliers = geometric_inliers(query_gray, _to_cv(cand))

        visual = normalize_similarity(sim)
        geometric = min(inliers / float(config.INLIER_TARGET), 1.0)
        base = (
            config.CLIP_WEIGHT * visual + config.GEOMETRY_WEIGHT * geometric
            if rerank
            else visual
        )
        matches.append(
            Match(
                pano_id=view.pano_id, lat=view.lat, lng=view.lng, heading=view.heading,
                file=view.file, slug=index.slug, city=index.display_name,
                provider=index.provider, similarity=sim, inliers=inliers, score=base,
            )
        )

    best_per_place: Dict[str, Match] = {}
    for m in matches:
        current = best_per_place.get(m.pano_id)
        if current is None or m.score > current.score:
            best_per_place[m.pano_id] = m

    places = _apply_consensus(list(best_per_place.values()))
    ranked = sorted(places, key=lambda m: m.score, reverse=True)[:top_k]
    confidence = _confidence(ranked)

    return ranked, {
        "views_searched": total_views,
        "candidates": len(candidates),
        "reranked": rerank,
        "confidence": confidence,
        "uncertain": confidence < config.MIN_CONFIDENCE,
        "skipped_indexes": skipped,
    }


def _apply_consensus(places: List[Match]) -> List[Match]:
    """Bonifie les lieux corroborés par d'autres bonnes réponses proches.

    Si la photo vient bien de X, plusieurs vues autour de X répondent fort.
    Une confusion, elle, produit un pic isolé : c'est exactement ce qui fait
    remonter un lieu sans rapport quand la vraie zone n'est pas couverte.
    """
    if len(places) < 2:
        return places

    radius = float(config.CONSENSUS_RADIUS_M)
    for place in places:
        support = 0.0
        neighbours = 0
        for other in places:
            if other is place:
                continue
            d = distance_m(place.lat, place.lng, other.lat, other.lng)
            if d < radius:
                support += other.score * (1.0 - d / radius)
                neighbours += 1
        place.support = neighbours
        place.score += config.CONSENSUS_WEIGHT * min(support, 1.0)
    return places


def _confidence(ranked: List[Match]) -> float:
    """Confiance : niveau absolu du premier, marge sur un concurrent réellement
    ailleurs, et vérification géométrique."""
    if not ranked:
        return 0.0
    top = ranked[0]

    confidence = normalize_similarity(top.similarity)

    # Comparer au deuxième n'a de sens que s'il désigne un autre endroit :
    # les suivants sont le plus souvent d'autres vues du même lieu.
    rival = next(
        (m for m in ranked[1:]
         if distance_m(top.lat, top.lng, m.lat, m.lng) > config.RIVAL_SEPARATION_M),
        None,
    )
    if rival is not None:
        margin = top.score - rival.score
        confidence *= float(np.clip(0.45 + 4.0 * margin, 0.35, 1.15))

    if top.inliers >= config.INLIER_TARGET:
        confidence = confidence * 1.25 + 0.15
    elif top.inliers >= 8:
        confidence += 0.08
    elif top.inliers == 0:
        # Aucune correspondance géométrique : la ressemblance est peut-être
        # celle de deux avenues quelconques.
        confidence *= 0.7

    return round(float(np.clip(confidence, 0.0, 1.0)), 3)


def resolve_slugs(requested: Optional[str]) -> List[str]:
    """`None` ou "all" → toutes les villes indexées."""
    available = [c.slug for c in store.list_cities()]
    if not requested or requested == "all":
        return available
    return [s for s in [requested] if s in available]
