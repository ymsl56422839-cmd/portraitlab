"""Interaction-only sampling, cache and native/proxy coordinate acceptance."""
from copy import deepcopy
import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from experiments.portraitlab_v2.render_proxy import ProxyRenderer,resample_patch


def settings():
    return {'retouch':{'exposure':0},'local':{'rect':None,'feather':0},'protection':[]}


class FakeStore:
    def __init__(self,width=2051,height=1301):
        yy,xx=np.indices((height,width))
        self.rgb=np.stack([(xx*31+yy*13)%180+25,(xx*7+yy*19)%170+30,(xx*11+yy*23)%160+35],-1).astype(np.uint8)
        self.corrected=self.rgb.copy();self.reads=0;self.repairs=[];self.renders=[];self.assets={};self.patches={}
        self.photo={'id':'photo','width':width,'height':height,'analysis_generation':'g1','selected_ids':[],
                    'candidates':[],'strength':1.,'_assets':{}}
        self.workspace=SimpleNamespace(read_assets=lambda _:self.assets,render=self.render)

    def original(self,_):
        self.reads+=1
        return self.rgb

    def repair_region(self,identifier,bounds,snapshot):
        self.repairs.append(bounds)
        a,b,d,e=bounds
        return self.corrected[b:e,a:d].copy()

    def _patch_data(self,identifier,candidate,generation):return self.patches[candidate['id']]

    def render(self,identifier,bounds,snapshot,parameters,context):
        self.renders.append((deepcopy(snapshot),deepcopy(parameters),context))
        return context['repaired'](bounds)

    def candidate(self,bounds,mask=None):
        self.photo['candidates']=[{'id':'repair','bounds':bounds,'patch':'repair.npz','kind':'manual'}]
        self.photo['selected_ids']=['repair']
        self.patches['repair']={'mask':np.ones((bounds[3]-bounds[1],bounds[2]-bounds[0]),np.float32) if mask is None else mask}


@pytest.mark.parametrize('bounds',[[509,511,1027,1033],[0,0,1,1],[2580,1316,2581,1317],[1,2,17,31]])
@pytest.mark.parametrize('channels',[None,3])
def test_sparse_area_sampling_matches_full_image_phase(bounds,channels):
    width,height=2581,1317;w,h=646,330;a,b,d,e=bounds
    shape=(e-b,d-a) if channels is None else (e-b,d-a,channels)
    patch=np.random.default_rng(12).uniform(-.7,1.3,shape).astype(np.float32)
    full=np.zeros((height,width) if channels is None else (height,width,channels),np.float32)
    full[b:e,a:d]=patch
    expected=cv2.resize(full,(w,h),interpolation=cv2.INTER_AREA)
    box,reduced=resample_patch(patch,bounds,[width,height],[w,h])
    actual=np.zeros_like(expected);l,t,r,z=box;actual[t:z,l:r]=reduced
    np.testing.assert_allclose(actual,expected,atol=5e-6,rtol=3e-5)


def test_adjacent_native_chunks_add_without_proxy_boundary_seams():
    native=np.random.default_rng(7).uniform(-30,30,(73,101,3)).astype(np.float32)
    actual=np.zeros((29,31,3),np.float32)
    for top,bottom in [(0,17),(17,52),(52,73)]:
        for left,right in [(0,13),(13,67),(67,101)]:
            box,part=resample_patch(native[top:bottom,left:right],[left,top,right,bottom],[101,73],[31,29])
            a,b,d,e=box;actual[b:e,a:d]+=part
    expected=cv2.resize(native,(31,29),interpolation=cv2.INTER_AREA)
    np.testing.assert_allclose(actual,expected,atol=1e-5,rtol=2e-5)


def test_selected_no_op_patch_does_not_replace_untouched_texture():
    store=FakeStore();store.candidate([477,479,1069,1039])
    proxy=ProxyRenderer(store);_,prepared=proxy.prepare(store.photo)
    context=prepared['context'];h,w=context['source'].shape[:2]
    np.testing.assert_array_equal(context['repaired']([0,0,w,h]),context['source'])
    assert store.repairs and all(d-a<=512 and e-b<=512 for a,b,d,e in store.repairs)


def test_real_sparse_correction_only_changes_globally_sampled_support():
    store=FakeStore();bounds=[501,503,1041,1037]
    mask=np.zeros((534,540),np.float32);mask[3:27,5:29]=1;mask[-11:-3,-17:-7]=1
    store.candidate(bounds,mask)
    a,b,d,e=bounds;region=store.corrected[b:e,a:d]
    region[mask>0]=np.clip(region[mask>0].astype(np.int16)+31,0,255).astype(np.uint8)
    _,prepared=ProxyRenderer(store).prepare(store.photo)
    context=prepared['context'];h,w=context['source'].shape[:2]
    actual=context['repaired']([0,0,w,h])
    support=cv2.resize(np.any(store.corrected!=store.rgb,axis=2).astype(np.float32),(w,h),interpolation=cv2.INTER_AREA)>0
    np.testing.assert_array_equal(actual[~support],context['source'][~support])
    expected=cv2.resize(store.corrected,(w,h),interpolation=cv2.INTER_AREA)
    assert np.max(np.abs(actual.astype(np.int16)-expected.astype(np.int16)))<=1
    assert np.any(actual[support]!=context['source'][support])


