"""Numerical/contract tests that do not download or require model weights."""
from pathlib import Path

import numpy as np
import cv2
import pytest

from portraitlab.engine import (Engine, DEFAULT_PARAMS, LOCAL_DEFAULT_PARAMS, PRESETS,
                                mask_for, validate_params, validate_strokes,
                                validate_local_adjustment, render_adjustments, ACNE_DETECTION_SENSITIVITY)
from portraitlab.vendor.rapidretouch_engine.tools import skin


@pytest.mark.parametrize("params", [{"smooth": True}, {"even": float("nan")}, {"typo": 1}, {"opacity": 2}, {"region": "all"}, {"engine": "mock"}])
def test_parameters_reject_unsafe_or_ambiguous_values(params):
    with pytest.raises(ValueError):
        validate_params(params)


def test_defaults_and_presets_are_valid():
    assert validate_params({}) == DEFAULT_PARAMS
    for preset in PRESETS.values():
        assert validate_params(preset) == preset


def test_brush_restores_only_original_selection():
    baseline = np.zeros((100, 160), np.float32)
    baseline[20:80, 40:120] = 0.8
    analysis = {"face_count": 1, "mask": baseline, "face_mask": baseline}
    protect = {"mode": "protect", "radius": 0.16, "points": [[0.5, 0.5]]}
    restore = {**protect, "mode": "restore"}
    erased = mask_for(analysis, baseline.shape, [protect])
    restored = mask_for(analysis, baseline.shape, [protect, restore])
    assert erased[50, 80] < 0.001
    assert restored[50, 80] > 0.799
    assert np.all(restored[baseline == 0] == 0)
    assert np.all(restored <= baseline + 1e-6)
    bigger = mask_for(analysis, (200, 320), [protect])
    assert bigger[100, 160] < 0.001


def test_brush_validation_and_no_face_guard():
    with pytest.raises(ValueError):
        validate_strokes([{"mode": "protect", "radius": 0.1, "points": [[2, 0]]}])
    mask = mask_for({"face_count": 0, "mask": np.ones((20, 20), np.float32)}, (40, 40))
    assert not mask.any()


def make_engine():
    # Constructor is read-only; rendering already-cached analysis needs no files.
    return Engine(Path(__file__).resolve().parents[1] / "models" / "uninstalled-test")


def no_face_analysis():
    return {"faces": [], "face_count": 0, "shape": [32, 32], "mask": np.zeros((32, 32), np.float32), "parts": {}}


def test_no_face_beauty_preserves_every_pixel():
    rgb = np.random.default_rng(7).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    out, _ = make_engine().render(rgb, DEFAULT_PARAMS, no_face_analysis())
    assert np.array_equal(out, rgb)


def test_exposure_uses_linear_light_and_opacity_zero_is_exact():
    rgb = np.full((32, 32, 3), 64, np.uint8)
    engine = make_engine()
    out, _ = engine.render(rgb, {"exposure": 1, "opacity": 1}, no_face_analysis())
    encoded = 64 / 255
    linear = ((encoded + 0.055) / 1.055) ** 2.4
    expected = round((1.055 * (2 * linear) ** (1 / 2.4) - 0.055) * 255)
    assert np.all(out == expected)
    unchanged, _ = engine.render(rgb, {"exposure": 2, "warmth": 1, "opacity": 0}, no_face_analysis())
    assert np.array_equal(unchanged, rgb)


def test_missing_models_do_not_create_or_download_files():
    engine = make_engine()
    assert not engine.models_dir.exists()
    assert not engine.status()["ready"]
    with pytest.raises(RuntimeError, match="不会自动联网"):
        engine.analyze(np.full((32, 32, 3), 64, np.uint8))
    assert not engine.models_dir.exists()


@pytest.mark.parametrize("raw", [
    {}, [], {"rect": {}, "params": {}},
    {"rect": {"x": .1, "y": .2, "width": .9, "height": .9}, "params": {}},
    {"rect": {"x": True, "y": 0, "width": .5, "height": .5}, "params": {}},
    {"rect": {"x": float("nan"), "y": 0, "width": .5, "height": .5}, "params": {}},
    {"rect": {"x": 0, "y": 0, "width": 0, "height": .5}, "params": {}},
    {"rect": {"x": 1, "y": 0, "width": 1e-14, "height": .5}, "params": {}},
    {"rect": {"x": 0, "y": 0, "width": .5, "height": .5}, "params": None},
    {"rect": {"x": 0, "y": 0, "width": .5, "height": .5}, "params": {"exposure": True}},
])
def test_local_adjustment_rejects_invalid_rectangles_and_params(raw):
    with pytest.raises(ValueError):
        validate_local_adjustment(raw)


