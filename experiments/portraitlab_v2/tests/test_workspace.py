"""Editing acceptance: state isolation, native pixels and reversible operations."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from experiments.portraitlab_v2.server import Store,create_app
from experiments.portraitlab_v2.workspace import defaults,save_analysis
from experiments.portraitlab_v2 import lighting_v2 as lighting


@pytest.fixture
def scene(tmp_path):
    yy,xx=np.indices((180,250))
    rgb=np.stack([60+xx%100,45+yy%90,30+(xx+yy)%80],-1).astype(np.uint8)
    path=tmp_path/'source.png';Image.fromarray(rgb).save(path)
    store=Store(tmp_path/'data');identifier=store.import_path(path)['id']
    yield store,identifier,rgb
    store.close()


def preview(store,identifier,settings):
    return store.workspace.preview(identifier,{'expected_version':store.photos[identifier]['version'],'settings':settings})


def commit(store,identifier):
    p=store.public(identifier)
    return store.workspace.commit(identifier,{'expected_version':p['version'],'preview_id':p['draft']['id']})


def whole(store,identifier,**kwargs):
    p=store.photos[identifier]
    return store.region(identifier,(0,0,p['width'],p['height']),**kwargs)


def test_draft_is_not_exported_and_commit_is_undoable(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.6
    p=preview(store,i,s)
    assert p['version']==0 and np.array_equal(whole(store,i),rgb)
    with pytest.raises(HTTPException):store.export(i,0)
    proposed=np.asarray(Image.open(io.BytesIO(store.tile(i,'preview',0,0,0,0,p['draft']['id']))))
    assert np.any(proposed!=rgb)
    commit(store,i)
    assert np.array_equal(proposed,whole(store,i))
    exported=np.asarray(Image.open(store.export(i,1)['path']))
    assert np.array_equal(exported,proposed)
    store.history(i,'undo',1);assert np.array_equal(whole(store,i),rgb)
    store.history(i,'redo',2);assert np.array_equal(whole(store,i),proposed)


def test_stale_draft_and_mutations_cannot_mix(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.2
    old=preview(store,i,s);s['retouch']['exposure']=.7;new=preview(store,i,s)
    with pytest.raises(HTTPException):store.tile(i,'preview',0,0,0,0,old['draft']['id'])
    with pytest.raises(HTTPException):store.workspace.commit(i,{'expected_version':0,'preview_id':old['draft']['id']})
    for mutation in [lambda:store.select(i,{'selected_ids':[]}),lambda:store.history(i,'undo'),lambda:store.workspace.remove([i])]:
        with pytest.raises(HTTPException):mutation()
    store.workspace.discard(i,{'expected_version':0,'preview_id':new['draft']['id']})
    assert not store.public(i)['draft'] and np.array_equal(whole(store,i),rgb)


def test_local_adjustment_adds_to_global_without_spilling(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.25
    preview(store,i,s);commit(store,i);global_result=whole(store,i)
    s['local'].update(enabled=True,rect=[50,40,140,130],feather=12)
    s['local']['params']['exposure']=.6
    preview(store,i,s);commit(store,i);local_result=whole(store,i)
    outside=np.ones(rgb.shape[:2],bool);outside[40:130,50:140]=False
    assert np.array_equal(local_result[outside],global_result[outside])
    assert np.any(local_result[60:110,70:120]!=global_result[60:110,70:120])


def test_protection_and_erasing_restore_only_masked_pixels(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.7
    s['protection']=[{'mode':'protect','radius':24,'points':[[100,90]]}]
    preview(store,i,s);commit(store,i);protected=whole(store,i)
    assert np.array_equal(protected[90,100],rgb[90,100])
    assert np.any(protected[20,20]!=rgb[20,20])
    s['protection'].append({'mode':'restore','radius':8,'points':[[100,90]]})
    preview(store,i,s);commit(store,i);erased=whole(store,i)
    assert np.any(erased[90,100]!=rgb[90,100])
    assert np.array_equal(erased[90,115],rgb[90,115])


def test_reset_restores_original_and_single_undo_restores_every_layer(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['temperature']=.5
    s['geometry']['strokes']=[{'mode':'push','x':.5,'y':.5,'dx':.015,'dy':0,'radius':.16,'strength':.7}]
    preview(store,i,s);commit(store,i);before=whole(store,i)
    assert np.any(before!=rgb)
    p=store.workspace.reset(i,{'expected_version':1})
    assert p['version']==2 and np.array_equal(whole(store,i),rgb)
    store.history(i,'undo',2);assert np.array_equal(whole(store,i),before)


def test_warped_native_tiles_match_export_and_restart(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.25
    s['geometry']['strokes']=[{'mode':'push','x':.5,'y':.5,'dx':.03,'dy':-.02,'radius':.24,'strength':.7}]
    preview(store,i,s);commit(store,i);full=whole(store,i)
    tiled=np.empty_like(full)
    for y in range(0,180,47):
        for x in range(0,250,61):
            r,b=min(x+61,250),min(y+47,180)
            tiled[y:b,x:r]=store.region(i,(x,y,r,b))
    assert np.array_equal(tiled,full)
    other=Store(store.data)
    try:assert np.array_equal(whole(other,i),full)
    finally:other.close()


def test_subject_background_lighting_separation(scene):
    store,i,rgb=scene;directory=store.folder(i)/'geometry';directory.mkdir()
    normal=np.zeros((18,25,3),np.float32);normal[...,2]=1
    mask=np.zeros((18,25),np.float32);mask[:,:12]=1
    np.savez(directory/'geometry.npz',normal=normal,depth=np.zeros((18,25),np.float32),subject_mask=mask)
    store.photos[i]['_assets']['lighting']=str(directory)
    store.photos[i]['preparation']['geometry_ready']=True
    s=defaults();s['lighting']['enabled']=True;s['lighting']['subject']['ambient']=.6
    preview(store,i,s);commit(store,i);result=whole(store,i)
    assert np.array_equal(result[:,170:],rgb[:,170:])
    assert np.any(result[:,:70]!=rgb[:,:70])


def test_missing_dependencies_reject_adjustments_atomically(scene):
    store,i,rgb=scene
    for key in ['skin_even','texture_soften','eye_brighten','mark_fade']:
        s=defaults();s['retouch'][key]=.5
        with pytest.raises(HTTPException):preview(store,i,s)
        assert store.public(i)['draft'] is None
    assert np.array_equal(whole(store,i),rgb)


def test_zero_result_matches_original_at_every_mip(scene):
    store,i,rgb=scene
    for level in range(store.public(i)['levels']):
        a=np.asarray(Image.open(io.BytesIO(store.tile(i,'original',level,0,0))))
        b=np.asarray(Image.open(io.BytesIO(store.tile(i,'result',level,0,0))))
        assert np.array_equal(a,b)


def test_interaction_proxy_never_replaces_native_tiles_or_export(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.5
    p=preview(store,i,s);draft=p['draft']['id']
    a=store.tile(i,'preview',0,0,0,0,draft,'interactive')
    b=store.tile(i,'preview',0,0,0,0,draft,'native')
    assert a==b
    commit(store,i)
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(a))),np.asarray(Image.open(store.export(i,1)['path'])))


def test_archive_keeps_source_and_edits(scene):
    store,i,rgb=scene;s=defaults();s['retouch']['exposure']=.3;preview(store,i,s);commit(store,i)
    state=deepcopy(store.photos[i]);store.workspace.remove([i])
    assert store.photos[i]['archived'] and store.folder(i).joinpath('original.npy').is_file()
    assert store.photos[i]['settings']==state['settings']
    assert np.array_equal(np.load(store.folder(i)/'original.npy'),rgb)
    with pytest.raises(HTTPException):preview(store,i,s)


def test_batch_preset_matches_single_resolution_and_undo(scene,tmp_path):
    from experiments.portraitlab_v2 import retouch_engine
    store,i,rgb=scene;source=tmp_path/'second.png';Image.fromarray(rgb).save(source)
    other=store.import_path(source)['id'];preset='general-indoor-yellow'
    expected=retouch_engine.resolve_preset(preset,strength=.4)['params']
    store.workspace.apply_batch({'photo_ids':[i,other],'preset_id':preset,'strength':.4,'device':'cpu'})
    started=time.monotonic()
    while store.workspace.batch['status']=='running':
        if time.monotonic()-started>15:raise TimeoutError('batch')
        time.sleep(.01)
    assert store.workspace.batch['done']==2
    assert all(item['status']=='done' for item in store.workspace.batch['items'])
    for identifier in (i,other):
        p=store.public(identifier)
        assert p['settings']['retouch']==expected and not p['batch_busy']
        store.history(identifier,'undo',p['version'])
        assert np.array_equal(whole(store,identifier),rgb)


def test_http_catalog_and_transactions(tmp_path):
    app=create_app(tmp_path/'http')
    with TestClient(app,base_url='http://127.0.0.1:18891') as client:
        headers={'X-PortraitLab-Token':client.get('/api/bootstrap').json()['token']}
        catalog=client.get('/api/presets').json()
        assert len(catalog['categories'])==4 and len(catalog['presets'])==32
        assert len(catalog['lighting_presets'])==12
        assert len(client.get('/api/models').json()['models'])==5
        stream=io.BytesIO();Image.new('RGB',(40,30),(60,70,80)).save(stream,format='PNG')
        i=client.post('/api/import',headers=headers,files={'files':('photo.png',stream.getvalue(),'image/png')}).json()['photos'][0]['id']
        s=defaults();s['retouch']['exposure']=.4
        proposed=client.post(f'/api/photos/{i}/preview',headers=headers,json={'expected_version':0,'settings':s})
        assert proposed.status_code==200,proposed.text
        draft=proposed.json()['draft']['id']
        assert client.get(f'/api/photos/{i}/tiles/preview/0/0/0.png?v=0&draft={draft}').status_code==200
        assert client.post(f'/api/photos/{i}/commit',headers=headers,json={'expected_version':0,'preview_id':draft}).status_code==200
        assert client.post(f'/api/photos/{i}/reset',headers=headers,json={'expected_version':1}).status_code==200
        assert client.delete(f'/api/photos/{i}',headers=headers).status_code in (400,422)
        assert client.post('/api/photos/remove',headers=headers,json={'photo_ids':[i]}).status_code==200
        assert client.get('/api/bootstrap').json()['photos']==[]