def test_fields_masks_and_native_settings_share_one_proxy_transform():
    store=FakeStore();bounds=[501,503,1041,1037];a,b,d,e=bounds
    mask=np.zeros((e-b,d-a),np.float32);mask[11:33,7:28]=1
    store.candidate(bounds,mask)
    store.assets={'analysis':{'image_size':[2051,1301],'face_width':800},'raw':{'source_shape':[1301,2051]},
                  'skin':{'image_size':[2051,1301],'fields':[{'bounds':bounds,'mask':mask,'delta':np.repeat(mask[...,None],3,2)*17}]}}
    original_assets=deepcopy(store.assets)
    params=settings();params['local']={'rect':[101,203,1051,1103],'feather':21}
    params['protection']=[{'radius':33,'points':[[501,703],[1051,1203]],'mode':'protect'}]
    original_params=deepcopy(params)
    proxy=ProxyRenderer(store);proxy.frame(store.photo,params)
    photo,transformed,context=store.renders[-1];w,h=photo['width'],photo['height'];sx,sy=w/2051,h/1301
    assert transformed['local']['rect']==[101*sx,203*sy,1051*sx,1103*sy]
    assert transformed['local']['feather']==21*min(sx,sy)
    assert transformed['protection'][0]['points']==[[501*sx,703*sy],[1051*sx,1203*sy]]
    assert context['assets']['analysis']['face_width']==800*min(sx,sy)
    assert context['assets']['raw']['source_shape']==[h,w]
    full=np.zeros((1301,2051),np.float32);full[b:e,a:d]=mask
    expected=cv2.resize(full,(w,h),interpolation=cv2.INTER_AREA)
    np.testing.assert_allclose(context['marks']([0,0,w,h]),expected,atol=2e-6)
    field=context['assets']['skin']['fields'][0];l,t,r,z=field['bounds']
    np.testing.assert_allclose(field['mask'],expected[t:z,l:r],atol=2e-6)
    np.testing.assert_allclose(field['delta'],np.repeat(expected[t:z,l:r,None],3,2)*17,atol=1e-5)
    assert params==original_params and store.assets['analysis']==original_assets['analysis']
    np.testing.assert_array_equal(store.assets['skin']['fields'][0]['delta'],original_assets['skin']['fields'][0]['delta'])


def test_selected_candidate_without_usable_mask_does_not_claim_marks():
    store=FakeStore(33,25);store.candidate([3,5,13,17],np.zeros((12,10),np.float32))
    _,prepared=ProxyRenderer(store).prepare(store.photo)
    assert prepared['context']['marks_available'] is False


@pytest.mark.parametrize('level',[1,2,3])
def test_adjacent_tiles_and_odd_edge_sizes_match_whole_proxy_sampling(level):
    store=FakeStore();proxy=ProxyRenderer(store);params=settings();width,height=2051,1301;scale=2**level
    whole=proxy.tile(store.photo,params,[0,0,width,height],level)
    assembled=np.zeros((math.ceil(height/scale),math.ceil(width/scale),3),np.uint8)
    edge=256*scale
    for top in range(0,height,edge):
        for left in range(0,width,edge):
            bottom,right=min(height,top+edge),min(width,left+edge)
            tile=proxy.tile(store.photo,params,[left,top,right,bottom],level)
            assert tile.shape==(math.ceil((bottom-top)/scale),math.ceil((right-left)/scale),3)
            assembled[top//scale:top//scale+tile.shape[0],left//scale:left//scale+tile.shape[1]]=tile
    np.testing.assert_array_equal(assembled,whole)
    assert len(store.renders)==1


def test_small_photo_identity_and_source_frame_grade_cache_invalidation():
    store=FakeStore(101,73);proxy=ProxyRenderer(store);p=store.photo;params=settings()
    np.testing.assert_array_equal(proxy.frame(p,params),store.rgb)
    proxy.frame(p,params);assert store.reads==1 and len(store.renders)==1
    params['retouch']['exposure']=.3;proxy.frame(p,params)
    assert store.reads==1 and len(store.renders)==2
    context=store.renders[-1][2];computations=[]
    def compute(box):
        computations.append(box)
        return np.ones((73,101,3),np.float32)
    one=context['grade']({'exposure':.2},[3,4,9,11],compute)
    context['grade']({'exposure':.2},[0,0,2,3],compute)
    assert one.shape==(7,6,3) and computations==[[0,0,101,73]]
    context['grade']({'exposure':.4},[0,0,2,3],compute)
    context['grade']({'exposure':.5},[0,0,2,3],compute)
    context['grade']({'exposure':.2},[0,0,2,3],compute)
    assert len(computations)==4
    for change in [{'strength':.7},{'analysis_generation':'g2'},{'_assets':{'analysis':'new-generation'}}]:
        p.update(change);proxy.frame(p,params)
    store.candidate([3,5,13,17]);proxy.frame(p,params)
    p['candidates'][0]['patch']='replacement.npz';proxy.frame(p,params)
    assert store.reads==6 and len(proxy.sources)==2 and len(proxy.frames)==3


@pytest.mark.parametrize('bounds,level',[([-1,0,4,4],1),([0,0,102,4],1),([2,2,2,4],1),([0,0,4,4],-1),([0,0,4,4],True)])
def test_invalid_tile_coordinates_fail_before_render(bounds,level):
    store=FakeStore(101,73)
    with pytest.raises(ValueError,match='图块'):
        ProxyRenderer(store).tile(store.photo,settings(),bounds,level)
    assert not store.reads and not store.renders
