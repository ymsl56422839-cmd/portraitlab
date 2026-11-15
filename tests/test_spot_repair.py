"""Manual donor healing contracts; no model, network or filesystem operations."""
import copy
import json

import numpy as np
import pytest

from portraitlab.spot_repair import apply_repairs, validate_repairs, MAX_REPAIRS


def repair(**kwargs):
    return {"x": .7, "y": .5, "source_x": .25, "source_y": .5,
            "radius": .08, "strength": 1., **kwargs}


def support(shape, operation):
    h, w = shape[:2]
    yy, xx = np.indices((h, w))
    return np.hypot(xx - operation["x"] * (w - 1), yy - operation["y"] * (h - 1)) < operation["radius"] * min(h, w)


def test_canonical_validation_is_independent_and_defaults_only_strength():
    value = [repair()]
    del value[0]["strength"]
    before = copy.deepcopy(value)
    clean = validate_repairs(value)
    assert value == before
    assert clean[0]["strength"] == 1.
    assert all(type(v) is float for v in clean[0].values())
    clean[0]["x"] = .2
    assert value == before
    assert validate_repairs(None) == []
    assert len(validate_repairs([repair()] * MAX_REPAIRS)) == MAX_REPAIRS


@pytest.mark.parametrize("value", [
    {}, "[]", (), 1, [None], [[]], [{}], [repair(extra=1)],
    [{key: val for key, val in repair().items() if key == "strength"}],
    [repair()] * (MAX_REPAIRS + 1),
])
def test_rejects_invalid_structure(value):
    with pytest.raises(ValueError):
        validate_repairs(value)


@pytest.mark.parametrize("key", ["x", "y", "source_x", "source_y", "radius", "strength"])
@pytest.mark.parametrize("bad", [True, None, "0.5", float("nan"), float("inf"), -float("inf"), 10 ** 1000])
def test_rejects_non_numeric_and_non_finite_fields(key, bad):
    with pytest.raises(ValueError):
        validate_repairs([repair(**{key: bad})])


@pytest.mark.parametrize("key,bad", [("x", -.001), ("y", 1.01), ("source_x", -1),
    ("source_y", 2), ("strength", -.01), ("strength", 1.01),
    ("radius", 0), ("radius", .0009), ("radius", .25001)])
def test_rejects_out_of_range_values(key, bad):
    with pytest.raises(ValueError):
        validate_repairs([repair(**{key: bad})])


@pytest.mark.parametrize("image", [None, [], np.zeros((0, 2, 3), np.uint8),
    np.zeros((2, 2), np.uint8), np.zeros((2, 2, 4), np.uint8),
    np.zeros((2, 2, 3), np.float32)])
def test_rejects_non_rgb8_images(image):
    with pytest.raises(ValueError):
        apply_repairs(image, [])


@pytest.mark.parametrize("repairs", [None, [], [repair(strength=0)],
    [repair(source_x=.7, source_y=.5)]])
def test_identity_is_bit_exact_and_output_independent(repairs):
    image = np.random.default_rng(10).integers(0, 256, (80, 120, 3), dtype=np.uint8)
    before = image.copy()
    result, meta = apply_repairs(image, repairs)
    np.testing.assert_array_equal(image, before)
    np.testing.assert_array_equal(result, image)
    assert not np.shares_memory(result, image)
    assert meta["changed_pixels"] == meta["applied"] == 0
    json.dumps(meta, allow_nan=False)


def test_heals_compact_blemish_with_real_donor_texture():
    h, w = 128, 160
    yy, xx = np.indices((h, w))
    texture = 3 * np.sin(xx * np.pi / 4) + 2 * np.cos(yy * np.pi / 4)
    clean = np.clip(np.array([168, 128, 108])[None, None, :] + texture[..., None], 0, 255).astype(np.uint8)
    image = clean.copy()
    blemish = np.hypot(xx - 104, yy - 64) <= 4
    image[blemish] = [115, 45, 39]
    operation = repair(x=104 / (w - 1), y=64 / (h - 1), source_x=40 / (w - 1), source_y=64 / (h - 1))
    result, meta = apply_repairs(image, [operation])
    error_before = np.abs(image[blemish].astype(float) - clean[blemish]).mean()
    error_after = np.abs(result[blemish].astype(float) - clean[blemish]).mean()
    assert error_after < error_before * .12
    assert result[blemish].astype(float).std(axis=0).min() > .5
    np.testing.assert_array_equal(result[~support(image.shape, operation)], image[~support(image.shape, operation)])
    assert meta["applied"] == 1 and meta["changed_pixels"] > 0
    np.testing.assert_array_equal(image[blemish], np.tile([115, 45, 39], (blemish.sum(), 1)))


