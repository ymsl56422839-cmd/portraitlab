"""Reversible library removal: atomic batches, persistence and task safety."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from portraitlab import engine
from portraitlab.server import create_app


class RemovalTestEngine:
    def __init__(self, models_dir):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def status(self):
        return {"ready": True, "models": [], "errors": []}

    def analyze(self, rgb):
        h, w = rgb.shape[:2]
        return {"faces": [], "face_count": 0, "shape": [h, w], "source_shape": [h, w],
                "parts": {}, "mask": np.zeros((h, w), np.float32), "warnings": [], "models": {}}

    def render(self, rgb, params, analysis, strokes=None):
        self.entered.set()
        assert self.release.wait(5), "preview test did not release renderer"
        return rgb.copy(), {"warnings": []}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTRAITLAB_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PORTRAITLAB_CACHE", str(tmp_path / "runtime-cache"))
    monkeypatch.setattr(engine, "Engine", RemovalTestEngine)
    return tmp_path


def open_client(app):
    client = TestClient(app, base_url="http://127.0.0.1:18765", raise_server_exceptions=False)
    token = client.get("/api/bootstrap").json()["token"]
    client.headers["X-PortraitLab-Token"] = token
    return client


@pytest.fixture
def api(configured):
    app = create_app(configured / "data")
    with open_client(app) as client:
        yield client, app.state.store, configured


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), (83, 117, 151)).save(stream, "PNG")
    return stream.getvalue()


def add(client, name="照片.png"):
    response = client.post("/api/import", files={"files": (name, image_bytes(), "image/png")})
    assert response.status_code == 200, response.text
    assert not response.json()["errors"]
    return response.json()["photos"][0]


def change(client, action, ids):
    return client.post(f"/api/photos/{action}", json={"ids": ids})


def library_ids(client):
    return [p["id"] for p in client.get("/api/photos").json()["photos"]]


def test_remove_restore_preserves_edits_originals_and_export_files(api):
    client, store, root = api
    external_original = root / "用户原图.png"
    external_original.write_bytes(image_bytes())
    first, second = add(client), add(client, "保留.png")
    identifier = first["id"]
    strokes = [{"mode": "protect", "radius": .05, "points": [[.2, .3], [.3, .4]]}]
    response = client.post(f"/api/photos/{identifier}/preview", json={"params": {"exposure": .5}, "strokes": strokes})
    assert response.status_code == 200, response.text
    submitted = client.post("/api/jobs", json={"ids": [identifier], "format": "png", "max_side": 0}).json()["job"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + submitted["id"]).json()["job"]
        if job["status"] not in {"queued", "running", "cancelling"}:
            break
        time.sleep(.01)
    assert job["status"] == "completed", job
    record = deepcopy(store.photo(identifier))
    protected = [external_original, Path(record["original_path"]), Path(record["source_path"]),
                 Path(record["preview_path"]), Path(job["items"][0]["path"]), Path(job["items"][0]["sidecar"])]
    before = {path: path.read_bytes() for path in protected}
    result = change(client, "remove", [identifier])
    assert result.status_code == 200, result.text
    assert result.json()["removed_ids"] == result.json()["last_removed_ids"] == [identifier]
    assert library_ids(client) == [second["id"]]
    assert client.get(first["original_url"]).status_code == 409
    assert client.post(f"/api/photos/{identifier}/preview", json={"params": {}}).status_code == 409
    assert client.post("/api/jobs", json={"ids": [identifier], "format": "png", "max_side": 0}).status_code == 409
    assert {path: path.read_bytes() for path in protected} == before
    assert store.state["photos"][identifier] == record
    restored = change(client, "restore", [identifier])
    assert restored.status_code == 200, restored.text
    assert restored.json()["photos"][0]["params"]["exposure"] == .5
    assert restored.json()["photos"][0]["strokes"] == strokes
    assert restored.json()["last_removed_ids"] == []
    assert library_ids(client) == [identifier, second["id"]]
    assert store.photo(identifier) == record
    assert {path: path.read_bytes() for path in protected} == before


def test_batches_validate_every_item_before_mutating(api):
    client, store, _ = api
    first, second = add(client), add(client)
    original_state = store.state_path.read_bytes()
    assert change(client, "remove", [first["id"], "missing"]).status_code == 404
    assert store.state_path.read_bytes() == original_state
    assert library_ids(client) == [first["id"], second["id"]]
    assert change(client, "remove", [first["id"], first["id"]]).json()["removed_ids"] == [first["id"]]
    after_remove = store.state_path.read_bytes()
    assert change(client, "remove", [second["id"], first["id"]]).status_code == 409
    assert change(client, "restore", [first["id"], second["id"]]).status_code == 409
    assert change(client, "restore", [first["id"], "missing"]).status_code == 404
    assert store.state_path.read_bytes() == after_remove
    assert library_ids(client) == [second["id"]]


def test_partial_restore_keeps_remaining_undo_ids(api):
    client, _, _ = api
    ids = [add(client)["id"] for _ in range(3)]
    assert change(client, "remove", ids).status_code == 200
    assert library_ids(client) == []
    assert change(client, "restore", ids[:1]).json()["last_removed_ids"] == ids[1:]
    assert client.get("/api/photos").json()["last_removed_ids"] == ids[1:]
    assert change(client, "restore", ids[1:]).status_code == 200
    assert library_ids(client) == ids


def test_reimport_removed_image_creates_usable_independent_record(api):
    client, store, _ = api
    original = add(client)
    assert client.post("/api/settings/apply", json={"ids": [original["id"]], "params": {"exposure": .6}}).status_code == 200
    assert change(client, "remove", [original["id"]]).status_code == 200
    imported = add(client)
    assert imported["id"] != original["id"]
    assert library_ids(client) == [imported["id"]]
    assert client.get(imported["original_url"]).status_code == 200
    assert client.post(f"/api/photos/{imported['id']}/preview", json={"params": {"exposure": 1.2}}).status_code == 200
    imported_record = deepcopy(store.photo(imported["id"]))
    assert change(client, "restore", [original["id"]]).status_code == 200
    assert store.photo(original["id"])["params"]["exposure"] == .6
    assert store.photo(imported["id"]) == imported_record
    assert library_ids(client) == [original["id"], imported["id"]]


def test_removal_and_last_batch_survive_restart(configured):
    app = create_app(configured / "data")
    with open_client(app) as client:
        first, second = add(client), add(client)
        assert change(client, "remove", [first["id"], second["id"]]).status_code == 200
    reopened = create_app(configured / "data")
    with open_client(reopened) as client:
        listed = client.get("/api/photos").json()
        assert listed["photos"] == []
        assert listed["last_removed_ids"] == [first["id"], second["id"]]
        assert change(client, "restore", listed["last_removed_ids"]).status_code == 200
        assert client.get(first["original_url"]).status_code == 200


@pytest.mark.parametrize("kind", ["jobs", "relight_jobs"])
@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_active_job_blocks_whole_removal_batch(api, kind, status):
    client, store, _ = api
    first, second = add(client), add(client)
    store.state.setdefault(kind, {})["active"] = {"id": "active", "status": status,
        "snapshots": [deepcopy(store.photo(second["id"]))], "items": []}
    before = deepcopy(store.state["photo_trash"])
    response = change(client, "remove", [first["id"], second["id"]])
    assert response.status_code == 409, response.text
    assert "任务结束" in response.json()["detail"]
    assert store.state["photo_trash"] == before
    assert library_ids(client) == [first["id"], second["id"]]


def test_completed_jobs_allow_removal_but_active_generated_result_blocks(api):
    client, store, _ = api
    first, second = add(client), add(client)
    store.state["jobs"]["old"] = {"status": "completed", "snapshots": [deepcopy(store.photo(first["id"]))]}
    store.state["relight_jobs"]["current"] = {"status": "running", "snapshots": [], "items": [{"photo_id": second["id"]}]}
    assert change(client, "remove", [first["id"]]).status_code == 200
    assert change(client, "remove", [second["id"]]).status_code == 409


def test_pending_preview_cannot_race_removal(api):
    client, store, _ = api
    identifier = add(client)["id"]
    store.engine.release.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f"/api/photos/{identifier}/preview", json={"params": {"exposure": .8}})
        try:
            assert store.engine.entered.wait(3)
            rejected = change(client, "remove", [identifier])
            assert rejected.status_code == 409, rejected.text
            assert "预览" in rejected.json()["detail"]
        finally:
            store.engine.release.set()
        assert pending.result(timeout=3).status_code == 200
    assert change(client, "remove", [identifier]).status_code == 200
    assert change(client, "restore", [identifier]).json()["photos"][0]["params"]["exposure"] == .8


@pytest.mark.parametrize("action", ["remove", "restore"])
def test_persist_failure_rolls_back_the_entire_operation(api, monkeypatch, action):
    client, store, _ = api
    ids = [add(client)["id"] for _ in range(2)]
    if action == "restore":
        assert change(client, "remove", ids).status_code == 200
    before_memory, before_disk = deepcopy(store.state), store.state_path.read_bytes()
    def fail():
        raise OSError("simulated disk failure")
    monkeypatch.setattr(store, "persist", fail)
    assert change(client, action, ids).status_code == 500
    assert store.state == before_memory
    assert store.state_path.read_bytes() == before_disk


def test_concurrent_duplicate_removal_has_one_success(api):
    client, store, _ = api
    ids = [add(client)["id"] for _ in range(2)]
    start = threading.Barrier(2)
    def attempt():
        start.wait(timeout=3)
        return change(client, "remove", ids).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(attempt) for _ in range(2)]
        statuses = sorted(future.result(timeout=5) for future in requests)
    assert statuses == [200, 409]
    assert set(store.state["photo_trash"]) == set(ids)
    assert change(client, "restore", ids).status_code == 200


@pytest.mark.parametrize("action", ["remove", "restore"])
@pytest.mark.parametrize("payload", [{}, {"ids": []}, {"ids": "wrong"}, {"ids": [True]}, {"ids": ["x"], "delete_files": True}])
def test_removal_endpoints_reject_invalid_requests(api, action, payload):
    client, store, _ = api
    before = store.state_path.read_bytes()
    assert client.post(f"/api/photos/{action}", json=payload).status_code == 400
    assert store.state_path.read_bytes() == before


def test_removal_endpoints_require_mutation_token(api):
    client, store, _ = api
    identifier = add(client)["id"]
    client.headers.pop("X-PortraitLab-Token")
    assert change(client, "remove", [identifier]).status_code == 401
    assert change(client, "restore", [identifier]).status_code == 401
    assert not store.state["photo_trash"]


def test_old_workspace_without_removal_fields_still_opens(configured):
    app = create_app(configured / "data")
    with open_client(app) as client:
        identifier = add(client)["id"]
    state_path = configured / "data/state.json"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    saved.pop("photo_trash")
    saved.pop("last_removed_ids")
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    reopened = create_app(configured / "data")
    with open_client(reopened) as client:
        assert library_ids(client) == [identifier]
        assert client.get("/api/photos").json()["last_removed_ids"] == []
