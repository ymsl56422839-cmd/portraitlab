"""Model-free acceptance contracts; run only in the unified validation phase.

Synthetic fields exercise mathematical behaviour, not depth-model quality or
real-photograph aesthetics. No downloads, inference or persistent writes occur.
"""
from copy import deepcopy
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from experiments.portraitlab_v2 import geometry_v2 as geometry
from experiments.portraitlab_v2 import lighting_v2 as lighting


def image(width=317,height=213,dtype=np.uint8):
    yy,xx=np.indices((height,width))
    result=np.stack([(xx*7+yy*3)%241,(xx*2+yy*11)%239,(xx*13+yy*5)%237],axis=-1).astype(np.uint8)
    return result if dtype==np.uint8 else result.astype(dtype)/160


def fields(width=31,height=19):
    yy,xx=np.mgrid[-1:1:complex(height),-1:1:complex(width)]
    normal=np.stack([xx*.45,yy*.3,np.ones_like(xx)],axis=-1).astype(np.float32)
    normal/=np.linalg.norm(normal,axis=-1,keepdims=True)
    return {'normal':normal,'depth':((xx+1)/2).astype(np.float32),
            'subject_mask':np.clip((xx+.3)*1.8,0,1).astype(np.float32)}


def stitch(rgb,render,edge_x=71,edge_y=53):
    height,width=rgb.shape[:2]
    result=np.empty_like(rgb)
    for top in range(0,height,edge_y):
        for left in range(0,width,edge_x):
            right,bottom=min(left+edge_x,width),min(top+edge_y,height)
            result[top:bottom,left:right]=render((left,top,right,bottom))
    return result


def push_state():
    return geometry.validate_geometry({'strokes':[
        {'mode':'push','x':.48,'y':.46,'dx':.018,'dy':-.012,'radius':.23,'strength':.8},
        {'mode':'push','x':.52,'y':.5,'dx':-.009,'dy':.008,'radius':.18,'strength':.6}]})


@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_disabled_lighting_needs_no_analysis_and_preserves_values(dtype):
    rgb=image(dtype=dtype)
    result=lighting.render_lighting(rgb,{},linear_input=dtype!=np.uint8)
    assert np.array_equal(result,rgb)
    assert result is not rgb
    if dtype==np.float32:assert result.max()>1


def test_subject_and_background_mix_once_in_linear_space():
    rgb=np.full((8,10,3),.8,np.float32)
    normal=np.zeros_like(rgb);normal[...,2]=1
    mask=np.broadcast_to(np.linspace(0,1,10,dtype=np.float32),(8,10)).copy()
    data={'normal':normal,'depth':np.zeros((8,10),np.float32),'subject_mask':mask}
    state=lighting.validate_lighting({'enabled':True,'subject':{'ambient':.5},'background':{'ambient':1.4}})
    result=lighting.render_lighting(rgb,data,state,linear_input=True)
    expected=rgb*(.5*mask[...,None]+1.4*(1-mask[...,None]))
    np.testing.assert_allclose(result,expected,rtol=2e-6,atol=2e-7)
    assert result[0,0,0]>1


@pytest.mark.parametrize('kind',['directional','point','spot','area'])
def test_each_light_type_changes_current_pixels_and_tracks_position(kind):
    rgb=np.full((91,143,3),.25,np.float32)
    data=fields();data['subject_mask'][:]=1
    light={**lighting.DEFAULT_LIGHT,'type':kind,'intensity':1.2,'radius':.8,'beam_angle':90,'softness':.3}
    state=lighting.validate_lighting({'enabled':True,'subject':{'ambient':.7,'lights':[light]}})
    first=lighting.render_lighting(rgb,data,state,linear_input=True)
    changed=deepcopy(state)
    if kind=='directional':changed['subject']['lights'][0]['azimuth']=30
    else:changed['subject']['lights'][0]['x']=.9
    second=lighting.render_lighting(rgb,data,changed,linear_input=True)
    assert np.max(np.abs(first-rgb))>1e-4
    assert np.max(np.abs(first-second))>1e-4


