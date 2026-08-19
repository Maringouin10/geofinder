"""Tests du classement : échelle absolue, vote de voisinage, aveu de doute.

Les embeddings sont fabriqués à la main pour poser des situations précises —
un faux positif isolé, une vraie zone corroborée, une requête qui ne
correspond à rien — impossibles à provoquer de façon fiable avec de vraies
photos.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import config, matcher, store  # noqa: E402


def unit(vec) -> np.ndarray:
    v = np.array(vec, dtype=np.float32)
    return v / np.linalg.norm(v)


def blend(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Vecteur unitaire à similarité contrôlée avec `a`."""
    return unit((1 - t) * a + t * b)


@pytest.fixture()
def fake_index(tmp_path, monkeypatch):
    """Construit un index en mémoire dont on maîtrise chaque similarité."""
    monkeypatch.setattr(config, "CITIES_DIR", tmp_path / "cities")

    def build(places, query_vec):
        """places : [(pano_id, lat, lng, similarité voulue)]"""
        views, vectors = [], []
        for i, (pano_id, lat, lng, sim) in enumerate(places):
            views.append(store.View(idx=i, pano_id=pano_id, lat=lat, lng=lng,
                                    heading=0, file=f"{pano_id}.jpg"))
            # Vecteur dont le cosinus avec la requête vaut exactement `sim`.
            orth = unit(np.array([-query_vec[1], query_vec[0]] + [0.0] * (len(query_vec) - 2)))
            vectors.append(unit(sim * query_vec + np.sqrt(max(1 - sim ** 2, 0)) * orth))

        index = store.CityIndex(
            slug="testville", name="Testville", display_name="Testville",
            lat=places[0][1], lng=places[0][2], provider="mapillary",
            created_at="now", views=views, model=config.model_id(),
        )
        index.embeddings = np.stack(vectors).astype(np.float32)
        monkeypatch.setattr(store, "load", lambda slug, with_embeddings=True: index)
        monkeypatch.setattr(store, "list_cities", lambda: [index])
        # Pas de vignettes sur disque : la vérification géométrique est neutralisée.
        monkeypatch.setattr(matcher, "_to_cv", lambda img, max_side=720: np.zeros((8, 8), np.uint8))
        return index

    return build


@pytest.fixture()
def query(monkeypatch):
    q = unit([1.0, 0.0, 0.0, 0.0])
    from backend.app import embedder

    monkeypatch.setattr(embedder, "embed_images",
                        lambda images, batch_size=16: np.stack([q] * len(list(images))))
    return q


IMG = Image.new("RGB", (64, 64), (30, 40, 50))


def test_absolute_scale_does_not_crown_a_weak_best(fake_index, query):
    """Le bug d'origine : une normalisation relative au lot donnait toujours 1,0
    au premier candidat, même quand rien ne correspondait vraiment."""
    fake_index([("a", 48.85, 2.29, 0.42), ("b", 48.90, 2.40, 0.40)], query)

    ranked, diag = matcher.search(IMG, ["testville"], top_k=5, rerank=False)

    assert ranked, "des candidats doivent quand même être proposés"
    # Similarités sous SIM_FLOOR : le score doit rester au plancher…
    assert ranked[0].score < 0.2
    # …et le système doit reconnaître qu'il ne sait pas.
    assert diag["uncertain"] is True
    assert diag["confidence"] < config.MIN_CONFIDENCE


def test_strong_and_isolated_still_scores_high(fake_index, query):
    fake_index([("a", 48.85, 2.29, 0.93), ("b", 48.95, 2.45, 0.50)], query)
    ranked, diag = matcher.search(IMG, ["testville"], top_k=5, rerank=False)
    assert ranked[0].pano_id == "a"
    assert diag["confidence"] > config.MIN_CONFIDENCE
    assert diag["uncertain"] is False


def test_neighbourhood_vote_beats_an_isolated_false_positive(fake_index, query):
    """Le cas « Tour Eiffel → Montparnasse » : un pic isolé un peu plus fort
    doit céder devant une zone que plusieurs vues corroborent."""
    fake_index([
        ("montparnasse", 48.8420, 2.3220, 0.80),   # faux positif isolé, le + fort
        ("eiffel_1",     48.8584, 2.2945, 0.78),   # vraie zone, trois vues proches
        ("eiffel_2",     48.8586, 2.2949, 0.77),
        ("eiffel_3",     48.8581, 2.2941, 0.76),
    ], query)

    ranked, _ = matcher.search(IMG, ["testville"], top_k=5, rerank=False)

    assert ranked[0].pano_id.startswith("eiffel"), \
        f"le lieu corroboré doit l'emporter, obtenu : {ranked[0].pano_id}"
    assert ranked[0].support >= 2, "le gagnant doit être soutenu par ses voisins"


def test_confidence_ignores_rivals_that_are_the_same_place(fake_index, query):
    """Les vues voisines du gagnant ne sont pas des concurrentes : les compter
    comme telles écrasait la confiance de bonnes réponses."""
    fake_index([
        ("x_1", 48.8584, 2.2945, 0.88),
        ("x_2", 48.8585, 2.2946, 0.87),   # à quelques mètres : même endroit
        ("x_3", 48.8586, 2.2947, 0.86),
    ], query)

    _, diag = matcher.search(IMG, ["testville"], top_k=5, rerank=False)
    assert diag["confidence"] > 0.5, "des vues du même lieu ne doivent pas faire douter"


def test_index_from_another_model_is_skipped(fake_index, query, monkeypatch):
    """Comparer des vecteurs de deux modèles produirait un classement
    arbitraire d'apparence sérieuse : mieux vaut refuser."""
    index = fake_index([("a", 48.85, 2.29, 0.95)], query)
    index.model = "ViT-B-32/un-autre-modele"

    ranked, diag = matcher.search(IMG, ["testville"], top_k=5, rerank=False)
    assert ranked == []
    assert diag["skipped_indexes"] and "un-autre-modele" in diag["skipped_indexes"][0]


def test_query_crops_zoom_towards_the_centre():
    crops = matcher.query_crops(Image.new("RGB", (400, 300)))
    assert len(crops) == len(config.QUERY_CROPS)
    assert crops[0].size == (400, 300)                  # l'original d'abord
    assert all(c.size[0] < 400 for c in crops[1:])      # puis des zooms
    # Recadrage centré : la marge doit être identique de chaque côté.
    assert crops[1].size == (280, 210)


def test_normalize_similarity_is_absolute():
    assert matcher.normalize_similarity(config.SIM_FLOOR) == 0.0
    assert matcher.normalize_similarity(config.SIM_CEIL) == 1.0
    assert matcher.normalize_similarity(0.0) == 0.0     # borné, jamais négatif
    mid = matcher.normalize_similarity((config.SIM_FLOOR + config.SIM_CEIL) / 2)
    assert 0.45 < mid < 0.55
