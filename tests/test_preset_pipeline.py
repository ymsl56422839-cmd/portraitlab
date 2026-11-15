"""Behavioral checks for scoped, photo-aware recipes; no model or network."""
from copy import deepcopy
import json

import numpy as np
import pytest

from portraitlab.engine import Engine, validate_params
from portraitlab.presets import NEUTRAL, PARAMETER_GROUPS, builtin_presets, custom_record
from portraitlab.preset_pipeline import resolve_preset, source_statistics


def recipe(identifier):
    return next(item for item in builtin_presets() if item['id'] == identifier)


def rgb(value=80, shape=(64, 64)):
    return np.full((*shape, 3), value, np.uint8)


def analysis(face=False, shape=(64, 64)):
    return {'face_count': int(face), 'faces': [], 'parts': {},
            'face_mask': np.ones(shape, np.float32) if face else np.zeros(shape, np.float32)}


def apply(identifier, current=None, source=None, cached=None, mask=None):
    return resolve_preset(recipe(identifier), NEUTRAL if current is None else current,
                          rgb() if source is None else source, analysis() if cached is None else cached, mask)


def test_skin_light_and_color_recipes_preserve_other_operations():
    current = {**NEUTRAL, 'exposure': .45, 'relight': .24, 'warmth': -.3, 'saturation': .82,
               'smooth': .7, 'acne': .2, 'acne_sensitivity': .9, 'opacity': .8}
    skin = apply('natural', current)['params']
    assert all(skin[key] == current[key] for key in PARAMETER_GROUPS['light'] + PARAMETER_GROUPS['color'])
    assert skin['smooth'] == .25 * .75 and skin['opacity'] == .8
    color = apply('warm_portrait', current)['params']
    assert all(color[key] == current[key] for key in PARAMETER_GROUPS['skin'] + PARAMETER_GROUPS['light'])
    assert color['warmth'] > 0 and color['saturation'] < 1
    light = apply('dim_scene', current)['params']
    assert all(light[key] == current[key] for key in PARAMETER_GROUPS['skin'] + PARAMETER_GROUPS['color'])
    assert light['relight'] == current['relight']


def test_face_only_light_preserves_existing_global_exposure():
    current = {**NEUTRAL, 'exposure': .2}
    result = apply('face_bright', current, rgb(65), analysis(True))
    assert result['apply_keys'] == ['relight']
    assert result['params']['exposure'] == .2
    assert result['params']['relight'] > 0


def test_age_light_recipes_keep_gentler_explicit_limits():
    source, cached = rgb(35), analysis(True)
    standard = apply('face_bright', source=source, cached=cached)['params']['relight']
    youth = apply('youth_school', source=source, cached=cached)['params']['relight']
    senior = apply('senior_soft_fill', source=source, cached=cached)['params']['relight']
    assert 0 < youth < senior < standard


def test_reset_is_neutral_but_next_edit_renders(tmp_path):
    reset = apply('original', {**NEUTRAL, 'smooth': .6, 'exposure': 1, 'opacity': 0})
    assert reset['params'] == NEUTRAL and reset['params']['opacity'] == 1
    source = rgb(75)
    edited = {**reset['params'], 'exposure': .5}
    rendered, _ = Engine(tmp_path / 'no-models').render(source, edited, analysis())
    assert np.all(rendered > source)


def test_zero_master_strength_is_reactivated_but_low_strength_is_explicit():
    result = apply('natural', {**NEUTRAL, 'opacity': 0})
    assert result['params']['opacity'] == 1
    assert any('恢复到 100%' in item for item in result['warnings'])
    result = apply('natural', {**NEUTRAL, 'opacity': .1})
    assert result['params']['opacity'] == .1
    assert '总强度 10%' in result['summary']


@pytest.mark.parametrize('identifier', ['natural', 'warm_portrait', 'dim_scene', 'auto_balance', 'backlight', 'lower_exposure'])
def test_repeat_application_does_not_accumulate(identifier):
    source, cached = rgb(70), analysis(True)
    first = apply(identifier, source=source, cached=cached)
    second = apply(identifier, first['params'], source, cached)
    assert first['params'] == second['params']


def test_photo_adaptive_lift_changes_pixels_and_varies_with_source(tmp_path):
    dark, lighter = rgb(45), rgb(110)
    low = apply('dim_scene', source=dark)
    high = apply('dim_scene', source=lighter)
    assert low['params']['exposure'] > high['params']['exposure'] >= 0
    rendered, _ = Engine(tmp_path / 'no-models').render(dark, low['params'], analysis())
    assert rendered.mean() > dark.mean() + 10
    assert 'EV' in low['summary']


