"""Offline download transaction tests: checksum failures must not enable files."""
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from experiments.portraitlab_v2.model_catalog import ModelCatalog


@pytest.mark.parametrize('valid',[True,False])
def test_fixed_download_is_checked_before_enabling_overlay(tmp_path,monkeypatch,valid):
    data=tmp_path/'照片数据';data.mkdir()
    external=tmp_path/'existing-models';external.mkdir();(external/'unchanged.bin').write_bytes(b'keep')
    store=SimpleNamespace(data=data,analysis_models=external,neural_models=external,ai_python=None,vendor=None,closing=False,
        lock=threading.RLock(),engine=None)
    models=ModelCatalog(store);payload=b'validated fixture weight';expected=hashlib.sha256(payload).hexdigest()
    record={'id':'fixture','group':'neural','name':'测试模型','relative_path':'fixture.bin','url':'https://example.invalid/fixed',
        'sha256':expected,'expected_bytes':len(payload)}
    class Download(io.BytesIO):headers={'Content-Length':str(len(payload))}
    monkeypatch.setattr('experiments.portraitlab_v2.model_catalog.urllib.request.urlopen',lambda *a,**k:Download(payload if valid else b'corrupt'))
    monkeypatch.setattr(models,'records',lambda:[])
    monkeypatch.setattr(models,'_check',lambda:None)
    models._download(record)
    if valid:
        assert models.download['status']=='complete'
        assert (data/'models/neural/fixture.bin').read_bytes()==payload
        assert store.neural_models==(data/'models/neural').resolve()
        saved=json.loads((data/'model-paths.json').read_text(encoding='utf-8'))
        assert saved['neural_models']=='models/neural'
    else:
        assert models.download['status']=='error'
        assert not (data/'models/neural/fixture.bin').exists()
        assert store.neural_models==external
        assert not (data/'model-paths.json').exists()
    assert (external/'unchanged.bin').read_bytes()==b'keep'
    assert not list(data.rglob('*.part'))
