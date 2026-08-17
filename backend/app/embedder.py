"""Extraction d'empreintes visuelles (CLIP) — le cœur de la reconnaissance."""
from __future__ import annotations

import logging
import threading
from typing import Iterable, List

import numpy as np
from PIL import Image

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None
_preprocess = None
_device = "cpu"


def _load():
    global _model, _preprocess, _device
    if _model is not None:
        return _model, _preprocess
    with _lock:
        if _model is None:
            import torch  # import paresseux : accélère le démarrage de l'API
            import open_clip

            _device = "cuda" if torch.cuda.is_available() else "cpu"
            model, _, preprocess = open_clip.create_model_and_transforms(
                config.CLIP_MODEL, pretrained=config.CLIP_PRETRAINED
            )
            model.eval().to(_device)
            _model, _preprocess = model, preprocess
    return _model, _preprocess


def warmup() -> None:
    """Précharge le modèle au démarrage, en tâche de fond.

    Un échec ici (poids absents du cache et réseau coupé) ne doit pas empêcher
    l'API de répondre : il sera resignalé, lisiblement, à la première requête.
    """
    try:
        _load()
        log.info("Modèle %s/%s chargé.", config.CLIP_MODEL, config.CLIP_PRETRAINED)
    except Exception as exc:  # noqa: BLE001
        log.warning("Préchargement du modèle impossible : %s", exc)


def embed_images(images: Iterable[Image.Image], batch_size: int = 16) -> np.ndarray:
    """Retourne une matrice (N, D) d'embeddings L2-normalisés."""
    import torch

    model, preprocess = _load()
    batch: List = []
    out: List[np.ndarray] = []

    def flush():
        if not batch:
            return
        tensor = torch.stack(batch).to(_device)
        with torch.no_grad():
            feats = model.encode_image(tensor)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        out.append(feats.cpu().numpy().astype(np.float32))
        batch.clear()

    for img in images:
        batch.append(preprocess(img.convert("RGB")))
        if len(batch) >= batch_size:
            flush()
    flush()

    if not out:
        return np.zeros((0, embedding_dim()), dtype=np.float32)
    return np.concatenate(out, axis=0)


def embed_image(image: Image.Image) -> np.ndarray:
    """Empreinte d'une seule image, vecteur (D,)."""
    return embed_images([image])[0]


def embedding_dim() -> int:
    model, _ = _load()
    return int(model.visual.output_dim)