@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_lighting_arbitrary_native_tiles_equal_full_render(dtype):
    rgb=image(dtype=dtype);height,width=rgb.shape[:2]
    data=lighting.prepare_fields(fields())
    state=lighting.preset_lighting('warmcool')
    state=lighting.preset_lighting('spot','background',state)
    full=lighting.render_lighting(rgb,data,state,linear_input=dtype!=np.uint8)
    tiled=stitch(rgb,lambda b:lighting.render_region(rgb[b[1]:b[3],b[0]:b[2]],data,state,b,(width,height),linear_input=dtype!=np.uint8))
    assert np.array_equal(full,tiled)


def test_lighting_rejects_missing_or_invalid_geometry_without_fallback():
    rgb=image(40,30);state=lighting.preset_lighting('soft')
    for broken in [{}, {'normal':np.zeros((3,4,3),np.float32),'depth':np.zeros((3,4)),'subject_mask':np.ones((3,4))}]:
        with pytest.raises(ValueError):lighting.render_lighting(rgb,broken,state)
    with pytest.raises(ValueError):lighting.subject_mask_from_analysis({'face_mask':np.ones((3,4))})
    actual=lighting.subject_mask_from_analysis({'parts':{'background':np.array([[0,1],[0,1]],np.float32)}})
    assert np.array_equal(actual,np.array([[1,0],[1,0]],np.float32))


@pytest.mark.parametrize('size',[(1200,800),(800,1200)])
def test_directional_canvas_handle_roundtrip_keeps_angles(size):
    state=lighting.validate_lighting({'enabled':True,'subject':{'lights':[
        {**lighting.DEFAULT_LIGHT,'azimuth':-137.,'elevation':31.}]}})
    light=state['subject']['lights'][0];point=lighting.light_handle(light,size)
    moved=lighting.move_light(state,'subject',light['id'],point['x'],point['y'],size)['subject']['lights'][0]
    assert moved['azimuth']==pytest.approx(light['azimuth'],abs=1e-7)
    assert moved['elevation']==pytest.approx(light['elevation'],abs=1e-7)


def test_all_twelve_light_presets_are_independent_valid_states():
    assert len(lighting.LIGHT_PRESETS)==12
    base=lighting.validate_lighting()
    for preset in lighting.LIGHT_PRESETS:
        state=lighting.preset_lighting(preset['id'],'background',base)
        assert lighting.lighting_active(state)
        assert state['subject']==base['subject']
        assert 1<=len(state['background']['lights'])<=4
    assert base==lighting.DEFAULT_LIGHTING


def test_geometry_identity_needs_no_landmarks_and_preserves_hdr():
    rgb=image(dtype=np.float32)
    result=geometry.render_region(rgb,(0,0,rgb.shape[1],rgb.shape[0]))
    assert np.array_equal(rgb,result)
    assert result.max()>1
    with pytest.raises(ValueError):
        geometry.render_region(rgb,(0,0,rgb.shape[1],rgb.shape[0]),{'faces':[{'face_index':0,'chin_length':.5}]},{})


@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_geometry_callable_tiles_equal_full_and_do_not_quantize_hdr(dtype):
    rgb=image(dtype=dtype);height,width=rgb.shape[:2];state=push_state()
    full=geometry.render_region(rgb,(0,0,width,height),state)
    requested=[]
    def reader(bounds):
        x0,y0,x1,y1=bounds;requested.append(bounds)
        assert 0<=x0<x1<=width and 0<=y0<y1<=height
        return rgb[y0:y1,x0:x1]
    tiled=stitch(rgb,lambda bounds:geometry.render_region(reader,bounds,state,image_size=(width,height)))
    assert np.any(full!=rgb)
    assert np.array_equal(full,tiled)
    assert requested
    if dtype==np.float32:
        assert full.max()>1
        assert np.any(np.abs(full*160-np.rint(full*160))>1e-4)
    with pytest.raises(ValueError):geometry.render_region(reader,(0,0,20,20),state)


