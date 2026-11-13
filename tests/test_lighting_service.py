"""Interactive lighting persistence, versioning and pixel-preserving exports."""
import io
import json
from pathlib import Path
import sys
import threading
import time

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from portraitlab import engine, lighting, lighting_geometry, lighting_service
from portraitlab.lighting_service import LightingController
from portraitlab.server import create_app


class FakeEngine:
    def __init__(self, models_dir):
        pass

    def status(self):
        return {"ready": True, "models": {}, "errors": []}

    def analyze(self, rgb):
        h, w = rgb.shape[:2]
        background = np.zeros((h, w), np.float32)
        background[:, :w//2] = 1
        mask = np.ones((h, w), np.float32)
        return {"faces": [], "face_count": 0, "shape": [h, w], "source_shape": [h, w],
                "parts": {"background": background}, "mask": mask, "face_mask": mask,
                "skin_mask": mask, "warnings": [], "models": {}}

    def render(self, rgb, params, analysis, strokes=None):
        return np.clip(rgb.astype(np.int16)+round(params["exposure"]*10), 0, 255).astype(np.uint8), {"warnings": []}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTRAITLAB_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PORTRAITLAB_CACHE", str(tmp_path/'runtime-cache'))
    monkeypatch.setattr(engine, "Engine", FakeEngine)
    monkeypatch.setattr(lighting_geometry, "status", lambda _: {"ready": True, "license": "test"})
    monkeypatch.setattr(lighting_geometry, "geometry_signature", lambda: "test-geometry-v1")

    def infer(self, input_path, output_dir, device, job):
        self.test_inferences = getattr(self, 'test_inferences', 0)+1
        with Image.open(input_path) as image:
            w, h = image.size
        normal = np.zeros((h, w, 3), np.uint8)
        normal[:] = [128, 128, 255]
        self.save_image(output_dir/'normal.png', normal, 'PNG')
        self.save_image(output_dir/'depth.png', Image.fromarray(np.full((h, w), 128, np.uint8)), 'PNG')
        return {"device": "test", "width": w, "height": h}

    monkeypatch.setattr(LightingController, '_infer_geometry', infer)
    return tmp_path


def open_client(path):
    app = create_app(path)
    app.state.store.relight.python = lambda: Path(sys.executable)
    client = TestClient(app, base_url='http://127.0.0.1:18765', raise_server_exceptions=False)
    client.headers['X-PortraitLab-Token'] = client.get('/api/bootstrap').json()['token']
    return client, app.state.store


@pytest.fixture
def api(configured):
    client, store = open_client(configured/'data')
    with client:
        yield client, store, configured


def picture(value=80):
    pixels = np.full((40, 64, 3), [value, 110, 140], np.uint8)
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, 'PNG')
    return stream.getvalue(), pixels


def import_photo(client, value=80):
    blob, pixels = picture(value)
    response = client.post('/api/import', files={'files': ('input.png', blob, 'image/png')})
    assert response.status_code == 200, response.text
    return response.json()['photos'][0], pixels


def wait_job(client, identifier, prefix='/api/lighting/jobs/'):
    deadline = time.monotonic()+8
    while time.monotonic() < deadline:
        job = client.get(prefix+identifier).json()['job']
        if job['status'] not in {'queued', 'running', 'cancelling'}:
            return job
        time.sleep(.01)
    raise AssertionError('job did not complete')


def prepare(client, identifier, refresh_geometry=False):
    response = client.post('/api/lighting/prepare', json={'photo_id': identifier, 'device': 'cpu', 'refresh_geometry': refresh_geometry})
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()['job']['id'])
    assert job['status'] == 'completed', job
    return job


def config():
    return {**lighting.DEFAULT_LIGHTING, 'enabled': True, 'scope': 'subject',
            'lights': [{**lighting.DEFAULT_LIGHT, 'id': 'test-key', 'intensity': 1.2}]}


def test_status_advertises_required_editor_contract(api):
    client, _, _ = api
    response = client.get('/api/lighting/status')
    assert response.status_code == 200
    assert response.json()['capabilities'] == {
        'canonical_preview': True, 'refresh_geometry': True, 'geometry_restore': True,
    }


def save(client, photo, prepared, settings=None):
    return client.post(f"/api/photos/{photo['id']}/lighting", json={
        'lighting': settings or config(), 'expected_version': prepared['version'],
        'geometry_key': prepared['geometry_key']})


