"""Local-only API, persistence and sequential export queue for PortraitLab.

Run ``python -B -m portraitlab.server``. No model or dependency is downloaded.
PORTRAITLAB_STORAGE_ROOT optionally restricts writable paths for an installation.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

from collections import OrderedDict
import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import importlib
from importlib.metadata import PackageNotFoundError, version as package_version
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import stat
import tempfile
import threading
import uuid
from typing import Any

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__

APP_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024 * 1024
MAX_PIXELS = 80_000_000
MAX_FILES = 50
PREVIEW_SIDE = 1600
COOKIE = "portraitlab_session"
SOURCE_FORMATS = {"JPEG", "PNG", "TIFF", "WEBP"}
SOURCE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
LOGGER = logging.getLogger("portraitlab")


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def writable_path(path: str | Path) -> Path:
    """Resolve a write target and apply an optional installation storage policy."""
    result = Path(path).expanduser().resolve()
    configured = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if result == Path(result.anchor):
        raise ValueError("请使用专用数据文件夹，不能直接使用磁盘根目录。")
    if configured:
        root = Path(configured).expanduser().resolve()
        if result == root or not result.is_relative_to(root):
            raise ValueError("文件位置不在此安装配置允许的存储目录内。")
    return result


def json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def atomic_write(path: Path, writer: Any, *, replace: bool = True) -> None:
    path = writable_path(path)
    writable_path(path.parent / "checked").parent.mkdir(parents=True, exist_ok=True)
    temporary = writable_path(path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp"))
    try:
        writer(temporary)
        writable_path(path)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise
            except OSError:
                # exFAT and some removable/network volumes do not support links.
                created = False
                try:
                    with path.open("xb") as target:
                        created = True
                        with temporary.open("rb") as source:
                            shutil.copyfileobj(source, target)
                except Exception:
                    if created:
                        path.unlink(missing_ok=True)
                    raise
            temporary.unlink()
    finally:
        if temporary.exists():
            writable_path(temporary).unlink()


def write_json(path: Path, value: Any, *, replace: bool = True) -> None:
    serialized = json.dumps(json_value(value), ensure_ascii=False, indent=2, allow_nan=False)
    atomic_write(path, lambda p: p.write_text(serialized, encoding="utf-8"), replace=replace)


def write_image(path: Path, rgb: np.ndarray | Image.Image, image_format: str, *, replace: bool = True) -> None:
    image = rgb if isinstance(rgb, Image.Image) else Image.fromarray(rgb)
    kwargs = {"icc_profile": SRGB_PROFILE} if image.mode == "RGB" else {}
    if image_format == "JPEG":
        kwargs.update(quality=95, subsampling=0)
    elif image_format == "TIFF":
        kwargs.update(compression="tiff_deflate")
    atomic_write(path, lambda p: image.save(p, format=image_format, **kwargs), replace=replace)


def decode_source(path: Path) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(path) as original:
        if original.format not in SOURCE_FORMATS:
            raise ValueError("仅支持 JPEG、PNG、TIFF 和 WebP 照片。")
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError("这一版不支持动画或多页图像，请先导出单张照片。")
        if original.width * original.height > MAX_PIXELS or min(original.size) < 16:
            raise ValueError("图片须至少为 16×16 像素，且不能超过 8000 万像素。")
        original.load()
        metadata = {"source_format": original.format, "source_mode": original.mode,
                    "source_dimensions": list(original.size), "had_icc_profile": bool(original.info.get("icc_profile")),
                    "exif_orientation": int(original.getexif().get(274, 1)), "working_precision": "RGB uint8",
                    "working_color_space": "sRGB", "warnings": []}
        image = ImageOps.exif_transpose(original)
        icc = original.info.get("icc_profile")
        if icc:
            try:
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                  ImageCms.createProfile("sRGB"), outputMode="RGB")
            except (ImageCms.PyCMSError, OSError, ValueError) as exc:
                raise ValueError("图片含有无法读取或无法转换的 ICC 配置，请先转换为 sRGB。") from exc
        else:
            image = image.convert("RGB")
            metadata["warnings"].append("原图没有 ICC 配置，按 sRGB 解释。")
        if original.mode not in ("RGB", "RGBA", "L", "P"):
            metadata["warnings"].append("当前处理链转换为 8 位 RGB，不保留高位深源精度。")
        if original.mode in ("RGBA", "LA") or "transparency" in original.info:
            metadata["warnings"].append("当前人像处理链输出不透明 RGB，原图透明通道不保留。")
        return image.copy(), metadata


def resize_rgb(rgb: np.ndarray, max_side: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if max_side and max(h, w) > max_side:
        scale = max_side / max(h, w)
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        return np.asarray(Image.fromarray(rgb).resize(size, Image.Resampling.LANCZOS)).copy()
    return rgb


def validate_strokes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 256:
        raise ValueError("笔刷记录须为列表，最多保留 256 笔。")
    cleaned, count = [], 0
    for stroke in raw:
        if not isinstance(stroke, dict) or stroke.get("mode") not in ("protect", "restore"):
            raise ValueError("笔刷模式仅支持 protect 或 restore。")
        radius = float(stroke.get("radius", .05))
        if not math.isfinite(radius) or not .001 <= radius <= .5:
            raise ValueError("笔刷半径必须在 0.001～0.5 之间。")
        points = stroke.get("points")
        if not isinstance(points, list) or not points:
            raise ValueError("每笔至少需要一个坐标。")
        count += len(points)
        if count > 16384:
            raise ValueError("笔刷坐标过多，请简化笔刷记录。")
        clean_points = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("笔刷坐标必须是 [x,y]。")
            x, y = float(point[0]), float(point[1])
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y)):
                raise ValueError("笔刷坐标必须在 0～1 之间。")
            clean_points.append([x, y])
        cleaned.append({"mode": stroke["mode"], "radius": radius, "points": clean_points})
    return cleaned


def safe_stem(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).stem).strip(" .")[:80] or "portrait"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", stem):
        stem = "_" + stem
    return stem


class PortraitStore:
    def __init__(self, data_dir: Path):
        self.data = writable_path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.folders = {key: writable_path(self.data / key) for key in ("uploads", "cache", "previews", "exports", "models", "temp", "logs")}
        self.runtime_cache = writable_path(os.environ.get("PORTRAITLAB_CACHE", self.data / "runtime-cache"))
        self.runtime_cache.mkdir(parents=True, exist_ok=True)
        self.folders["temp"] = writable_path(self.runtime_cache / "temp")
        for folder in self.folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = os.environ["TMP"] = str(self.folders["temp"])
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        tempfile.tempdir = str(self.folders["temp"])
        if not LOGGER.handlers:
            handler = logging.FileHandler(writable_path(self.folders["logs"] / "server.log"), encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOGGER.addHandler(handler)
            LOGGER.setLevel(logging.INFO)
        self.state_path = writable_path(self.data / "state.json")
        self.lock, self.engine_lock = threading.RLock(), threading.RLock()
        self.photo_locks: dict[str, threading.Lock] = {}
        self.analysis_cache: OrderedDict[str, dict] = OrderedDict()
        self.engine_module = importlib.import_module(".engine", __package__)
        self.engine = self.engine_module.Engine(self.folders["models"])
        packages = {}
        for package in ("mediapipe", "opencv-python", "opencv-contrib-python", "numpy"):
            try:
                packages[package] = package_version(package)
            except PackageNotFoundError:
                packages[package] = None
        engine_source = Path(self.engine_module.__file__).read_bytes()
        self.analysis_signature = {"cache_schema": 2, "application": __version__,
                                   "engine_sha256": hashlib.sha256(engine_source).hexdigest(),
                                   "packages": packages}
        self.defaults = dict(self.engine_module.DEFAULT_PARAMS)
        self.state = {"schema": 1, "photos": {}, "jobs": {}, "presets": {}}
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if loaded.get("schema") != 1 or not isinstance(loaded.get("photos"), dict):
                    raise ValueError("unsupported state schema")
                self.state.update(loaded)
            except Exception as exc:
                raise RuntimeError("无法读取现有工作区状态；为保护原数据，服务未覆盖该文件。") from exc
        for job in self.state["jobs"].values():
            if job["status"] in ("queued", "running", "cancelling"):
                job.update(status="interrupted", error="上次进程已退出，请重新提交未完成照片。", updated_at=now())
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.worker = threading.Thread(target=self._work_loop, name="portraitlab-export", daemon=True)
        self.worker.start()
        from .relight_service import RelightController
        self.relight = RelightController(self, checked=writable_path, save_json=write_json,
                                         save_image=write_image, timestamp=now, app_root=APP_ROOT)
        from .model_setup import ModelSetup
        self.model_setup = ModelSetup(self.engine, self.engine_module.MODEL_SPECS, writable_path)
        self.persist()

    def persist(self) -> None:
        with self.lock:
            write_json(self.state_path, self.state)

    def params(self, supplied: Any) -> dict:
        if not isinstance(supplied, dict):
            raise ValueError("params 必须是参数对象。")
        result = self.engine_module.validate_params({**self.defaults, **supplied})
        return dict(result)

    def photo(self, photo_id: str) -> dict:
        try:
            return self.state["photos"][photo_id]
        except KeyError:
            raise HTTPException(404, "未找到这张照片。")

    def public_photo(self, photo: dict) -> dict:
        out = {k: photo.get(k) for k in ("id", "name", "width", "height", "face_count", "status", "params", "warnings", "version", "strokes", "generation")}
        prefix = f"/api/photos/{photo['id']}/image"
        out["original_url"] = prefix + "?kind=original"
        if photo.get("generation_input_preview_path"):
            out["original_url"] = prefix + "?kind=input"
            out["comparison_kind"] = "generation_input"
        out["preview_url"] = prefix + (f"?kind=preview&v={photo.get('preview_version', 0)}" if photo.get("preview_path") else "?kind=original")
        out["mask_url"] = prefix + f"?kind=mask&v={photo.get('preview_version', 0)}" if photo.get("mask_path") else None
        return json_value(out)

    def public_job(self, job: dict) -> dict:
        return json_value({k: v for k, v in job.items() if k != "snapshots"})

    def add_photo(self, photo_id: str, name: str, raw_path: Path, sha: str) -> dict:
        image, color = decode_source(raw_path)
        normalized = writable_path(self.folders["cache"] / (photo_id + ".source.png"))
        original_preview = writable_path(self.folders["previews"] / (photo_id + ".original.jpg"))
        write_image(normalized, image, "PNG", replace=False)
        thumb = image.copy()
        thumb.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
        write_image(original_preview, thumb, "JPEG", replace=False)
        os.chmod(raw_path, stat.S_IREAD)
        photo = {"id": photo_id, "name": name, "width": image.width, "height": image.height,
                 "face_count": None, "status": "imported", "params": dict(self.defaults), "strokes": [],
                 "warnings": color["warnings"], "source_warnings": color["warnings"], "color": color,
                 "original_path": str(raw_path), "source_path": str(normalized), "original_preview_path": str(original_preview),
                 "sha256": sha, "version": 0, "created_at": now()}
        with self.lock:
            self.state["photos"][photo_id] = photo
            self.persist()
            return self.public_photo(photo)

    def add_generated(self, source: Path, source_name: str, provenance: dict, input_path: Path) -> dict:
        photo_id = uuid.uuid4().hex
        raw = writable_path(self.folders["uploads"] / (photo_id + ".png"))
        content = source.read_bytes()
        atomic_write(raw, lambda p: p.write_bytes(content), replace=False)
        self.add_photo(photo_id, safe_stem(source_name) + " · AI重打光.png", raw, hashlib.sha256(content).hexdigest())
        with self.lock:
            photo = self.photo(photo_id)
            # The generated image starts without a second, implicit beauty pass.
            photo["params"] = {**self.defaults, "smooth": 0.0, "blemishes": 0.0,
                               "even": 0.0, "texture": 0.0, "opacity": 1.0}
            photo["generation"] = provenance
            comparison = writable_path(self.folders["previews"] / (photo_id + ".input.jpg"))
            with Image.open(input_path) as original:
                meta = provenance.get("metadata", {})
                resized, box = meta.get("resize_before_center_crop"), meta.get("crop_box")
                if resized and box:
                    before = original.convert("RGB").resize(tuple(resized), Image.Resampling.LANCZOS).crop(tuple(box))
                else:
                    before = ImageOps.fit(original.convert("RGB"), (photo["width"], photo["height"]), Image.Resampling.LANCZOS)
                before.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
                write_image(comparison, before, "JPEG", replace=False)
            photo["generation_input_preview_path"] = str(comparison)
            photo["source_warnings"].append("AI 生成结果：可能改变五官、发丝、衣物或背景，请逐张检查。")
            photo["warnings"] = list(photo["source_warnings"])
            self.persist()
            return self.public_photo(photo)

    def rgb(self, photo: dict) -> np.ndarray:
        source = writable_path(photo["source_path"])
        if not source.is_relative_to(self.data):
            raise ValueError("照片缓存路径不属于当前工作区。")
        with Image.open(source) as image:
            return np.asarray(image.convert("RGB")).copy()

    def analysis(self, photo: dict, rgb: np.ndarray) -> dict:
        key = photo["id"]
        with self.engine_lock:
            if key in self.analysis_cache:
                self.analysis_cache.move_to_end(key)
                return self.analysis_cache[key]
            cached = writable_path(self.folders["cache"] / (key + ".analysis.npz"))
            result = None
            # Engine hashes are checked on analyze; disk cache additionally records source and model file fingerprints.
            fingerprint = self.model_fingerprint()
            if cached.exists():
                try:
                    with np.load(cached, allow_pickle=False) as packed:
                        tree = json.loads(str(packed["__tree"].item()))
                        if (tree["sha256"] == photo["sha256"] and tree["models"] == fingerprint
                                and tree.get("signature") == self.analysis_signature):
                            def unpack(value):
                                if isinstance(value, dict) and set(value) == {"__array"}:
                                    return packed[value["__array"]].copy()
                                if isinstance(value, dict):
                                    return {k: unpack(v) for k, v in value.items()}
                                if isinstance(value, list):
                                    return [unpack(v) for v in value]
                                return value
                            result = unpack(tree["analysis"])
                except Exception:
                    LOGGER.exception("Ignoring unusable analysis cache for %s", key)
            if result is None:
                result = self.engine.analyze(rgb)
                arrays = {}
                def pack(value):
                    if isinstance(value, np.ndarray):
                        array_key = "a" + str(len(arrays))
                        arrays[array_key] = value
                        return {"__array": array_key}
                    if isinstance(value, dict):
                        return {str(k): pack(v) for k, v in value.items()}
                    if isinstance(value, (list, tuple)):
                        return [pack(v) for v in value]
                    return json_value(value)
                tree = {"sha256": photo["sha256"], "models": fingerprint,
                        "signature": self.analysis_signature, "analysis": pack(result)}
                arrays["__tree"] = np.asarray(json.dumps(tree, ensure_ascii=False, allow_nan=False))
                def save_npz(path):
                    with path.open("wb") as stream:
                        np.savez_compressed(stream, **arrays)
                atomic_write(cached, save_npz)
            self.analysis_cache[key] = result
            while len(self.analysis_cache) > 8:
                self.analysis_cache.popitem(last=False)
            return result

    def model_fingerprint(self) -> list[dict]:
        return [{"name": p.name, "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
                for p in sorted(self.folders["models"].iterdir()) if p.is_file()]

    def update_params(self, ids: list[str], params: dict) -> list[dict]:
        validated = self.params(params)
        with self.lock:
            photos = [self.photo(photo_id) for photo_id in ids]
            for photo in photos:
                photo.update(params=dict(validated), version=photo["version"] + 1, status="settings_changed")
            self.persist()
            return [self.public_photo(p) for p in photos]

    def render_preview(self, photo_id: str, requested_version: int) -> dict:
        with self.lock:
            per_photo = self.photo_locks.setdefault(photo_id, threading.Lock())
        with per_photo:
            with self.lock:
                current = self.photo(photo_id)
                if current["version"] != requested_version:
                    return {"photo": self.public_photo(current), "meta": {"stale": True}}
                snapshot = json.loads(json.dumps(current))
            try:
                full = self.rgb(snapshot)
                analysis = self.analysis(snapshot, full)
                preview = resize_rgb(full, PREVIEW_SIDE)
                del full
                with self.engine_lock:
                    output, meta = self.engine.render(preview, snapshot["params"], analysis, strokes=snapshot["strokes"])
                    mask = self.engine_module.mask_for(analysis, preview.shape[:2], snapshot["strokes"], region=snapshot["params"].get("region", "face"))
                preview_path = writable_path(self.folders["previews"] / f"{photo_id}.{requested_version}.jpg")
                mask_path = writable_path(self.folders["previews"] / f"{photo_id}.{requested_version}.mask.png")
                with self.lock:
                    current = self.photo(photo_id)
                    if current["version"] != requested_version:
                        return {"photo": self.public_photo(current), "meta": {"stale": True}}
                    write_image(preview_path, np.asarray(output, dtype=np.uint8), "JPEG")
                    write_image(mask_path, Image.fromarray(np.rint(np.clip(mask, 0, 1) * 255).astype(np.uint8)), "PNG")
                    old_paths = [current.get("preview_path"), current.get("mask_path")]
                    warnings = list(dict.fromkeys(snapshot.get("source_warnings", []) + list(analysis.get("warnings", [])) + list(meta.get("warnings", []))))
                    current.update(preview_path=str(preview_path), mask_path=str(mask_path), preview_version=requested_version,
                                   status="ready", face_count=int(analysis.get("face_count", 0)), warnings=warnings,
                                   last_meta=json_value(meta))
                    self.persist()
                    result = {"photo": self.public_photo(current), "meta": json_value(meta)}
                for old in old_paths:
                    if old and old not in (str(preview_path), str(mask_path)):
                        try:
                            path = writable_path(old)
                            if path.parent == self.folders["previews"]:
                                path.unlink(missing_ok=True)
                        except OSError:
                            LOGGER.info("Old preview still in use: %s", old)
                return result
            except Exception as exc:
                LOGGER.exception("Preview failed for %s", photo_id)
                with self.lock:
                    current = self.photo(photo_id)
                    if current["version"] == requested_version:
                        current.update(status="error", warnings=snapshot.get("source_warnings", []) + [str(exc)])
                        self.persist()
                raise

    def new_job(self, ids: list[str], image_format: str, max_side: int, output_dir: str | None) -> dict:
        if image_format not in ("jpg", "png", "tiff"):
            raise ValueError("导出格式仅支持 jpg、png、tiff。")
        if isinstance(max_side, bool) or not isinstance(max_side, int) or (max_side != 0 and not 64 <= max_side <= 30000):
            raise ValueError("最长边须为 0（原尺寸），或 64～30000 的整数。")
        job_id = uuid.uuid4().hex
        if output_dir is not None and not isinstance(output_dir, str):
            raise ValueError("导出位置必须是目录字符串。")
        destination = writable_path(output_dir) if output_dir else writable_path(self.folders["exports"] / (datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S") + "-" + job_id[:6]))
        with self.lock:
            snapshots = [json.loads(json.dumps(self.photo(i))) for i in ids]
            destination.mkdir(parents=True, exist_ok=True)
            job = {"id": job_id, "status": "queued", "done": 0, "total": len(ids), "format": image_format,
                   "max_side": max_side, "output_dir": str(destination), "created_at": now(), "updated_at": now(),
                   "cancel_requested": False, "items": [{"photo_id": p["id"], "name": p["name"], "status": "queued"} for p in snapshots],
                   "snapshots": snapshots}
            self.state["jobs"][job_id] = job
            self.persist()
            self.queue.put(job_id)
            return self.public_job(job)

    def _work_loop(self) -> None:
        while True:
            job_id = self.queue.get()
            if job_id is None:
                return
            try:
                self._run_job(job_id)
            except Exception as exc:
                LOGGER.exception("Export job failed: %s", job_id)
                with self.lock:
                    job = self.state["jobs"][job_id]
                    job.update(status="failed", error=str(exc), updated_at=now())
                    self.persist()
            finally:
                self.queue.task_done()

    def _run_job(self, job_id: str) -> None:
        with self.lock:
            job = self.state["jobs"][job_id]
            job.update(status="running", updated_at=now())
            self.persist()
        failures = 0
        for index, snapshot in enumerate(job["snapshots"]):
            with self.lock:
                if job["cancel_requested"]:
                    break
                job["items"][index]["status"] = "running"
                self.persist()
            try:
                rgb = self.rgb(snapshot)
                analysis = self.analysis(snapshot, rgb)
                rgb = resize_rgb(rgb, job["max_side"])
                with self.engine_lock:
                    output, meta = self.engine.render(rgb, snapshot["params"], analysis, strokes=snapshot.get("strokes", []))
                directory = writable_path(job["output_dir"])
                extension, pil_format = {"jpg": ("jpg", "JPEG"), "png": ("png", "PNG"), "tiff": ("tiff", "TIFF")}[job["format"]]
                stem = f"{safe_stem(snapshot['name'])}-{snapshot['id'][:8]}-{job_id[:6]}"
                number = 0
                while True:
                    name = stem + (f"-{number}" if number else "") + "." + extension
                    destination = writable_path(directory / name)
                    sidecar = writable_path(directory / (name + ".json"))
                    if destination.exists() or sidecar.exists():
                        number += 1
                        continue
                    try:
                        write_image(destination, np.asarray(output, dtype=np.uint8), pil_format, replace=False)
                        break
                    except FileExistsError:
                        number += 1
                metadata = {"application": "PortraitLab", "version": __version__, "created_at": now(),
                            "source_name": snapshot["name"], "source_sha256": snapshot["sha256"],
                            "original_dimensions": [snapshot["width"], snapshot["height"]],
                            "output_dimensions": [output.shape[1], output.shape[0]], "output_precision": "8-bit RGB",
                            "output_color_space": "sRGB", "source_color": snapshot.get("color"),
                            "params": snapshot["params"], "strokes": snapshot.get("strokes", []),
                            "models": analysis.get("models"), "engine_meta": meta,
                            "generation": snapshot.get("generation")}
                write_json(sidecar, metadata, replace=False)
                item_result = {"status": "completed", "path": str(destination), "sidecar": str(sidecar), "meta": json_value(meta)}
                del rgb, output
            except Exception as exc:
                failures += 1
                LOGGER.exception("Photo export failed: %s", snapshot["id"])
                item_result = {"status": "error", "error": str(exc)}
            with self.lock:
                job["items"][index].update(item_result)
                job.update(done=index + 1, updated_at=now())
                self.persist()
        with self.lock:
            if job["cancel_requested"] and job["done"] < job["total"]:
                job["status"] = "cancelled"
                for item in job["items"]:
                    if item["status"] == "queued":
                        item["status"] = "cancelled"
            else:
                job["status"] = "completed_with_errors" if failures else "completed"
            job["updated_at"] = now()
            self.persist()


def create_app(data_dir: Path | None = None, port: int = 18765) -> FastAPI:
    if not 1024 <= port <= 65535:
        raise ValueError("服务端口必须在 1024～65535 之间。")
    data = data_dir or Path(os.environ.get("PORTRAITLAB_DATA", str(APP_ROOT / ".local/runtime")))
    store = PortraitStore(data)
    token = secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(app):
        yield
        store.model_setup.close()
        store.relight.close()
        store.queue.put(None)
        store.worker.join(timeout=10)
        close_engine = getattr(store.engine, "close", None)
        if close_engine:
            close_engine()
    app = FastAPI(title="PortraitLab", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan,
                  telemetry={"tracing": False, "metrics": False, "logs": False,
                             "operation_spans": False, "auto_configure": False})
    app.state.store, app.state.token, app.state.port = store, token, port

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return JSONResponse({"detail": "请求主机不匹配。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin != "http://" + host:
            return JSONResponse({"detail": "不允许跨站请求。"}, status_code=403)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return JSONResponse({"detail": "单次上传总大小不能超过 512 MiB。"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效请求长度。"}, status_code=400)
        # Enforce the same cap before multipart spooling, including chunked requests.
        original_receive = request._receive
        received_bytes = 0
        async def limited_receive():
            nonlocal received_bytes
            message = await original_receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "单次上传总大小不能超过 512 MiB。")
            return message
        request._receive = limited_receive
        if request.url.path.startswith("/api/") and request.url.path != "/api/bootstrap":
            header = request.headers.get("x-portraitlab-token", "")
            cookie = request.cookies.get(COOKIE, "")
            valid_header = secrets.compare_digest(header, token)
            valid_cookie = secrets.compare_digest(cookie, token)
            if (request.method not in ("GET", "HEAD") and not valid_header) or (request.method in ("GET", "HEAD") and not (valid_header or valid_cookie)):
                return JSONResponse({"detail": "会话已失效，请刷新页面。"}, status_code=401)
        try:
            response = await call_next(request)
        except Exception:
            # Starlette may wrap a receive exception in an AnyIO ExceptionGroup.
            if received_bytes > MAX_REQUEST_BYTES:
                return JSONResponse({"detail": "单次上传总大小不能超过 512 MiB。"}, status_code=413)
            raise
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def processing_error(request, exc):
        LOGGER.exception("Processing unavailable: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        LOGGER.exception("Unhandled request error", exc_info=exc)
        return JSONResponse({"detail": "处理失败，详情已写入工作区日志。"}, status_code=500)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(400, "请求内容不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "请求内容必须是对象。")
        return data

    def ids_from(data: dict) -> list[str]:
        ids = data.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or not all(isinstance(i, str) for i in ids):
            raise ValueError("请选择 1～500 张照片。")
        return list(dict.fromkeys(ids))

    @app.get("/api/bootstrap")
    async def bootstrap():
        status = await run_in_threadpool(store.engine.status)
        presets = getattr(store.engine_module, "PRESETS", {"自然": store.defaults})
        preset_list = [{"id": str(key), "name": str(key), "params": params} for key, params in presets.items()]
        response = JSONResponse(json_value({"version": __version__, "token": token, "defaults": store.defaults,
                                           "presets": preset_list, "models": status,
                                           "storage": {"data_dir": str(store.data), "export_dir": str(store.folders["exports"]),
                                                       "max_files": MAX_FILES, "max_file_mib": 100, "max_pixels": MAX_PIXELS},
                                           "processing": {"precision": "RGB 8-bit", "color_space": "sRGB", "formats": ["jpg", "png", "tiff"]}}))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/photos")
    async def photos():
        with store.lock:
            return {"photos": [store.public_photo(p) for p in store.state["photos"].values()]}

    @app.post("/api/import")
    async def import_photos(request: Request):
        form = await request.form(max_files=MAX_FILES, max_fields=10)
        files = [f for k, f in form.multi_items() if k == "files" and hasattr(f, "read")]
        if not files or len(files) > MAX_FILES:
            await form.close()
            raise ValueError("一次请选择 1～50 张照片。")
        added, errors, total = [], [], 0
        try:
            for uploaded in files:
                name = Path((uploaded.filename or "image").replace("\\", "/")).name
                suffix = Path(name).suffix.lower()
                raw_path = None
                try:
                    if suffix not in SOURCE_EXTENSIONS:
                        raise ValueError("文件扩展名不受支持。")
                    photo_id = uuid.uuid4().hex
                    raw_path = writable_path(store.folders["uploads"] / (photo_id + suffix))
                    sha, size = hashlib.sha256(), 0
                    with raw_path.open("xb") as target:
                        while chunk := await uploaded.read(1024 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_FILE_BYTES or total > MAX_REQUEST_BYTES:
                                raise ValueError("照片超过 100 MiB，或本次总上传超过 512 MiB。")
                            target.write(chunk)
                            sha.update(chunk)
                    photo = await run_in_threadpool(store.add_photo, photo_id, name, raw_path, sha.hexdigest())
                    added.append(photo)
                except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                    LOGGER.info("Import rejected %s: %s", name, exc)
                    if raw_path and raw_path.exists():
                        os.chmod(writable_path(raw_path), stat.S_IWRITE)
                        writable_path(raw_path).unlink(missing_ok=True)
                    errors.append({"name": name, "error": str(exc)})
        finally:
            await form.close()
        return {"photos": added, "errors": errors}

    @app.get("/api/photos/{photo_id}/image")
    async def photo_image(photo_id: str, kind: str = "preview"):
        if kind not in ("original", "preview", "mask", "input"):
            raise ValueError("未知图像类型。")
        with store.lock:
            photo = store.photo(photo_id)
            key = {"original": "original_preview_path", "preview": "preview_path", "mask": "mask_path", "input": "generation_input_preview_path"}[kind]
            value = photo.get(key)
            if not value:
                raise HTTPException(404, "此预览尚未生成。")
            path = writable_path(value)
            if path.parent != store.folders["previews"]:
                raise HTTPException(403, "无效预览位置。")
        return FileResponse(path, media_type="image/png" if kind == "mask" else "image/jpeg")

    @app.post("/api/photos/{photo_id}/preview")
    async def preview(photo_id: str, request: Request):
        data = await body(request)
        with store.lock:
            photo = store.photo(photo_id)
            params = store.params(data.get("params", photo["params"]))
            strokes = validate_strokes(data["strokes"]) if "strokes" in data else photo["strokes"]
            # Validate the entire edit before persisting either part of it.
            photo.update(params=params, strokes=strokes, version=photo["version"] + 1, status="settings_changed")
            version = photo["version"]
            store.persist()
        return await run_in_threadpool(store.render_preview, photo_id, version)

    @app.post("/api/settings/apply")
    async def apply_settings(request: Request):
        data = await body(request)
        ids = ids_from(data)
        return {"photos": store.update_params(ids, data.get("params")), "ids": ids}

    @app.post("/api/photos/{photo_id}/mask")
    async def mask(photo_id: str, request: Request):
        data = await body(request)
        strokes = validate_strokes(data.get("strokes"))
        with store.lock:
            photo = store.photo(photo_id)
            photo.update(strokes=strokes, version=photo["version"] + 1, status="settings_changed")
            version = photo["version"]
            store.persist()
        return await run_in_threadpool(store.render_preview, photo_id, version)

    @app.post("/api/jobs")
    async def jobs_create(request: Request):
        data = await body(request)
        job = store.new_job(ids_from(data), data.get("format", "png"), data.get("max_side", 0), data.get("output_dir") or None)
        return {"job": job}

    @app.get("/api/jobs")
    async def jobs_list():
        with store.lock:
            return {"jobs": [store.public_job(j) for j in list(store.state["jobs"].values())[-30:]][::-1]}

    def get_job(job_id):
        with store.lock:
            if job_id not in store.state["jobs"]:
                raise HTTPException(404, "未找到该导出任务。")
            return store.state["jobs"][job_id]

    @app.get("/api/jobs/{job_id}")
    async def jobs_get(job_id: str):
        with store.lock:
            return {"job": store.public_job(get_job(job_id))}

    @app.post("/api/jobs/{job_id}/cancel")
    async def jobs_cancel(job_id: str):
        with store.lock:
            job = get_job(job_id)
            if job["status"] in ("queued", "running", "cancelling"):
                job.update(cancel_requested=True, status="cancelling", updated_at=now())
                store.persist()
            return {"job": store.public_job(job)}

    @app.post("/api/jobs/{job_id}/open")
    async def jobs_open(job_id: str):
        with store.lock:
            directory = writable_path(get_job(job_id)["output_dir"])
        if not directory.is_dir():
            raise ValueError("导出目录尚不存在。")
        os.startfile(str(directory))
        return {"ok": True, "path": str(directory)}

    @app.get("/api/relight/status")
    async def relight_status(probe: bool = False):
        return await run_in_threadpool(lambda: store.relight.status(probe=probe))

    @app.get("/api/models/status")
    async def models_status():
        return await run_in_threadpool(store.model_setup.status)

    @app.post("/api/models/download")
    async def models_download():
        return {"installation": store.model_setup.start()}

    @app.post("/api/relight/download")
    async def relight_download():
        return {"job": store.relight.create("download")}

    @app.post("/api/relight/jobs")
    async def relight_create(request: Request):
        data = await body(request)
        return {"job": store.relight.create("generate", ids_from(data), data.get("params", {}))}

    @app.get("/api/relight/jobs")
    async def relight_jobs():
        with store.lock:
            return {"jobs": [store.relight.public_job(j) for j in list(store.relight.jobs.values())[-30:]][::-1]}

    @app.get("/api/relight/jobs/{job_id}")
    async def relight_job(job_id: str):
        with store.lock:
            return {"job": store.relight.public_job(store.relight.get(job_id))}

    @app.post("/api/relight/jobs/{job_id}/cancel")
    async def relight_cancel(job_id: str):
        return {"job": store.relight.cancel(job_id)}

    @app.post("/api/shutdown")
    async def shutdown():
        callback = getattr(app.state, "shutdown_callback", None)
        if callback is None:
            raise RuntimeError("该启动方式未提供桌面停止接口。")
        callback()
        return {"ok": True}

    licenses = APP_ROOT / "licenses"
    if licenses.is_dir():
        app.mount("/licenses", StaticFiles(directory=licenses), name="licenses")

    web = APP_ROOT / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description="PortraitLab 本地服务（仅监听127.0.0.1）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORTRAITLAB_PORT", "18765")))
    parser.add_argument("--data", type=Path, help="工作数据目录；默认使用PORTRAITLAB_DATA")
    args = parser.parse_args()
    port = args.port
    app = create_app(data_dir=args.data, port=port)
    # Explicit loopback binding and a single worker; no externally accessible server.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, access_log=False))
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
