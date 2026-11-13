import hashlib
import io
from pathlib import Path
import time

from portraitlab.model_setup import ModelSetup
from portraitlab.server import writable_path


class Engine:
    def __init__(self, root):
        self.models_dir = root
    def status(self):
        exists = (self.models_dir / 'sample/model.bin').is_file()
        return {'ready': exists, 'models': {'sample': {'exists': exists, 'verified': False}}}


class Reply(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


def run_install(tmp_path, monkeypatch, digest):
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT', str(tmp_path))
    payload = b'model-bytes-for-installer-integrity-test'
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Reply(payload))
    setup = ModelSetup(Engine(tmp_path/'models'), {'sample': {'filename': 'model.bin', 'url': 'https://example.invalid/model', 'sha256': digest}}, writable_path)
    setup.start()
    setup.thread.join(timeout=5)
    assert not setup.thread.is_alive()
    return setup


def test_installer_accepts_only_matching_digest(tmp_path, monkeypatch):
    digest = hashlib.sha256(b'model-bytes-for-installer-integrity-test').hexdigest()
    setup = run_install(tmp_path, monkeypatch, digest)
    assert setup.status()['installation']['status'] == 'completed'
    assert setup.status()['models']['ready']
    assert setup.status()['models']['preparation_state'] == 'ready'
    assert setup.status()['models']['models']['sample']['verified']
    assert setup.status()['installation']['phase'] == 'ready'
    assert not list(tmp_path.rglob('*.part'))


def test_existing_download_is_verified_without_network(tmp_path, monkeypatch):
    payload = b'already-downloaded-model'
    path = tmp_path / 'models/sample/model.bin'
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    def no_network(*args, **kwargs):
        raise AssertionError('Local valid model must not access the network')
    monkeypatch.setattr('urllib.request.urlopen', no_network)
    setup = ModelSetup(Engine(tmp_path / 'models'), {'sample': {'filename': 'model.bin', 'url': 'https://example.invalid/model', 'sha256': hashlib.sha256(payload).hexdigest()}}, lambda p: p)
    assert setup.status()['models']['preparation_state'] == 'downloaded'
    setup.start()
    setup.thread.join(timeout=5)
    assert setup.status()['models']['preparation_state'] == 'ready'
    assert setup.status()['installation']['total_bytes'] == 0
    path.write_bytes(b'modified-after-verification')
    assert setup.status()['models']['preparation_state'] == 'downloaded'
    assert not setup.status()['models']['models']['sample']['verified']
    path.unlink()
    assert setup.status()['models']['preparation_state'] == 'missing'


def test_download_progress_and_failure_do_not_leave_stale_percentage(tmp_path, monkeypatch):
    payload = b'valid-model'
    setup = ModelSetup(Engine(tmp_path / 'models'), {'sample': {'filename': 'model.bin', 'url': 'https://example.invalid/model', 'sha256': hashlib.sha256(payload).hexdigest()}}, lambda p: p)
    def failing_network(*args, **kwargs):
        assert setup.status()['installation']['phase'] == 'downloading'
        raise OSError('Connection unavailable')
    monkeypatch.setattr('urllib.request.urlopen', failing_network)
    setup.start()
    setup.thread.join(timeout=5)
    status = setup.status()
    assert status['installation']['phase'] == 'failed'
    assert status['installation']['bytes'] == status['installation']['total_bytes'] == 0
    assert status['models']['preparation_state'] == 'missing'


def test_installer_does_not_activate_bad_download(tmp_path, monkeypatch):
    setup = run_install(tmp_path, monkeypatch, '0'*64)
    assert setup.status()['installation']['status'] == 'failed'
    assert not setup.status()['models']['ready']
    assert not list(tmp_path.rglob('*.part'))
