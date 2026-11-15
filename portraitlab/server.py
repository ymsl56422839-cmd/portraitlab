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
from .presets import BUILTIN_IDS, CATEGORIES, PORTRAIT_GROUPS, MAX_CUSTOM_PRESETS, builtin_presets, custom_payload, custom_record
from .gpu_service import GPUCompositor, validate_device
from .spot_repair import validate_repairs
from .face_shape import validate_face_shape, build_operations, warp_image

APP_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024 * 1024
MAX_PRESET_REQUEST_BYTES = 64 * 1024
MAX_PIXELS = 80_000_000
MAX_FILES = 50
PREVIEW_SIDE = 1600
PREVIEW_CONTRACT = "canonical-full-resolution-area-png-v3-edit-tools"
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
        self.state = {"schema": 1, "photos": {}, "photo_trash": {}, "last_removed_ids": [],
                      "jobs": {}, "presets": {}, "preset_favorites": [], "processing_device": "auto"}
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if loaded.get("schema") != 1 or not isinstance(loaded.get("photos"), dict):
                    raise ValueError("unsupported state schema")
                self.state.update(loaded)
            except Exception as exc:
                raise RuntimeError("无法读取现有工作区状态；为保护原数据，服务未覆盖该文件。") from exc
        # Removal is a reversible library operation. Original records and all
        # image/export files remain in place; old workspaces have no trash.
        trash, last_removed = self.state["photo_trash"], self.state["last_removed_ids"]
        if (not isinstance(trash, dict)
                or any(not isinstance(identifier, str) or identifier not in self.state["photos"]
                       or not isinstance(record, dict)
                       or not isinstance(record.get("removed_at"), str)
                       or not isinstance(record.get("batch_id"), str)
                       for identifier, record in trash.items())
                or not isinstance(last_removed, list)
                or any(not isinstance(identifier, str) or identifier not in trash for identifier in last_removed)):
            raise RuntimeError("无法读取照片移除记录；为保护原数据，服务未覆盖工作区。")
        # Earlier workspaces already had an empty presets object but no favorites.
        # Reject damaged preset state rather than silently deleting user recipes.
        try:
            saved_presets = self.state["presets"]
            if not isinstance(saved_presets, dict) or len(saved_presets) > MAX_CUSTOM_PRESETS:
                raise ValueError("invalid saved presets")
            normalized = {}
            for identifier, preset in saved_presets.items():
                if not isinstance(identifier, str) or not re.fullmatch(r"user_[0-9a-f]{32}", identifier) or not isinstance(preset, dict):
                    raise ValueError("invalid saved preset")
                normalized[identifier] = custom_record(identifier, {key: preset[key] for key in ("name", "params", "description") if key in preset})
            favorites = self.state["preset_favorites"]
            if not isinstance(favorites, list) or not all(isinstance(value, str) for value in favorites):
                raise ValueError("invalid saved favorites")
            known = BUILTIN_IDS | normalized.keys()
            self.state["presets"] = normalized
            self.state["preset_favorites"] = list(dict.fromkeys(value for value in favorites if value in known))
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("无法读取已保存的预设；为保护原数据，服务未覆盖工作区。") from exc
        validate_device(self.state["processing_device"])
        try:
            for photo in self.state["photos"].values():
                photo["local_adjustment"] = self.engine_module.validate_local_adjustment(photo.get("local_adjustment"))
                photo["spot_repairs"] = validate_repairs(photo.get("spot_repairs"))
                photo["face_shape"] = validate_face_shape(photo.get("face_shape"))
                if photo.get("preview_path") and photo.get("preview_contract") != PREVIEW_CONTRACT:
                    # The next selection refreshes legacy JPEG/scale-dependent
                    # previews before the user starts another retouching edit.
                    if photo.get("status") == "ready":
                        photo["status"] = "settings_changed"
        except (ValueError, TypeError) as exc:
            raise RuntimeError("无法读取已保存的局部调整；为保护原数据，服务未覆盖工作区。") from exc
        for job in self.state["jobs"].values():
            if job["status"] in ("queued", "running", "cancelling"):
                job.update(status="interrupted", error="上次进程已退出，请重新提交未完成照片。", updated_at=now())
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.worker = threading.Thread(target=self._work_loop, name="portraitlab-export", daemon=True)
        self.worker.start()
        from .relight_service import RelightController
        self.relight = RelightController(self, checked=writable_path, save_json=write_json,
                                         save_image=write_image, timestamp=now, app_root=APP_ROOT)
        self.gpu = GPUCompositor(self, checked=writable_path, app_root=APP_ROOT)
        self.engine.compositor = self.gpu
        from .model_setup import ModelSetup
        self.model_setup = ModelSetup(self.engine, self.engine_module.MODEL_SPECS, writable_path)
        from .lighting_service import LightingController
        self.lighting = LightingController(self, checked=writable_path, save_json=write_json,
                                          save_image=write_image, timestamp=now, app_root=APP_ROOT)
        self.persist()

    def persist(self) -> None:
        with self.lock:
            write_json(self.state_path, self.state)

    def hardware_selection(self, data):
        if not isinstance(data, dict) or set(data) != {"device"}:
            raise ValueError("设备请求仅支持 device 字段。")
        device = validate_device(data["device"])
        # Probe outside the store lock: other HTTP reads remain responsive.
        result = self.gpu.validate_selection(device)
        with self.lock:
            previous = self.state["processing_device"]
            self.state["processing_device"] = device
            try:
                self.persist()
            except Exception:
                self.state["processing_device"] = previous
                raise
        self.gpu.selection_changed(device)
        result.pop("actual_device", None)
        return result

    def params(self, supplied: Any) -> dict:
        if not isinstance(supplied, dict):
            raise ValueError("params 必须是参数对象。")
        result = self.engine_module.validate_params({**self.defaults, **supplied})
        return dict(result)

    def photo(self, photo_id: str) -> dict:
        try:
            photo = self.state["photos"][photo_id]
        except KeyError:
            raise HTTPException(404, "未找到这张照片。")
        if photo_id in self.state["photo_trash"]:
            raise HTTPException(409, "这张照片已从照片库移除，请先撤销移除。")
        return photo

    def removal_undo_ids(self) -> list[str]:
        return [identifier for identifier in self.state["last_removed_ids"]
                if identifier in self.state["photo_trash"]]

    def remove_photos(self, ids: list[str]) -> dict:
        """Atomically hide a batch; never unlink originals, copies or exports."""
        with self.lock:
            photos = [self.photo(identifier) for identifier in ids]
            wanted = set(ids)
            for kind, jobs in (("导出", self.state["jobs"]),
                               ("光影重塑", self.state.get("relight_jobs", {})),
                               ("准备布光", self.state.get("lighting_jobs", {}))):
                for job in jobs.values():
                    if job.get("status") not in {"queued", "running", "cancelling"}:
                        continue
                    references = {p.get("id") for p in job.get("snapshots", [])}
                    references.update(item.get(key) for item in job.get("items", [])
                                      for key in ("photo_id", "source_id"))
                    if wanted & references:
                        raise HTTPException(409, f"所选照片正在{kind}，请等待任务结束后再移除。")
            if any(self.photo_locks.get(p["id"]) is not None and self.photo_locks[p["id"]].locked()
                   for p in photos):
                raise HTTPException(409, "所选照片正在更新预览，请稍后再移除。")
            removed_at, batch_id = now(), uuid.uuid4().hex
            previous_trash, previous_last = self.state["photo_trash"], self.state["last_removed_ids"]
            self.state["photo_trash"] = {**previous_trash, **{
                identifier: {"removed_at": removed_at, "batch_id": batch_id} for identifier in ids}}
            self.state["last_removed_ids"] = list(ids)
            try:
                self.persist()
            except Exception:
                self.state["photo_trash"], self.state["last_removed_ids"] = previous_trash, previous_last
                raise
            return {"removed_ids": list(ids), "removed_at": removed_at,
                    "last_removed_ids": self.removal_undo_ids()}

    def restore_photos(self, ids: list[str]) -> dict:
        """Restore the same records, IDs and edits, without replacing library items."""
        with self.lock:
            photos = []
            for identifier in ids:
                if identifier not in self.state["photos"]:
                    raise HTTPException(404, "未找到要恢复的照片。")
                if identifier not in self.state["photo_trash"]:
                    raise HTTPException(409, "部分照片已在照片库中，本次恢复未应用。")
                photos.append(self.state["photos"][identifier])
            previous_trash, previous_last = self.state["photo_trash"], self.state["last_removed_ids"]
            wanted = set(ids)
            self.state["photo_trash"] = {identifier: record for identifier, record in previous_trash.items()
                                         if identifier not in wanted}
            self.state["last_removed_ids"] = [identifier for identifier in previous_last if identifier not in wanted]
            try:
                self.persist()
            except Exception:
                self.state["photo_trash"], self.state["last_removed_ids"] = previous_trash, previous_last
                raise
            return {"restored_ids": list(ids), "photos": [self.public_photo(p) for p in photos],
                    "last_removed_ids": self.removal_undo_ids()}

    def public_photo(self, photo: dict) -> dict:
        from .lighting import validate_lighting
        out = {k: photo.get(k) for k in ("id", "name", "width", "height", "face_count", "status", "params", "warnings", "version", "strokes", "generation", "local_adjustment", "spot_repairs", "face_shape", "shape_geometry")}
        out["lighting"] = validate_lighting(photo.get("lighting"))
        out["lighting_geometry"] = photo.get("lighting_geometry")
        out["preview_contract"] = photo.get("preview_contract")
        out["preview_needs_refresh"] = bool(photo.get("preview_path") and photo.get("preview_contract") != PREVIEW_CONTRACT)
        prefix = f"/api/photos/{photo['id']}/image"
        out["original_url"] = prefix + "?kind=original"
        if photo.get("generation_input_preview_path"):
            out["original_url"] = prefix + "?kind=input"
            out["comparison_kind"] = "generation_input"
        out["preview_url"] = prefix + (f"?kind=preview&v={photo.get('preview_version', 0)}" if photo.get("preview_path") else "?kind=original")
        out["mask_url"] = prefix + f"?kind=mask&v={photo.get('preview_version', 0)}" if photo.get("mask_path") else None
        out["local_mask_url"] = prefix + f"?kind=local_mask&v={photo.get('preview_version', 0)}" if photo.get("local_adjustment") and photo.get("local_mask_path") else None
        return json_value(out)

    def public_job(self, job: dict) -> dict:
        return json_value({k: v for k, v in job.items() if k != "snapshots"})

    def preset_catalog(self) -> dict:
        with self.lock:
            return json_value({"presets": builtin_presets() + list(self.state["presets"].values()),
                               "categories": list(CATEGORIES), "portrait_groups": list(PORTRAIT_GROUPS),
                               "favorites": list(self.state["preset_favorites"])})

    def apply_preset(self, photo_id: str, data: dict) -> dict:
        """Resolve photo-aware operations, then atomically apply to one scope."""
        from .preset_pipeline import resolve_preset
        if (set(data) != {"preset_id", "scope", "expected_version"}
                or not isinstance(data["preset_id"], str)
                or data["scope"] not in ("global", "local")
                or type(data["expected_version"]) is not int):
            raise ValueError("预设请求需要 preset_id、scope 和 expected_version。")
        with self.lock:
            photo = self.photo(photo_id)
            if photo["version"] != data["expected_version"]:
                raise HTTPException(409, "照片已有更新，请等待预览完成后重新应用预设。")
            preset = next((p for p in self.preset_catalog()["presets"] if p["id"] == data["preset_id"]), None)
            if preset is None:
                raise HTTPException(404, "未找到这个预设。")
            snapshot = json.loads(json.dumps(photo))
        local = self.engine_module.validate_local_adjustment(snapshot.get("local_adjustment"))
        if data["scope"] == "local" and local is None:
            raise ValueError("请先框选要调整的区域。")
        rgb = self.rgb(snapshot)
        analysis = self.analysis(snapshot, rgb)
        mask = None
        if data["scope"] == "local":
            rgb, _ = self.lighting.retouch_base({**snapshot, "local_adjustment": None, "spot_repairs": [], "face_shape": None}, rgb, analysis)
            mask = local["rect"]
        current = snapshot["params"] if data["scope"] == "global" else local["params"]
        resolved = resolve_preset(preset, current, rgb, analysis, local_mask=mask)
        with self.lock:
            photo = self.photo(photo_id)
            if photo["version"] != snapshot["version"]:
                raise HTTPException(409, "预设分析期间照片已有更新，本次未覆盖后续调整。")
            previous = dict(photo)
            if data["scope"] == "local":
                photo["local_adjustment"] = {**local, "params": self.params(resolved["params"])}
            else:
                photo["params"] = self.params(resolved["params"])
            photo.update(version=photo["version"] + 1, status="settings_changed")
            try:
                self.persist()
            except Exception:
                photo.clear()
                photo.update(previous)
                raise
            return json_value({**resolved, "apply_keys": resolved.get("apply_keys", resolved.get("applied_keys", [])),
                               "photo": self.public_photo(photo), "preset": {"id": preset["id"], "name": preset["name"]}})

    def save_preset(self, data: dict) -> dict:
        cleaned = custom_payload(data)
        with self.lock:
            if len(self.state["presets"]) >= MAX_CUSTOM_PRESETS:
                raise ValueError(f"最多保存 {MAX_CUSTOM_PRESETS} 个自定义预设，请先删除不再使用的预设。")
            if any(item["name"].casefold() == cleaned["name"].casefold() for item in self.state["presets"].values()):
                raise HTTPException(409, "已有同名自定义预设，请换一个名称。")
            identifier = "user_" + uuid.uuid4().hex
            preset = custom_record(identifier, cleaned)
            previous = self.state["presets"]
            self.state["presets"] = {**previous, identifier: preset}
            try:
                self.persist()
            except Exception:
                self.state["presets"] = previous
                raise
            return {"preset": json_value(preset), **self.preset_catalog()}

    def delete_preset(self, identifier: str) -> dict:
        with self.lock:
            if identifier in BUILTIN_IDS:
                raise HTTPException(403, "内置预设不能删除，可以取消收藏。")
            if identifier not in self.state["presets"]:
                raise HTTPException(404, "未找到这个预设。")
            previous, favorites = self.state["presets"], self.state["preset_favorites"]
            self.state["presets"] = {key: value for key, value in previous.items() if key != identifier}
            self.state["preset_favorites"] = [value for value in favorites if value != identifier]
            try:
                self.persist()
            except Exception:
                self.state["presets"], self.state["preset_favorites"] = previous, favorites
                raise
            return self.preset_catalog()

    def favorite_preset(self, identifier: str, data: dict) -> dict:
        if set(data) != {"favorite"} or not isinstance(data.get("favorite"), bool):
            raise ValueError("favorite 必须是 true 或 false。")
        with self.lock:
            if identifier not in BUILTIN_IDS and identifier not in self.state["presets"]:
                raise HTTPException(404, "未找到这个预设。")
            previous = self.state["preset_favorites"]
            values = [value for value in previous if value != identifier]
            if data["favorite"]:
                values.append(identifier)
            self.state["preset_favorites"] = values
            try:
                self.persist()
            except Exception:
                self.state["preset_favorites"] = previous
                raise
            return self.preset_catalog()

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
                 "face_count": None, "status": "imported", "params": dict(self.defaults), "strokes": [], "local_adjustment": None, "spot_repairs": [], "face_shape": validate_face_shape(None),
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
        self.add_photo(photo_id, safe_stem(source_name) + " · 光影重塑.png", raw, hashlib.sha256(content).hexdigest())
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

    def shape_layout(self, snapshot: dict, shape=None, *, analysis=None) -> dict:
        """Pure layout in normalized source coordinates; no edit state is saved."""
        clean = validate_face_shape(snapshot.get("face_shape") if shape is None else shape)
        if analysis is None:
            analysis = self.analysis(snapshot, self.rgb(snapshot))
        operations, meta = build_operations(clean, analysis, snapshot["width"], snapshot["height"])
        return json_value({**meta, "operations": operations, "face_shape": clean,
                           "photo_id": snapshot["id"], "version": snapshot["version"]})

    def update_params(self, ids: list[str], params: dict) -> list[dict]:
        validated = self.params(params)
        with self.lock:
            photos = [self.photo(photo_id) for photo_id in ids]
            for photo in photos:
                photo.update(params=dict(validated), version=photo["version"] + 1, status="settings_changed")
            self.persist()
            return [self.public_photo(p) for p in photos]

    def restore_params(self, entries: Any) -> list[dict]:
        """Undo one batch only if none of its numeric settings changed afterwards."""
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise ValueError("批量撤销须包含 1～500 张照片。")
        validated, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "params", "expected_params"}:
                raise ValueError("每项必须包含 id、params 和 expected_params。")
            identifier = entry["id"]
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or identifier in seen:
                raise ValueError("照片 ID 无效或重复。")
            seen.add(identifier)
            validated.append((identifier, self.params(entry["params"]), self.params(entry["expected_params"])))
        with self.lock:
            photos = [self.photo(identifier) for identifier, _, _ in validated]
            if any(photo["params"] != expected for photo, (_, _, expected) in zip(photos, validated)):
                raise HTTPException(409, "部分照片已有后续调整，本次批量撤销未应用。")
            previous = [(p["params"], p["version"], p["status"]) for p in photos]
            for photo, (_, params, _) in zip(photos, validated):
                photo.update(params=params, version=photo["version"] + 1, status="settings_changed")
            try:
                self.persist()
            except Exception:
                for photo, (params, version, status) in zip(photos, previous):
                    photo.update(params=params, version=version, status=status)
                raise
            return [self.public_photo(photo) for photo in photos]

    def local_preview_mask(self, snapshot: dict, analysis: dict, shape: tuple[int, int]) -> np.ndarray | None:
        """Display the local skin region without allocating a full-size mask.

        Match engine.apply_local_adjustment's inward smoothstep and its
        source-pixel feather length, projected onto the saved preview grid.
        The mask indicates eligible skin, independently of slider intensity.
        """
        local = snapshot.get("local_adjustment")
        if local is None:
            return None
        mask = self.engine_module.mask_for(analysis, shape, snapshot.get("strokes", []), region=local["params"]["region"])
        h, w = shape
        source_h, source_w = snapshot["height"], snapshot["width"]
        rect = local["rect"]
        yy, xx = np.ogrid[:h, :w]
        xx, yy = (xx + .5) * (source_w / w), (yy + .5) * (source_h / h)
        left, top = rect["x"] * source_w, rect["y"] * source_h
        right, bottom = (rect["x"] + rect["width"]) * source_w, (rect["y"] + rect["height"]) * source_h
        distance = np.minimum(np.minimum(xx - left, right - xx), np.minimum(yy - top, bottom - yy))
        weight = np.clip(distance / min(32.0, .01 * min(source_h, source_w)), 0, 1).astype(np.float32)
        mask *= weight * weight * (3 - 2 * weight)
        return mask

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
                has_lighting = bool(snapshot.get("lighting_geometry") or (snapshot.get("lighting") or {}).get("enabled"))
                base, meta = self.lighting.retouch_base(snapshot, full, analysis)
                if has_lighting:
                    width, height = self.lighting.preview_size(snapshot)
                else:
                    scale = min(1.0, PREVIEW_SIDE / max(base.shape[:2]))
                    width, height = max(1, round(base.shape[1] * scale)), max(1, round(base.shape[0] * scale))
                output = self.lighting.resize_base(base, width, height)
                del base, full
                mask = self.engine_module.mask_for(analysis, output.shape[:2], snapshot["strokes"], region=snapshot["params"].get("region", "face"))
                local_mask = self.local_preview_mask(snapshot, analysis, output.shape[:2])
                shape_geometry = self.shape_layout(snapshot, analysis=analysis)
                if shape_geometry["operations"]:
                    mask = warp_image(mask, shape_geometry["operations"])
                    if local_mask is not None:
                        local_mask = warp_image(local_mask, shape_geometry["operations"])
                output, lighting_meta = self.lighting.render(snapshot, output)
                meta["lighting"] = lighting_meta
                preview_format, preview_extension = "PNG", "png"
                preview_path = writable_path(self.folders["previews"] / f"{photo_id}.{requested_version}.{preview_extension}")
                mask_path = writable_path(self.folders["previews"] / f"{photo_id}.{requested_version}.mask.png")
                local_mask_path = writable_path(self.folders["previews"] / f"{photo_id}.{requested_version}.local-mask.png") if local_mask is not None else None
                with self.lock:
                    current = self.photo(photo_id)
                    if current["version"] != requested_version:
                        return {"photo": self.public_photo(current), "meta": {"stale": True}}
                    write_image(preview_path, np.asarray(output, dtype=np.uint8), preview_format)
                    write_image(mask_path, Image.fromarray(np.rint(np.clip(mask, 0, 1) * 255).astype(np.uint8)), "PNG")
                    if local_mask_path is not None:
                        write_image(local_mask_path, Image.fromarray(np.rint(np.clip(local_mask, 0, 1) * 255).astype(np.uint8)), "PNG")
                    old_paths = [current.get("preview_path"), current.get("mask_path"), current.get("local_mask_path")]
                    warnings = list(dict.fromkeys(snapshot.get("source_warnings", []) + list(analysis.get("warnings", [])) + list(meta.get("warnings", []))))
                    current.update(preview_path=str(preview_path), mask_path=str(mask_path), preview_version=requested_version,
                                   local_mask_path=str(local_mask_path) if local_mask_path is not None else None,
                                   preview_contract=PREVIEW_CONTRACT,
                                   shape_geometry=shape_geometry,
                                   status="ready", face_count=int(analysis.get("face_count", 0)), warnings=warnings,
                                   last_meta=json_value(meta))
                    self.persist()
                    result = {"photo": self.public_photo(current), "meta": json_value(meta)}
                for old in old_paths:
                    if old and old not in (str(preview_path), str(mask_path), str(local_mask_path)):
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
                base, meta = self.lighting.retouch_base(snapshot, rgb, analysis)
                if job["max_side"] and max(base.shape[:2]) > job["max_side"]:
                    scale = job["max_side"]/max(base.shape[:2])
                    output = self.lighting.resize_base(base, max(1, round(base.shape[1]*scale)), max(1, round(base.shape[0]*scale)))
                else:
                    output = base
                del base
                output, lighting_meta = self.lighting.render(snapshot, output)
                meta["lighting"] = lighting_meta
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
                            "local_adjustment": snapshot.get("local_adjustment"),
                            "spot_repairs": snapshot.get("spot_repairs", []),
                            "face_shape": snapshot.get("face_shape"),
                            "lighting": snapshot.get("lighting"), "lighting_geometry": snapshot.get("lighting_geometry"),
                            "processing_device": meta.get("processing_device"),
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
        store.lighting.close()
        store.relight.close()
        store.gpu.close()
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
        request_limit = MAX_PRESET_REQUEST_BYTES if request.url.path.startswith("/api/presets") else MAX_REQUEST_BYTES
        limit_message = "预设请求不能超过 64 KiB。" if request_limit == MAX_PRESET_REQUEST_BYTES else "单次上传总大小不能超过 512 MiB。"
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > request_limit:
                    return JSONResponse({"detail": limit_message}, status_code=413)
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
                if received_bytes > request_limit:
                    raise HTTPException(413, limit_message)
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
            if received_bytes > request_limit:
                return JSONResponse({"detail": limit_message}, status_code=413)
            raise
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or Path(request.url.path).suffix.lower() in {".html", ".js", ".css"}:
            # Local incremental updates must replace the loaded editor scripts.
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
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
        except HTTPException:
            raise
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
        preparation = await run_in_threadpool(store.model_setup.status)
        status = preparation["models"]
        catalog = store.preset_catalog()
        response = JSONResponse(json_value({"version": __version__, "token": token, "defaults": store.defaults,
                                           "local_defaults": store.engine_module.LOCAL_DEFAULT_PARAMS,
                                           "capabilities": {"local_adjustments": True, "canonical_preview": True,
                                                            "preset_pipeline": 2, "spot_repairs": True,
                                                            "repair_brush": True, "face_shape": True,
                                                            "canonical_preview_contract": PREVIEW_CONTRACT},
                                           "presets": catalog["presets"], "preset_categories": catalog["categories"],
                                           "preset_portrait_groups": catalog["portrait_groups"],
                                           "preset_favorites": catalog["favorites"], "models": status,
                                           "storage": {"data_dir": str(store.data), "export_dir": str(store.folders["exports"]),
                                                       "max_files": MAX_FILES, "max_file_mib": 100, "max_pixels": MAX_PIXELS},
                                           "processing": {"precision": "RGB 8-bit", "color_space": "sRGB", "formats": ["jpg", "png", "tiff"]}}))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/presets")
    async def presets_list():
        return store.preset_catalog()

    @app.get("/api/hardware")
    async def hardware_status():
        return await run_in_threadpool(store.gpu.status)

    @app.post("/api/hardware")
    async def hardware_select(request: Request):
        return await run_in_threadpool(store.hardware_selection, await body(request))

    @app.post("/api/presets")
    async def presets_save(request: Request):
        return store.save_preset(await body(request))

    @app.delete("/api/presets/{preset_id}")
    async def presets_delete(preset_id: str):
        return store.delete_preset(preset_id)

    @app.post("/api/presets/{preset_id}/favorite")
    async def presets_favorite(preset_id: str, request: Request):
        return store.favorite_preset(preset_id, await body(request))

    @app.post("/api/photos/{photo_id}/preset")
    async def apply_photo_preset(photo_id: str, request: Request):
        return await run_in_threadpool(store.apply_preset, photo_id, await body(request))

    @app.get("/api/photos")
    async def photos():
        with store.lock:
            return {"photos": [store.public_photo(p) for p in store.state["photos"].values()
                               if p["id"] not in store.state["photo_trash"]],
                    "last_removed_ids": store.removal_undo_ids()}

    @app.post("/api/photos/remove")
    async def photos_remove(request: Request):
        data = await body(request)
        if set(data) != {"ids"}:
            raise ValueError("移除照片请求必须仅包含 ids。")
        return store.remove_photos(ids_from(data))

    @app.post("/api/photos/restore")
    async def photos_restore(request: Request):
        data = await body(request)
        if set(data) != {"ids"}:
            raise ValueError("恢复照片请求必须仅包含 ids。")
        return store.restore_photos(ids_from(data))

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
        if kind not in ("original", "preview", "mask", "local_mask", "input"):
            raise ValueError("未知图像类型。")
        with store.lock:
            photo = store.photo(photo_id)
            key = {"original": "original_preview_path", "preview": "preview_path", "mask": "mask_path", "local_mask": "local_mask_path", "input": "generation_input_preview_path"}[kind]
            value = photo.get(key)
            if not value:
                raise HTTPException(404, "此预览尚未生成。")
            path = writable_path(value)
            if path.parent != store.folders["previews"]:
                raise HTTPException(403, "无效预览位置。")
        return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")

    @app.get("/api/photos/{photo_id}/face-layout")
    async def face_layout(photo_id: str):
        with store.lock:
            snapshot = json.loads(json.dumps(store.photo(photo_id)))
        return await run_in_threadpool(store.shape_layout, snapshot)

    @app.post("/api/photos/{photo_id}/shape-layout")
    async def shape_layout(photo_id: str, request: Request):
        data = await body(request)
        if set(data) != {"face_shape"}:
            raise ValueError("塑形布局请求必须仅包含 face_shape。")
        clean = validate_face_shape(data["face_shape"])
        with store.lock:
            snapshot = json.loads(json.dumps(store.photo(photo_id)))
        return await run_in_threadpool(store.shape_layout, snapshot, clean)

    @app.post("/api/photos/{photo_id}/preview")
    async def preview(photo_id: str, request: Request):
        data = await body(request)
        with store.lock:
            photo = store.photo(photo_id)
            params = store.params(data.get("params", photo["params"]))
            strokes = validate_strokes(data["strokes"]) if "strokes" in data else photo["strokes"]
            local_adjustment = store.engine_module.validate_local_adjustment(data.get("local_adjustment", photo.get("local_adjustment")))
            spot_repairs = validate_repairs(data.get("spot_repairs", photo.get("spot_repairs")))
            face_shape = validate_face_shape(data.get("face_shape", photo.get("face_shape")))
            raw_lighting = data.get("lighting", photo.get("lighting"))
            if "lighting_geometry" in data:
                lighting, geometry = store.lighting.validate_edit(photo, raw_lighting, data["lighting_geometry"])
            else:
                lighting, geometry = store.lighting.validate_edit(photo, raw_lighting)
            # Validate the entire edit before persisting either part of it.
            previous = dict(photo)
            photo.update(params=params, strokes=strokes, lighting=lighting, lighting_geometry=geometry,
                         local_adjustment=local_adjustment, spot_repairs=spot_repairs, face_shape=face_shape,
                         version=photo["version"] + 1, status="settings_changed")
            version = photo["version"]
            try:
                store.persist()
            except Exception:
                photo.clear()
                photo.update(previous)
                raise
        try:
            return await run_in_threadpool(store.render_preview, photo_id, version)
        except Exception:
            with store.lock:
                current = store.state["photos"].get(photo_id)
                if current is not None and current.get("version") == version:
                    current.clear()
                    current.update(previous)
                    store.persist()
            raise

    @app.get("/api/lighting/status")
    async def lighting_status():
        return await run_in_threadpool(store.lighting.status)

    @app.post("/api/lighting/models/download")
    async def lighting_download(request: Request):
        data = await body(request)
        if data:
            raise ValueError("准备布光模型无需额外参数。")
        return {"job": store.lighting.create("download")}

    @app.post("/api/lighting/prepare")
    async def lighting_prepare(request: Request):
        data = await body(request)
        if set(data) - {"photo_id", "device", "refresh_geometry"} or not isinstance(data.get("photo_id"), str):
            raise ValueError("请选择要准备的照片。")
        return {"job": store.lighting.create("prepare", data["photo_id"], data.get("device", "auto"), data.get("refresh_geometry", False))}

    @app.get("/api/lighting/jobs")
    async def lighting_jobs():
        with store.lock:
            return {"jobs": [store.lighting.public_job(job) for job in list(store.lighting.jobs.values())[-30:]][::-1]}

    @app.get("/api/lighting/jobs/{job_id}")
    async def lighting_job(job_id: str):
        with store.lock:
            return {"job": store.lighting.public_job(store.lighting.get(job_id))}

    @app.post("/api/lighting/jobs/{job_id}/cancel")
    async def lighting_cancel(job_id: str):
        return {"job": store.lighting.cancel(job_id)}

    @app.get("/api/lighting/assets/{job_id}/{name}")
    async def lighting_asset(job_id: str, name: str):
        return FileResponse(store.lighting.asset(job_id, name), media_type="image/png")

    @app.post("/api/photos/{photo_id}/lighting")
    async def lighting_save(photo_id: str, request: Request):
        data = await body(request)
        if set(data) != {"lighting", "expected_version", "geometry_key"} or type(data["expected_version"]) is not int:
            raise ValueError("保留布光调整需要 lighting、expected_version 和 geometry_key。")
        with store.lock:
            photo = store.photo(photo_id)
            if photo["version"] != data["expected_version"]:
                raise HTTPException(409, "照片已有更新，请重新准备预览后保留布光调整。")
            configuration, key = store.lighting.validate_edit(photo, data["lighting"], data["geometry_key"])
            previous = dict(photo)
            photo.update(lighting=configuration, lighting_geometry=key, version=photo["version"]+1, status="settings_changed")
            version = photo["version"]
            try:
                store.persist()
            except Exception:
                photo.clear()
                photo.update(previous)
                raise
        try:
            return await run_in_threadpool(store.render_preview, photo_id, version)
        except Exception:
            # Rendering is part of retaining the edit. A failed preview must not
            # leave new settings saved while the UI reports that saving failed.
            with store.lock:
                current = store.state["photos"].get(photo_id)
                if current is not None and current.get("version") == version:
                    current.clear()
                    current.update(previous)
                    store.persist()
            raise

    @app.post("/api/settings/apply")
    async def apply_settings(request: Request):
        data = await body(request)
        ids = ids_from(data)
        return {"photos": store.update_params(ids, data.get("params")), "ids": ids}

    @app.post("/api/settings/restore")
    async def restore_settings(request: Request):
        data = await body(request)
        if set(data) != {"photos"}:
            raise ValueError("批量撤销请求必须仅包含 photos。")
        photos = store.restore_params(data["photos"])
        return {"photos": photos, "ids": [photo["id"] for photo in photos]}

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