def test_prepare_keeps_original_and_unlit_pixels_and_caches_geometry(api):
    client, store, _ = api
    photo, original = import_photo(client)
    first = prepare(client, photo['id'])
    result = first['result']
    base = np.asarray(Image.open(io.BytesIO(client.get(result['base_url']).content)))
    assert np.array_equal(base, original)
    assert store.lighting.test_inferences == 1
    response = save(client, photo, result)
    assert response.status_code == 200, response.text
    second = prepare(client, photo['id'])['result']
    assert second['version'] > result['version']
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(client.get(second['base_url']).content))), base)
    assert store.lighting.test_inferences == 1
    assert len(client.get('/api/photos').json()['photos']) == 1
    assert np.array_equal(store.rgb(store.photo(photo['id'])), original)


def test_confident_background_is_zero_and_hair_edges_remain_soft():
    foreground = np.array([[.0573, .08356, .1, .2, .35, .5, .65, .8, .9, .98]], np.float32)
    analysis = {'parts': {'background': 1-foreground}}
    controller = LightingController.__new__(LightingController)
    mask = controller._subject_mask(analysis, np.zeros((1, 10, 3), np.uint8), 10, 1)
    # The observed clear-background confidence floor must not leak lighting.
    assert np.array_equal(mask[0, :3], np.zeros(3, np.uint8))
    assert np.array_equal(mask[0, -2:], np.full(2, 255, np.uint8))
    assert np.all((mask[0, 3:8] > 0) & (mask[0, 3:8] < 255))
    assert np.all(np.diff(mask.astype(np.int16)[0, 3:8]) > 0)


def test_mask_policy_changes_new_cache_keys_without_invalidating_saved_geometry(api, monkeypatch):
    client, store, _ = api
    photo, pixels = import_photo(client)
    prepared = prepare(client, photo['id'])['result']
    assert save(client, photo, prepared).status_code == 200
    saved = json.loads(json.dumps(store.photo(photo['id'])))
    expected, _ = store.lighting.render(saved, pixels)
    old_key = saved['lighting_geometry']
    monkeypatch.setattr(lighting_service, 'SUBJECT_MASK_POLICY_VERSION', 'future-mask-policy-test')
    assert store.lighting.geometry_key(saved) != old_key
    output, _ = store.lighting.render(saved, pixels)
    assert np.array_equal(output, expected)
    assert store.lighting.geometry_directory(old_key).is_dir()
    reopened = prepare(client, photo['id'])['result']
    assert reopened['geometry_key'] == old_key
    refreshed = prepare(client, photo['id'], refresh_geometry=True)['result']
    assert refreshed['geometry_key'] != old_key
    assert refreshed['meta']['mask_policy']['version'] == 'future-mask-policy-test'
    # Reanalysis alone does not replace a previously committed geometry key.
    assert store.photo(photo['id'])['lighting_geometry'] == old_key


def test_save_same_photo_and_generic_preview_undo_persist(api):
    client, store, root = api
    photo, _ = import_photo(client)
    result = prepare(client, photo['id'])['result']
    response = save(client, photo, result)
    assert response.status_code == 200, response.text
    saved = response.json()['photo']
    assert saved['id'] == photo['id'] and saved['lighting']['enabled']
    classical = client.post(f"/api/photos/{photo['id']}/preview", json={'params': {'exposure': .4}})
    assert classical.status_code == 200
    assert classical.json()['photo']['lighting'] == saved['lighting']
    undone = client.post(f"/api/photos/{photo['id']}/preview", json={'lighting': lighting.DEFAULT_LIGHTING})
    assert undone.status_code == 200
    assert not undone.json()['photo']['lighting']['enabled']
    persisted = json.loads((root/'data/state.json').read_text(encoding='utf-8'))
    assert not persisted['photos'][photo['id']]['lighting']['enabled']
    assert persisted['photos'][photo['id']]['params']['exposure'] == .4


def test_stale_save_and_cross_photo_geometry_do_not_mutate(api):
    client, store, _ = api
    photo, _ = import_photo(client)
    result = prepare(client, photo['id'])['result']
    changed = client.post(f"/api/photos/{photo['id']}/preview", json={'params': {'exposure': .3}}).json()['photo']
    before = json.loads(json.dumps(store.photo(photo['id'])))
    assert save(client, photo, result).status_code == 409
    assert store.photo(photo['id']) == before
    other, _ = import_photo(client, 90)
    response = save(client, other, {**result, 'version': other['version']})
    assert response.status_code in {400, 409}, response.text
    assert not client.get('/api/photos').json()['photos'][1]['lighting']['enabled']


