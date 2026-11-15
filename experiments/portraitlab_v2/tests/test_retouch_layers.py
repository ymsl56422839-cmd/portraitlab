"""Unified V2 acceptance cases, authored before the coordinated test phase.

No downloads, real model loads, browser launches, or GPU benchmarks occur here.
Call pytest with its basetemp and all configurable caches on the task's F drive.
"""
from collections import Counter
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from experiments.portraitlab_v2 import model_catalog as catalog
from experiments.portraitlab_v2 import retouch_engine as retouch
from experiments.portraitlab_v2 import skin_model


def portrait_fixture(height=143,width=187):
    yy,xx=np.mgrid[:height,:width]
    rgb=np.stack([100+xx%70,80+yy%60,70+(xx+yy)%65],axis=2).astype(np.uint8)
    mask=np.zeros((height,width),np.float32);mask[5:-5,5:-5]=1
    prepared=retouch.prepare_analysis(rgb,{'face_mask':mask,'skin_mask':mask})
    prepared['face_width']=180.
    return rgb,prepared


def test_neutral_parameters_are_byte_exact_without_face_analysis():
    rgb,_=portrait_fixture()
    rendered=retouch.render_region(rgb,[0,0,rgb.shape[1],rgb.shape[0]],{},analysis={})
    np.testing.assert_array_equal(rendered,rgb)
    zero=retouch.render_region(rgb,[7,9,46,55],{'amount':0,'skin_even':1},analysis={})
    np.testing.assert_array_equal(zero,rgb[9:55,7:46])


def test_linear_reader_never_quantizes_between_layers():
    rgb=np.zeros((17,23,3),np.uint8)
    value=np.array([.1834567,.3176543,1.125678],np.float32)
    def reader(bounds):
        x0,y0,x1,y1=bounds
        return np.broadcast_to(value,(y1-y0,x1-x0,3)).copy()
    rendered=retouch.render_region(rgb,[2,3,18,14],{'exposure':.125},analysis={},
                                  region_reader=reader,reader_linear=True,linear_output=True)
    assert rendered.dtype==np.float32
    np.testing.assert_allclose(rendered,np.broadcast_to(value*np.float32(2**.125),rendered.shape),atol=1e-7,rtol=1e-6)
    assert rendered[...,2].min()>1
    rounded=retouch.srgb_to_linear(retouch.encode_srgb(value)/255)
    assert np.max(np.abs(value-rounded))>.01


def test_sequential_spatial_halo_matches_whole_frame():
    rgb,prepared=portrait_fixture(257,319)
    params={'texture_soften':.35,'tone_even':.22,'redness':.28,'shine_reduce':.3,'fine_lines':.2}
    whole=retouch.render_region(rgb,[0,0,319,257],params,prepared,linear_output=True)
    tiled=np.empty_like(whole)
    for bounds,result in retouch.iter_render_tiles(rgb,params,prepared,tile_size=64,linear_output=True):
        x0,y0,x1,y1=bounds;tiled[y0:y1,x0:x1]=result
    np.testing.assert_allclose(tiled,whole,atol=3e-5,rtol=2e-5)


@pytest.mark.parametrize('parameter',['texture_soften','tone_even','redness','eye_brighten','fine_lines'])
def test_portrait_layers_refuse_missing_regions(parameter):
    rgb=np.full((20,22,3),100,np.uint8)
    with pytest.raises(ValueError,match='分析'):
        retouch.render_region(rgb,[0,0,22,20],{parameter:.3},{})


def test_neural_and_mark_layers_require_explicit_preparation():
    rgb,prepared=portrait_fixture()
    bounds=[0,0,rgb.shape[1],rgb.shape[0]]
    with pytest.raises(ValueError,match='神经均肤'):
        retouch.render_region(rgb,bounds,{'skin_even':.4},prepared)
    with pytest.raises(ValueError,match='选定'):
        retouch.render_region(rgb,bounds,{'mark_fade':.4},prepared)
    with pytest.raises(ValueError,match='确认'):
        retouch.render_region(rgb,bounds,{'mark_fade':.4},prepared,
                              mark_region_reader=lambda _:np.ones(rgb.shape[:2],np.float32))


