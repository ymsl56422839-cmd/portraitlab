"""Device fallback and real subprocess lifecycle, without Torch or a GPU."""
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from portraitlab.engine import Engine, compose_cpu, validate_params
from portraitlab.gpu_service import GPUCompositor, validate_device


def service(tmp_path, *, preference="auto", python=None, timeout=5):
    def checked(path):
        value = Path(path).resolve()
        assert value.is_relative_to(tmp_path.resolve())
        return value
    def environment():
        result = {**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path), "TMPDIR": str(tmp_path),
                  "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"}
        for name in ("CUDA_CACHE_PATH", "TORCH_HOME", "XDG_CACHE_HOME", "TORCH_EXTENSIONS_DIR", "TORCHINDUCTOR_CACHE_DIR", "TRITON_CACHE_DIR"):
            target = checked(tmp_path / "runtime-cache" / name.lower())
            target.mkdir(parents=True, exist_ok=True)
            result[name] = str(target)
        return result
    store = SimpleNamespace(lock=threading.RLock(), state={"processing_device": preference},
                            runtime_cache=tmp_path / "cache",
                            relight=SimpleNamespace(python=lambda: python, environment=environment))
    return GPUCompositor(store, checked=checked, app_root=tmp_path, timeout=timeout)


def image_values():
    original = np.random.default_rng(55).integers(0, 256, (32, 48, 3), np.uint8)
    changed = np.clip(original.astype(np.float32) / 255 + .025, 0, 1)
    return original, changed, validate_params({"exposure": .4, "warmth": -.6, "opacity": .75}), None


@pytest.mark.parametrize("value", [None, [], {}, "gpu", "CUDA", 1, True])
def test_device_contract(value):
    with pytest.raises(ValueError):
        validate_device(value)


def test_cpu_never_probes_or_launches_worker(tmp_path, monkeypatch):
    gpu = service(tmp_path, preference="cpu", python=sys.executable)
    monkeypatch.setattr(gpu, "_start", lambda: pytest.fail("CPU mode attempted CUDA startup"))
    assert gpu.status()["preference"] == "cpu"
    args = image_values()
    result, meta = gpu.compose(*args)
    np.testing.assert_array_equal(result, compose_cpu(*args))
    assert meta["actual_device"] == "cpu" and "warning" not in meta
    assert not gpu.root.exists()
    gpu.close()


def test_uninstalled_runtime_auto_falls_back_explicit_cuda_errors(tmp_path):
    gpu = service(tmp_path)
    args = image_values()
    result, meta = gpu.compose(*args)
    np.testing.assert_array_equal(result, compose_cpu(*args))
    assert meta["actual_device"] == "cpu" and "回退 CPU" in meta["warning"]
    assert not gpu.status()["classic_gpu_available"]
    gpu.store.state["processing_device"] = "cuda"
    with pytest.raises(RuntimeError, match="运行组件"):
        gpu.compose(*args)
    with pytest.raises(RuntimeError, match="无法选择 GPU"):
        gpu.validate_selection("cuda")
    gpu.close()


def test_opacity_zero_exact_without_cuda_and_engine_metadata(tmp_path, monkeypatch):
    gpu = service(tmp_path, preference="cuda", python=sys.executable)
    monkeypatch.setattr(gpu, "_probe", lambda **_: pytest.fail("Opacity zero attempted CUDA"))
    original, changed, params, field = image_values()
    params["opacity"] = 0
    result, meta = gpu.compose(original, changed, params, field)
    np.testing.assert_array_equal(result, original)
    engine = Engine(tmp_path / "models")
    engine.compositor = gpu
    result, meta = engine.render(original, params, {"faces": [], "face_count": 0})
    np.testing.assert_array_equal(result, original)
    assert meta["processing_device"]["actual_device"] == gpu.last_result["actual_device"] == "cpu"
    gpu.close()