def test_second_manual_stroke_uses_display_coordinates_and_reports_clamps():
    state=push_state()
    operations,metadata=geometry.build_operations(state,{},317,213)
    assert operations[1]['cx']==pytest.approx(state['strokes'][1]['x'])
    assert operations[1]['cy']==pytest.approx(state['strokes'][1]['y'])
    state['strokes'][1]['dx']=.8
    _,metadata=geometry.build_operations(state,{},317,213)
    assert metadata['stroke_clamps']==[1]
    assert metadata['warnings']


def test_protection_core_edges_and_restore_keep_source_coordinates():
    width,height=317,213;state=push_state()
    state['protection']=[{'x':.48,'y':.46,'radius':.12,'strength':1.}]
    operations,_=geometry.build_operations(state,{},width,height)
    protected=np.array([[.48,.46],[0.,.5],[1.,.5],[.5,0.],[.5,1.]])
    assert np.array_equal(geometry.map_points(protected,operations,image_size=(width,height),value=state),protected)
    plain=push_state();plain['strokes']=[plain['strokes'][0]]
    first,_=geometry.build_operations(plain,{},width,height)
    center=np.array([[.48,.46]])
    displaced=geometry.map_points(center,first,image_size=(width,height),value=plain)
    assert np.linalg.norm(displaced-center)>1e-4
    plain['strokes'].append({'mode':'restore','x':.48,'y':.46,'radius':.15,'strength':1.})
    restored,_=geometry.build_operations(plain,{},width,height)
    assert np.array_equal(geometry.map_points(center,restored,image_size=(width,height),value=plain),center)


def test_forward_overlay_roundtrip_matches_the_inverse_map():
    state=push_state();size=(317,213)
    operations,_=geometry.build_operations(state,{},*size)
    source=np.array([[.48,.46],[.50,.49],[.2,.2],[.8,.8]])
    displayed=geometry.map_points(source,operations,image_size=size,value=state,inverse=False)
    restored=geometry.map_points(displayed,operations,image_size=size,value=state)
    np.testing.assert_allclose(restored,source,atol=1e-8,rtol=0)


def test_background_region_reads_source_once_without_inverse_coordinates(monkeypatch):
    size=(7643,5098);bounds=(0,0,512,512);requests=[]
    state={'strokes':[{'mode':'push','x':.5,'y':.5,'dx':.005,'dy':0,'radius':.05,'strength':.6}]}
    expected=image(512,512,dtype=np.float32)
    def source(region):
        requests.append(region)
        assert tuple(region)==bounds
        return expected
    def forbidden(*args,**kwargs):
        raise AssertionError('A disjoint background tile must not allocate/evaluate an inverse map')
    monkeypatch.setattr(geometry,'_inverse',forbidden)
    result=geometry.render_region(source,bounds,state,image_size=size)
    assert requests==[bounds]
    assert np.array_equal(result,expected)
    assert result is not expected


@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_support_culling_is_pixel_identical_to_unculled_chained_operations(monkeypatch,dtype):
    rgb=image(773,531,dtype=dtype);height,width=rgb.shape[:2]
    state=push_state()
    state['strokes'] += [
        {'mode':'restore','x':.51,'y':.49,'radius':.14,'strength':.7},
        {'mode':'push','x':.04,'y':.96,'dx':.008,'dy':-.008,'radius':.13,'strength':.8},
        {'mode':'push','x':.91,'y':.06,'dx':-.009,'dy':.008,'radius':.06,'strength':.4}]
    state['protection']=[{'x':.48,'y':.46,'radius':.04,'strength':1.}]
    optimized=geometry.render_region(rgb,(0,0,width,height),state)
    optimized_tiles=stitch(rgb,lambda b:geometry.render_region(rgb,b,state),edge_x=139,edge_y=103)
    # Force the pre-optimization path: all regions allocate and evaluate the
    # original inverse maps. This compares exact pixels rather than tolerances.
    monkeypatch.setattr(geometry,'_intersects_support',lambda *args:True)
    unculled=geometry.render_region(rgb,(0,0,width,height),state)
    assert np.array_equal(optimized,unculled)
    assert np.array_equal(optimized_tiles,unculled)


