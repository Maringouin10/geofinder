"""Test d'intégration : indexation d'une ville puis recherche d'une photo.

Tourne entièrement hors ligne (mode démo + encodeur substitué, cf. conftest),
mais exerce le vrai code : job d'indexation, écriture disque, index numpy,
recherche CLIP-like, revérification ORB et routes HTTP.
"""
from __future__ import annotations

import io
import time

import pytest
from PIL import Image


def _wait(job_id: str, client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    pytest.fail(f"Job {job_id} non terminé après {timeout}s")


@pytest.fixture()
def client(geofinder):
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as c:
        yield c


def test_health_reports_demo_mode(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["demo_mode"] is True


def test_find_without_index_is_rejected(client):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="JPEG")
    resp = client.post("/api/find", files={"file": ("q.jpg", buf.getvalue(), "image/jpeg")})
    assert resp.status_code == 409


def test_index_then_find_locates_the_source_panorama(client):
    job = client.post("/api/cities", json={
        "city": "Testville", "max_panos": 12, "headings": 2,
        "spacing_m": 120, "radius_km": 1.5,
    }).json()

    job = _wait(job["id"], client)
    assert job["state"] == "done", job.get("error")
    assert job["panos"] > 0 and job["views"] == job["panos"] * 2

    cities = client.get("/api/cities").json()["cities"]
    assert len(cities) == 1
    city = cities[0]
    assert city["slug"] == "testville"
    assert city["demo"] is True

    # On rejoue une vue indexée comme si c'était la photo de l'utilisateur :
    # le système doit la relocaliser exactement.
    from backend.app import store

    index = store.load(city["slug"])
    target = index.views[len(index.views) // 2]
    source = (store.images_dir(city["slug"]) / target.file).read_bytes()

    resp = client.post(
        "/api/find",
        files={"file": ("photo.jpg", source, "image/jpeg")},
        data={"city": city["slug"], "top_k": "5", "rerank": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["best"]["pano_id"] == target.pano_id
    assert body["best"]["lat"] == pytest.approx(target.lat, abs=1e-6)
    assert body["best"]["lng"] == pytest.approx(target.lng, abs=1e-6)
    assert body["best"]["score"] >= body["matches"][-1]["score"]
    assert body["diagnostic"]["views_searched"] == len(index.views)
    # Image identique → l'homographie RANSAC doit retrouver beaucoup d'inliers.
    assert body["best"]["inliers"] > 20


def test_find_survives_recompression_and_resize(client):
    job = client.post("/api/cities", json={
        "city": "Testville", "max_panos": 10, "headings": 2,
        "spacing_m": 120, "radius_km": 1.5,
    }).json()
    assert _wait(job["id"], client)["state"] == "done"

    from backend.app import store

    index = store.load("testville")
    target = index.views[0]
    with Image.open(store.images_dir("testville") / target.file) as img:
        degraded = img.resize((320, 320)).crop((10, 10, 310, 300))
    buf = io.BytesIO()
    degraded.save(buf, format="JPEG", quality=60)

    body = client.post(
        "/api/find",
        files={"file": ("photo.jpg", buf.getvalue(), "image/jpeg")},
        data={"city": "testville"},
    ).json()
    assert body["best"]["pano_id"] == target.pano_id


def test_thumbnails_are_served_and_path_traversal_blocked(client):
    job = client.post("/api/cities", json={
        "city": "Testville", "max_panos": 10, "headings": 1,
        "spacing_m": 120, "radius_km": 1.5,
    }).json()
    assert _wait(job["id"], client)["state"] == "done"

    from backend.app import store

    view = store.load("testville").views[0]
    ok = client.get(f"/api/cities/testville/images/{view.file}")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"

    assert client.get("/api/cities/testville/images/nope.jpg").status_code == 404
    assert client.get("/api/cities/..%2f..%2fetc/images/passwd").status_code in (400, 404)


def test_delete_city_removes_index(client):
    job = client.post("/api/cities", json={
        "city": "Testville", "max_panos": 10, "headings": 1,
        "spacing_m": 120, "radius_km": 1.5,
    }).json()
    assert _wait(job["id"], client)["state"] == "done"

    assert client.delete("/api/cities/testville").status_code == 200
    assert client.get("/api/cities").json()["cities"] == []
    assert client.delete("/api/cities/testville").status_code == 404


def test_rejects_non_image_upload(client):
    resp = client.post("/api/find", files={"file": ("x.txt", b"not an image", "text/plain")})
    assert resp.status_code in (400, 409)
