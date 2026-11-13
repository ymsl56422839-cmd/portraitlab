"""Behavioral contracts for deterministic, source-preserving lighting."""
from copy import deepcopy
import numpy as np
import pytest

from portraitlab.lighting import DEFAULT_LIGHT, DEFAULT_LIGHTING, LIGHT_PRESETS, validate_lighting, render_lighting


def scene():
    rgb = np.random.default_rng(8).integers(0, 256, (65, 81, 3), dtype=np.uint8)
    normal = np.empty((17, 21, 3), dtype=np.uint8)
    normal[:] = (128, 128, 255)
    depth = np.full((17, 21), 128, dtype=np.uint8)
    mask = np.zeros((65, 81), dtype=np.uint8)
    mask[10:55, 20:61] = 255
    return rgb, normal, depth, mask


def config(**changes):
    return {**DEFAULT_LIGHTING, 'enabled': True, 'lights': [dict(DEFAULT_LIGHT)], **changes}


@pytest.mark.parametrize('settings', [None, {}, config(enabled=False), config(strength=0), config(lights=[]),
    config(ambient=0, lights=[{**DEFAULT_LIGHT, 'enabled': False}]),
    config(ambient=0, lights=[{**DEFAULT_LIGHT, 'intensity': 0}])])
def test_inactive_lighting_is_exact_identity(settings):
    rgb, normal, depth, mask = scene()
    assert np.array_equal(render_lighting(rgb, normal, depth, mask, settings), rgb)


@pytest.mark.parametrize('kind', ['directional', 'point', 'spot', 'area'])
def test_light_types_preserve_unmasked_bytes_and_change_subject(kind):
    rgb, normal, depth, mask = scene()
    result = render_lighting(rgb, normal, depth, mask, config(ambient=.7, lights=[{**DEFAULT_LIGHT, 'type': kind, 'intensity': 1.5, 'beam_angle': 100}]))
    assert np.array_equal(result[mask == 0], rgb[mask == 0])
    assert np.any(result[mask == 255] != rgb[mask == 255])
    assert result.shape == rgb.shape and result.dtype == np.uint8


def test_surface_normals_change_directional_response():
    rgb, normal, depth, mask = scene()
    rgb[:] = 100
    tilted = np.empty_like(normal)
    tilted[:] = (218, 128, 218)
    light = {**DEFAULT_LIGHT, 'azimuth': 0, 'elevation': 30, 'softness': 0}
    params = config(ambient=.4, scope='all', lights=[light])
    front = render_lighting(rgb, normal, depth, mask, params)
    right = render_lighting(rgb, tilted, depth, mask, params)
    assert right.mean() > front.mean()+5
    light['azimuth'] = 180
    left = render_lighting(rgb, tilted, depth, mask, params)
    assert left.mean() < front.mean()-5


def test_all_scope_applies_to_background_and_feathered_scope_blends():
    rgb, normal, depth, mask = scene()
    rgb[:] = 100
    subject = render_lighting(rgb, normal, depth, mask, config())
    whole = render_lighting(rgb, normal, depth, mask, config(scope='all'))
    assert np.any(whole[mask == 0] != rgb[mask == 0])
    feathered = mask.astype(np.float32)/510
    half = render_lighting(rgb, normal, depth, feathered, config())
    assert np.all(half[mask > 0] >= rgb[mask > 0])
    assert np.all(half[mask > 0] <= subject[mask > 0])


def test_unknown_invalid_and_nonfinite_values_rejected():
    cases = [config(no_such_field=3), config(strength=True), config(ambient=float('nan')),
             config(scope='unknown'), config(lights=[DEFAULT_LIGHT]*2),
             config(lights=[{**DEFAULT_LIGHT, 'type': 'diffusion'}]),
             config(lights=[{**DEFAULT_LIGHT, 'z': 0}]),
             config(lights=[{**DEFAULT_LIGHT, 'temperature': float('inf')}]),
             config(lights=[{**DEFAULT_LIGHT, 'name': 'extra'}])]
    for value in cases:
        with pytest.raises(ValueError):
            validate_lighting(value)


def test_defaults_and_presets_are_isolated_and_valid():
    a = validate_lighting(None)
    a['lights'].append(dict(DEFAULT_LIGHT))
    assert validate_lighting(None)['lights'] == []
    assert len(LIGHT_PRESETS) == 10
    for preset in LIGHT_PRESETS:
        result = validate_lighting(preset['lighting'])
        assert result['enabled'] and result['scope'] == 'subject'
        assert 1 <= len(result['lights']) <= 4
        result['lights'][0]['intensity'] = 0
        assert preset['lighting']['lights'][0]['intensity'] > 0


def test_point_light_distance_and_spot_direction_affect_shading():
    rgb, normal, depth, mask = scene()
    rgb[:] = 100
    lamp = {**DEFAULT_LIGHT, 'type': 'point', 'x': .5, 'y': .5, 'z': .5, 'softness': 0}
    p = config(ambient=.5, scope='all', lights=[lamp])
    close = render_lighting(rgb, normal, depth, mask, p)
    lamp['z'] = 3
    far = render_lighting(rgb, normal, depth, mask, p)
    assert close.mean() > far.mean()+10
    lamp.update(type='spot', z=1, target_x=.5, target_y=.5, beam_angle=30)
    aimed = render_lighting(rgb, normal, depth, mask, p)
    lamp['target_x'] = -1
    away = render_lighting(rgb, normal, depth, mask, p)
    assert aimed[32, 40].mean() > away[32, 40].mean()+10


def test_large_output_uses_fixed_geometry_without_shape_drift():
    rgb = np.full((300, 401, 3), 128, np.uint8)
    normal = np.full((8, 9, 3), (128, 128, 255), np.uint8)
    depth = np.zeros((8, 9), np.uint8)
    mask = np.zeros((300, 401), np.uint8)
    mask[50:250, 60:340] = 255
    output = render_lighting(rgb, normal, depth, mask, LIGHT_PRESETS[0]['lighting'])
    assert output.shape == rgb.shape
    assert np.array_equal(output[mask == 0], rgb[mask == 0])


def test_validation_does_not_mutate_input():
    p = config()
    snapshot = deepcopy(p)
    validate_lighting(p)
    render_lighting(*scene(), p)
    assert p == snapshot


@pytest.mark.parametrize('kind', ['directional', 'point', 'spot', 'area'])
def test_background_scope_preserves_subject_and_changes_background(kind):
    rgb, normal, depth, mask = scene()
    result = render_lighting(rgb, normal, depth, mask, config(scope='background', ambient=.6, lights=[{**DEFAULT_LIGHT, 'type':kind}]))
    assert np.array_equal(result[mask == 255], rgb[mask == 255])
    assert np.any(result[mask == 0] != rgb[mask == 0])


@pytest.mark.parametrize('scope', ['subject', 'background', 'all'])
def test_all_scopes_zero_strength_are_exact(scope):
    rgb, normal, depth, mask = scene()
    result = render_lighting(rgb, normal, depth, mask, config(scope=scope, strength=0))
    assert np.array_equal(result,rgb)
