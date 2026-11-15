"""Candidate isolation and review semantics; no model downloads or GPU required."""
import copy
import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'neural_candidate.py'
SPEC = importlib.util.spec_from_file_location('neural_candidate_test_subject', MODULE_PATH)
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class Predictor:
    device = 'cpu'
    device_name = 'test double'
    load_seconds = 0

    def __init__(self, probability):
        self.probability = probability
        self.calls = []

    def detect(self, rgb):
        return self.probability

    def repair(self, rgb, soft_mask, bounds):
        self.calls.append((soft_mask.copy(), list(bounds)))
        a, b, c, d = bounds
        return rgb[b:d, a:c].astype(np.float32) + 40


def fixture_data():
    rgb = np.full((50, 60, 3), 100, np.uint8)
    roi = [10, 8, 30, 28]
    p = np.zeros((20, 20), np.float32)
    # Ring and central island have overlapping bounding boxes but no shared pixels.
    p[2:10, 2] = p[2:10, 9] = p[2, 2:10] = p[9, 2:10] = .9
    p[5:7, 5:7] = .7
    p[15:17, 15:17] = .4
    return rgb, roi, np.ones((50, 60), np.float32), Predictor(p)


def test_default_disabled_and_zero_strength_are_exact_identity():
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    assert len(meta['candidates']) == 3
    assert all(not c['default_enabled'] and c['requires_review'] for c in meta['candidates'])
    assert np.array_equal(subject.compose_candidates(rgb, meta, patches), rgb)
    ids = [c['id'] for c in meta['candidates']]
    assert np.array_equal(subject.compose_candidates(rgb, meta, patches, ids, strength=0), rgb)


def test_selection_changes_only_support_and_translates_roi_coordinates():
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    candidate = meta['candidates'][0]
    assert candidate['bounds'] == [12, 10, 20, 18]
    assert candidate['thumb_bounds'] == [0, 0, 44, 42]
    assert np.array_equal(predictor.calls[0][0] > 0, predictor.probability == .9)
    output = subject.compose_candidates(rgb, meta, patches, [candidate['id']], strength=.7)
    expected = np.zeros(rgb.shape[:2], bool)
    expected[8:28, 10:30] = predictor.probability == .9
    assert np.array_equal(np.any(output != rgb, axis=2), expected)
    assert (output[expected] == 128).all()
    assert not np.shares_memory(output, rgb)


def test_overlapping_boxes_remain_independent_and_order_invariant():
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    ids = [c['id'] for c in meta['candidates']]
    both = subject.compose_candidates(rgb, meta, patches, ids[:2], strength=1)
    reverse = subject.compose_candidates(rgb, meta, patches, ids[1::-1], strength=1)
    assert np.array_equal(both, reverse)
    assert (both[13:15, 15:17] == 140).all()
    assert (both[23:25, 25:27] == 100).all()
    assert all(np.count_nonzero(call[0]) in (28, 4) for call in predictor.calls)


def test_soft_detection_and_allowed_mask_are_applied_once():
    rgb, roi, mask, predictor = fixture_data()
    mask[:] = .5
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    candidate = meta['candidates'][2]
    np.testing.assert_allclose(patches[candidate['id']]['delta'], 8, atol=1e-6)
    np.testing.assert_allclose(patches[candidate['id']]['mask'], .2, atol=1e-6)
    output = subject.compose_candidates(rgb, meta, patches, [candidate['id']], strength=.5)
    assert (output[23:25, 25:27] == 104).all()


@pytest.mark.parametrize('protection_value,expected', [(0, 140), (.5, 120), (1, 100)])
def test_final_protection_can_be_changed_without_inference(protection_value, expected):
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    ids = [c['id'] for c in meta['candidates']]
    protection = np.full(rgb.shape[:2], protection_value, np.float32)
    output = subject.compose_candidates(rgb, meta, patches, ids[:2], strength=1, protection=protection)
    assert (output[13:15, 15:17] == expected).all()
    assert len(predictor.calls) == 3


def test_mask_exclusion_is_enforced_before_repair():
    rgb, roi, mask, predictor = fixture_data()
    mask[13:15, 15:17] = 0
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    assert len(meta['candidates']) == 2
    assert all(not call[0][5:7, 5:7].any() for call in predictor.calls)
    output = subject.compose_candidates(rgb, meta, patches, list(patches))
    assert np.array_equal(output[mask == 0], rgb[mask == 0])


def test_no_visible_change_is_not_offered_as_a_candidate():
    rgb, roi, mask, predictor = fixture_data()
    mask[:] = .001
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    assert meta['unchanged_components'] == 3
    assert meta['candidates'] == [] and patches == {}


def test_source_binding_and_candidate_selection_validation():
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    changed = rgb.copy()
    changed[0, 0, 0] += 1
    with pytest.raises(ValueError, match='different source'):
        subject.compose_candidates(changed, meta, patches)
    for ids in (['absent'], ['c0001', 'c0001']):
        with pytest.raises(ValueError, match='Unknown or duplicate'):
            subject.compose_candidates(rgb, meta, patches, ids)
    malformed = copy.deepcopy(meta)
    malformed['candidates'][0]['bounds'][0] = -1
    with pytest.raises(ValueError, match='bounds'):
        subject.compose_candidates(rgb, malformed, patches, ['c0001'])


@pytest.mark.parametrize('roi', [[-1, 0, 20, 20], [0, 0, 61, 20], [0, 0, 0, 20], [0., 0, 20, 20], [True, 0, 20, 20]])
def test_invalid_roi_rejected_before_inference(roi):
    rgb, _, mask, predictor = fixture_data()
    with pytest.raises(ValueError, match='ROI'):
        subject.analyse_candidates(rgb, roi, mask, predictor)
    assert not predictor.calls


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -.01, 1.01])
def test_invalid_mask_rejected(value):
    rgb, roi, mask, predictor = fixture_data()
    mask[10, 12] = value
    with pytest.raises(ValueError, match='mask'):
        subject.analyse_candidates(rgb, roi, mask, predictor)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -.1, 1.1])