FAKE_WORKER = '''import json, sys, time
from pathlib import Path
import numpy as np
for line in sys.stdin:
    request = json.loads(line)
    if request["action"] == "hang":
        time.sleep(60)
    elif request["action"] == "crash":
        sys.exit(11)
    elif request["action"] == "stderr":
        sys.stderr.write("warning\\n" * 40000)
        sys.stderr.flush()
    elif request["action"] == "compose":
        np.save(request["output"], np.load(request["original"]))
    print(json.dumps({"id": request["id"], "ok": True, "gpu_name": "protocol-test-only",
                     "actual_device": "cuda", "tiles": 1, "max_tile_pixels": 262144,
                     "torch_version": "fake-protocol-test"}), flush=True)
'''


def fake_service(tmp_path, timeout=5):
    worker = tmp_path / "portraitlab/gpu_worker.py"
    worker.parent.mkdir()
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    return service(tmp_path, python=sys.executable, timeout=timeout)


def test_persistent_protocol_drains_stderr_cleans_jobs_and_closes(tmp_path):
    gpu = fake_service(tmp_path)
    try:
        assert gpu.status()["classic_gpu_available"]
        process = gpu.process
        with gpu.lock:
            assert gpu._invoke({"action": "stderr"})["ok"]
        assert len(gpu.stderr) <= 80
        result, meta = gpu.compose(*image_values())
        assert meta["actual_device"] == "cuda" and meta["gpu_name"] == "protocol-test-only"
        assert gpu.process is process
        assert list(gpu.root.iterdir()) == []
        gpu.store.state["processing_device"] = "cpu"
        gpu.selection_changed("cpu")
        assert process.poll() is not None and gpu.process is None
    finally:
        gpu.close()


@pytest.mark.parametrize("action", ["hang", "crash"])
def test_timeout_and_death_terminate_and_restart(tmp_path, action):
    gpu = fake_service(tmp_path)
    try:
        assert gpu.status()["classic_gpu_available"]
        process = gpu.process
        gpu.timeout = .2
        with gpu.lock, pytest.raises(RuntimeError):
            gpu._invoke({"action": action})
        assert process.poll() is not None and gpu.process is None
        gpu.timeout = 5
        assert gpu.status(refresh=True)["classic_gpu_available"]
        assert gpu.process is not process
    finally:
        gpu.close()
    assert gpu.process is None


def test_runtime_failure_fallback_and_explicit_failure_cleanup(tmp_path, monkeypatch):
    gpu = service(tmp_path)
    gpu.root.mkdir(parents=True)
    monkeypatch.setattr(gpu, "_probe", lambda **_: {"classic_gpu_available": True, "gpu_name": "fake"})
    def out_of_memory(_):
        raise RuntimeError("CUDA out of memory")
    monkeypatch.setattr(gpu, "_invoke", out_of_memory)
    args = image_values()
    result, meta = gpu.compose(*args)
    np.testing.assert_array_equal(result, compose_cpu(*args))
    assert "out of memory" in meta["warning"] and meta["actual_device"] == "cpu"
    assert list(gpu.root.iterdir()) == []
    gpu.store.state["processing_device"] = "cuda"
    with pytest.raises(RuntimeError, match="out of memory"):
        gpu.compose(*args)
    assert list(gpu.root.iterdir()) == []
    gpu.close()


def test_close_interrupts_inflight_request_without_waiting_for_timeout(tmp_path):
    gpu = fake_service(tmp_path, timeout=60)
    assert gpu.status()["classic_gpu_available"]
    process = gpu.process
    entered = threading.Event()
    errors = []
    def pending():
        with gpu.lock:
            entered.set()
            try:
                gpu._invoke({"action": "hang"})
            except Exception as exc:
                errors.append(exc)
    thread = threading.Thread(target=pending)
    thread.start()
    assert entered.wait(5)
    gpu.close()
    thread.join(5)
    assert not thread.is_alive() and errors
    assert process.poll() is not None