def test_sparse_confirmed_mark_reader_changes_only_its_support():
    rgb=np.full((73,81,3),110,np.uint8);rgb[34:39,38:43]=[180,70,65]
    prepared=retouch.prepare_analysis(rgb,{'face_mask':np.ones((73,81),np.float32),'skin_mask':np.ones((73,81),np.float32)})
    def reader(bounds):
        x0,y0,x1,y1=bounds
        result=np.zeros((y1-y0,x1-x0),np.float32)
        a,b,c,d=max(38,x0),max(34,y0),min(43,x1),min(39,y1)
        if a<c and b<d:result[b-y0:d-y0,a-x0:c-x0]=1
        return result
    result=retouch.render_region(rgb,[0,0,81,73],{'mark_fade':.7},prepared,
                                  mark_region_reader=reader,mark_region_available=True)
    support=reader([0,0,81,73])>0
    assert np.any(result[support]!=rgb[support])
    np.testing.assert_array_equal(result[~support],rgb[~support])


def test_catalog_has_32_distinct_scoped_recipes_and_review_actions():
    presets=retouch.list_presets()
    assert len(presets)==32 and len({p['id'] for p in presets})==32
    assert Counter(p['category'] for p in presets)=={'general':8,'senior':8,'adult':8,'youth':8}
    signatures={tuple(sorted(p['params'].items())) for p in presets}
    assert len(signatures)==32
    for preset in presets:
        resolved=retouch.resolve_preset(preset['id'],{'exposure':.73},strength=.5)
        retouch.validate_params(resolved['params'])
        again=retouch.resolve_preset(preset['id'],resolved['params'],strength=.5)
        assert again['params']==resolved['params']
        if 'exposure' not in preset['params']:assert resolved['params']['exposure']==.73
        if resolved['params']['skin_even']>0:assert 'prepare_skin' in resolved['needs']
        if 'review_blemishes' in preset['actions']:
            assert resolved['warnings'] and 'mark_fade' not in preset['params']


class FakeSkin:
    device='cpu';device_name='test double';load_seconds=0
    def predict_curve(self,rgb):return np.full((512,512,3),.6,np.float32)


def test_skin_field_contract_protects_zero_mask_and_rejects_tampering(tmp_path,monkeypatch):
    rgb,prepared=portrait_fixture(27,31)
    source=tmp_path/'input.npy';np.save(source,rgb,allow_pickle=False)
    mask=np.zeros((27,31),np.float32);mask[9:17,10:20]=1
    mask_path=tmp_path/'mask.npy';np.save(mask_path,mask,allow_pickle=False)
    vendor=tmp_path/'vendor';vendor.mkdir();(vendor/'LICENSE').write_text('fixture license',encoding='utf-8')
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT',str(tmp_path))
    monkeypatch.setenv('PORTRAITLAB_WORKER_CACHE_ROOT',str(tmp_path/'cache'))
    request={'kind':'skin','input':str(source),'roi':[2,3,29,24],'allowed_mask':str(mask_path),
             'vendor':str(vendor),'models':str(tmp_path),'output':str(tmp_path/'result')}
    meta=skin_model.run_skin_request(request,FakeSkin())
    fields=skin_model.load_skin_fields(tmp_path/'result')
    assert meta['kind']=='skin' and fields['fields'][0]['skin_feature'].shape==(512,512,3)
    result=retouch.render_region(rgb,[0,0,31,27],{'skin_even':.7},prepared,skin_fields=fields)
    np.testing.assert_array_equal(result[mask==0],rgb[mask==0])
    assert np.any(result[mask>0]!=rgb[mask>0])
    fields['source_rgb_sha256']='wrong-source'
    with pytest.raises(ValueError,match='原片'):
        retouch.render_region(rgb,[0,0,31,27],{'skin_even':.7},prepared,skin_fields=fields)
    patch=tmp_path/'result/skin-field.npz';patch.write_bytes(patch.read_bytes()+b'tampered')
    with pytest.raises(ValueError,match='校验'):
        skin_model.load_skin_fields(tmp_path/'result')


