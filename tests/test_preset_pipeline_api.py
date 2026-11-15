"""Scoped preset and donor repair API regressions, without models or network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import io
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from portraitlab import preset_pipeline, spot_repair
from portraitlab.engine import validate_params
from portraitlab.presets import PARAMETER_GROUPS
from portraitlab.server import create_app
from test_server import api, import_one, preview_pixels, wait_job


SEED = {"smooth": .13, "blemishes": .17, "even": .21, "texture": .19,
        "acne": .23, "acne_sensitivity": .67, "region": "skin", "exposure": .37,
        "relight": .29, "warmth": -.33, "saturation": .77, "opacity": .62}
LOCAL = {"rect": {"x": .5, "y": .2, "width": .45, "height": .6},
         "params": {"exposure": 1., "warmth": .12, "even": .09}}
REPAIR = {"x": .7, "y": .5, "source_x": .3, "source_y": .5,
          "radius": .08, "strength": .85}
STROKE = {"mode": "protect", "radius": .025, "points": [[.1, .1]]}


def preview(client, photo, **data):
    response = client.post(f"/api/photos/{photo['id']}/preview", json=data)
    assert response.status_code == 200, response.text
    assert not response.json()["meta"].get("stale")
    return response.json()


def preset(client, photo, identifier, scope="global", version=None):
    return client.post(f"/api/photos/{photo['id']}/preset", json={
        "preset_id": identifier, "scope": scope,
        "expected_version": photo["version"] if version is None else version})


def import_pattern(client, blemish=True):
    yy, xx = np.indices((96, 160))
    texture = 2 * np.sin(xx * np.pi / 4) + 2 * np.cos(yy * np.pi / 4)
    pixels = np.clip(np.array([168, 128, 108]) + texture[..., None], 0, 255).astype(np.uint8)
    if blemish:
        pixels[np.hypot(xx - 111, yy - 48) <= 3] = [95, 40, 30]
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "PNG")
    response = client.post("/api/import", files={"files": ("donor-fixture.png", stream.getvalue(), "image/png")})
    assert response.status_code == 200, response.text
    return response.json()["photos"][0], pixels


@contextmanager
def reopened(store):
    # Simulate a quiescent restart rather than leaving two export workers active.
    store.queue.put(None)
    store.worker.join(timeout=5)
    assert not store.worker.is_alive()
    app = create_app(store.data)
    with TestClient(app, base_url="http://127.0.0.1:18765", raise_server_exceptions=False) as client:
        response = client.get("/api/bootstrap")
        assert response.status_code == 200, response.text
        client.headers["X-PortraitLab-Token"] = response.json()["token"]
        yield client, app.state.store


@pytest.mark.parametrize("identifier,group", [("natural", "skin"), ("dim_scene", "light"), ("warm_portrait", "color")])
def test_builtin_operations_preserve_other_groups_and_independent_edits(api, identifier, group):
    client, _ = api
    photo = import_one(client)
    saved = preview(client, photo, params=SEED, local_adjustment=LOCAL,
                    strokes=[STROKE], spot_repairs=[REPAIR])["photo"]
    before = deepcopy(saved)
    response = preset(client, saved, identifier)
    assert response.status_code == 200, response.text
    result, after = response.json(), response.json()["photo"]
    assert result["apply_mode"] == "scoped"
    assert result["apply_groups"] == [group]
    assert after["version"] == saved["version"] + 1
    assert any(after["params"][key] != before["params"][key] for key in PARAMETER_GROUPS[group])
    for other, keys in PARAMETER_GROUPS.items():
        if other != group:
            assert {key: after["params"][key] for key in keys} == {key: before["params"][key] for key in keys}
    assert after["params"]["opacity"] == before["params"]["opacity"]
    for key in ("local_adjustment", "strokes", "spot_repairs"):
        assert after[key] == before[key]
    assert result["summary"] and result["analysis"]["scene"]["sample_count"] > 0


def test_stale_preset_version_does_not_mutate_photo_or_persisted_state(api):
    client, app = api
    photo = import_one(client)
    saved = preview(client, photo, params=SEED)["photo"]
    before = deepcopy(app.state.store.photo(photo["id"]))
    disk = app.state.store.state_path.read_bytes()
    response = preset(client, saved, "natural", version=photo["version"])
    assert response.status_code == 409
    assert app.state.store.photo(photo["id"]) == before
    assert app.state.store.state_path.read_bytes() == disk


def test_edit_during_preset_analysis_wins_without_being_overwritten(api, monkeypatch):
    client, app = api
    photo = import_one(client)
    saved = preview(client, photo, params=SEED)["photo"]
    entered, release = threading.Event(), threading.Event()
    resolve = preset_pipeline.resolve_preset
    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(8), "concurrent edit did not finish"
        return resolve(*args, **kwargs)
    monkeypatch.setattr(preset_pipeline, "resolve_preset", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(preset, client, saved, "natural")
        try:
            assert entered.wait(8)
            edited = preview(client, saved, params={**saved["params"], "exposure": .81})["photo"]
            persisted = app.state.store.state_path.read_bytes()
        finally:
            release.set()
        response = pending.result(timeout=8)
    assert response.status_code == 409, response.text
    actual = app.state.store.photo(photo["id"])
    assert actual["version"] == edited["version"]
    assert actual["params"] == edited["params"]
    assert app.state.store.state_path.read_bytes() == persisted


def test_local_preset_keeps_rect_and_measures_global_base_before_local_and_spots(api, monkeypatch):
    client, app = api
    photo, source = import_pattern(client)
    saved = preview(client, photo, params={**SEED, "exposure": 1.2},
                    local_adjustment=LOCAL, spot_repairs=[REPAIR])["photo"]
    captured = {}
    resolve = preset_pipeline.resolve_preset
    def observe(recipe, current, rgb, analysis, local_mask=None):
        captured.update(rgb=rgb.copy(), mask=deepcopy(local_mask))
        return resolve(recipe, current, rgb, analysis, local_mask=local_mask)
    monkeypatch.setattr(preset_pipeline, "resolve_preset", observe)
    response = preset(client, saved, "auto_balance", scope="local")
    assert response.status_code == 200, response.text
    result, after = response.json(), response.json()["photo"]
    global_base = np.clip(source.astype(np.int16) + 12, 0, 255).astype(np.uint8)
    np.testing.assert_array_equal(captured["rgb"], global_base)
    assert captured["mask"] == LOCAL["rect"]
    assert result["analysis"]["scope"] == "selection"
    assert after["params"] == saved["params"]
    assert after["spot_repairs"] == saved["spot_repairs"]
    assert after["local_adjustment"]["rect"] == saved["local_adjustment"]["rect"]
    for group in ("skin", "color"):
        for key in PARAMETER_GROUPS[group]:
            assert after["local_adjustment"]["params"][key] == saved["local_adjustment"]["params"][key]
    # Inspect the actual reported statistic, not merely the resolver's arguments.
    selected = global_base[19:77, 80:152].astype(np.float64) / 255
    linear = np.where(selected <= .04045, selected / 12.92, ((selected + .055) / 1.055) ** 2.4)
    median = float(np.median(linear @ np.array([.2126, .7152, .0722])))
    assert result["analysis"]["scene"]["median_luminance"] == pytest.approx(median, abs=1e-7)


def test_legacy_custom_preset_still_replaces_complete_parameter_snapshot(api):
    client, app = api
    photo = import_one(client)
    saved = preview(client, photo, params=SEED)["photo"]
    custom = client.post("/api/presets", json={"name": "旧参数快照", "params": {
        "smooth": .43, "exposure": -.6, "warmth": .55, "saturation": .44, "opacity": .35}}).json()["preset"]
    legacy = {key: deepcopy(custom[key]) for key in ("id", "name", "description", "params")}
    legacy["params"].pop("acne_sensitivity", None)
    app.state.store.state["presets"][custom["id"]] = legacy
    app.state.store.persist()
    with reopened(app.state.store) as (again, store):
        response = preset(again, saved, custom["id"])
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["apply_mode"] == "snapshot"
        assert result["photo"]["params"] == validate_params(legacy["params"])
        assert set(result["apply_groups"]) == set(PARAMETER_GROUPS)
        assert "完整参数快照" in result["summary"]
        assert store.photo(photo["id"])["params"] == result["photo"]["params"]


@pytest.mark.parametrize("payload,status", [
    ({}, 400), ({"preset_id": "natural", "scope": "global"}, 400),
    ({"preset_id": "natural", "scope": "global", "expected_version": True}, 400),
    ({"preset_id": "natural", "scope": "bad", "expected_version": 1}, 400),
])
def test_malformed_preset_requests_are_atomic(api, payload, status):
    client, app = api
    photo = import_one(client)
    before = deepcopy(app.state.store.photo(photo["id"]))
    disk = app.state.store.state_path.read_bytes()
    response = client.post(f"/api/photos/{photo['id']}/preset", json=payload)
    assert response.status_code == status, response.text
    assert app.state.store.photo(photo["id"]) == before
    assert app.state.store.state_path.read_bytes() == disk


def test_missing_preset_and_absent_local_rect_do_not_change_photo(api):
    client, app = api
    photo = import_one(client)
    before = deepcopy(app.state.store.photo(photo["id"]))
    assert preset(client, photo, "does-not-exist").status_code == 404
    assert preset(client, photo, "natural", scope="local").status_code == 400
    assert app.state.store.photo(photo["id"]) == before


def test_spot_layer_preview_export_restart_undo_and_single_application(api, monkeypatch):
    client, app = api
    photo, _ = import_pattern(client)
    assert photo["spot_repairs"] == []
    base_photo = preview(client, photo, params={"exposure": .5}, local_adjustment=LOCAL)["photo"]
    baseline = preview_pixels(client, base_photo)
    calls = []
    apply = spot_repair.apply_repairs
    def observe(rgb, repairs):
        calls.append(rgb.copy())
        return apply(rgb, repairs)
    monkeypatch.setattr(spot_repair, "apply_repairs", observe)
    response = preview(client, base_photo, spot_repairs=[REPAIR])
    healed = response["photo"]
    pixels = preview_pixels(client, healed)
    assert len(calls) == 1
    # The one repair pass receives an already-composed global+local base.
    np.testing.assert_array_equal(calls[0], baseline)
    assert np.any(pixels != baseline)
    assert response["meta"]["spot_repairs"]["applied"] == 1
    assert healed["spot_repairs"] == [REPAIR]
    retained = preview(client, healed)["photo"]
    np.testing.assert_array_equal(preview_pixels(client, retained), pixels)
    assert len(calls) == 1, "unchanged preview must use the independent repaired-base cache"
    undone = preview(client, retained, spot_repairs=[])["photo"]
    np.testing.assert_array_equal(preview_pixels(client, undone), baseline)
    assert undone["params"] == base_photo["params"]
    assert undone["local_adjustment"] == base_photo["local_adjustment"]
    restored = preview(client, undone, spot_repairs=[REPAIR])["photo"]
    np.testing.assert_array_equal(preview_pixels(client, restored), pixels)
    export = client.post("/api/jobs", json={"ids": [photo["id"]], "format": "png", "max_side": 0})
    assert export.status_code == 200, export.text
    job = wait_job(client, export.json()["job"]["id"])
    assert job["status"] == "completed", job
    item = job["items"][0]
    with Image.open(item["path"]) as image:
        np.testing.assert_array_equal(np.asarray(image), pixels)
    sidecar = json.loads(Path(item["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["spot_repairs"] == [REPAIR]
    assert sidecar["engine_meta"]["spot_repairs"]["applied"] == 1
    persisted = json.loads(app.state.store.state_path.read_text(encoding="utf-8"))
    assert persisted["photos"][photo["id"]]["spot_repairs"] == [REPAIR]
    before_restart_calls = len(calls)
    with reopened(app.state.store) as (again, store):
        public = again.get("/api/photos").json()["photos"][0]
        assert public["spot_repairs"] == [REPAIR]
        after_restart = preview(again, public)["photo"]
        np.testing.assert_array_equal(preview_pixels(again, after_restart), pixels)
        assert len(calls) == before_restart_calls
        assert store.engine.calls == 0, "valid analysis and repaired-base caches survive restart"


@pytest.mark.parametrize("bad", [[{**REPAIR, "source_x": True}], [{**REPAIR, "radius": 0}], [REPAIR] * 101])
def test_invalid_spot_edit_cannot_partially_commit_other_adjustments(api, bad):
    client, app = api
    photo = import_one(client)
    saved = preview(client, photo, params=SEED, spot_repairs=[REPAIR])["photo"]
    before = deepcopy(app.state.store.photo(photo["id"]))
    disk = app.state.store.state_path.read_bytes()
    response = client.post(f"/api/photos/{photo['id']}/preview", json={
        "params": {"exposure": 1.8}, "strokes": [STROKE],
        "local_adjustment": LOCAL, "spot_repairs": bad})
    assert response.status_code == 400, response.text
    assert app.state.store.photo(photo["id"]) == before
    assert app.state.store.state_path.read_bytes() == disk
    assert saved["spot_repairs"] == [REPAIR]


def test_repair_edits_change_lighting_base_cache_key_but_light_only_edits_do_not(api):
    client, app = api
    photo = import_one(client)
    store = app.state.store
    original = deepcopy(store.photo(photo["id"]))
    repaired = {**original, "spot_repairs": [REPAIR]}
    moved = {**original, "spot_repairs": [{**REPAIR, "x": .6}]}
    partial = {**original, "spot_repairs": [{**REPAIR, "strength": .4}]}
    keys = [store.lighting.base_key(snapshot) for snapshot in (original, repaired, moved, partial)]
    assert len(set(keys)) == 4
    assert store.lighting.base_key({**repaired, "lighting": {"enabled": True, "strength": .2}}) == keys[1]
    assert store.lighting.base_key({**repaired, "spot_repairs": []}) == keys[0]
