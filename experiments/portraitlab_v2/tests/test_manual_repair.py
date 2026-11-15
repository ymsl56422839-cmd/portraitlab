"""Manual-mark contract tests; all networks are replaced by deterministic fakes."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location('manual_repair_test_subject',
                                             Path(__file__).resolve().parents[1] / 'manual_repair.py')
manual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manual)


class Predictor:
    device = 'cpu'
    device_name = 'test double'
    load_seconds = 0

    def __init__(self):
        self.calls = []

    def detect(self, *_):
        raise AssertionError('Manual repair must never run detection')

    def repair(self, rgb, mask, bounds):
        self.calls.append((rgb.shape, mask.copy(), bounds))
        a, b, c, d = bounds
        assert rgb.shape[0] <= 576 and rgb.shape[1] <= 576
        assert c-a <= 384 and d-b <= 384
        return rgb[b:d, a:c].astype(np.float32) + 40


def stroke(points, radius=10):
    return {'radius': radius, 'points': points}


def full_mask(blocks, width, height):
    return manual._mask_crop(blocks, [0, 0, width, height])


def test_line_is_continuous_round_and_feathered_inward():
    _, blocks = manual.build_manual_mask([stroke([[20, 40], [610, 40]])], 650, 100)
    mask = full_mask(blocks, 650, 100)
    assert (mask[39:41, 20:610] == 1).all()
    assert mask[39, 11] > 0 and mask[39, 9] == 0
    assert not mask[:30].any() and not mask[50:].any()
    assert ((mask > 0) & (mask < 1)).any()
    repeated = manual.build_manual_mask([stroke([[20,40],[610,40]])]*2, 650, 100)[1]
    assert np.array_equal(full_mask(repeated,650,100), mask)


def test_single_point_and_image_edges_are_clipped_exactly():
    _, blocks = manual.build_manual_mask([stroke([[0, 0]], 7), stroke([[99.9, 79.9]], 7)], 100, 80)
    mask = full_mask(blocks,100,80)
    assert mask[0,0] == 1 and mask[-1,-1] == 1
    yy, xx = np.mgrid[:80, :100]
    allowed = ((xx+.5)**2+(yy+.5)**2 < 49) | ((xx+.5-99.9)**2+(yy+.5-79.9)**2 < 49)
    assert not mask[~allowed].any()


def test_cross_tile_repair_uses_all_context_and_disjoint_support():
    rgb = np.full((100,650,3),100,np.uint8)
    marks = [stroke([[20,40],[610,40]])]
    predictor = Predictor()
    meta, patches = manual.analyse_manual(rgb,marks,predictor)
    assert len(meta['candidates']) == 3 and len(predictor.calls) == 3
    mask = full_mask(manual.build_manual_mask(marks,650,100)[1],650,100)
    summed_mask = np.zeros(mask.shape,np.float32)
    for candidate in meta['candidates']:
        x0,y0,x1,y1 = candidate['bounds']
        patch = patches[candidate['id']]
        assert patch['delta'].shape == (y1-y0,x1-x0,3)
        np.testing.assert_allclose(patch['delta'],patch['mask'][...,None]*np.ones((1,1,3))*40,atol=3e-5)
        summed_mask[y0:y1,x0:x1] += patch['mask']
    np.testing.assert_array_equal(summed_mask,mask)
    output = manual.core.compose_candidates(rgb,meta,patches,list(patches),strength=1)
    assert np.array_equal(output[mask==0],rgb[mask==0])
    assert (output[39,20:610] == 140).all()
    # The first prediction extends beyond its result block and sees adjacent marks.
    assert np.count_nonzero(predictor.calls[0][1]) > meta['candidates'][0]['area']


def test_overlapping_predictions_blend_instead_of_hard_tile_seams():
    class ContextSensitive(Predictor):
        def repair(self,rgb,mask,bounds):
            a,b,c,d=bounds
            self.calls.append(bounds)
            return np.full((d-b,c-a,3),100+len(self.calls)*40,np.float32)
    rgb=np.full((90,650,3),100,np.uint8)
    meta,patches=manual.analyse_manual(rgb,[stroke([[20,40],[610,40]],12)],ContextSensitive())
    result=manual.core.compose_candidates(rgb,meta,patches,list(patches),strength=1)
    for boundary in (256,512):
        adjacent=result[39,boundary-2:boundary+2,0].astype(np.int32)
        assert np.abs(np.diff(adjacent)).max() <= 2


@pytest.mark.parametrize('marks', [
    [], [stroke([[2,2]])]*101, [stroke([])], [stroke([[2,2]]*5001)],
    [stroke([[2,2]],1)], [stroke([[2,2]],81)], [stroke([[2,2]],True)],
    [stroke([[float('nan'),2]])], [stroke([[2,float('inf')]])],
    [stroke([[True,2]])], [stroke([[-.1,2]])], [stroke([[100,2]])],
    [stroke([[2,80]])], [stroke([[2]])], ['not-an-object'],
])
def test_invalid_strokes_rejected(marks):
    with pytest.raises(ValueError):
        manual.validate_strokes(marks,100,80)


@pytest.mark.parametrize('rgb',[np.zeros((10,10),np.uint8),np.zeros((10,10,3),np.float32),np.zeros((1,10,3),np.uint8)])
def test_invalid_rgb_rejected_before_repair(rgb):
    predictor=Predictor()
    with pytest.raises(ValueError):
        manual.analyse_manual(rgb,[stroke([[0,0]])],predictor)
    assert not predictor.calls


def test_work_limits_reject_entire_group_before_inference():
    sparse = [stroke([[x*256+128,y*256+128]],2) for y in range(9) for x in range(8)]
    predictor=Predictor()
    with pytest.raises(ValueError,match='64'):
        manual.analyse_manual(np.zeros((2304,2048,3),np.uint8),sparse,predictor)
    assert not predictor.calls
    large = [stroke([[80,y],[1200,y]],80) for y in range(80,1201,140)]
    with pytest.raises(ValueError,match='1000000'):
        manual.analyse_manual(np.zeros((1280,1280,3),np.uint8),large,predictor)
    assert not predictor.calls


def test_manual_request_contract_hashes_and_no_overwrite(tmp_path,monkeypatch):
    rgb=np.full((100,100,3),100,np.uint8)
    np.save(tmp_path/'input.npy',rgb,allow_pickle=False)
    vendor=tmp_path/'vendor';vendor.mkdir()
    (vendor/'LICENSE').write_text('test license',encoding='utf-8')
    request={'kind':'manual','input':str(tmp_path/'input.npy'),'vendor':str(vendor),
             'models':str(tmp_path/'models'),'output':str(tmp_path/'job'),
             'strokes':[stroke([[50,50]],8)]}
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT',str(tmp_path))
    monkeypatch.setenv('PORTRAITLAB_WORKER_CACHE_ROOT',str(tmp_path/'cache'))
    import persistent_worker
    metadata=persistent_worker._default_runner(request,Predictor())
    saved=json.loads((tmp_path/'job/candidates.json').read_text(encoding='utf-8'))
    assert saved==metadata and saved['kind']=='manual'
    assert saved['default_strength']==.7 and saved['candidates'][0]['default_enabled']
    for candidate in saved['candidates']:
        path=tmp_path/'job'/candidate['patch']
        assert manual.core.sha256(path)==candidate['patch_sha256']
        with np.load(path,allow_pickle=False) as data:
            assert data['delta'].dtype==np.float32 and data['mask'].dtype==np.float32
    with pytest.raises(FileExistsError):
        manual.run_manual_request(request,Predictor())
    request['output']=str(tmp_path.parent/'outside')
    with pytest.raises(ValueError,match='storage root'):
        manual.run_manual_request(request,Predictor())