def test_neutral_local_defaults_do_not_repeat_global_adjustments():
    rgb = np.random.default_rng(31).integers(20, 180, (80, 100, 3), dtype=np.uint8)
    processor = make_engine()
    params = {"exposure": .6, "warmth": .3, "saturation": .8, "opacity": .7}
    global_result, _ = processor.render(rgb, params, no_face_analysis())
    local = {"rect": {"x": .2, "y": .1, "width": .6, "height": .8}, "params": {}}
    assert validate_local_adjustment(local)["params"] == LOCAL_DEFAULT_PARAMS
    output, _ = render_adjustments(processor, rgb, params, no_face_analysis(), local_adjustment=local)
    np.testing.assert_array_equal(output, global_result)


def test_local_color_preserves_global_exterior_and_feathers_strictly_inside():
    rgb = np.full((200, 200, 3), [60, 80, 100], dtype=np.uint8)
    processor = make_engine()
    params = {"exposure": .5, "opacity": 1}
    global_result, _ = processor.render(rgb, params, no_face_analysis())
    local = {"rect": {"x": .25, "y": .25, "width": .5, "height": .5},
             "params": {"exposure": .8, "warmth": .3, "saturation": 1.3}}
    output, meta = render_adjustments(processor, rgb, params, no_face_analysis(), local_adjustment=local)
    exterior = np.ones((200, 200), bool)
    exterior[50:150, 50:150] = False
    np.testing.assert_array_equal(output[exterior], global_result[exterior])
    expected, _ = processor.render(global_result, validate_local_adjustment(local)["params"], no_face_analysis())
    np.testing.assert_array_equal(output[52:148, 52:148], expected[52:148, 52:148])
    assert np.all(output[50, 90] > global_result[50, 90])
    assert np.all(output[50, 90] < expected[50, 90])
    assert meta["local_adjustment"]["feather_pixels"] == 2


@pytest.mark.parametrize("value", [-.01, 1.01, True, float("nan"), "0.5"])
def test_acne_strength_validation(value):
    with pytest.raises(ValueError):
        validate_params({"acne": value})


def test_acne_strength_is_independent_of_detection_and_other_skin_tools(monkeypatch):
    rgb = np.full((40, 40, 3), 100, np.uint8)
    baseline = np.zeros((40, 40), np.float32)
    baseline[4:36, 4:36] = 1
    analysis = {"faces": [np.zeros((478, 2), np.float32)], "face_count": 1,
                "face_mask": baseline, "mask": baseline, "parts": {}}
    amounts = []
    def detect(source, faces, amount, **kwargs):
        amounts.append(amount)
        return [((0, 0, 1, 1), np.ones(source.shape[:2], np.float32))]
    def repair(source, faces, params, **kwargs):
        assert params == {"acne": 1.0}
        assert kwargs["acne"]
        return source + .2
    monkeypatch.setattr(skin, "acne_masks", detect)
    monkeypatch.setattr(skin, "apply", repair)
    processor = make_engine()
    results = []
    for strength in (0, .25, .5, 1):
        result, meta = processor.render(rgb, {**LOCAL_DEFAULT_PARAMS, "acne": strength}, analysis)
        results.append(result)
        np.testing.assert_array_equal(result[baseline == 0], rgb[baseline == 0])
        assert abs(int(result[20, 20, 0]) - (100 + 51 * strength)) <= 1
        assert meta["acne"]["detection_sensitivity"] == .5
    assert amounts == [.5, .5, .5]
    np.testing.assert_array_equal(results[0], rgb)
    protect = [{"mode": "protect", "radius": .2, "points": [[.5, .5]]}]
    protected, _ = processor.render(rgb, {**LOCAL_DEFAULT_PARAMS, "acne": 1}, analysis, protect)
    np.testing.assert_array_equal(protected[20, 20], rgb[20, 20])
    # The same acne-only repair is confined to the chosen local rectangle.
    local = {"rect": {"x": .5, "y": .5, "width": .4, "height": .4}, "params": {"acne": 1}}
    selected, _ = render_adjustments(processor, rgb, LOCAL_DEFAULT_PARAMS, analysis, local_adjustment=local)
    np.testing.assert_array_equal(selected[:20], rgb[:20])
    np.testing.assert_array_equal(selected[:, :20], rgb[:, :20])
    assert selected[28, 28, 0] > rgb[28, 28, 0]


def test_acne_with_no_detected_spots_or_no_face_is_exact(monkeypatch):
    rgb = np.random.default_rng(8).integers(0, 256, (40, 40, 3), dtype=np.uint8)
    analysis = {"faces": [np.zeros((478, 2), np.float32)], "face_count": 1,
                "face_mask": np.ones((40, 40), np.float32), "parts": {}}
    monkeypatch.setattr(skin, "acne_masks", lambda *args, **kwargs: [((0, 0, 1, 1), np.zeros((40, 40), np.float32))])
    monkeypatch.setattr(skin, "apply", lambda *args, **kwargs: pytest.fail("No repair should run without a detected spot"))
    for current in (analysis, no_face_analysis()):
        output, meta = make_engine().render(rgb, {**LOCAL_DEFAULT_PARAMS, "acne": 1}, current)
        np.testing.assert_array_equal(output, rgb)
        assert meta["acne"]["spots_found"] is False