def test_support_culling_reduces_inverse_blocks_without_changing_pixels(monkeypatch):
    rgb=image(1024,768);size=(1024,768);state={'strokes':[
        {'mode':'push','x':.5,'y':.5,'dx':.004,'dy':0,'radius':.05,'strength':.6}]}
    inverse=geometry._inverse;calls=[]
    def counted(points,*args):
        calls.append(points.shape[:2])
        return inverse(points,*args)
    monkeypatch.setattr(geometry,'_inverse',counted)
    optimized=geometry.render_region(rgb,(0,0,*size),state)
    optimized_count=len(calls);calls.clear()
    monkeypatch.setattr(geometry,'_intersects_support',lambda *args:True)
    unculled=geometry.render_region(rgb,(0,0,*size),state)
    assert 0<optimized_count<len(calls)
    assert len(calls)==12
    assert np.array_equal(optimized,unculled)


@pytest.mark.parametrize('bounds',[(0,0,64,513),(64,0,65,513),(65,0,66,513),(191,0,194,513)])
def test_support_boundary_tiles_match_original_math(monkeypatch,bounds):
    rgb=image(513,513,dtype=np.float32)
    state={'strokes':[{'mode':'push','x':.25,'y':.5,'dx':.01,'dy':0,'radius':64/513,'strength':.8}]}
    optimized=geometry.render_region(rgb,bounds,state)
    monkeypatch.setattr(geometry,'_intersects_support',lambda *args:True)
    unculled=geometry.render_region(rgb,bounds,state)
    assert np.array_equal(optimized,unculled)


@pytest.mark.parametrize('scope,mask_value',[('subject',0.),('background',1.)])
@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_zero_coverage_lighting_skips_normals_and_depth(monkeypatch,scope,mask_value,dtype):
    rgb=image(71,53,dtype=dtype);raw=fields();raw['subject_mask'][:]=mask_value
    data=lighting.prepare_fields(raw);state=lighting.preset_lighting('soft',scope)
    sample=lighting._sample;sampled=[]
    def checked(field,*args):
        assert field is data.subject_mask,'An identity tile must not sample normal or depth'
        sampled.append(field)
        return sample(field,*args)
    monkeypatch.setattr(lighting,'_sample',checked)
    # Float mode is explicit, as in the editor's linear compositing path.
    result=lighting.render_region(rgb,data,state,linear_input=dtype!=np.uint8)
    assert len(sampled)==1
    assert np.array_equal(result,rgb)
    assert result is not rgb


@pytest.mark.parametrize('scope,mask_value',[('subject',1e-6),('background',1-1e-6)])
def test_soft_mask_values_are_not_treated_as_zero_coverage(monkeypatch,scope,mask_value):
    rgb=np.full((20,30,3),2.,np.float32);raw=fields();raw['subject_mask'][:]=mask_value
    data=lighting.prepare_fields(raw)
    state=lighting.validate_lighting({'enabled':True,scope:{'ambient':.5}})
    sample=lighting._sample;sampled=[]
    def checked(field,*args):
        sampled.append(field)
        return sample(field,*args)
    monkeypatch.setattr(lighting,'_sample',checked)
    result=lighting.render_region(rgb,data,state,linear_input=True)
    assert len(sampled)==3
    assert any(field is data.normal for field in sampled)
    assert any(field is data.depth for field in sampled)
    assert np.any(result!=rgb)


@pytest.mark.parametrize('dtype',[np.uint8,np.float32])
def test_mask_culling_matches_original_lighting_math_at_hard_and_soft_edges(monkeypatch,dtype):
    rgb=image(317,213,dtype=dtype);raw=fields();raw['subject_mask'][:,:10]=0;raw['subject_mask'][:,21:]=1
    data=lighting.prepare_fields(raw);state=lighting.preset_lighting('window')
    optimized=lighting.render_lighting(rgb,data,state,linear_input=dtype!=np.uint8)
    monkeypatch.setattr(lighting,'_group_active',lambda group:True)
    unculled=lighting.render_lighting(rgb,data,state,linear_input=dtype!=np.uint8)
    assert np.array_equal(optimized,unculled)
