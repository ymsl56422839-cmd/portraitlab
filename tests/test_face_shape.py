"""Image geometry contracts without model downloads or application state."""
import copy

import numpy as np
import pytest

from portraitlab.face_shape import (
    PARAMETERS, MAX_STROKES, validate_face_shape, has_shape, build_operations,
    apply_face_shape, warp_image, map_points, forward_points,
)


OVAL = (10,338,297,332,284,251,389,356,454,323,361,288,397,365,379,378,400,377,
        152,148,176,149,150,136,172,58,132,93,234,127,162,21,54,103,67,109)


def face(cx=.5, cy=.5, fw=.4, fh=.62):
    angles = np.linspace(0, 2*np.pi, 468, endpoint=False)
    p = np.stack((cx + fw*.5*np.cos(angles), cy + fh*.5*np.sin(angles)), axis=-1)
    for i, index in enumerate(OVAL):
        angle = -np.pi/2 + i*2*np.pi/len(OVAL)
        p[index] = [cx + fw*.5*np.cos(angle), cy + fh*.5*np.sin(angle)]
    p[123], p[352] = [cx-fw*.42,cy+fh*.06], [cx+fw*.42,cy+fh*.06]
    p[172], p[397] = [cx-fw*.38,cy+fh*.32], [cx+fw*.38,cy+fh*.32]
    p[234], p[454] = [cx-fw*.5,cy], [cx+fw*.5,cy]
    return p.astype(np.float32)


