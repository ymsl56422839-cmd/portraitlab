"""Isolated generative worker queue. SPDX-License-Identifier: AGPL-3.0-only.

The HTTP process does not import PyTorch. Requests contain only server-selected
paths; generation is explicit, cancellable, and creates a new library image.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
import uuid

import numpy as np
from PIL import Image

ACTIVE = {"queued", "running", "cancelling"}
DEFAULTS = {"prompt": "portrait, natural soft window light, realistic skin texture, photographic",
            "negative_prompt": "low quality, blurry, deformed face, extra fingers, oversaturated",
            "direction": "left", "seed": 12345, "steps": 20, "denoise": .5,
            "cfg": 2., "long_side": 512, "low_vram": True, "device": "auto"}


def validate_options(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("重打光参数包含未知字段。")
    data = {**DEFAULTS, **raw}
    for key in ("prompt", "negative_prompt"):
        if not isinstance(data[key], str) or len(data[key]) > 2000:
            raise ValueError("提示词须为不超过 2000 字的文本。")
    if not data["prompt"].strip():
        raise ValueError("请填写光线描述或选择一个预设。")
    if data["direction"] not in ("none", "left", "right", "top", "bottom"):
        raise ValueError("光线方向不受支持。")
    if data["device"] not in ("auto", "cuda", "cpu"):
        raise ValueError("设备模式不受支持。")
    if type(data["low_vram"]) is not bool:
        raise ValueError("低显存模式须为布尔值。")
    for key, lo, hi in (("seed", -1, 2**32 - 1), ("steps", 10, 50)):
        if type(data[key]) is not int or not lo <= data[key] <= hi:
            raise ValueError(f"{key} 超出允许范围。")
    if type(data["long_side"]) is not int or data["long_side"] not in (256, 512, 768, 1024):
        raise ValueError("生成尺寸须为 256、512、768 或 1024 像素档。")
    for key, lo, hi in (("denoise", .1, .9), ("cfg", 1, 10)):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{key} 超出允许范围。")
    if data["seed"] == -1:
        data["seed"] = secrets.randbelow(2**32)
    return data


class Cancelled(Exception):
    pass


class RelightController:
    def __init__(self, store, *, checked, save_json, save_image, timestamp, app_root):
        self.store = store
        self.checked, self.save_json, self.save_image, self.now = checked, save_json, save_image, timestamp
        self.app_root = app_root
        self.root = checked(store.data / "relight")
        self.root.mkdir(parents=True, exist_ok=True)
        self.models = checked(os.environ.get("PORTRAITLAB_RELIGHT_MODELS", store.folders["models"] / "ic-light"))
        self.models.mkdir(parents=True, exist_ok=True)
        self.jobs = store.state.setdefault("relight_jobs", {})
        for job in self.jobs.values():
            if job["status"] in ACTIVE:
                job.update(status="interrupted", error="程序上次退出，未完成的生成需要重新提交。", updated_at=self.now())
        self.queue = queue.Queue()
        self.thread = None
        self.process = None
        self.stop = threading.Event()
        self.status_cache = None
        self.hardware = {}
        self.status_lock = threading.Lock()

    def python(self):
        configured = os.environ.get("PORTRAITLAB_AI_PYTHON")
        candidates = [Path(configured)] if configured else [
            self.app_root.parent / "AI/python.exe",
            self.app_root.parent / "AI/runtime/python.exe",
            self.app_root.parents[1] / "work/portraitlab-relight/venv/Scripts/python.exe",
            self.app_root / ".local/ai-venv/Scripts/python.exe",
            self.app_root / ".local/ai-venv/bin/python",
        ]
        return next((p.resolve() for p in candidates if p.is_file()), None)

    def environment(self):
        result = os.environ.copy()
        cache = self.checked(self.store.runtime_cache / "ai")
        temp = self.checked(self.store.folders["temp"] / "ai")
        for p in (cache, temp):
            p.mkdir(parents=True, exist_ok=True)
        result.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
                       "HF_HOME": str(cache / "huggingface"), "TORCH_HOME": str(cache / "torch"),
                       "XDG_CACHE_HOME": str(cache), "HF_HUB_DISABLE_TELEMETRY": "1",
                       "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", "DO_NOT_TRACK": "1",
                       "CUDA_CACHE_PATH": str(cache / "cuda"), "TORCH_EXTENSIONS_DIR": str(cache / "torch-extensions"),
                       "TORCHINDUCTOR_CACHE_DIR": str(cache / "inductor"), "TRITON_CACHE_DIR": str(cache / "triton")})
        return result

    def invoke(self, request, directory, job=None):
        python = self.python()
        if python is None:
            raise RuntimeError("尚未安装 AI 运行组件，请使用含 AI 运行时的便携包，或按说明安装可选组件。")
        directory = self.checked(directory)
        directory.mkdir(parents=True, exist_ok=True)
        req = self.checked(directory / "request.json")
        res = self.checked(directory / "result.json")
        progress = self.checked(directory / "progress.json")
        self.save_json(req, {**request, "models_dir": str(self.models)})
        for old in (res, progress):
            old.unlink(missing_ok=True)
        log = self.checked(directory / "worker.log")
        command = [str(python), "-X", "utf8", "-B", str(self.app_root / "portraitlab/relight.py"),
                   "--request", str(req), "--result", str(res), "--progress", str(progress)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with log.open("w", encoding="utf-8") as stream:
            proc = subprocess.Popen(command, cwd=self.app_root, env=self.environment(),
                                    stdout=stream, stderr=subprocess.STDOUT, creationflags=flags)
            if job is not None:
                self.process = proc
            started = time.monotonic()
            last_save = started
            try:
                while proc.poll() is None:
                    if self.stop.is_set() or (job and job.get("cancel_requested")):
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=10)
                        raise Cancelled("任务已停止。")
                    if job is None and time.monotonic() - started > 45:
                        proc.kill()
                        proc.wait(timeout=10)
                        raise RuntimeError("AI 组件检查超时，请查看工作区日志。")
                    if job and progress.exists():
                        try:
                            info = json.loads(progress.read_text(encoding="utf-8"))
                            with self.store.lock:
                                job["progress"] = info
                                job["updated_at"] = self.now()
                                if time.monotonic() - last_save > 2:
                                    self.store.persist()
                                    last_save = time.monotonic()
                        except (OSError, ValueError):
                            pass
                    self.stop.wait(.25)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                if self.process is proc:
                    self.process = None
        if not res.exists():
            raise RuntimeError(f"AI 进程未返回结果（退出码 {proc.returncode}），详情：{log}")
        response = json.loads(res.read_text(encoding="utf-8"))
        if proc.returncode or not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "AI 处理失败，请查看日志。"))
        return response

    def status(self, *, probe=False):
        with self.status_lock:
            if not probe and self.status_cache and time.monotonic() - self.status_cache[0] < 5:
                return self.status_cache[1]
            python = self.python()
            result = {"runtime_ready": bool(python), "ready": False, "models_dir": str(self.models),
                      "defaults": DEFAULTS, "message": "AI 运行组件尚未安装，请将全部 AI ZIP 解压到软件同一文件夹，再重新打开面板。"}
            if python:
                try:
                    info = self.invoke({"action": "status"}, self.root / "status")
                    result.update(info)
                    if probe:
                        self.hardware = self.invoke({"action": "probe"}, self.root / "probe")
                        self.hardware.pop("action", None)
                        self.hardware.pop("ok", None)
                        self.hardware["hardware_checked"] = True
                    result.update(self.hardware)
                    result["runtime_ready"] = True
                    result["message"] = "AI 模型已准备。" if result.get("ready") else "需要先下载 AI 模型。"
                except Exception as exc:
                    result["message"] = str(exc)
            self.status_cache = (time.monotonic(), result)
            return result

    def public_job(self, job):
        return {k: v for k, v in job.items() if k not in {"snapshots"}}

    def create(self, action, ids=None, params=None):
        if action not in {"download", "generate"}:
            raise ValueError("未知 AI 任务类型。")
        if not self.python():
            raise RuntimeError("缺少 AI 运行组件，请使用完整版或按说明准备 AI 运行时。")
        options = validate_options(params or {}) if action == "generate" else {}
        ids = list(dict.fromkeys(ids or []))
        if action == "generate" and not 1 <= len(ids) <= 50:
            raise ValueError("一次请选择 1～50 张照片进行重打光。")
        with self.store.lock:
            if any(j["status"] in ACTIVE for j in self.jobs.values()):
                raise RuntimeError("已有 AI 任务运行中，请等待完成或停止该任务。")
            snapshots = [json.loads(json.dumps(self.store.photo(i))) for i in ids]
            job_id = uuid.uuid4().hex
            directory = self.checked(self.root / job_id)
            directory.mkdir(parents=True)
            job = {"id": job_id, "action": action, "status": "queued", "done": 0,
                   "total": len(snapshots) if action == "generate" else 1, "params": options,
                   "snapshots": snapshots, "created_at": self.now(), "updated_at": self.now(),
                   "cancel_requested": False, "output_dir": str(directory), "items": [], "progress": {}}
            self.jobs[job_id] = job
            self.store.persist()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="portraitlab-ai", daemon=True)
                self.thread.start()
            self.queue.put(job_id)
            return self.public_job(job)

    def get(self, job_id):
        if job_id not in self.jobs:
            raise ValueError("未找到这个 AI 任务。")
        return self.jobs[job_id]

    def cancel(self, job_id):
        with self.store.lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                job.update(status="cancelling", cancel_requested=True, updated_at=self.now())
                self.store.persist()
            return self.public_job(job)

    def _loop(self):
        while not self.stop.is_set():
            job_id = self.queue.get()
            if job_id is None:
                break
            job = self.jobs[job_id]
            try:
                if job["cancel_requested"]:
                    raise Cancelled()
                with self.store.lock:
                    job.update(status="running", updated_at=self.now())
                    self.store.persist()
                if job["action"] == "download":
                    result = self.invoke({"action": "download"}, Path(job["output_dir"]), job)
                    job.update(done=1, result=result)
                else:
                    self._generate(job)
                job["status"] = "completed_with_errors" if any(i.get("error") for i in job["items"]) else "completed"
            except Cancelled:
                job["status"] = "cancelled"
            except Exception as exc:
                job.update(status="failed", error=str(exc))
            finally:
                with self.store.lock:
                    job["updated_at"] = self.now()
                    self.status_cache = None
                    self.store.persist()
                self.queue.task_done()

    def _generate(self, job):
        for index, snapshot in enumerate(job["snapshots"]):
            if job["cancel_requested"] or self.stop.is_set():
                raise Cancelled()
            directory = self.checked(Path(job["output_dir"]) / str(index + 1))
            directory.mkdir(parents=True)
            item = {"source_id": snapshot["id"], "name": snapshot["name"], "status": "running"}
            with self.store.lock:
                job["items"].append(item)
                job["progress"] = {"phase": "prepare", "message": "准备当前照片与人物前景…"}
                self.store.persist()
            try:
                rgb = self.store.rgb(snapshot)
                analysis = self.store.analysis(snapshot, rgb)
                background = analysis.get("parts", {}).get("background")
                if background is None:
                    raise ValueError("未获得人物前景选区，请使用可识别人脸的人像照片。")
                with self.store.engine_lock:
                    processed, _ = self.store.lighting.retouch_base(snapshot, rgb, analysis)
                input_path = self.checked(directory / "input.png")
                mask_path = self.checked(directory / "foreground.png")
                self.save_image(input_path, processed, "PNG")
                foreground = Image.fromarray(np.uint8(np.clip(1 - background, 0, 1) * 255))
                foreground = foreground.resize((processed.shape[1], processed.shape[0]), Image.Resampling.BILINEAR)
                from .face_shape import warp_image
                operations = self.store.shape_layout(snapshot, analysis=analysis)["operations"]
                if operations:
                    foreground = Image.fromarray(warp_image(np.asarray(foreground), operations))
                self.save_image(mask_path, foreground, "PNG")
                h, w = rgb.shape[:2]
                edge = job["params"]["long_side"]
                width = max(256, round(w / max(w, h) * edge / 64) * 64)
                height = max(256, round(h / max(w, h) * edge / 64) * 64)
                if max(w / h, h / w) > 4:
                    raise ValueError("请使用长宽比不超过 4:1 的人像，避免生成尺寸严重变形。")
                options = {k: v for k, v in job["params"].items() if k != "long_side"}
                options["seed"] = (options["seed"] + index) % 2**32
                output_path = self.checked(directory / "generated.png")
                result = self.invoke({"action": "generate", **options, "input_path": str(input_path),
                                      "mask_path": str(mask_path), "output_path": str(output_path),
                                      "width": width, "height": height}, directory, job)
                if job["cancel_requested"] or self.stop.is_set():
                    raise Cancelled()
                returned = Path(result.get("output_path", "")).resolve()
                if returned != output_path or not output_path.is_file():
                    raise RuntimeError("AI 输出路径或文件异常，未导入结果。")
                provenance = {"kind": "generative_relight", "source_id": snapshot["id"],
                              "source_name": snapshot["name"], "source_sha256": snapshot["sha256"],
                              "source_dimensions": [snapshot["width"], snapshot["height"]],
                              "input_edits": {"params": snapshot["params"], "strokes": snapshot.get("strokes", []),
                                              "local_adjustment": snapshot.get("local_adjustment"),
                                              "spot_repairs": snapshot.get("spot_repairs", []),
                                              "face_shape": snapshot.get("face_shape")},
                              "request": options, "metadata": result.get("metadata", {}),
                              "generated_dimensions": [result.get("width"), result.get("height")], "created_at": self.now()}
                generated = self.store.add_generated(output_path, snapshot["name"], provenance, input_path)
                self.save_json(directory / "provenance.json", provenance)
                item.update(status="completed", photo_id=generated["id"], width=generated["width"], height=generated["height"])
            except Cancelled:
                item["status"] = "cancelled"
                raise
            except Exception as exc:
                item.update(status="error", error=str(exc))
            finally:
                with self.store.lock:
                    job.update(done=index + 1, updated_at=self.now())
                    self.store.persist()

    def close(self):
        self.stop.set()
        self.queue.put(None)
        proc = self.process
        if proc and proc.poll() is None:
            proc.terminate()
        if self.thread:
            self.thread.join(timeout=15)
