"""Integration boundaries tested with a controlled generator; no AI quality claim."""
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest
from PIL import Image

from test_server import api, import_one, picture, wait_job
from portraitlab.relight_service import Cancelled, validate_options
from portraitlab.server import writable_path


def test_exclusive_output_falls_back_without_hardlinks(tmp_path, monkeypatch):
    from portraitlab.server import atomic_write
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT', str(tmp_path))
    def no_links(*args):
        raise OSError('This volume does not support hard links')
    monkeypatch.setattr('os.link', no_links)
    target = tmp_path / 'result.bin'
    atomic_write(target, lambda p: p.write_bytes(b'complete'), replace=False)
    assert target.read_bytes() == b'complete'
    with pytest.raises(FileExistsError):
        atomic_write(target, lambda p: p.write_bytes(b'overwritten'), replace=False)
    assert target.read_bytes() == b'complete'


def await_ai(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get('/api/relight/jobs/' + job_id).json()['job']
        if job['status'] not in {'queued', 'running', 'cancelling'}:
            return job
        time.sleep(.02)
    raise AssertionError('AI job timed out')


def test_paths_are_portable_but_optional_policy_is_enforced(tmp_path, monkeypatch):
    # These calls resolve paths only and do not create files on another drive.
    monkeypatch.delenv('PORTRAITLAB_STORAGE_ROOT', raising=False)
    assert writable_path('C:/Example/人像软件/Data') == Path('C:/Example/人像软件/Data').resolve()
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT', str(tmp_path))
    assert writable_path(tmp_path / '数据') == (tmp_path / '数据').resolve()
    with pytest.raises(ValueError):
        writable_path(tmp_path / '../outside')


@pytest.mark.parametrize('options', [
    {'input_path': 'injected.png'}, {'seed': True}, {'steps': 100},
    {'long_side': 2048}, {'denoise': float('nan')}, {'device': 'unknown'},
])
def test_generation_rejects_untrusted_options(options):
    with pytest.raises(ValueError):
        validate_options(options)


def test_generated_photo_preserves_source_and_export_provenance(api, monkeypatch):
    client, app = api
    original = import_one(client)
    controller = app.state.store.relight
    monkeypatch.setattr(controller, 'python', lambda: Path(sys.executable))
    captured = []

    def generate(request, directory, job=None):
        captured.append(request)
        with Image.open(request['input_path']) as source, Image.open(request['mask_path']) as mask:
            assert source.size == mask.size
        Image.new('RGB', (request['width'], request['height']), (170, 120, 85)).save(request['output_path'])
        return {'ok': True, 'output_path': request['output_path'], 'width': request['width'],
                'height': request['height'], 'metadata': {'model': 'test-double', 'seed': request['seed']}}

    monkeypatch.setattr(controller, 'invoke', generate)
    response = client.post('/api/relight/jobs', json={'ids': [original['id']], 'params': {'seed': 18, 'long_side': 512}})
    assert response.status_code == 200, response.text
    job = await_ai(client, response.json()['job']['id'])
    assert job['status'] == 'completed', job
    assert len(captured) == 1
    assert captured[0]['seed'] == 18
    generated_id = job['items'][0]['photo_id']
    assert generated_id != original['id']
    photos = client.get('/api/photos').json()['photos']
    assert len(photos) == 2
    generated = next(p for p in photos if p['id'] == generated_id)
    assert generated['params']['opacity'] == 1
    assert generated['params']['smooth'] == 0
    assert generated['comparison_kind'] == 'generation_input'
    before = client.get(generated['original_url'])
    after = client.get(generated['preview_url'])
    assert before.status_code == after.status_code == 200
    assert before.content != after.content
    assert generated['generation']['source_id'] == original['id']
    assert generated['generation']['metadata']['model'] == 'test-double'
    stored = app.state.store.photo(original['id'])
    assert Path(stored['original_path']).read_bytes() == picture()
    assert generated['generation']['source_sha256'] == hashlib.sha256(picture()).hexdigest()
    export = client.post('/api/jobs', json={'ids': [generated_id]}).json()['job']
    done = wait_job(client, export['id'])
    sidecar = json.loads(Path(done['items'][0]['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['generation']['request']['seed'] == 18
    assert sidecar['output_dimensions'] == [generated['width'], generated['height']]


def test_cancelled_generation_is_not_imported(api, monkeypatch):
    client, app = api
    photo = import_one(client)
    controller = app.state.store.relight
    monkeypatch.setattr(controller, 'python', lambda: Path(sys.executable))
    def waiting(request, directory, job=None):
        for _ in range(200):
            if job['cancel_requested']:
                raise Cancelled()
            time.sleep(.01)
        raise AssertionError('Cancellation was not delivered')
    monkeypatch.setattr(controller, 'invoke', waiting)
    job = client.post('/api/relight/jobs', json={'ids': [photo['id']]}).json()['job']
    assert client.post('/api/relight/jobs/' + job['id'] + '/cancel').status_code == 200
    assert await_ai(client, job['id'])['status'] == 'cancelled'
    assert len(client.get('/api/photos').json()['photos']) == 1


def test_relight_routes_require_mutation_token(api, monkeypatch):
    client, app = api
    photo = import_one(client)
    client.headers.pop('X-PortraitLab-Token')
    assert client.post('/api/relight/jobs', json={'ids': [photo['id']]}).status_code == 401
    assert client.post('/api/relight/download').status_code == 401
