"""Native pixel, state and HTTP acceptance checks; no model inference."""
import io
import json
from pathlib import Path
import sys
import threading

import numpy as np
from PIL import Image
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from experiments.portraitlab_v2.server import Store, create_app


@pytest.fixture
def photo(tmp_path):
    yy, xx = np.indices((707, 1031))
    rgb = np.stack([xx % 251, yy % 253, (xx + yy) % 255], axis=-1).astype(np.uint8)
    source = tmp_path/'input.png'
    Image.fromarray(rgb).save(source)
    store = Store(tmp_path/'data')
    info = store.import_path(source)
    identifier = info['id']
    folder = store.folder(identifier)/'analysis'/'test'
    folder.mkdir(parents=True)
    candidates = []
    for index, bounds in enumerate(([490, 490, 530, 530], [850, 50, 867, 70])):
        x0, y0, x1, y1 = bounds
        delta = np.zeros((y1-y0, x1-x0, 3), np.float32)
        delta[2:-2, 2:-2] = [4.5, -9.25, 13.25]
        name = f'c{index}.npz'
        np.savez_compressed(folder/name, delta=delta)
        candidates.append({'id':f'c{index}', 'bounds':bounds, 'patch':name, 'score':.8, 'default_enabled':False})
    store.photos[identifier].update(candidates=candidates, analysis_generation='test', analysis={'status':'ready'})
    store.persist()
    yield store, identifier, rgb
    store.close()


def unpack(data):
    return np.asarray(Image.open(io.BytesIO(data)))


