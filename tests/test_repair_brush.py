"""Continuous repair behavior: path coverage, donor safety and replay."""
import copy
import json

import numpy as np
import pytest

from portraitlab.spot_repair import apply_repairs, validate_repairs, MAX_STROKE_POINTS, MAX_TOTAL_POINTS
from portraitlab.vendor.rapidretouch_engine.tools.colour import linear_to_srgb, srgb_to_linear


def stroke(**kwargs):
    return {"mode": "sampled", "x": 140/239, "y": 64/127,
            "source_x": 68/239, "source_y": 40/127, "radius": 12/128,
            "strength": 1., "hardness": .65,
            "points": [[140/239, 64/127], [175/239, 64/127]], **kwargs}


def fixture():
    yy, xx = np.indices((128, 240))
    detail = 3*np.sin(xx*np.pi/4)+2*np.cos(yy*np.pi/4)
    clean = np.rint(np.array([170, 130, 110])[None,None,:]+detail[...,None]).astype(np.uint8)
    image = clean.copy()
    defect = (xx >= 140) & (xx <= 175) & (np.abs(yy-64) <= 3)
    image[defect] = [100, 45, 38]
    return image, clean, defect


def path_support(shape, operation):
    h,w=shape[:2];yy,xx=np.indices((h,w),dtype=float)
    points=np.asarray(operation['points'])*(w-1,h-1)
    ds=[]
    for a,b in zip(points[:-1],points[1:]):
        v=b-a;t=np.clip(((xx-a[0])*v[0]+(yy-a[1])*v[1])/max(1e-20,float(v@v)),0,1)
        ds.append(np.hypot(xx-a[0]-t*v[0],yy-a[1]-t*v[1]))
    if not ds:ds=[np.hypot(xx-points[0,0],yy-points[0,1])]
    return np.minimum.reduce(ds)<operation['radius']*min(h,w)


def test_new_contract_is_independent_and_legacy_shape_preserved():
    raw=stroke();before=copy.deepcopy(raw);clean=validate_repairs([raw])[0]
    assert clean==raw and raw==before
    clean['points'][0][0]=0;assert raw==before
    for mode in ['auto','restore']:
        automatic={k:v for k,v in raw.items() if not k.startswith('source_')};automatic['mode']=mode
        assert not {'source_x','source_y'}&set(validate_repairs([automatic])[0])
    old={k:v for k,v in raw.items() if k not in {'mode','points','hardness'}}
    assert set(validate_repairs([old])[0])==set(old)


@pytest.mark.parametrize('change',[
 {'mode':'unknown'},{'mode':True},{'points':[]},{'points':[[.1,.2]]},
 {'points':[[140/239,64/127],[True,.2]]},{'points':[[140/239,64/127],[.2,float('nan')]]},
 {'hardness':True},{'hardness':float('inf')},{'hardness':-1},{'hardness':1.01},
 {'points':[[140/239,64/127]]*(MAX_STROKE_POINTS+1)},
])
def test_rejects_invalid_continuous_records(change):
    with pytest.raises(ValueError):validate_repairs([stroke(**change)])