def fake_store(data):
    data.mkdir(parents=True,exist_ok=True)
    executable=data/'fake-python.exe';executable.write_bytes(b'fixture interpreter marker')
    return SimpleNamespace(data=data,analysis_models=None,neural_models=None,vendor=None,ai_python=executable,
                           lock=threading.RLock(),closing=False,engine=None)


@pytest.mark.parametrize('response',[{'ok':True},{'ok':True,'cuda_available':'false','torch_version':'2'},
    {'ok':True,'cuda_available':False},{'ok':True,'cuda_available':True,'torch_version':'2','gpu_name':None}])
def test_device_errors_do_not_claim_cpu_readiness(tmp_path,monkeypatch,response):
    store=fake_store(tmp_path/'data');store._request_worker=lambda *args,**kwargs:response
    models=catalog.ModelCatalog(store)
    monkeypatch.setattr(models,'records',lambda:[])
    monkeypatch.setattr(models,'dependencies',lambda:{'ai_runtime':{'state':'unverified'}})
    monkeypatch.setattr(catalog.importlib,'import_module',lambda _:SimpleNamespace(tasks=object()))
    models._check()
    assert models.devices['status']=='error'
    assert models.devices['cuda_available'] is None and models.devices['torch_version'] is None


def test_overlay_config_survives_data_move_without_overriding_external_sources(tmp_path):
    original=tmp_path/'first';store=fake_store(original)
    external=tmp_path/'external-source';external.mkdir();store.analysis_models=external
    models=catalog.ModelCatalog(store);overlay=models._overlay_root('neural');overlay.mkdir(parents=True)
    models._persist_overlay('neural_models',overlay)
    saved=json.loads((original/'model-paths.json').read_text(encoding='utf-8'))
    assert saved['neural_models']=='models/neural' and saved['analysis_models'] is None
    moved=tmp_path/'moved';replacement=fake_store(moved);replacement.analysis_models=external
    (moved/'models/neural').mkdir(parents=True)
    (moved/'model-paths.json').write_bytes((original/'model-paths.json').read_bytes())
    catalog.ModelCatalog(replacement)
    assert replacement.neural_models==moved/'models/neural'
    assert replacement.analysis_models==external


@pytest.mark.parametrize('configuration',['{broken',json.dumps({'schema_version':2,'neural_models':'../outside'})])
def test_broken_overlay_config_preserves_launch_paths(tmp_path,configuration):
    store=fake_store(tmp_path/'data');configured=tmp_path/'launch-neural';store.neural_models=configured
    (store.data/'model-paths.json').write_text(configuration,encoding='utf-8')
    models=catalog.ModelCatalog(store)
    assert store.neural_models==configured and models.configuration_messages


def test_ready_weight_is_blocked_when_runtime_or_vendor_is_missing(tmp_path):
    store=fake_store(tmp_path/'data');store.ai_python=None
    store.neural_models=tmp_path/'models';store.neural_models.mkdir()
    path=store.neural_models/skin_model.MODEL_FILE;path.write_bytes(b'fixture weight')
    models=catalog.ModelCatalog(store)
    models.verified['skin']=(str(path.resolve()),catalog._signature(path),True)
    record=next(item for item in models.records() if item['id']=='skin')
    assert record['file_state']=='ready' and record['state']=='blocked'
    assert not record['downloadable']


def test_unknown_expected_weight_size_does_not_use_corrupt_local_size(tmp_path):
    store=fake_store(tmp_path/'data');store.analysis_models=tmp_path/'analysis'
    spec=catalog.MODEL_SPECS['face_landmarker']
    path=store.analysis_models/'face_landmarker'/spec['filename'];path.parent.mkdir(parents=True)
    path.write_bytes(b'broken')
    record=next(item for item in catalog.ModelCatalog(store).records() if item['id']=='face_landmarker')
    assert record['bytes']==6 and record['expected_bytes'] is None