def test_strength_is_linear_light_blend_between_input_and_full_repair():
    image = np.full((80, 100, 3), [170, 130, 110], np.uint8)
    image[38:43, 67:72] = [80, 40, 30]
    full, _ = apply_repairs(image, [repair()])
    half, _ = apply_repairs(image, [repair(strength=.5)])
    yy, xx = 40, 69
    assert np.all(image[yy, xx] < half[yy, xx])
    assert np.all(half[yy, xx] < full[yy, xx])


@pytest.mark.parametrize("x,y,sx,sy", [(0, 0, 1, 1), (1, 0, 0, 1),
    (0, 1, 1, 0), (1, 1, 0, 0), (.5, .5, 0, 0), (0, .5, .7, .5)])
def test_edges_only_use_valid_corresponding_source_pixels(x, y, sx, sy):
    image = np.random.default_rng(3).integers(40, 210, (81, 101, 3), dtype=np.uint8)
    operation = repair(x=x, y=y, source_x=sx, source_y=sy, radius=.2)
    result, meta = apply_repairs(image, [operation])
    yy, xx = np.indices(image.shape[:2])
    src_x, src_y = xx + (sx - x) * 100, yy + (sy - y) * 80
    valid = support(image.shape, operation) & (src_x >= 0) & (src_x <= 100) & (src_y >= 0) & (src_y <= 80)
    np.testing.assert_array_equal(result[~valid], image[~valid])
    assert meta["warnings"]
    assert result.dtype == np.uint8 and result.shape == image.shape
    json.dumps(meta, allow_nan=False)


def test_subpixel_no_valid_pixels_is_explicit_noop():
    image = np.zeros((4, 4, 3), np.uint8)
    result, meta = apply_repairs(image, [repair(x=.5, y=.5, radius=.001)])
    np.testing.assert_array_equal(result, image)
    assert meta["repairs"][0]["no_op_reason"] == "no_valid_pixels"
    assert meta["warnings"]


@pytest.mark.parametrize("shape", [(1, 1, 3), (1, 12, 3), (12, 1, 3), (2, 2, 3)])
def test_tiny_images_are_safe(shape):
    image = np.full(shape, 128, np.uint8)
    result, meta = apply_repairs(image, [repair(x=0, y=0, source_x=1, source_y=1, radius=.25)])
    assert result.shape == image.shape and result.dtype == np.uint8
    assert not np.shares_memory(result, image)
    json.dumps(meta, allow_nan=False)


def test_donors_always_sample_input_and_replay_is_deterministic():
    image = np.random.default_rng(11).integers(80, 200, (128, 240, 3), dtype=np.uint8)
    first = repair(x=.3, y=.5, source_x=.1, source_y=.2, radius=.06)
    second = repair(x=.8, y=.5, source_x=.3, source_y=.5, radius=.06)
    single, _ = apply_repairs(image, [second])
    replay, metadata = apply_repairs(image, [first, second])
    again, repeated_meta = apply_repairs(image, [first, second])
    region = support(image.shape, second)
    np.testing.assert_array_equal(replay[region], single[region])
    np.testing.assert_array_equal(replay, again)
    assert metadata == repeated_meta
    assert metadata["changed_pixels"] == int(np.count_nonzero(np.any(replay != image, axis=2)))
    undone, _ = apply_repairs(image, [first])
    np.testing.assert_array_equal(undone[region], image[region])


def test_large_radius_uses_bounded_tone_fields_and_tiles(monkeypatch):
    import portraitlab.spot_repair as module
    original = module._tone_fields
    sizes = []
    def observed(*args):
        fields = original(*args)
        sizes.extend(field.shape[:2] for field in fields)
        return fields
    monkeypatch.setattr(module, "_tone_fields", observed)
    image = np.full((1000, 1400, 3), [170, 130, 110], np.uint8)
    image[480:520, 1000:1040] = [80, 40, 30]
    operation = repair(x=.73, radius=.25)
    result, meta = apply_repairs(image, [operation])
    assert all(max(shape) <= module.TONE_EDGE for shape in sizes)
    assert meta["applied"] == 1
    np.testing.assert_array_equal(result[~support(image.shape, operation)], image[~support(image.shape, operation)])


def test_constant_and_black_donors_do_not_create_a_dark_disc():
    image = np.full((96, 160, 3), [160, 120, 100], np.uint8)
    image[:, :70] = 0
    operation = repair(x=.8, source_x=.2, radius=.06)
    result, _ = apply_repairs(image, [operation])
    np.testing.assert_array_equal(result, image)
