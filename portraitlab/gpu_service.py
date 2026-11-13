"""Persistent, bounded CUDA worker bridge. SPDX-License-Identifier: AGPL-3.0-only."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

import numpy as np

CPU_STAGE = "CPU 人脸识别、修肤、调色合成与文件压缩。"
GPU_STAGE = "CPU 人脸识别与修肤；GPU 曝光、面部曝光乘场、色彩、透明度合成和 sRGB 数值编码；CPU 文件压缩。"


def validate_device(value):
    if not isinstance(value, str) or value not in ("auto", "cpu", "cuda"):
        raise ValueError("处理设备须为 auto、cpu 或 cuda。")
    return value


class GPUCompositor:
    """Only this lock serializes CUDA work; no store lock is held while waiting."""
    def __init__(self, store, *, checked, app_root, timeout=90.):
        self.store, self.checked, self.app_root = store, checked, Path(app_root)
        self.root = checked(store.runtime_cache / "gpu")
        self.timeout = timeout
        self.lock = threading.RLock()
        self.process_lock = threading.Lock()
        self.closed = threading.Event()
        self.process = None
        self.readers = []
        self.responses = None
        self.stderr = deque(maxlen=80)
        self.probe_cache = None
        self.last_result = None

    def preference(self):
        with self.store.lock:
            return validate_device(self.store.state.get("processing_device", "auto"))

    def _start(self):
        if self.process is not None and self.process.poll() is not None:
            self._stop()
        with self.process_lock:
            if self.closed.is_set():
                raise RuntimeError("GPU 工作进程已关闭。")
            if self.process is not None and self.process.poll() is None:
                return
            python = self.store.relight.python()
            if python is None:
                raise RuntimeError("未安装含 CUDA PyTorch 的 AI 运行组件；经典修图可继续使用 CPU。")
            self.root.mkdir(parents=True, exist_ok=True)
            responses = queue.Queue()
            self.stderr.clear()
            process = subprocess.Popen(
                [str(python), "-X", "utf8", "-B", str(self.app_root / "portraitlab/gpu_worker.py"), "--root", str(self.root)],
                cwd=self.app_root, env=self.store.relight.environment(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.process, self.responses = process, responses

            def read_output():
                try:
                    for line in process.stdout:
                        try:
                            responses.put(json.loads(line))
                        except ValueError:
                            responses.put({"ok": False, "error": "GPU 进程返回了无法识别的数据。"})
                finally:
                    responses.put(None)

            def drain_errors():
                # Bounded chunks also drain warnings with no line breaks.
                while text := process.stderr.readline(4096):
                    self.stderr.append(text.rstrip())

            self.readers = [threading.Thread(target=read_output, name="portraitlab-gpu-out", daemon=True),
                            threading.Thread(target=drain_errors, name="portraitlab-gpu-err", daemon=True)]
            for reader in self.readers:
                reader.start()

    def _stop(self):
        with self.process_lock:
            process, self.process = self.process, None
            readers, self.readers = self.readers, []
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()

    def _invoke(self, request):
        """Caller holds self.lock. Failed/dead workers restart on the next call."""
        self._start()
        identifier = uuid.uuid4().hex
        try:
            self.process.stdin.write(json.dumps({"id": identifier, **request}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = self.responses.get(timeout=self.timeout)
            if response is None:
                raise RuntimeError("GPU 工作进程意外退出。" + (" " + self.stderr[-1][:400] if self.stderr else ""))
            if response.get("id") != identifier or not response.get("ok"):
                raise RuntimeError(str(response.get("error", "GPU 响应不匹配。"))[:1000])
            return response
        except queue.Empty as exc:
            self._stop()
            raise RuntimeError("GPU 计算超时，工作进程已停止；下一次处理将重新启动。") from exc
        except Exception:
            self._stop()
            raise

    def _probe(self, *, refresh=False):
        with self.lock:
            if self.probe_cache and not refresh and time.monotonic() - self.probe_cache[0] < 30:
                return dict(self.probe_cache[1])
            try:
                result = self._invoke({"action": "probe"})
                info = {"cuda_available": True, "classic_gpu_available": True, "gpu_name": result["gpu_name"],
                        "message": "CUDA 实际计算校验通过，可加速经典调色合成。"}
            except Exception as exc:
                info = {"cuda_available": False, "classic_gpu_available": False, "gpu_name": None,
                        "message": str(exc)}
            self.probe_cache = time.monotonic(), info
            return dict(info)

    def status(self, *, device=None, refresh=False):
        preference = validate_device(device if device is not None else self.preference())
        if preference == "cpu":
            # CPU-only mode never imports/launches Torch, even when querying UI.
            info = dict(self.probe_cache[1]) if self.probe_cache else {
                "cuda_available": False, "classic_gpu_available": False, "gpu_name": None}
            info["message"] = "已选择 CPU；此模式不启动 CUDA 检测或 GPU 工作进程。"
        else:
            info = self._probe(refresh=refresh)
        info.update(preference=preference, stage_note=GPU_STAGE if preference != "cpu" and info["classic_gpu_available"] else CPU_STAGE)
        if self.last_result:
            info["actual_device"] = self.last_result["actual_device"]
        if preference == "auto" and not info["classic_gpu_available"]:
            info["warning"] = "自动模式使用 CPU：" + info["message"]
        return info

    def validate_selection(self, device):
        value = validate_device(device)
        status = self.status(device=value, refresh=value != "cpu")
        if value == "cuda" and not status["classic_gpu_available"]:
            raise RuntimeError("无法选择 GPU：" + status["message"])
        return status

    def selection_changed(self, device):
        self.last_result = None
        if device == "cpu":
            # Serialize against an in-flight composition; it may finish first.
            with self.lock:
                self._stop()

    def passthrough(self):
        info = {"preference": self.preference(), "actual_device": "cpu", "gpu_name": None,
                "stage_note": "当前调整不改变像素，直接保留原图；未运行 GPU 合成。"}
        self.last_result = dict(info)
        return info

    def compose(self, original, changed, params, field):
        from .engine import compose_cpu
        preference = self.preference()
        warning = None
        if preference != "cpu" and params["opacity"] > 0:
            with self.lock:
                availability = self._probe()
                if availability["classic_gpu_available"]:
                    directory = self.checked(self.root / ("job-" + uuid.uuid4().hex))
                    directory.mkdir(parents=True)
                    try:
                        paths = {name: self.checked(directory / (name + ".npy")) for name in ("original", "changed", "field", "output")}
                        np.save(paths["original"], original, allow_pickle=False)
                        np.save(paths["changed"], np.asarray(changed, dtype=np.float32), allow_pickle=False)
                        if field is not None:
                            np.save(paths["field"], np.asarray(field, dtype=np.float32), allow_pickle=False)
                        response = self._invoke({"action": "compose", "original": str(paths["original"]),
                                                 "changed": str(paths["changed"]), "output": str(paths["output"]),
                                                 "field": str(paths["field"]) if field is not None else None,
                                                 "params": {key: params[key] for key in ("exposure", "relight", "warmth", "saturation", "opacity")}})
                        output = np.load(paths["output"], allow_pickle=False)
                        if output.dtype != np.uint8 or output.shape != original.shape or response.get("actual_device") != "cuda":
                            raise RuntimeError("GPU 输出的尺寸、精度或设备记录异常。")
                        info = {"preference": preference, "actual_device": "cuda", "gpu_name": response["gpu_name"],
                                "stage_note": GPU_STAGE, "tiles": response["tiles"],
                                "max_tile_pixels": response["max_tile_pixels"], "torch_version": response["torch_version"]}
                        self.last_result = dict(info)
                        return output, info
                    except Exception as exc:
                        self._stop()
                        warning = "GPU 合成失败：" + str(exc)
                        self.probe_cache = time.monotonic(), {"cuda_available": False, "classic_gpu_available": False,
                                                             "gpu_name": availability["gpu_name"], "message": warning}
                    finally:
                        # Delete only this exact owned job, never another worker's files.
                        resolved = directory.resolve()
                        if resolved.parent != self.root.resolve() or not resolved.name.startswith("job-"):
                            raise RuntimeError("GPU 临时作业路径校验失败。")
                        shutil.rmtree(resolved)
                else:
                    warning = availability["message"]
            if preference == "cuda":
                raise RuntimeError(warning or "GPU 合成不可用。")
        info = {"preference": preference, "actual_device": "cpu", "gpu_name": None, "stage_note": CPU_STAGE}
        if warning:
            info["warning"] = "已回退 CPU；" + warning
        output = compose_cpu(original, changed, params, field)
        self.last_result = dict(info)
        return output, info

    def close(self):
        self.closed.set()
        # Interrupt a pending request so shutdown need not wait for its timeout.
        self._stop()