@pytest.mark.parametrize('scope, policy', [
    ('subject', 'exact_input_pixels_outside_subject_mask'),
    ('background', 'exact_input_pixels_inside_subject_mask'),
    ('all', 'whole_image'),
])
def test_export_applies_lighting_once_and_preserves_unselected_pixels(api, scope, policy):
    client, store, root = api
    photo, original = import_photo(client)
    edited = client.post(f"/api/photos/{photo['id']}/preview", json={'params': {'exposure': .4}}).json()['photo']
    result = prepare(client, photo['id'])['result']
    assert save(client, edited, result, {**config(), 'scope': scope}).status_code == 200
    snapshot = json.loads(json.dumps(store.photo(photo['id'])))
    base = original+4
    expected, _ = store.lighting.render(snapshot, base)
    response = client.post('/api/jobs', json={'ids':[photo['id']], 'format':'png', 'max_side':0})
    job = wait_job(client, response.json()['job']['id'], '/api/jobs/')
    assert job['status'] == 'completed', job
    output = np.asarray(Image.open(job['items'][0]['path']))
    assert np.array_equal(output, expected)
    # The left half is background; the right half is an opaque subject.
    # PNG must preserve the unselected half byte-for-byte after prior retouching.
    assert np.array_equal(output[:, :32], base[:, :32]) == (scope == 'subject')
    assert np.array_equal(output[:, 32:], base[:, 32:]) == (scope == 'background')
    sidecar = json.loads(Path(job['items'][0]['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['lighting'] == snapshot['lighting']
    assert sidecar['engine_meta']['lighting']['method'] == 'deterministic_image_space_lighting'
    assert sidecar['engine_meta']['lighting']['background_policy'] == policy


def test_asset_allowlist_and_removal_gate(api):
    client, _, _ = api
    photo, _ = import_photo(client)
    job = prepare(client, photo['id'])
    assert client.get(job['result']['mask_url']).status_code == 200
    assert client.get(f"/api/lighting/assets/{job['id']}/geometry.json").status_code == 404
    assert client.post('/api/photos/remove', json={'ids':[photo['id']]}).status_code == 200
    assert client.get(job['result']['base_url']).status_code == 409


def test_running_preparation_blocks_removal_and_cancel_is_recoverable(api, monkeypatch):
    client, store, _ = api
    photo, _ = import_photo(client)
    entered = threading.Event()
    def blocked(self, *args):
        job = args[-1]
        entered.set()
        while True:
            self._check_cancel(job)
            time.sleep(.01)
    monkeypatch.setattr(LightingController, '_infer_geometry', blocked)
    response = client.post('/api/lighting/prepare', json={'photo_id':photo['id']})
    identifier = response.json()['job']['id']
    assert entered.wait(3)
    assert client.post('/api/photos/remove', json={'ids':[photo['id']]}).status_code == 409
    assert client.post(f'/api/lighting/jobs/{identifier}/cancel', json={}).status_code == 200
    assert wait_job(client, identifier)['status'] == 'cancelled'
    assert client.post('/api/photos/remove', json={'ids':[photo['id']]}).status_code == 200


def test_status_and_prepare_never_download_without_explicit_action(api, monkeypatch):
    client, store, _ = api
    calls = []
    monkeypatch.setattr(lighting_geometry, 'status', lambda _: {'ready':False})
    monkeypatch.setattr(lighting_geometry, 'download', lambda *args, **kwargs: calls.append('download') or {'ready':True})
    assert not client.get('/api/lighting/status').json()['ready']
    photo, _ = import_photo(client)
    response = client.post('/api/lighting/prepare', json={'photo_id':photo['id']})
    assert wait_job(client, response.json()['job']['id'])['status'] == 'failed'
    assert not calls
    response = client.post('/api/lighting/models/download', json={})
    assert wait_job(client, response.json()['job']['id'])['status'] == 'completed'
    assert calls == ['download']


@pytest.mark.parametrize('payload', [{'photo_id':1}, {'photo_id':'missing'}, {'photo_id':'x','device':'other'}, {'photo_id':'x','device':[]}, {'photo_id':'x','path':'F:/injected'}, {'photo_id':'x','refresh_geometry':'yes'}])
def test_prepare_rejects_invalid_requests(api, payload):
    client, _, _ = api
    assert client.post('/api/lighting/prepare', json=payload).status_code in {400, 404}


def test_lighting_mutations_require_session_token(api):
    client, _, _ = api
    assert client.post('/api/lighting/models/download', json={}, headers={'X-PortraitLab-Token':'wrong'}).status_code == 401


def test_failed_persist_leaves_saved_lighting_unchanged(api, monkeypatch):
    client, store, _ = api
    photo, _ = import_photo(client)
    result = prepare(client, photo['id'])['result']
    before = json.loads(json.dumps(store.photo(photo['id'])))
    monkeypatch.setattr(store, 'persist', lambda: (_ for _ in ()).throw(OSError('write failed')))
    assert save(client, photo, result).status_code == 500
    assert store.photo(photo['id']) == before


@pytest.mark.parametrize('scope', ['subject', 'background', 'all'])
def test_committed_lighting_survives_restart_without_reanalysis(configured, scope):
    first, store = open_client(configured/'data')
    with first:
        photo, pixels = import_photo(first)
        result = prepare(first, photo['id'])['result']
        response = save(first, photo, result, {**config(), 'scope': scope})
        assert response.status_code == 200
        expected, _ = store.lighting.render(store.photo(photo['id']), pixels)
    second, reopened = open_client(configured/'data')
    with second:
        photos = second.get('/api/photos').json()['photos']
        assert len(photos) == 1 and photos[0]['lighting']['enabled']
        assert photos[0]['lighting']['scope'] == scope
        output, _ = reopened.lighting.render(reopened.photo(photo['id']), pixels)
        assert np.array_equal(output, expected)
        assert getattr(reopened.lighting, 'test_inferences', 0) == 0
        again = prepare(second, photo['id'])
        assert again['status'] == 'completed'
        assert again['result']['lighting']['scope'] == scope
        assert getattr(reopened.lighting, 'test_inferences', 0) == 0


def test_live_saved_and_export_share_one_full_resolution_retouch_base(api, monkeypatch):
    client, store, _ = api
    source = np.full((1800, 900, 3), [80, 110, 140], np.uint8)
    blob = io.BytesIO()
    Image.fromarray(source).save(blob, 'PNG')
    photo = client.post('/api/import', files={'files': ('large.png', blob.getvalue(), 'image/png')}).json()['photos'][0]
    calls = []
    def scale_sensitive_render(rgb, params, analysis, strokes=None):
        calls.append(rgb.shape)
        return np.clip(rgb.astype(np.int16)+rgb.shape[0]//200, 0, 255).astype(np.uint8), {'warnings': []}
    monkeypatch.setattr(store.engine, 'render', scale_sensitive_render)
    result = prepare(client, photo['id'])['result']
    asset = lambda name: np.asarray(Image.open(io.BytesIO(client.get(result[name+'_url']).content)))
    base, normal, depth, mask = [asset(name) for name in ('base', 'normal', 'depth', 'mask')]
    assert result['base_contract'] == lighting_service.BASE_CONTRACT
    assert base.shape[:2] == (1024, 512)
    assert np.array_equal(base[0, 0], source[0, 0]+9)
    expected = lighting.render_lighting(base, normal, depth, mask, config())
    saved = save(client, photo, result).json()['photo']
    preview_response = client.get(saved['preview_url'])
    preview = np.asarray(Image.open(io.BytesIO(preview_response.content)))
    assert preview_response.headers['content-type'].startswith('image/png')
    assert np.array_equal(preview, expected)
    response = client.post('/api/jobs', json={'ids': [photo['id']], 'format': 'png', 'max_side': 1024})
    job = wait_job(client, response.json()['job']['id'], '/api/jobs/')
    assert job['status'] == 'completed', job
    assert np.array_equal(np.asarray(Image.open(job['items'][0]['path'])), expected)
    # Even disabling lights keeps the same base/grid: saving cannot jump back
    # to a separately computed 1600px JPEG.
    off = save(client, saved, {**result, 'version': saved['version']}, {**config(), 'enabled': False}).json()['photo']
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(client.get(off['preview_url']).content))), base)
    assert calls == [source.shape]


def test_retouch_base_invalidates_only_for_retouch_inputs_and_bounds_cache(api, monkeypatch):
    client, store, _ = api
    photo, original = import_photo(client)
    snapshot = json.loads(json.dumps(store.photo(photo['id'])))
    base, first = store.lighting.retouch_base(snapshot)
    light_only = {**snapshot, 'lighting': config()}
    again, reused = store.lighting.retouch_base(light_only)
    assert reused['retouch_base']['cache_hit'] and np.array_equal(again, base)
    assert first['retouch_base']['key'] == reused['retouch_base']['key']
    for exposure in (.1, .2, .3, .4):
        edited = {**snapshot, 'params': {**snapshot['params'], 'exposure': exposure}}
        rendered, meta = store.lighting.retouch_base(edited)
        assert meta['retouch_base']['key'] != first['retouch_base']['key']
        assert np.array_equal(rendered, original+round(exposure*10))
    cache = store.lighting.base_root / photo['id']
    assert len(list(cache.glob('*.png'))) == lighting_service.BASE_CACHE_PER_PHOTO
    assert len(list(cache.glob('*.json'))) == lighting_service.BASE_CACHE_PER_PHOTO
    monkeypatch.setattr(lighting_service, 'BASE_CACHE_BYTES', 1)
    store.lighting.retouch_base({**snapshot, 'params': {**snapshot['params'], 'exposure': .5}})
    assert len(list(store.lighting.base_root.glob('*/*.png'))) == 1


def test_incomplete_base_cache_retries_without_changing_photo(api, monkeypatch):
    client, store, _ = api
    photo, original = import_photo(client)
    snapshot = json.loads(json.dumps(store.photo(photo['id'])))
    original_write = store.lighting.save_json
    def fail_base_meta(path, value):
        if 'retouch-base' in path.parts:
            raise OSError('cache disk write failed')
        return original_write(path, value)
    monkeypatch.setattr(store.lighting, 'save_json', fail_base_meta)
    with pytest.raises(OSError):
        store.lighting.retouch_base(snapshot)
    assert store.photo(photo['id']) == snapshot
    monkeypatch.setattr(store.lighting, 'save_json', original_write)
    output, meta = store.lighting.retouch_base(snapshot)
    assert not meta['retouch_base']['cache_hit']
    assert np.array_equal(output, original)
    output, meta = store.lighting.retouch_base(snapshot)
    assert meta['retouch_base']['cache_hit']


def test_failed_render_rolls_back_lighting_and_generic_preview_edits(api, monkeypatch):
    client, store, root = api
    photo, _ = import_photo(client)
    prepared = prepare(client, photo['id'])['result']
    previous = json.loads(json.dumps(store.photo(photo['id'])))
    monkeypatch.setattr(store.lighting, 'retouch_base', lambda *args: (_ for _ in ()).throw(OSError('cache failed')))
    assert save(client, photo, prepared).status_code == 500
    assert store.photo(photo['id']) == previous
    assert json.loads((root/'data/state.json').read_text(encoding='utf-8'))['photos'][photo['id']] == previous
    assert client.post(f"/api/photos/{photo['id']}/preview", json={
        'lighting': config(), 'lighting_geometry': prepared['geometry_key']}).status_code == 500
    assert store.photo(photo['id']) == previous


def test_preview_undo_restores_geometry_and_rejects_other_photo(api, monkeypatch):
    client, store, _ = api
    photo, _ = import_photo(client)
    old = prepare(client, photo['id'])['result']
    assert save(client, photo, old).status_code == 200
    monkeypatch.setattr(lighting_service, 'SUBJECT_MASK_POLICY_VERSION', 'explicit-refresh-v3')
    fresh = prepare(client, photo['id'], refresh_geometry=True)['result']
    assert fresh['geometry_key'] != old['geometry_key']
    assert save(client, photo, fresh).status_code == 200
    endpoint = f"/api/photos/{photo['id']}/preview"
    undo = client.post(endpoint, json={'lighting': config(), 'lighting_geometry': old['geometry_key']})
    assert undo.status_code == 200
    assert undo.json()['photo']['lighting_geometry'] == old['geometry_key']
    other, _ = import_photo(client, 90)
    foreign = prepare(client, other['id'])['result']
    assert client.post(endpoint, json={'lighting_geometry': foreign['geometry_key']}).status_code == 409
    assert client.post(endpoint, json={'lighting_geometry': None}).status_code == 409
    cleared = client.post(endpoint, json={'lighting': lighting.DEFAULT_LIGHTING, 'lighting_geometry': None})
    assert cleared.status_code == 200
    assert cleared.json()['photo']['lighting_geometry'] is None