def test_natural_balance_brightens_dark_and_dims_bright_source():
    assert apply('auto_balance', source=rgb(50))['params']['exposure'] > 0
    assert apply('auto_balance', source=rgb(200))['params']['exposure'] < 0


def test_broad_highlights_limit_global_lift():
    source = rgb(45)
    source[:16] = 250
    result = apply('dim_scene', source=source)
    assert result['params']['exposure'] == 0
    assert any('高光余量' in item for item in result['warnings'])
    assert result['params']['relight'] == 0


def test_rectangle_statistics_use_only_selected_pixels():
    source = rgb(220, (80, 120))
    source[:, :60] = 40
    left = {'x': 0, 'y': 0, 'width': .5, 'height': 1}
    right = {'x': .5, 'y': 0, 'width': .5, 'height': 1}
    dark = apply('auto_balance', source=source, mask=left)
    bright = apply('auto_balance', source=source, mask=right)
    assert dark['params']['exposure'] > 0 > bright['params']['exposure']
    assert dark['analysis']['scope'] == 'selection'
    assert dark['analysis']['scene']['sample_count'] == 80 * 60


def test_small_local_roi_keeps_face_statistics_in_correct_coordinates():
    source = rgb(220, (1200, 1600))
    source[400:500, 800:900] = 40
    face_mask = np.zeros((120, 160), np.float32)
    face_mask[40:50, 80:90] = 1
    cached = {'face_count': 1, 'face_mask': face_mask}
    local = {'x': .5, 'y': 1/3, 'width': 1/16, 'height': 1/12}
    result = apply('face_bright', source=source, cached=cached, mask=local)
    assert result['analysis']['face']['median_luminance'] < .03
    assert result['analysis']['face']['sample_count'] >= 9000
    assert result['params']['relight'] > 0


def test_array_mask_statistics_and_no_face_feedback():
    source = rgb(210)
    source[:32] = 35
    mask = np.zeros((32, 32), np.float32)
    mask[:16] = 1
    result = apply('dim_scene', source=source, mask=mask)
    assert result['params']['exposure'] > 0
    missing = apply('face_bright')
    assert missing['params']['relight'] == 0
    assert any('未检测到' in item for item in missing['warnings'])
    skin = apply('acne_standard')
    assert not any('痘点' in item and '未检测到' in item for item in skin['warnings'])


@pytest.mark.parametrize('value', [0, 255])
def test_black_and_white_images_return_finite_bounded_values(value):
    for identifier in ('auto_balance', 'dim_scene', 'lower_exposure', 'backlight'):
        result = apply(identifier, source=rgb(value), cached=analysis(True))
        json.dumps(result, allow_nan=False)
        assert result['params'] == validate_params(result['params'])
        assert result['warnings']


def test_existing_custom_preset_remains_a_complete_snapshot():
    custom = custom_record('user_' + '0' * 32, {'name': '旧自定义', 'params': {'exposure': .2, 'opacity': .4}})
    current = {**NEUTRAL, 'warmth': .7, 'smooth': .5}
    result = resolve_preset(custom, current, rgb(), analysis())
    assert result['params'] == custom['params']
    assert result['apply_mode'] == 'snapshot' and '完整参数快照' in result['summary']


def test_acne_detection_and_strength_are_separate():
    gentle = apply('acne_gentle')['params']
    strong = apply('acne_strong')['params']
    assert gentle['acne_sensitivity'] == .35 and strong['acne_sensitivity'] == .7
    assert gentle['acne'] == .2 and strong['acne'] == .8
    color = apply('warm_portrait', strong)['params']
    assert color['acne_sensitivity'] == .7 and color['acne'] == .8


def test_resolution_is_read_only():
    source, cached, current = rgb(), analysis(True), dict(NEUTRAL)
    selected = recipe('natural')
    before = deepcopy((selected, current, source, cached))
    resolve_preset(selected, current, source, cached)
    assert selected == before[0] and current == before[1]
    np.testing.assert_array_equal(source, before[2])
    np.testing.assert_array_equal(cached['face_mask'], before[3]['face_mask'])


@pytest.mark.parametrize('mask', [np.zeros((8, 8)), np.full((8, 8), np.nan), {'x': -.1, 'y': 0, 'width': 1, 'height': 1},
                                 {'x': 0, 'y': 0, 'width': 1.1, 'height': 1}])
def test_invalid_or_empty_selection_does_not_guess(mask):
    with pytest.raises(ValueError):
        source_statistics(rgb(), analysis(), mask)
