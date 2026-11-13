"""Numerical/contract tests that do not download or require model weights."""
from pathlib import Path

import numpy as np
import pytest

from portraitlab.engine import Engine, DEFAULT_PARAMS, PRESETS, mask_for, validate_params, validate_strokes


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