def test_total_point_budget_and_sampled_donor_required():
    raw=stroke(points=[[140/239,64/127]]*MAX_STROKE_POINTS)
    with pytest.raises(ValueError):validate_repairs([raw]*(MAX_TOTAL_POINTS//MAX_STROKE_POINTS+1))
    del raw['source_x']
    with pytest.raises(ValueError):validate_repairs([raw])


def test_two_point_path_repairs_middle_without_holes_and_keeps_outside_exact():
    image,clean,defect=fixture();before=image.copy();operation=stroke()
    result,meta=apply_repairs(image,[operation])
    error_before=np.abs(image[defect].astype(float)-clean[defect]).mean()
    error_after=np.abs(result[defect].astype(float)-clean[defect]).mean()
    assert error_after<.15*error_before
    assert np.all(np.any(result[64,140:176]!=image[64,140:176],axis=1))
    np.testing.assert_array_equal(result[~path_support(image.shape,operation)],image[~path_support(image.shape,operation)])
    np.testing.assert_array_equal(image,before)
    assert meta['requested']==meta['applied']==1 and meta['repairs'][0]['path_points']==2
    json.dumps(meta,allow_nan=False)


def test_retracing_a_path_does_not_accumulate_opacity_within_one_stroke():
    image,_,_=fixture();operation=stroke(strength=.3)
    first,_=apply_repairs(image,[operation])
    repeat=stroke(strength=.3,points=[operation['points'][0],operation['points'][1],operation['points'][0],operation['points'][1]])
    retraced,_=apply_repairs(image,[repeat]);np.testing.assert_array_equal(first,retraced)
    full,_=apply_repairs(image,[stroke()])
    expected=np.rint(linear_to_srgb(.3*srgb_to_linear(full[64,155].astype(np.float32)/255)+.7*srgb_to_linear(image[64,155].astype(np.float32)/255))*255)
    assert np.max(np.abs(first[64,155].astype(float)-expected))<=1


def test_restore_is_local_and_restores_input_to_this_layer():
    image,_,_=fixture();repaired,_=apply_repairs(image,[stroke()])
    restore={'mode':'restore','x':155/239,'y':64/127,'radius':5/128,'hardness':1.,'strength':1.,'points':[[155/239,64/127]]}
    restored,meta=apply_repairs(image,[stroke(),restore])
    support=path_support(image.shape,restore)
    np.testing.assert_array_equal(restored[support],image[support])
    np.testing.assert_array_equal(restored[~support],repaired[~support])
    assert meta['repairs'][1]['changed_pixels']>0


@pytest.mark.parametrize('mode',['sampled','auto','restore'])
def test_zero_strength_is_exact_noop(mode):
    image,_,_=fixture();result,meta=apply_repairs(image,[stroke(mode=mode,strength=0)])
    np.testing.assert_array_equal(result,image)
    assert meta['repairs'][0]['no_op_reason']=='zero_strength'


def test_automatic_sampling_removes_drawn_defect_and_reports_actual_donor():
    image,clean,defect=fixture();operation=stroke(mode='auto')
    del operation['source_x'];del operation['source_y']
    result,meta=apply_repairs(image,[operation]);entry=meta['repairs'][0]
    assert entry['no_op_reason'] is None and entry['changed_pixels']>0
    assert 0<=entry['source_x']<=1 and 0<=entry['source_y']<=1
    assert np.abs(result[defect].astype(float)-clean[defect]).mean()<8
    np.testing.assert_array_equal(result[~path_support(image.shape,operation)],image[~path_support(image.shape,operation)])
    again,repeated=apply_repairs(image,[operation]);np.testing.assert_array_equal(again,result);assert repeated==meta


def test_automatic_sampling_rejects_unsafe_mixed_context():
    yy,xx=np.indices((128,240));image=np.repeat((((xx//3+yy//3)%2)*255).astype(np.uint8)[...,None],3,axis=2)
    result,meta=apply_repairs(image,[stroke(mode='auto')])
    np.testing.assert_array_equal(result,image)
    assert meta['repairs'][0]['no_op_reason']=='no_suitable_source' and meta['warnings']


def test_dense_and_sparse_collinear_sampling_have_same_result():
    image,_,_=fixture()
    sparse,_=apply_repairs(image,[stroke()])
    dense=stroke(points=[[x/239,64/127] for x in range(140,176)])
    output,_=apply_repairs(image,[dense])
    np.testing.assert_array_equal(output,sparse)


def test_continuous_donor_remains_the_immutable_layer_input():
    image=np.random.default_rng(111).integers(80,180,(128,240,3),dtype=np.uint8)
    donor_edit=stroke(x=68/239,y=40/127,points=[[68/239,40/127]],source_x=20/239,source_y=40/127,radius=.04)
    final_edit=stroke(radius=.03)
    only,_=apply_repairs(image,[final_edit]);both,_=apply_repairs(image,[donor_edit,final_edit])
    support=path_support(image.shape,final_edit)
    np.testing.assert_array_equal(only[support],both[support])
