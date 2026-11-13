"""Service security/persistence tests; no downloaded model or network required."""
import io
import json
from pathlib import Path
import time
import threading

import numpy as np
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from portraitlab import engine
from portraitlab import server
from portraitlab.server import create_app, writable_path, validate_strokes


class FakeEngine:
    def __init__(self, models_dir):
        self.calls = 0

    def status(self):
        return {"ready": True, "models": [], "errors": []}

    def analyze(self, rgb):
        self.calls += 1
        h, w = rgb.shape[:2]
        return {"faces": [], "face_count": 0, "shape": [h, w], "source_shape": [h, w],
                "parts": {"background": np.zeros((h // 2, w // 2), np.float32)}, "face_mask": np.ones((h, w), np.float32),
                "skin_mask": np.ones((h, w), np.float32), "mask": np.ones((h, w), np.float32),
                "warnings": [], "models": {"test": "synthetic"}}

    def render(self, rgb, params, analysis, strokes=None):
        offset = round(float(params["exposure"]) * 10)
        return np.clip(rgb.astype(np.int16) + offset, 0, 255).astype(np.uint8), {"warnings": [], "test_engine": True}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTRAITLAB_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(engine, "Engine", FakeEngine)
    app = create_app(tmp_path / "data")
    client = TestClient(app, base_url="http://127.0.0.1:18765", raise_server_exceptions=False)
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    token = bootstrap.json()["token"]
    client.headers["X-PortraitLab-Token"] = token
    yield client, app
    app.state.store.relight.close()
    app.state.store.queue.put(None)
    client.close()


def picture():
    image = Image.fromarray(np.full((40, 64, 3), [80, 110, 140], dtype=np.uint8))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def import_one(client):
    response = client.post("/api/import", files={"files": ("测试人像.png", picture(), "image/png")})
    assert response.status_code == 200, response.text
    assert response.json()["errors"] == []
    return response.json()["photos"][0]


def wait_job(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + job_id).json()["job"]
        if job["status"] in {"completed", "completed_with_errors", "failed", "cancelled"}:
            return job
        time.sleep(.03)
    raise AssertionError("job timeout")


def test_host_origin_and_post_token_enforced(api):
    client, _ = api
    assert client.get("/api/bootstrap", headers={"host": "evil.example:18765"}).status_code == 403
    assert client.get("/api/bootstrap", headers={"origin": "http://evil.example"}).status_code == 403
    # A SameSite session cookie alone is deliberately insufficient for mutations.
    token = client.headers.pop("X-PortraitLab-Token")
    assert client.get("/api/photos").status_code == 200
    assert client.post("/api/settings/apply", json={}).status_code == 401
    client.cookies.clear()
    assert client.get("/api/photos").status_code == 401
    client.headers["X-PortraitLab-Token"] = token
    assert client.get("/api/photos").status_code == 200


def test_import_preserves_original_and_previews_cache_analysis(api):
    client, app = api
    photo = import_one(client)
    assert (photo["width"], photo["height"]) == (64, 40)
    stored = app.state.store.state["photos"][photo["id"]]
    assert Path(stored["original_path"]).read_bytes() == picture()
    assert client.get(photo["original_url"]).status_code == 200
    response = client.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": .5}})
    assert response.status_code == 200, response.text
    current = response.json()["photo"]
    assert current["status"] == "ready"
    assert current["mask_url"]
    assert client.get(current["preview_url"]).status_code == 200
    again = client.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": 1}})
    assert again.status_code == 200
    assert app.state.store.engine.calls == 1


def test_batch_snapshots_parameters_and_saves_sidecar(api):
    client, app = api
    photo = import_one(client)
    client.post("/api/settings/apply", json={"ids": [photo["id"]], "params": {"exposure": .5}})
    response = client.post("/api/jobs", json={"ids": [photo["id"]], "format": "png", "max_side": 0})
    assert response.status_code == 200, response.text
    # Export must retain the settings captured when the job was submitted.
    client.post("/api/settings/apply", json={"ids": [photo["id"]], "params": {"exposure": 1.5}})
    job = wait_job(client, response.json()["job"]["id"])
    assert job["status"] == "completed", job
    assert job["done"] == job["total"] == 1
    item = job["items"][0]
    with Image.open(item["path"]) as image:
        assert image.size == (64, 40)
        np.testing.assert_array_equal(np.asarray(image)[0, 0], [85, 115, 145])
        assert image.info.get("icc_profile")
    sidecar = json.loads(Path(item["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["params"]["exposure"] == .5
    assert sidecar["output_precision"] == "8-bit RGB"
    assert sidecar["source_sha256"] == app.state.store.photo(photo["id"])["sha256"]


def test_bad_formats_parameters_and_output_paths(api):
    client, _ = api
    photo = import_one(client)
    assert client.post("/api/jobs", json={"ids": [photo["id"]], "output_dir": "C:/portraitlab-should-never-write"}).status_code == 400
    assert client.post("/api/settings/apply", json={"ids": [photo["id"]], "params": {"exposure": 999}}).status_code == 400
    rejected = client.post("/api/import", files={"files": ("notimage.jpg", b"ZJV1fake", "image/jpeg")})
    assert rejected.status_code == 200
    assert rejected.json()["photos"] == []
    assert len(rejected.json()["errors"]) == 1
    with pytest.raises(ValueError):
        writable_path("F:/codex/../outside")


def test_masks_persist_and_settings_do_not_copy_strokes(api):
    client, app = api
    first, second = import_one(client), import_one(client)
    stroke = {"mode": "protect", "radius": .05, "points": [[.2, .3], [.25, .3]]}
    response = client.post(f"/api/photos/{first['id']}/mask", json={"strokes": [stroke]})
    assert response.status_code == 200, response.text
    applied = client.post("/api/settings/apply", json={"ids": [first["id"], second["id"]], "params": {"smooth": .4}})
    assert applied.status_code == 200
    assert app.state.store.photo(first["id"])["strokes"] == [stroke]
    assert app.state.store.photo(second["id"])["strokes"] == []
    with pytest.raises(ValueError):
        validate_strokes([{**stroke, "points": [[float("nan"), 0]]}])


def test_chunked_upload_limit_precedes_multipart_spooling(api, monkeypatch):
    client, app = api
    monkeypatch.setattr(server, "MAX_REQUEST_BYTES", 256)
    boundary = "portraitlabtestboundary"
    prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"x.png\"\r\n"
              "Content-Type: image/png\r\n\r\n").encode()
    def chunks():
        yield prefix
        yield b"x" * 1000
        yield ("\r\n--" + boundary + "--\r\n").encode()
    response = client.post("/api/import", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    assert "content-length" not in response.request.headers
    assert response.status_code == 413, response.text
    assert app.state.store.state["photos"] == {}
    assert list(app.state.store.folders["uploads"].iterdir()) == []


def test_preview_commits_parameters_and_strokes_atomically(api):
    client, app = api
    photo = import_one(client)
    stroke = {"mode": "protect", "radius": .04, "points": [[.25, .5]]}
    response = client.post(f"/api/photos/{photo['id']}/preview",
                           json={"params": {"exposure": .6}, "strokes": [stroke]})
    assert response.status_code == 200, response.text
    saved = response.json()["photo"]
    assert saved["params"]["exposure"] == .6
    assert saved["strokes"] == [stroke]
    version = saved["version"]
    bad = client.post(f"/api/photos/{photo['id']}/preview",
                      json={"params": {"exposure": 1.2}, "strokes": [{"mode": "unknown"}]})
    assert bad.status_code == 400
    unchanged = app.state.store.photo(photo["id"])
    assert unchanged["version"] == version
    assert unchanged["params"]["exposure"] == .6
    assert unchanged["strokes"] == [stroke]


def test_cancel_finishes_current_photo_then_stops(api):
    client, app = api
    first, second = import_one(client), import_one(client)
    entered, release = threading.Event(), threading.Event()
    original_render = app.state.store.engine.render
    def slow_render(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_render(*args, **kwargs)
    app.state.store.engine.render = slow_render
    response = client.post("/api/jobs", json={"ids": [first["id"], second["id"]]})
    job_id = response.json()["job"]["id"]
    try:
        assert entered.wait(5)
        assert client.post(f"/api/jobs/{job_id}/cancel").json()["job"]["cancel_requested"]
    finally:
        release.set()
    job = wait_job(client, job_id)
    assert job["status"] == "cancelled"
    assert job["done"] == 1
    assert job["items"][0]["status"] == "completed"
    assert job["items"][1]["status"] == "cancelled"


def test_state_and_analysis_survive_restart(api):
    client, app = api
    photo = import_one(client)
    response = client.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": .25}})
    assert response.status_code == 200
    app.state.store.queue.put(None)
    app.state.store.worker.join(5)
    restarted = create_app(app.state.store.data)
    second = TestClient(restarted, base_url="http://127.0.0.1:18765", raise_server_exceptions=False)
    try:
        token = second.get("/api/bootstrap").json()["token"]
        second.headers["X-PortraitLab-Token"] = token
        photos = second.get("/api/photos").json()["photos"]
        assert photos[0]["params"]["exposure"] == .25
        response = second.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": .5}})
        assert response.status_code == 200, response.text
        assert restarted.state.store.engine.calls == 0
        restarted.state.store.analysis_cache.clear()
        restarted.state.store.analysis_signature["packages"]["mediapipe"] = "simulated-sdk-upgrade"
        response = second.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": .75}})
        assert response.status_code == 200, response.text
        assert restarted.state.store.engine.calls == 1
    finally:
        restarted.state.store.queue.put(None)
        second.close()