def assemble(store, identifier, kind, level=0):
    p = store.public(identifier)
    w, h = (p['width'] + 2**level - 1)//2**level, (p['height'] + 2**level - 1)//2**level
    output = np.zeros((h, w, 3), np.uint8)
    for y in range((h+511)//512):
        for x in range((w+511)//512):
            tile = unpack(store.tile(identifier, kind, level, x, y))
            output[y*512:y*512+tile.shape[0], x*512:x*512+tile.shape[1]] = tile
    return output


def test_native_tiles_equal_source_including_non_square_edges(photo):
    store, identifier, rgb = photo
    assert store.public(identifier)['selected_ids'] == []
    assert np.array_equal(assemble(store,identifier,'original'), rgb)
    assert np.array_equal(assemble(store,identifier,'result'), rgb)


def test_candidate_only_changes_support_and_export_matches_tiles(photo):
    store, identifier, rgb = photo
    store.select(identifier, {'selected_ids':['c0'], 'strength':.7, 'expected_version':0})
    result = assemble(store,identifier,'result')
    change = np.any(result != rgb, axis=2)
    assert change.sum() == 36*36
    assert not change[:490].any() and not change[530:].any()
    assert np.array_equal(result[50:70,850:867], rgb[50:70,850:867])
    exported = store.export(identifier)
    assert np.array_equal(np.asarray(Image.open(exported['path'])), result)
    metadata = json.loads(Path(exported['path']).with_suffix('.json').read_text(encoding='utf-8'))
    assert metadata['selected_ids'] == ['c0']


def test_unaffected_tile_reuses_cache_and_stale_result_rejected(photo):
    store, identifier, _ = photo
    original = store.tile(identifier,'result',0,1,0)
    store.select(identifier, {'selected_ids':['c0']})
    # c0 crosses this tile; a distant edge tile should retain its cached bytes.
    edge = store.tile(identifier,'result',0,2,0)
    store.select(identifier, {'selected_ids':['c0','c1']})
    assert store.tile(identifier,'result',0,2,0) is edge
    assert store.tile_metrics[-1]['cached'] is True
    assert store.tile(identifier,'result',0,1,0) != original
    with pytest.raises(HTTPException) as error:
        store.tile(identifier,'result',0,1,0,expected_version=0)
    assert error.value.status_code == 409
    store.tile(identifier,'original',0,1,0,expected_version=0)


@pytest.mark.parametrize('invalid', [
    {'selected_ids':['missing']}, {'selected_ids':['c0','c0']},
    {'selected_ids':['c0'],'strength':float('nan')}, {'selected_ids':['c0'],'strength':True},
    {'selected_ids':['c0'],'strength':1.01}, {'selected_ids':['c0'],'typo':1}, [],
])
def test_invalid_updates_are_atomic(photo, invalid):
    store, identifier, _ = photo
    before = store.public(identifier)
    with pytest.raises(ValueError):store.select(identifier, invalid)
    assert store.public(identifier) == before


def test_version_conflict_history_and_restart(photo):
    store, identifier, rgb = photo
    store.select(identifier, {'selected_ids':['c0'], 'expected_version':0})
    with pytest.raises(HTTPException):store.select(identifier, {'selected_ids':[], 'expected_version':0})
    with pytest.raises(HTTPException):store.history(identifier,'undo',expected_version=0)
    with pytest.raises(HTTPException):store.export(identifier,expected_version=0)
    assert store.history(identifier,'undo')['selected_ids'] == []
    assert np.array_equal(assemble(store,identifier,'result'), rgb)
    assert store.history(identifier,'redo')['selected_ids'] == ['c0']
    restored = Store(store.data)
    try:
        assert restored.public(identifier)['history'] == store.public(identifier)['history']
        assert np.array_equal(assemble(restored,identifier,'result'), assemble(store,identifier,'result'))
        assert restored.history(identifier,'undo')['selected_ids'] == []
    finally:restored.close()


def test_snapshot_does_not_mix_analysis_generations(photo):
    store, identifier, rgb = photo
    store.select(identifier, {'selected_ids':['c0']})
    snapshot = json.loads(json.dumps(store.photos[identifier]))
    expected = store.region(identifier,(0,0,1031,707),snapshot=snapshot)
    store.photos[identifier].update(analysis_generation='other',candidates=[])
    assert np.array_equal(store.region(identifier,(0,0,1031,707),snapshot=snapshot), expected)
    assert not np.array_equal(expected,rgb)


def test_http_import_and_local_protection(tmp_path):
    app = create_app(tmp_path/'http-data')
    stream = io.BytesIO();Image.new('RGB',(32,24),(20,40,60)).save(stream,format='PNG')
    with TestClient(app,base_url='http://127.0.0.1:18891') as client:
        token = client.get('/api/bootstrap').json()['token']
        headers = {'X-PortraitLab-Token':token}
        assert client.get('/api/bootstrap',headers={'Host':'evil.example'}).status_code == 403
        assert client.get('/api/bootstrap',headers={'Origin':'http://evil.example'}).status_code == 403
        assert client.post('/api/import',files={'files':('photo.png',stream.getvalue(),'image/png')}).status_code == 403
        response = client.post('/api/import',headers=headers,files={'files':('photo.png',stream.getvalue(),'image/png')})
        assert response.status_code == 200, response.text
        identifier = response.json()['photos'][0]['id']
        assert client.get(f'/api/photos/{identifier}/candidates/missing/thumbnail').status_code == 404
        assert client.get(f'/api/photos/{identifier}/faces/999/thumbnail').status_code == 404
        assert client.get('/api/exports/missing.png').status_code == 404
        assert client.post(f'/api/photos/{identifier}/analyze',headers=headers,json={}).status_code == 400
        assert client.get(f'/api/photos/{identifier}').json()['analysis']['status'] == 'idle'
        assert client.post(f'/api/photos/{identifier}/selection',headers=headers,json=[]).status_code == 400
        assert client.get(f'/api/photos/{identifier}/tiles/original/0/0/0.png?v=0').status_code == 200
        assert client.post(f'/api/photos/{identifier}/export',headers=headers,json={}).status_code == 400
        assert client.post(f'/api/photos/{identifier}/undo',headers=headers,json={'expected_version':1}).status_code == 409
        assert client.post(f'/api/photos/{identifier}/export',headers=headers,json={'expected_version':0}).status_code == 200
        mixed=client.post('/api/import',headers=headers,files=[
            ('files',('good.png',stream.getvalue(),'image/png')),
            ('files',('bad.png',b'invalid image','image/png'))])
        assert mixed.status_code == 200
        assert len(mixed.json()['photos']) == len(mixed.json()['errors']) == 1


def test_worker_failure_is_visible_and_recoverable(photo, monkeypatch):
    store, identifier, _ = photo
    class Engine:
        def analyze(self, _):raise RuntimeError('test model unavailable')
        def close(self):pass
    store.engine = Engine()
    store._analyze(identifier,None,'cpu')
    assert store.public(identifier)['analysis']['status'] == 'error'
    assert 'test model unavailable' in store.public(identifier)['analysis']['message']
    assert store.select(identifier,{'selected_ids':[]})['selected_ids'] == []


def test_shutdown_prevents_late_worker_spawn(photo, monkeypatch):
    store, identifier, _ = photo
    class Engine:
        def analyze(self, _):
            store.closing=True
            return {}
        def close(self):pass
    store.engine=Engine()
    def forbidden(*args, **kwargs):raise AssertionError('worker spawned after closing')
    monkeypatch.setattr('experiments.portraitlab_v2.server.subprocess.Popen',forbidden)
    store._analyze(identifier,None,'cpu')
    assert store.public(identifier)['analysis']['status'] == 'error'
    assert '取消' in store.public(identifier)['analysis']['message']


def manual_stub(store, monkeypatch, *, fail=False):
    """Actual sparse brush renderer with a deterministic local repair predictor."""
    from experiments.portraitlab_v2.manual_repair import run_manual_request
    class Predictor:
        device='cpu';device_name='test';load_seconds=0
        def repair(self,rgb,mask,bounds):
            x0,y0,x1,y1=bounds
            return np.clip(rgb[y0:y1,x0:x1].astype(np.float32)+[20,-12,8],0,255)
        def detect(self,*args):raise AssertionError('manual repair must not call detection')
    def work(request):
        if fail:raise RuntimeError('test repair failure')
        # Protocol output is generated by the same adapter, using test weights metadata.
        result=run_manual_request(request,predictor=Predictor())
        return {'ok':True,'model_reused':True}
    store.ai_python=Path('test-python');store.vendor=store.data/'vendor';store.neural_models=store.data/'models'
    store.vendor.mkdir(exist_ok=True);(store.vendor/'LICENSE').write_text('test fixture',encoding='utf-8')
    monkeypatch.setattr(store,'_request_worker',work)
    class Immediate:
        def submit(self,fn,*args):fn(*args)
        def shutdown(self,**kwargs):pass
    store.pool.shutdown();store.pool=Immediate()


def test_manual_is_undoable_as_one_operation_and_idempotent(photo,monkeypatch):
    store,identifier,rgb=photo;manual_stub(store,monkeypatch)
    body={'expected_version':0,'request_id':'stable-client-id','strokes':[{'radius':10,'points':[[510,508],[523,518]]}],'device':'cpu'}
    p=store.manual(identifier,body)
    assert p['analysis']['status']=='ready',p['analysis']
    ids=p['selected_ids'];assert ids and len(p['history'])==1
    assert all(c['kind']=='manual' for c in p['candidates'] if c['id'] in ids)
    result=assemble(store,identifier,'result');assert np.any(result!=rgb)
    assert not np.any(result[:498]!=rgb[:498])
    assert store.manual(identifier,body)['version']==p['version']
    with pytest.raises(HTTPException):store.manual(identifier,{**body,'strokes':[{'radius':12,'points':[[510,508]]}]})
    assert store.history(identifier,'undo')['selected_ids']==[]
    assert np.array_equal(assemble(store,identifier,'result'),rgb)
    assert store.history(identifier,'redo')['selected_ids']==ids
    assert np.array_equal(assemble(store,identifier,'result'),result)
    p=store.public(identifier)
    store.select(identifier,{'selected_ids':[],'expected_version':p['version']})
    assert np.array_equal(assemble(store,identifier,'result'),rgb)
    store.history(identifier,'undo')
    assert np.array_equal(assemble(store,identifier,'result'),result)


def test_manual_failure_preserves_prior_edits_and_rejects_stale_version(photo,monkeypatch):
    store,identifier,rgb=photo;manual_stub(store,monkeypatch,fail=True)
    before=store.select(identifier,{'selected_ids':['c0']})
    body={'request_id':'failure','expected_version':0,'strokes':[{'radius':10,'points':[[510,510]]}]}
    with pytest.raises(HTTPException):store.manual(identifier,body)
    body['expected_version']=before['version']
    p=store.manual(identifier,body)
    assert p['analysis']['status']=='error'
    assert p['selected_ids']==before['selected_ids'] and p['history']==before['history']
    assert p['version']==before['version']


def test_manual_overlay_avoids_double_application_and_can_reveal_prior_layer(photo,monkeypatch):
    store,identifier,rgb=photo;manual_stub(store,monkeypatch)
    p=store.select(identifier,{'selected_ids':['c0'],'strength':1})
    prior=assemble(store,identifier,'result')
    p=store.manual(identifier,{'expected_version':p['version'],'strokes':[{'radius':15,'points':[[510,510]]}]})
    assert p['analysis']['status']=='ready',p['analysis']
    result=assemble(store,identifier,'result')
    expected=np.clip(rgb[510,510].astype(float)+[20,-12,8],0,255).astype(np.uint8)
    assert np.array_equal(result[510,510],expected)
    # Selection ordering does not reorder immutable layers.
    p=store.select(identifier,{'selected_ids':list(reversed(p['selected_ids']))})
    assert np.array_equal(assemble(store,identifier,'result'),result)
    p=store.select(identifier,{'selected_ids':['c0']})
    assert np.array_equal(assemble(store,identifier,'result'),prior)


def test_manual_can_start_before_automatic_analysis_and_survives_restart(photo,monkeypatch):
    store,identifier,rgb=photo;manual_stub(store,monkeypatch)
    p=store.photos[identifier];p.update(candidates=[],analysis_generation='none',analysis={'status':'idle'})
    p=store.manual(identifier,{'expected_version':0,'strokes':[{'radius':8,'points':[[100,100]]}]})
    assert p['analysis']['status']=='ready',p['analysis']
    assert p['analysis_generation']!='none'
    expected=assemble(store,identifier,'result')
    restored=Store(store.data)
    try:assert np.array_equal(assemble(restored,identifier,'result'),expected)
    finally:restored.close()


def test_manual_commit_disk_failure_does_not_apply_any_edits(photo,monkeypatch):
    store,identifier,_=photo;manual_stub(store,monkeypatch)
    previous=store.public(identifier);real_persist=store.persist;calls=0
    def disk_full():
        nonlocal calls
        calls+=1
        if calls>1:raise OSError('disk full')
        real_persist()
    monkeypatch.setattr(store,'persist',disk_full)
    p=store.manual(identifier,{'expected_version':0,'strokes':[{'radius':8,'points':[[100,100]]}]})
    assert p['analysis']['status']=='error'
    for key in ['candidates','selected_ids','history','redo','version','analysis_generation']:
        assert p[key]==previous[key]


def test_reset_disk_failure_keeps_visible_prior_result(photo,monkeypatch):
    store,identifier,_=photo
    before=store.select(identifier,{'selected_ids':['c0']})
    def failure():raise OSError('disk full')
    monkeypatch.setattr(store,'persist',failure)
    with pytest.raises(OSError):store.select(identifier,{'selected_ids':[]})
    assert store.public(identifier)==before
