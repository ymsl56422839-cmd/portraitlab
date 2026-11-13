"""Opt-in real CUDA comparison: set PORTRAITLAB_GPU_TEST_PYTHON to the AI Python.

Normal CI does not install Torch/download anything. The numerical reference is
the production NumPy CPU compositor, not a second copy of the CUDA formula.
"""
import os
from pathlib import Path

import numpy as np
import pytest

from portraitlab.engine import compose_cpu, validate_params
from test_gpu_service import service


@pytest.mark.skipif(not os.environ.get("PORTRAITLAB_GPU_TEST_PYTHON"), reason="Explicit CUDA runtime required")
@pytest.mark.parametrize("params,with_field", [
    ({"opacity": 1, "exposure": 1.2}, False),
    ({"opacity": .37, "exposure": -1.8, "warmth": -.91, "saturation": 1.8, "relight": .77}, True),
    ({"opacity": 1, "exposure": 2, "warmth": 1, "saturation": 2, "relight": -.88}, True),
    ({"opacity": .93, "exposure": .8, "warmth": 1, "saturation": 0, "relight": -.68}, True),
    ({"opacity": 0, "exposure": 2, "warmth": -1, "saturation": 2, "relight": 1}, True),
])
def test_real_cuda_matches_cpu_within_one_8bit_value(tmp_path, params, with_field):
    python = Path(os.environ["PORTRAITLAB_GPU_TEST_PYTHON"])
    assert python.is_file()
    gpu = service(tmp_path, preference="cuda", python=python, timeout=90)
    gpu.app_root = Path(__file__).resolve().parents[1]
    rng = np.random.default_rng(20261113)
    # Cross the fixed CUDA tile boundary in both dimensions.
    original = rng.integers(0, 256, (513, 1025, 3), np.uint8)
    changed = np.clip(original.astype(np.float32) / 255 + rng.normal(0, .04, original.shape).astype(np.float32), 0, 1)
    field = rng.uniform(0, 1, original.shape[:2]).astype(np.float32) if with_field else None
    params = validate_params(params)
    try:
        expected = compose_cpu(original, changed, params, field)
        actual, metadata = gpu.compose(original, changed, params, field)
        assert np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max() <= 1
        if params["opacity"] == 0:
            np.testing.assert_array_equal(actual, original)
            assert gpu.process is None
        else:
            assert metadata["actual_device"] == "cuda" and metadata["tiles"] >= 3
            assert list(gpu.root.iterdir()) == []
    finally:
        gpu.close()