def test_invalid_probability_rejected(value):
    rgb, roi, mask, predictor = fixture_data()
    predictor.probability[0, 0] = value
    with pytest.raises(ValueError, match='probability'):
        subject.analyse_candidates(rgb, roi, mask, predictor)


@pytest.mark.parametrize('strength', [True, '0.5', float('nan'), -1, 1.1])
def test_invalid_strength_rejected(strength):
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor)
    with pytest.raises(ValueError, match='strength'):
        subject.compose_candidates(rgb, meta, patches, strength=strength)


def test_large_regions_and_candidate_caps_are_explicit():
    rgb = np.full((10, 500, 3), 100, np.uint8)
    p = np.zeros((10, 500), np.float32)
    p[1, :450] = .9
    predictor = Predictor(p)
    meta, patches = subject.analyse_candidates(rgb, [0, 0, 500, 10], np.ones((10, 500)), predictor)
    assert patches == {} and not predictor.calls
    assert any('大面积' in warning for warning in meta['warnings'])
    rgb, roi, mask, predictor = fixture_data()
    meta, patches = subject.analyse_candidates(rgb, roi, mask, predictor, max_candidates=1)
    assert len(patches) == 1 and meta['omitted_components'] == 2
    assert any('上限' in warning for warning in meta['warnings'])


def test_worker_contract_and_no_overwrite(tmp_path, monkeypatch):
    # pytest basetemp must be on the caller's configured work drive.
    rgb, roi, mask, predictor = fixture_data()
    np.save(tmp_path / 'input.npy', rgb, allow_pickle=False)
    np.save(tmp_path / 'mask.npy', mask, allow_pickle=False)
    vendor = tmp_path / 'vendor'
    vendor.mkdir()
    (vendor / 'LICENSE').write_text('test license fixture', encoding='utf-8')
    request = dict(input=str(tmp_path / 'input.npy'), roi=roi, allowed_mask=str(tmp_path / 'mask.npy'),
                   vendor=str(vendor), models=str(tmp_path), output=str(tmp_path / 'job'))
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT', str(tmp_path))
    monkeypatch.setenv('PORTRAITLAB_WORKER_CACHE_ROOT', str(tmp_path / 'cache'))
    meta = subject.run_request(request, predictor)
    saved = json.loads((tmp_path / 'job/candidates.json').read_text(encoding='utf-8'))
    assert saved == meta
    for candidate in saved['candidates']:
        path = tmp_path / 'job' / candidate['patch']
        assert subject.sha256(path) == candidate['patch_sha256']
        with np.load(path, allow_pickle=False) as data:
            assert data['delta'].dtype == np.float32
            assert data['mask'].shape == data['delta'].shape[:2]
    with pytest.raises(FileExistsError):
        subject.run_request(request, predictor)
    request['output'] = str(tmp_path.parent / 'outside')
    with pytest.raises(ValueError, match='storage root'):
        subject.run_request(request, predictor)


def test_deep_job_output_uses_separate_short_cache(tmp_path, monkeypatch):
    rgb, roi, mask, predictor = fixture_data()
    np.save(tmp_path/'input.npy',rgb,allow_pickle=False)
    np.save(tmp_path/'mask.npy',mask,allow_pickle=False)
    vendor=tmp_path/'vendor';vendor.mkdir()
    (vendor/'LICENSE').write_text('test license',encoding='utf-8')
    deep=tmp_path/('a'*70)/('b'*70)/('c'*70)/'result'
    short=tmp_path/'cache'
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT',str(tmp_path))
    monkeypatch.setenv('PORTRAITLAB_WORKER_CACHE_ROOT',str(short))
    assert len(str(deep))>260
    subject.run_request({'input':str(tmp_path/'input.npy'),'roi':roi,'allowed_mask':str(tmp_path/'mask.npy'),
                         'vendor':str(vendor),'models':str(tmp_path),'output':str(deep)},predictor)
    assert (deep/'candidates.json').is_file()
    assert Path(os.environ['TEMP'])==short/'temp'
    assert Path(os.environ['CUDA_CACHE_PATH'])==short/'cuda'
    assert not (deep/'worker-cache').exists()


def test_cache_rejects_relative_or_outside_storage_roots(tmp_path,monkeypatch):
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT',str(tmp_path))
    monkeypatch.delenv('PORTRAITLAB_WORKER_CACHE_ROOT',raising=False)
    with pytest.raises(ValueError,match='绝对路径'):
        subject.configure_worker_cache(tmp_path/'job','relative-cache')
    with pytest.raises(ValueError,match='超出'):
        subject.configure_worker_cache(tmp_path/'job',tmp_path.parent/'other-cache')


@pytest.mark.skipif(os.name!='nt',reason='Windows native cache path limit')
def test_long_windows_cache_is_rejected_without_fallback(tmp_path,monkeypatch):
    monkeypatch.setenv('PORTRAITLAB_STORAGE_ROOT',str(tmp_path))
    monkeypatch.delenv('PORTRAITLAB_WORKER_CACHE_ROOT',raising=False)
    long_root=tmp_path/('a'*100)/('b'*100)
    previous=os.environ.get('TEMP')
    with pytest.raises(ValueError,match='过长'):
        subject.configure_worker_cache(long_root)
    assert os.environ.get('TEMP')==previous
    assert not (long_root/'worker-cache').exists()