def photograph(h=240, w=320):
    y,x=np.mgrid[:h,:w]
    return np.stack(((x*3+y)%256,(y*5+x)%256,((x//7+y//9)%2)*100+80),axis=-1).astype(np.uint8)


def stroke(**values):
    return {"mode":"push","x":.5,"y":.5,"dx":.02,"dy":-.01,"radius":.12,"strength":1.,**values}


def circle(**values):
    return {"kind":"push","cx":.5,"cy":.5,"rx":.2,"ry":.2,"dx":.025,"dy":0.,"strength":1.,**values}


def test_defaults_are_exact_independent_noops_and_descriptors_exist():
    original=photograph()
    result,meta=apply_face_shape(original,None,{"faces":[face()]})
    assert np.array_equal(result,original) and result is not original
    assert meta["changed_pixels"]==0 and not meta["active"]
    assert len(meta["faces"])==1 and meta["faces"][0]["valid"]
    assert meta["faces"][0]["anchors"]["left"][0] < meta["faces"][0]["anchors"]["right"][0]
    clean=validate_face_shape(None);clean["faces"].append({})
    assert validate_face_shape(None)=={"faces":[],"strokes":[]}
    assert not has_shape({"faces":[{"face_index":0}]})


def test_validation_copies_and_defaults_fields():
    value={"faces":[{"face_index":0,"jaw_left":-.3}],"strokes":[{"mode":"restore","x":.4,"y":.5,"radius":.1}]}
    before=copy.deepcopy(value);clean=validate_face_shape(value)
    assert value==before
    assert clean["faces"][0]["width_right"]==0
    assert clean["strokes"][0]["strength"]==1
    assert clean["strokes"][0]["dx"]==0
    clean["faces"][0]["jaw_left"]=1
    assert value==before and has_shape(value)


@pytest.mark.parametrize("bad",[
    [],{"unknown":1},{"faces":None},{"strokes":{}},
    {"faces":[{}]},{"faces":[{"face_index":True}]},{"faces":[{"face_index":1.5}]},
    {"faces":[{"face_index":0,"jaw_left":float("nan")}]},
    {"faces":[{"face_index":0,"chin_width":True}]},
    {"faces":[{"face_index":0,"chin_length":2}]},
    {"faces":[{"face_index":0},{"face_index":0}]},
    {"strokes":[{"mode":"push","x":.5,"y":.5,"radius":0}]},
    {"strokes":[{"mode":"push","x":.5,"y":.5,"radius":.1,"dx":float("inf")}]},
    {"strokes":[{"mode":"erase","x":.5,"y":.5,"radius":.1}]},
    {"strokes":[{"mode":"push","x":.5,"y":.5,"radius":.1,"source":1}]},
    {"strokes":[{"mode":"push","x":.5,"y":.5,"radius":.1,"strength":10**1000}]},
    {"strokes":[{"mode":"restore","x":.5,"y":.5,"radius":.1}]*(MAX_STROKES+1)},
])
def test_rejects_malformed_and_unsafe_state(bad):
    with pytest.raises(ValueError):validate_face_shape(bad)


@pytest.mark.parametrize("parameter",PARAMETERS)
@pytest.mark.parametrize("dimensions",[(240,320),(320,240)])
def test_each_face_parameter_has_visible_local_effect(parameter,dimensions):
    original=photograph(*dimensions)
    result,meta=apply_face_shape(original,{"faces":[{"face_index":0,parameter:.8}]},{"faces":[face()]})
    assert meta["changed_pixels"]>5
    assert meta["applied_faces"]==[0]
    assert not np.array_equal(result,original)
    h,w=original.shape[:2];y,x=np.mgrid[:h,:w];px,py=x/(w-1),y/(h-1)
    support=np.zeros((h,w),bool)
    for op in meta["operations"]:
        support |= ((px-op["cx"])/op["rx"])**2+((py-op["cy"])/op["ry"])**2<1
    assert np.array_equal(result[~support],original[~support])
    assert np.array_equal(result[:12],original[:12])


def test_selected_person_and_side_do_not_modify_other_face():
    original=photograph(360,640)
    analysis={"faces":[face(.22,.5,.18,.55),face(.76,.5,.18,.55)]}
    output,meta=apply_face_shape(original,{"faces":[{"face_index":1,"jaw_left":-1}]},analysis)
    assert meta["applied_faces"]==[1]
    assert np.array_equal(output[:,:320],original[:,:320])
    assert np.array_equal(output[:,int(.8*640):],original[:,int(.8*640):])
    assert meta["changed_pixels"]>0


def test_missing_face_warns_but_manual_push_still_works():
    original=photograph()
    result,meta=apply_face_shape(original,{"faces":[{"face_index":3,"width_left":1}],"strokes":[stroke()]},{"faces":[]})
    assert meta["changed_pixels"]>0 and meta["warnings"]
    assert meta["applied_faces"]==[] and len(meta["operations"])==1
    untouched,no_face=apply_face_shape(original,{"faces":[{"face_index":0,"chin_length":1}]},{"faces":[np.zeros((4,2))]})
    assert np.array_equal(untouched,original) and no_face["warnings"]


def test_inverse_composition_and_restore_are_coordinate_math_not_pixel_blend():
    ops=[circle(),circle(kind="restore",cx=.51,dx=0,strength=.6)]
    point=np.array([.51,.5])
    expected=.6*point+.4*map_points(point,[ops[0]])
    assert np.allclose(map_points(point,ops),expected,rtol=0,atol=1e-13)
    ops[-1]["strength"]=1
    assert np.array_equal(map_points(point,ops),point)
    outside=np.array([[0.,0.],[.2,.5],[1.,1.]])
    assert np.array_equal(map_points(outside,ops),outside)


def test_forward_inverse_roundtrip_on_actual_mixed_operations():
    state={"faces":[{"face_index":0,**dict.fromkeys(PARAMETERS,-.8)}],
           "strokes":[stroke(),{"mode":"restore","x":.44,"y":.5,"radius":.08,"strength":.35}]}
    ops,_=build_operations(state,{"faces":[face()]},1024,768)
    rng=np.random.default_rng(5);source=rng.uniform(.15,.85,(500,2))
    view=forward_points(source,ops)
    assert np.max(np.abs(map_points(view,ops)-source))<2e-7
    assert np.max(np.abs(map_points(source,ops,inverse=False)-view))==0


def test_manual_anchor_follows_prior_parameter_changes():
    shape={"faces":[{"face_index":0,"width_left":-.8}],"strokes":[stroke(x=.3,y=.5,dx=.01)]}
    ops,_=build_operations(shape,{"faces":[face()]},640,480)
    expected=forward_points([.3,.5],ops[:-1])
    assert np.allclose([ops[-1]["cx"],ops[-1]["cy"]],expected,atol=1e-10)


def test_large_drag_is_capped_and_zero_strength_is_identity():
    state={"strokes":[stroke(dx=.8,dy=.6)]}
    ops,meta=build_operations(state,{},320,240)
    assert meta["stroke_clamps"]==[0]
    op=ops[0]
    assert np.isclose(np.hypot(op["dx"]/op["rx"],op["dy"]/op["ry"]),.2)
    result,info=apply_face_shape(photograph(),{"strokes":[stroke(strength=0)]},{})
    assert not info["operations"] and np.array_equal(result,photograph())


@pytest.mark.parametrize("dtype",[np.uint8,np.uint16,np.float32,np.float64,np.bool_])
def test_scalar_warp_keeps_dtype_and_unselected_pixels(dtype):
    y,x=np.mgrid[:128,:180];original=((x+y)%100).astype(dtype)
    output=warp_image(original,[circle()])
    assert output.dtype==original.dtype and output.shape==original.shape
    assert np.array_equal(output[:20],original[:20])
    if dtype!=np.bool_:assert not np.array_equal(output,original)


def test_scalar_and_rgb_maps_agree_and_sample_only_once():
    y,x=np.mgrid[:100,:140];scalar=(x+y*2).astype(np.float32)
    rgb=np.repeat(scalar[...,None],3,axis=2)
    ops=[circle(),circle(cx=.52,dx=-.02,dy=.01)]
    actual=warp_image(scalar,ops)
    colour=warp_image(rgb,ops)
    assert np.array_equal(actual,colour[...,0])
    assert np.array_equal(colour[...,0],colour[...,1])


def test_single_channel_dimension_is_kept_and_long_images_are_tiled():
    array=np.arange(40000,dtype=np.float32)[None,:].repeat(16,axis=0)[...,None]
    op=circle(cx=.5,cy=.5,rx=4/39999,ry=4/15,dx=0.5/39999,dy=0)
    out=warp_image(array,[op])
    assert out.shape==array.shape and out.dtype==array.dtype
    assert np.array_equal(out[:,:19000],array[:,:19000])
    assert not np.array_equal(out,array)


@pytest.mark.parametrize("bad",[np.zeros((0,2,3),np.uint8),np.zeros((10,10,5),np.uint8),np.zeros((10,10),np.int64)])
def test_invalid_image_input(bad):
    with pytest.raises(ValueError):warp_image(bad,[])


def test_unsafe_operations_and_points_rejected():
    with pytest.raises(ValueError):map_points([[float("nan"),.5]],[])
    with pytest.raises(ValueError):map_points([[.5,.5]],[circle(dx=1)])
    with pytest.raises(ValueError):map_points([[.5,.5]],[circle(rx=0)])
    with pytest.raises(ValueError):build_operations(None,{},True,20)
