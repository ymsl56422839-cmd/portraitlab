"""Cached geometry and reversible, non-generative lighting edits.

The server retains unlit pixels. Only explicit preparation runs a geometry
worker; moving lights in the browser never performs inference.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

import cv2
import numpy as np
from fastapi import HTTPException
from PIL import Image

from . import lighting
from . import lighting_geometry

ACTIVE = {"queued", "running", "cancelling"}
ASSETS = {"base.png", "normal.png", "depth.png", "mask.png"}
GEOMETRY_SCHEMA = "portraitlab-interactive-lighting-1"
SUBJECT_MASK_POLICY_VERSION = "person-probability-feather-10-90-v2"
SUBJECT_MASK_LOW, SUBJECT_MASK_HIGH = .1, .9
BASE_CONTRACT = "canonical-full-resolution-retouch-v1"
BASE_CACHE_BYTES = 512 * 1024 * 1024
BASE_CACHE_PER_PHOTO = 3
_UNCHANGED_GEOMETRY = object()


class Cancelled(RuntimeError):
    pass


class LightingController:
    def __init__(self, store, *, checked, save_json, save_image, timestamp, app_root):
        self.store, self.checked = store, checked
        self.save_json, self.save_image, self.now = save_json, save_image, timestamp
        self.app_root = Path(app_root)
        self.root = checked(store.data / "lighting")
        self.geometry_root = checked(self.root / "geometry")
        self.sessions = checked(self.root / "sessions")
        self.base_root = checked(self.root / "retouch-base")
        self.models = checked(store.folders["models"])
        for directory in (self.root, self.geometry_root, self.sessions, self.models, self.base_root):
            directory.mkdir(parents=True, exist_ok=True)
        self.jobs = store.state.setdefault("lighting_jobs", {})
        for job in self.jobs.values():
            if job.get("status") in ACTIVE:
                job.update(status="interrupted", error="上次分析未完成，请重新准备照片。", updated_at=self.now())
        self.queue = queue.Queue()
        self.thread = None
        self.stop = threading.Event()
        self.process = None
        self._cancellations = {}

    def public_job(self, job):
        return {key: value for key, value in job.items() if key not in {"snapshot", "snapshots"}}

    def get(self, identifier):
        if identifier not in self.jobs:
            raise HTTPException(404, "未找到此布光任务。")
        return self.jobs[identifier]

    def status(self):
        model = lighting_geometry.status(self.models)
        runtime = self.store.relight.python()
        with self.store.lock:
            active = next((self.public_job(job) for job in self.jobs.values() if job["status"] in ACTIVE), None)
        ready = bool(runtime and model.get("ready"))
        model_state = "ready" if model.get("ready") else "missing"
        if active and active["action"] == "download":
            model_state = "downloading"
        result = {"runtime_ready": bool(runtime), "ready": ready, "model_state": model_state,
                  "capabilities": {"canonical_preview": True, "refresh_geometry": True,
                                   "geometry_restore": True},
                  "models": model, "defaults": lighting.DEFAULT_LIGHTING, "light_defaults": lighting.DEFAULT_LIGHT,
                  "presets": lighting.LIGHT_PRESETS,
                  "active_job": active,
                  "message": "照片分析模型已就绪。" if ready else "请先准备照片分析模型。" if runtime else "需要包含 AI 运行组件的完整软件目录。"}
        probe = getattr(self.store.gpu, "probe_cache", None)
        if probe:
            result.update({key: probe[1].get(key) for key in ("cuda_available", "gpu_name")})
        return result

    def create(self, action, photo_id=None, device="auto", refresh_geometry=False):
        if action not in {"download", "prepare"} or not isinstance(device, str) or device not in {"auto", "cpu", "cuda"}:
            raise ValueError("布光任务或设备选项无效。")
        if type(refresh_geometry) is not bool:
            raise ValueError("重新分析选项必须为布尔值。")
        with self.store.lock:
            if any(job["status"] in ACTIVE for job in self.jobs.values()):
                raise HTTPException(409, "已有布光准备任务，请等待完成或先停止。")
            snapshot = json.loads(json.dumps(self.store.photo(photo_id))) if action == "prepare" else None
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "action": action, "status": "queued", "device": device,
                   "refresh_geometry": refresh_geometry,
                   "photo_id": photo_id, "created_at": self.now(), "updated_at": self.now(),
                   "progress": {"message": "等待准备…", "fraction": 0}, "cancel_requested": False,
                   "snapshot": snapshot, "snapshots": [snapshot] if snapshot else []}
            self.jobs[identifier] = job
            self.store.persist()
            self._cancellations[identifier] = threading.Event()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="portraitlab-lighting", daemon=True)
                self.thread.start()
            self.queue.put(identifier)
            return self.public_job(job)

    def cancel(self, identifier):
        with self.store.lock:
            job = self.get(identifier)
            if job["status"] in ACTIVE:
                job.update(cancel_requested=True, status="cancelling", updated_at=self.now())
                self._cancellations.setdefault(identifier, threading.Event()).set()
                self.store.persist()
            return self.public_job(job)

    def _check_cancel(self, job):
        if self.stop.is_set() or self._cancellations[job["id"]].is_set():
            raise Cancelled("布光准备已停止。")

    def _progress(self, job, message, fraction=None):
        self._check_cancel(job)
        if isinstance(message, dict):
            data = dict(message)
        else:
            data = {"message": str(message)}
        if fraction is not None:
            data["fraction"] = float(fraction)
        with self.store.lock:
            job.update(progress=data, updated_at=self.now())

    def _loop(self):
        while not self.stop.is_set():
            identifier = self.queue.get()
            if identifier is None:
                self.queue.task_done()
                return
            job = self.jobs[identifier]
            try:
                self._check_cancel(job)
                with self.store.lock:
                    job.update(status="running", updated_at=self.now())
                    self.store.persist()
                if job["action"] == "download":
                    result = lighting_geometry.download(self.models,
                        progress=lambda message, *args: self._progress(job, message, *args),
                        cancel_event=self._cancellations[identifier])
                    job["result"] = result if isinstance(result, dict) else lighting_geometry.status(self.models)
                else:
                    job["result"] = self._prepare(job)
                self._check_cancel(job)
                job.update(status="completed", progress={"message": "准备完成。", "fraction": 1})
            except Exception as exc:
                cancelled = self.stop.is_set() or self._cancellations[identifier].is_set()
                job.update(status="cancelled" if cancelled else "failed", error=str(exc))
            finally:
                with self.store.lock:
                    job["updated_at"] = self.now()
                    self.store.persist()
                self.queue.task_done()

    def geometry_key(self, snapshot):
        signature = str(lighting_geometry.geometry_signature())
        analysis_signature = json.dumps(self.store.analysis_signature, sort_keys=True)
        return hashlib.sha256((GEOMETRY_SCHEMA+SUBJECT_MASK_POLICY_VERSION+snapshot["sha256"]+signature+analysis_signature).encode()).hexdigest()

    def base_key(self, snapshot):
        """A lighting edit never invalidates its unlit, full-resolution base."""
        signature = {"contract": BASE_CONTRACT, "source": snapshot["sha256"],
                     "params": snapshot["params"], "strokes": snapshot.get("strokes", []),
                     "local_adjustment": snapshot.get("local_adjustment"),
                     "analysis": self.store.analysis_signature,
                     "models": self.store.model_fingerprint(),
                     "device_preference": self.store.state.get("processing_device", "auto")}
        return hashlib.sha256(json.dumps(signature, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def retouch_base(self, snapshot, original=None, analysis=None):
        """Render retouching once at source resolution; all lighting paths reuse it.

        The cache is disposable, never an edit record. A failed or incomplete
        image/metadata pair is ignored and recomputed from the source pixels.
        """
        key = self.base_key(snapshot)
        directory = self.checked(self.base_root / snapshot["id"])
        if directory.parent != self.base_root:
            raise ValueError("修片底图缓存位置无效。")
        image_path, meta_path = directory / (key+".png"), directory / (key+".json")
        with self.store.engine_lock:
            try:
                cached = json.loads(meta_path.read_text(encoding="utf-8"))
                if cached.get("key") != key or cached.get("contract") != BASE_CONTRACT:
                    raise ValueError("底图缓存版本不同")
                with Image.open(image_path) as image:
                    if image.size != (snapshot["width"], snapshot["height"]):
                        raise ValueError("底图缓存尺寸不同")
                    base = np.asarray(image.convert("RGB")).copy()
                os.utime(image_path, None)
                meta = cached["meta"]
                meta["retouch_base"] = {"contract": BASE_CONTRACT, "key": key, "cache_hit": True}
                return base, meta
            except (OSError, ValueError, KeyError):
                pass
            original = self.store.rgb(snapshot) if original is None else original
            analysis = self.store.analysis(snapshot, original) if analysis is None else analysis
            local = self.store.engine_module.validate_local_adjustment(snapshot.get("local_adjustment"))
            if local is not None:
                # Moving/resizing the rectangle or changing its sliders reuses
                # the expensive global skin pass, including after a restart.
                global_snapshot = {**snapshot, "local_adjustment": None}
                global_base, meta = self.retouch_base(global_snapshot, original, analysis)
                base, local_meta = self.store.engine_module.apply_local_adjustment(
                    self.store.engine, global_base, analysis, local, snapshot.get("strokes", []))
                meta = {**meta, "local_adjustment": local_meta}
                meta["warnings"] = list(dict.fromkeys(meta.get("warnings", []) + local_meta.get("engine_meta", {}).get("warnings", [])))
                if local_meta.get("engine_meta", {}).get("processing_device"):
                    meta["processing_device"] = local_meta["engine_meta"]["processing_device"]
            else:
                base, meta = self.store.engine_module.render_adjustments(
                    self.store.engine, original, snapshot["params"], analysis, snapshot.get("strokes", []))
            directory.mkdir(parents=True, exist_ok=True)
            self.save_image(image_path, base, "PNG")
            self.save_json(meta_path, {"contract": BASE_CONTRACT, "key": key, "meta": meta})
            self._prune_base_cache(image_path)
            meta = dict(meta)
            meta["retouch_base"] = {"contract": BASE_CONTRACT, "key": key, "cache_hit": False}
            return base, meta

    def _prune_base_cache(self, keep):
        """Bound disposable images; retain the just-produced file even if huge."""
        def discard(path):
            try:
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
            except OSError:
                pass  # A later preparation retries pruning an in-use file.
        siblings = sorted(keep.parent.glob("*.png"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in siblings[BASE_CACHE_PER_PHOTO:]:
            if path != keep:
                discard(path)
        files = sorted(self.base_root.glob("*/*.png"), key=lambda p: p.stat().st_mtime_ns)
        total = sum(path.stat().st_size for path in files)
        for path in files:
            if total <= BASE_CACHE_BYTES:
                break
            if path == keep:
                continue
            size = path.stat().st_size
            discard(path)
            if not path.exists():
                total -= size

    @staticmethod
    def resize_base(base, width, height):
        if base.shape[:2] == (height, width):
            return base
        return cv2.resize(base, (width, height), interpolation=cv2.INTER_AREA)

    def preview_size(self, snapshot):
        """Saved previews use exactly the same sample grid as the live editor."""
        key = snapshot.get("lighting_geometry")
        if key:
            _, meta = self._geometry_meta(key, snapshot["sha256"])
            return int(meta["width"]), int(meta["height"])
        ratio = min(1., 1024/max(snapshot["width"], snapshot["height"]))
        return max(16, round(snapshot["width"]*ratio)), max(16, round(snapshot["height"]*ratio))

    def geometry_directory(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("照片布光分析标识无效，请重新准备照片。")
        path = self.checked(self.geometry_root / key)
        if path.parent != self.geometry_root:
            raise ValueError("无效分析缓存位置。")
        return path

    def _geometry_meta(self, key, source_sha):
        directory = self.geometry_directory(key)
        try:
            meta = json.loads((directory / "prepared.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("布光分析缓存不可用，请重新准备照片。") from exc
        if (meta.get("schema") != GEOMETRY_SCHEMA or meta.get("source_sha256") != source_sha
                or any(not (directory / name).is_file() for name in ("normal.png", "depth.png", "mask.png"))):
            raise RuntimeError("布光分析缓存与照片不匹配，请重新准备。")
        return directory, meta

    def _infer_geometry(self, input_path, output_dir, device, job):
        python = self.store.relight.python()
        if python is None:
            raise RuntimeError("缺少照片分析运行组件，请使用完整软件目录。")
        env = self.store.relight.environment()
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        command = [str(python), "-B", str(self.app_root / "portraitlab/lighting_worker.py"),
                   "--input", str(input_path), "--output", str(output_dir), "--models", str(self.models),
                   "--device", device, "--edge", "1024"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with (output_dir / "worker.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=self.app_root, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=flags)
            self.process = process
            try:
                while process.poll() is None:
                    self._check_cancel(job)
                    self.stop.wait(.1)
                if process.returncode:
                    log.flush()
                    detail = "照片空间分析失败，请检查设备与模型。详情见布光准备日志。"
                    for line in (output_dir / "worker.log").read_text(encoding="utf-8", errors="replace").splitlines()[::-1]:
                        try:
                            failure = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(failure, dict) and failure.get("error"):
                            detail = str(failure["error"])
                            break
                    raise RuntimeError(detail)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=8)
                self.process = None
        return json.loads((output_dir / "geometry.json").read_text(encoding="utf-8"))

    def _subject_mask(self, analysis, original, width, height):
        background = analysis.get("parts", {}).get("background")
        if background is None:
            # The classic engine skips segmentation when there is no detected face.
            # Lighting still needs the complete person (hair and clothing included).
            adapter = getattr(self.store.engine, "_parts_adapter", None)
            if adapter is None:
                raise RuntimeError("人物选区不可用，请先准备基础处理模型。")
            segment = cv2.resize(original, (width, height), interpolation=cv2.INTER_AREA).astype(np.float32)/255
            with self.store.engine_lock:
                background = adapter.predict(segment).get("background")
        if background is None:
            raise RuntimeError("无法分析人物选区，请检查基础处理模型。")
        foreground = np.clip(1-np.asarray(background, np.float32), 0, 1)
        foreground = cv2.resize(foreground, (width, height), interpolation=cv2.INTER_LINEAR)
        # Multiclass confidence maps can retain a foreground floor around 8%
        # on clear background. Clamp confident exterior/interior regions while
        # keeping the entire 10%-90% interval continuous for soft hair edges.
        return np.rint(np.clip((foreground-SUBJECT_MASK_LOW)/(SUBJECT_MASK_HIGH-SUBJECT_MASK_LOW), 0, 1)*255).astype(np.uint8)

    def _prepare(self, job):
        snapshot = job["snapshot"]
        self._progress(job, "读取原片与已有修片调整…", .05)
        original = self.store.rgb(snapshot)
        analysis = self.store.analysis(snapshot, original)
        ratio = min(1., 1024/max(original.shape[:2]))
        width, height = max(16, round(original.shape[1]*ratio)), max(16, round(original.shape[0]*ratio))
        preview = cv2.resize(original, (width, height), interpolation=cv2.INTER_AREA)
        key = self.geometry_key(snapshot)
        # Opening the editor must display the geometry that was actually saved.
        # An explicit reanalysis is the only way to adopt a newer mask policy.
        if snapshot.get("lighting_geometry") and not job.get("refresh_geometry", False):
            try:
                self._geometry_meta(snapshot["lighting_geometry"], snapshot["sha256"])
                key = snapshot["lighting_geometry"]
            except (RuntimeError, ValueError):
                pass
        directory = self.geometry_directory(key)
        try:
            _, geometry_meta = self._geometry_meta(key, snapshot["sha256"])
        except RuntimeError:
            self._progress(job, "分析照片空间与表面朝向，仅首次需要…", .2)
            if not lighting_geometry.status(self.models).get("ready"):
                raise RuntimeError("照片分析模型尚未准备，请先下载并校验模型。")
            directory.mkdir(parents=True, exist_ok=True)
            input_path = self.checked(directory / "input.png")
            self.save_image(input_path, preview, "PNG")
            inferred = self._infer_geometry(input_path, directory, job["device"], job)
            self._check_cancel(job)
            mask = self._subject_mask(analysis, original, width, height)
            self.save_image(directory / "mask.png", Image.fromarray(mask), "PNG")
            coverage = float(np.count_nonzero(mask > 32)/mask.size)
            geometry_meta = {"schema": GEOMETRY_SCHEMA, "source_sha256": snapshot["sha256"],
                             "key": key, "width": width, "height": height, "analysis": inferred,
                             "mask_policy": {"version": SUBJECT_MASK_POLICY_VERSION,
                                             "foreground_low": SUBJECT_MASK_LOW, "foreground_high": SUBJECT_MASK_HIGH},
                             "subject_coverage": coverage,
                             "warnings": ["未找到清晰的人物选区；仅主体布光可能没有效果，可选择全图范围。"] if coverage < .005 else []}
            self.save_json(directory / "prepared.json", geometry_meta)
        self._progress(job, "准备保留原片结构的布光预览…", .85)
        width, height = int(geometry_meta["width"]), int(geometry_meta["height"])
        full_base, base_meta = self.retouch_base(snapshot, original, analysis)
        base = self.resize_base(full_base, width, height)
        del full_base
        self._check_cancel(job)
        session = self.checked(self.sessions / job["id"])
        session.mkdir(parents=True, exist_ok=True)
        self.save_image(session / "base.png", base, "PNG")
        for name in ("normal.png", "depth.png", "mask.png"):
            shutil.copyfile(directory / name, self.checked(session / name))
        prefix = f"/api/lighting/assets/{job['id']}/"
        return {"photo_id": snapshot["id"], "version": snapshot["version"],
                "base_url": prefix+"base.png", "normal_url": prefix+"normal.png", "depth_url": prefix+"depth.png",
                "mask_url": prefix+"mask.png", "width": width, "height": height, "geometry_key": key,
                "base_key": base_meta["retouch_base"]["key"], "base_contract": BASE_CONTRACT,
                "lighting": lighting.validate_lighting(snapshot.get("lighting")), "meta": geometry_meta}

    def asset(self, identifier, name):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier) or name not in ASSETS:
            raise HTTPException(404, "未找到布光预览文件。")
        with self.store.lock:
            job = self.get(identifier)
            if job["action"] != "prepare" or job["status"] != "completed":
                raise HTTPException(404, "布光预览尚未准备完成。")
            self.store.photo(job["photo_id"])
        path = self.checked(self.sessions / identifier / name)
        if not path.is_file():
            raise HTTPException(404, "布光预览文件不可用，请重新准备。")
        return path

    def validate_edit(self, photo, raw, key=_UNCHANGED_GEOMETRY):
        config = lighting.validate_lighting(raw)
        if key is _UNCHANGED_GEOMETRY:
            key = photo.get("lighting_geometry")
        if key is None:
            if config.get("enabled"):
                raise HTTPException(409, "启用布光需要先完成照片分析。")
        else:
            self.geometry_directory(key)
            try:
                self._geometry_meta(key, photo["sha256"])
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
        return config, key

    def render(self, snapshot, rgb):
        config = lighting.validate_lighting(snapshot.get("lighting"))
        if not config.get("enabled"):
            return rgb, {"enabled": False}
        directory, meta = self._geometry_meta(snapshot.get("lighting_geometry"), snapshot["sha256"])
        with Image.open(directory / "normal.png") as image:
            normal = np.asarray(image.convert("RGB")).copy()
        with Image.open(directory / "depth.png") as image:
            depth = np.asarray(image.convert("L")).copy()
        with Image.open(directory / "mask.png") as image:
            mask = np.asarray(image.convert("L")).copy()
        result = lighting.render_lighting(rgb, normal, depth, mask, config)
        background_policy = {"subject": "exact_input_pixels_outside_subject_mask",
                             "background": "exact_input_pixels_inside_subject_mask",
                             "all": "whole_image"}[config["scope"]]
        return result, {"enabled": True, "params": config, "geometry": meta,
                        "method": "deterministic_image_space_lighting", "processing_device": "cpu",
                        "background_policy": background_policy}

    def close(self):
        self.stop.set()
        for event in self._cancellations.values():
            event.set()
        self.queue.put(None)
        if self.thread:
            self.thread.join(timeout=12)
