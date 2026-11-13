"""Hardware API authentication, state persistence and application shutdown."""
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from portraitlab import engine
from portraitlab.engine import Engine as RealEngine
from portraitlab.server import PortraitStore, create_app
from test_server import api, FakeEngine, import_one, wait_job


def test_hardware_state_and_missing_runtime(api, monkeypatch):
    client, app = api
    store = app.state.store
    monkeypatch.setattr(store.relight, "python", lambda: None)
    try:
        status = client.get("/api/hardware").json()
        assert status["preference"] == "auto" and not status["classic_gpu_available"]
        assert "CPU" in status["warning"]
        for raw in ({}, {"device": "gpu"}, {"device": []}, {"device": "cpu", "path": "elsewhere"}):
            assert client.post("/api/hardware", json=raw).status_code == 400
        selected = client.post("/api/hardware", json={"device": "cpu"})
        assert selected.status_code == 200 and selected.json()["preference"] == "cpu"
        assert client.post("/api/hardware", json={"device": "cuda"}).status_code == 409
        assert store.state["processing_device"] == "cpu"
        loaded = PortraitStore(store.data)
        try:
            assert loaded.state["processing_device"] == "cpu"
            assert loaded.gpu.process is None
        finally:
            loaded.model_setup.close()
            loaded.relight.close()
            loaded.gpu.close()
            loaded.queue.put(None)
            loaded.worker.join(5)
        client.headers.pop("X-PortraitLab-Token")
        assert client.post("/api/hardware", json={"device": "auto"}).status_code == 401
        client.cookies.clear()
        assert client.get("/api/hardware").status_code == 401
    finally:
        store.gpu.close()


def test_lifespan_closes_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTRAITLAB_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(engine, "Engine", FakeEngine)
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1:18765"):
        assert not app.state.store.gpu.closed.is_set()
    assert app.state.store.gpu.closed.is_set()
    assert not app.state.store.worker.is_alive()


def test_real_engine_preview_and_export_report_actual_cpu(api, monkeypatch):
    client, app = api
    store = app.state.store
    monkeypatch.setattr(store.relight, "python", lambda: None)
    # Restore the real compositor while substituting only model inference.
    store.engine = RealEngine(store.folders["models"])
    store.engine.compositor = store.gpu
    def analyze(rgb):
        h, w = rgb.shape[:2]
        return {"faces": [], "face_count": 0, "shape": [h, w], "source_shape": [h, w],
                "mask": np.zeros((h, w), np.float32), "parts": {}, "warnings": [], "models": {}}
    monkeypatch.setattr(store.engine, "analyze", analyze)
    photo = import_one(client)
    try:
        preview = client.post(f"/api/photos/{photo['id']}/preview", json={"params": {"exposure": .4}})
        assert preview.status_code == 200, preview.text
        meta = preview.json()["meta"]["processing_device"]
        assert meta["actual_device"] == "cpu" and "回退 CPU" in meta["warning"]
        assert store.gpu.process is None
        job = client.post("/api/jobs", json={"ids": [photo["id"]], "format": "png"}).json()["job"]
        job = wait_job(client, job["id"])
        assert job["status"] == "completed", job
        sidecar = json.loads(Path(job["items"][0]["sidecar"]).read_text(encoding="utf-8"))
        assert sidecar["processing_device"]["actual_device"] == "cpu"
        assert sidecar["processing_device"] == sidecar["engine_meta"]["processing_device"]
    finally:
        store.gpu.close()