def test_zero_detection_sensitivity_still_repairs_clearest_spots(monkeypatch):
    # Stub only face localization: detection, skin.apply and frequency-separated
    # repair remain the real upstream code. Sensitivity zero means clearest-only,
    # not disabled; passing zero into skin.apply used to activate its noop path.
    size, face_width = 256, 200
    yy, xx = np.mgrid[:size, :size]
    lab = np.empty((size, size, 3), np.float32)
    lab[:] = (65, 12, 18)
    lab[..., 0] += .35 * np.sin(xx * 2.1) * np.cos(yy * 1.8)
    red = (xx - 80) ** 2 + (yy - 80) ** 2 <= 9
    lab[red] += (-8, 12, 0)
    source = np.rint(np.clip(cv2.cvtColor(lab, cv2.COLOR_Lab2RGB), 0, 1) * 255).astype(np.uint8)
    weight = np.ones((size, size), np.float32)
    landmarks = np.zeros((478, 2), np.float32)
    cached = {"faces": [landmarks], "face_count": 1, "face_mask": weight, "parts": {}}
    monkeypatch.setattr(skin, "_face_boxes", lambda shape, faces: [(landmarks, face_width, (0, 0, size, size))])
    monkeypatch.setattr(skin, "_face_weights", lambda *args, **kwargs: (weight, weight, None, landmarks))
    monkeypatch.setattr(skin, "face_width", lambda points: face_width)
    monkeypatch.setattr(skin, "wrinkle_zones", lambda shape, points: {"chin_lines": np.zeros(shape, np.float32)})
    spots = skin.acne_masks(source.astype(np.float32) / 255, cached["faces"], 0)
    assert spots[0][1][red].mean() > .7
    result, meta = make_engine().render(source, {**LOCAL_DEFAULT_PARAMS, "acne": 1, "acne_sensitivity": 0}, cached)
    assert meta["acne"]["detection_sensitivity"] == 0
    assert meta["acne"]["spots_found"] is True
    before_a = cv2.cvtColor(source.astype(np.float32) / 255, cv2.COLOR_RGB2Lab)[..., 1]
    after_a = cv2.cvtColor(result.astype(np.float32) / 255, cv2.COLOR_RGB2Lab)[..., 1]
    assert np.abs(after_a[red] - 12).mean() < .5 * np.abs(before_a[red] - 12).mean()


def test_upstream_acne_repairs_red_spot_preserves_mole_line_and_texture():
    # Actual detector and frequency-separated inpainting, without model files.
    size, face_width = 256, 200
    yy, xx = np.mgrid[:size, :size]
    lab = np.empty((size, size, 3), np.float32)
    lab[:] = (65, 12, 18)
    lab[..., 0] += .35 * np.sin(xx * 2.1) * np.cos(yy * 1.8)
    clean = lab.copy()
    red = ((xx - 80) ** 2 + (yy - 80) ** 2) <= 3 ** 2
    mole = ((xx - 174) ** 2 + (yy - 80) ** 2) <= 3 ** 2
    line = (yy >= 170) & (yy < 172) & (xx > 50) & (xx < 200)
    lab[red] += (-8, 12, 0)
    lab[mole] += (-18, .5, 0)
    lab[line] += (-8, 1, 0)
    original = lab.copy()
    weight = np.ones((size, size), np.float32)
    spots = skin.acne_spots(lab, weight, face_width, ACNE_DETECTION_SENSITIVITY, weight)
    assert spots[red].mean() > .7
    assert not np.any(spots[mole]) and not np.any(spots[line])
    skin._heal_acne(lab, weight, face_width, ACNE_DETECTION_SENSITIVITY, weight, spots)
    assert np.abs(lab[red, 1] - clean[red, 1]).mean() < .5 * np.abs(original[red, 1] - clean[red, 1]).mean()
    np.testing.assert_array_equal(lab[mole], original[mole])
    np.testing.assert_array_equal(lab[line], original[line])
    # Fine texture survives in the repaired centre, unlike a constant fill.
    fine = lab[..., 0] - cv2.GaussianBlur(lab[..., 0], (0, 0), .8)
    assert fine[78:83, 78:83].std() > .04


def test_local_pass_reports_its_actual_device():
    class ChangingDevice:
        def __init__(self):
            self.calls = 0
        def render(self, rgb, params, analysis, strokes=None):
            self.calls += 1
            return rgb.copy(), {"processing_device": {"actual_device": "cuda" if self.calls == 1 else "cpu"}, "warnings": []}
    rgb = np.zeros((20, 20, 3), np.uint8)
    _, meta = render_adjustments(ChangingDevice(), rgb, LOCAL_DEFAULT_PARAMS, {},
                                local_adjustment={"rect": {"x": .2, "y": .2, "width": .5, "height": .5}, "params": {}})
    assert meta["processing_device"]["actual_device"] == "cpu"
