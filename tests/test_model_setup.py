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
        return {'ready': (self.models_dir / 'sample/model.bin').exists()}


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
    assert not list(tmp_path.rglob('*.part'))


def test_installer_does_not_activate_bad_download(tmp_path, monkeypatch):
    setup = run_install(tmp_path, monkeypatch, '0'*64)
    assert setup.status()['installation']['status'] == 'failed'
    assert not setup.status()['models']['ready']
    assert not list(tmp_path.rglob('*.part'))
