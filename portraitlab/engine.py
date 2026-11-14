"""Classic portrait engine with optional CUDA composition. SPDX-License-Identifier: AGPL-3.0-only.

RapidRetouch algorithms are vendored beside this module. MediaPipe adapters
load model bytes to support Unicode paths. This wrapper adds provenance, masks,
and the small PortraitLab parameter contract. Importing it never downloads or
writes files. Inputs and outputs are sRGB-encoded uint8 RGB arrays.
Face inference and RapidRetouch skin processing remain on CPU.
"""
from __future__ import annotations

import hashlib
import math
import os
from numbers import Real
from pathlib import Path
import threading
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from .vendor.rapidretouch_engine.tools import skin
from .vendor.rapidretouch_engine.tools.colour import linear_to_srgb, srgb_to_linear
from .vendor.rapidretouch_engine.tools.filters import blur

ALGORITHM = {
    "name": "RapidRetouch classic skin tools + PortraitLab wrapper",
    "repository": "https://github.com/crispin81/RapidRetouch",
    "commit": "d6f9a848fa5ba5628239b568777c5be3e7851a8e",
    "license": "AGPL-3.0-only",
    "relight": "face-local radial exposure in linear RGB; not generative relighting",
    "acne": "fixed-sensitivity compact spot detection, frequency-separated tone inpainting, independent delta strength",
}
MODEL_SPECS = {
    "face_landmarker": {
        "filename": "face_landmarker.task",
        "sha256": "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
        "url": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "code_license": "Apache-2.0",
        "weights_license": "not-independently-verified",
        "upstream_license_claim": "Apache-2.0",
        "license_source": "https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker#models",
        "input_edge": 1600,
    },
    "selfie_multiclass": {
        "filename": "selfie_multiclass_256x256.tflite",
        "sha256": "c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0",
        "url": "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/1/selfie_multiclass_256x256.tflite",
        "code_license": "Apache-2.0",
        "weights_license": "not-independently-verified",
        "upstream_license_claim": "Apache-2.0",
        "license_source": "https://storage.googleapis.com/mediapipe-assets/Model%20Card%20Multiclass%20Segmentation.pdf",
        "input_edge": 1024,
    },
}
DEFAULT_PARAMS = {
    "smooth": 0.25, "blemishes": 0.20, "even": 0.15, "texture": 0.10, "acne": 0.0,
    "exposure": 0.0, "warmth": 0.0, "saturation": 1.0, "relight": 0.0,
    "opacity": 0.75, "region": "face", "engine": "classic",
}
PRESETS = {
    "自然": dict(DEFAULT_PARAMS),
    "细腻": {**DEFAULT_PARAMS, "smooth": 0.45, "blemishes": 0.4, "even": 0.25, "texture": 0.25, "opacity": 0.85},
    "原图": {**DEFAULT_PARAMS, "smooth": 0.0, "blemishes": 0.0, "even": 0.0, "texture": 0.0, "opacity": 0.0},
}
_RANGES = {
    "smooth": (0, 1), "blemishes": (0, 1), "even": (0, 1), "texture": (0, 1), "acne": (0, 1),
    "exposure": (-2, 2), "warmth": (-1, 1), "saturation": (0, 2),
    "relight": (-1, 1), "opacity": (0, 1),
}
# Local adjustments are an additional pass over the global result. New numeric
# beauty controls inherit zero here, rather than silently doubling global edits.
LOCAL_DEFAULT_PARAMS = {**DEFAULT_PARAMS, **dict.fromkeys(_RANGES, 0.0),
                        "saturation": 1.0, "opacity": 1.0}
ANALYSIS_EDGE = 1600
ACNE_DETECTION_SENSITIVITY = 0.5
_LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def validate_params(params: dict | None) -> dict:
    """Merge supplied values with defaults; reject typos, booleans and NaN."""
    if params is None:
        params = {}
    if not isinstance(params, dict) or any(not isinstance(k, str) for k in params):
        raise ValueError("参数必须是以字符串为键的字典")
    unknown = set(params) - set(DEFAULT_PARAMS)
    if unknown:
        raise ValueError("未知参数：" + ", ".join(sorted(unknown)))
    result = {**DEFAULT_PARAMS, **params}
    for key, (lo, hi) in _RANGES.items():
        value = result[key]
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise ValueError(f"{key} 必须是有限数值")
        if not lo <= value <= hi:
            raise ValueError(f"{key} 必须在 {lo} 到 {hi} 之间")
        result[key] = float(value)
    if result["region"] not in ("face", "skin") or not isinstance(result["region"], str):
        raise ValueError("region 必须是 face 或 skin")
    if result["engine"] != "classic" or not isinstance(result["engine"], str):
        raise ValueError("当前仅支持 classic 引擎")
    return result


def validate_strokes(strokes: list | None) -> list[dict]:
    if strokes is None:
        return []
    if not isinstance(strokes, list) or len(strokes) > 500:
        raise ValueError("笔刷记录必须是列表，最多 500 笔")
    output, total = [], 0
    for stroke in strokes:
        if not isinstance(stroke, dict) or set(stroke) != {"mode", "radius", "points"}:
            raise ValueError("每笔必须包含且仅包含 mode、radius、points")
        if stroke["mode"] not in ("protect", "restore"):
            raise ValueError("笔刷模式必须是 protect 或 restore")
        radius = stroke["radius"]
        if isinstance(radius, bool) or not isinstance(radius, Real) or not math.isfinite(float(radius)) or not 0 <= radius <= 1:
            raise ValueError("笔刷半径必须在 0 到 1 之间，按图像短边归一化")
        points = stroke["points"]
        if not isinstance(points, list) or not points:
            raise ValueError("笔刷 points 必须是非空坐标列表")
        clean = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("笔刷坐标必须是 [x,y]")
            if any(isinstance(x, bool) or not isinstance(x, Real) or not math.isfinite(float(x)) or not 0 <= x <= 1 for x in point):
                raise ValueError("笔刷坐标必须是 0 到 1 的有限数值")
            clean.append([float(point[0]), float(point[1])])
        total += len(clean)
        if total > 50000:
            raise ValueError("笔刷总采样点不能超过 50000")
        output.append({"mode": stroke["mode"], "radius": float(radius), "points": clean})
    return output


def validate_local_adjustment(raw: dict | None) -> dict | None:
    """One persistent rectangle; UI selection visibility is not an image edit."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"rect", "params"}:
        raise ValueError("局部调整必须包含且仅包含 rect 和 params")
    rect = raw["rect"]
    if not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}:
        raise ValueError("矩形必须包含且仅包含 x、y、width、height")
    clean = {}
    for key, value in rect.items():
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise ValueError("矩形坐标必须是有限数值")
        clean[key] = float(value)
    if (not 0 <= clean["x"] < 1 or not 0 <= clean["y"] < 1 or clean["width"] <= 0 or clean["height"] <= 0
            or clean["x"] + clean["width"] > 1 + 1e-12
            or clean["y"] + clean["height"] > 1 + 1e-12):
        raise ValueError("矩形必须位于图像内，使用 0 到 1 的归一化坐标且宽高大于零")
    # Clamp only machine rounding at the right/bottom edge, never real overflow.
    clean["width"] = min(clean["width"], 1 - clean["x"])
    clean["height"] = min(clean["height"], 1 - clean["y"])
    if not isinstance(raw["params"], dict):
        raise ValueError("局部 params 必须是参数对象")
    params = validate_params({**LOCAL_DEFAULT_PARAMS, **raw["params"]})
    return {"rect": clean, "params": params}


def apply_local_adjustment(engine, base: np.ndarray, analysis: dict, adjustment: dict | None,
                           strokes: list | None = None) -> tuple[np.ndarray, dict]:
    """Apply a second pass only inside the rectangle, feathering strictly inward.

    Global pixels are already rendered at source resolution. Processing the
    whole base retains original landmark coordinates and filter context; only
    selected pixel centres can enter the result. No re-encoding touches the
    exterior, even when the local engine changes every input pixel.
    """
    local = validate_local_adjustment(adjustment)
    if local is None:
        return base, {"enabled": False}
    h, w = base.shape[:2]
    rect = local["rect"]
    left, top = rect["x"] * w, rect["y"] * h
    right, bottom = (rect["x"] + rect["width"]) * w, (rect["y"] + rect["height"]) * h
    x0, y0 = max(0, math.ceil(left - .5)), max(0, math.ceil(top - .5))
    x1, y1 = min(w, math.ceil(right - .5)), min(h, math.ceil(bottom - .5))
    feather = min(32.0, .01 * min(h, w))
    meta = {"enabled": True, **local, "feather_pixels": feather,
            "outside_policy": "exact_global_pixels_outside_rectangle",
            "composition": "additional_pass_on_global_result_with_inward_feather"}
    if x1 <= x0 or y1 <= y0:
        return base.copy(), meta
    changed, local_meta = engine.render(base, local["params"], analysis, strokes=strokes)
    output = base.copy()
    yy, xx = np.ogrid[y0:y1, x0:x1]
    distance = np.minimum(np.minimum(xx + .5 - left, right - xx - .5),
                          np.minimum(yy + .5 - top, bottom - yy - .5))
    weight = np.clip(distance / feather, 0, 1).astype(np.float32)
    weight = (weight * weight * (3 - 2 * weight))[..., None]
    source = base[y0:y1, x0:x1]
    target = changed[y0:y1, x0:x1]
    source_lin = srgb_to_linear(source.astype(np.float32) / 255)
    target_lin = srgb_to_linear(target.astype(np.float32) / 255)
    blended = np.rint(np.clip(linear_to_srgb(source_lin + weight * (target_lin - source_lin)), 0, 1) * 255).astype(np.uint8)
    # Exact copy at both limits avoids introducing numerical drift.
    output[y0:y1, x0:x1] = np.where(weight == 1, target, np.where(weight == 0, source, blended))
    meta["engine_meta"] = local_meta
    return output, meta


def render_adjustments(engine, rgb: np.ndarray, params: dict, analysis: dict,
                       strokes: list | None = None, local_adjustment: dict | None = None) -> tuple[np.ndarray, dict]:
    """Shared global/local composition for exports and legacy generation input."""
    local = validate_local_adjustment(local_adjustment)
    base, meta = engine.render(rgb, params, analysis, strokes=strokes)
    output, local_meta = apply_local_adjustment(engine, base, analysis, local, strokes)
    meta = {**meta, "local_adjustment": local_meta}
    meta["warnings"] = list(dict.fromkeys(meta.get("warnings", []) + local_meta.get("engine_meta", {}).get("warnings", [])))
    local_device = local_meta.get("engine_meta", {}).get("processing_device")
    if local_device is not None:
        meta["processing_device"] = local_device
    return output, meta


def _rgb_input(rgb: np.ndarray) -> np.ndarray:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 16:
        raise ValueError("输入必须是至少 16×16 的 uint8 RGB 三通道图像")
    return np.ascontiguousarray(rgb)


def _resize_map(array: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    data = np.asarray(array, dtype=np.float32)
    if data.ndim != 2 or not np.isfinite(data).all():
        raise ValueError("分析掩膜必须是有限的二维数组")
    if data.shape != tuple(shape):
        data = cv2.resize(data, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
    return np.clip(data, 0, 1).astype(np.float32)


def _faces(analysis: dict) -> list[np.ndarray]:
    faces = []
    for data in analysis.get("faces", []):
        face = np.asarray(data, dtype=np.float32)
        if face.shape != (478, 2) or not np.isfinite(face).all():
            raise ValueError("人脸关键点必须是有限的 478×2 归一化坐标")
        faces.append(face)
    return faces


def mask_for(analysis: dict, shape: tuple[int, int], strokes: list | None = None, region: str = "face") -> np.ndarray:
    """Return an editable skin mask at any matching image resolution.

    Protect erases automated skin selection. Restore restores that original
    selection; it cannot paint eyes, hair or background into the selection.
    Global exposure/color are intentionally independent of this beauty mask.
    """
    if region not in ("face", "skin"):
        raise ValueError("region 必须是 face 或 skin")
    h, w = int(shape[0]), int(shape[1])
    if min(h, w) < 1:
        raise ValueError("掩膜尺寸无效")
    clean_strokes = validate_strokes(strokes)
    if not analysis.get("face_count", len(analysis.get("faces", []))):
        return np.zeros((h, w), np.float32)
    value = analysis.get("face_mask" if region == "face" else "skin_mask", analysis.get("mask"))
    if value is None:
        return np.zeros((h, w), np.float32)
    baseline = _resize_map(value, (h, w))
    result = baseline.copy()
    for stroke in clean_strokes:
        radius = stroke["radius"] * min(h, w)
        if radius == 0:
            continue
        points = np.asarray(stroke["points"], np.float32) * [w - 1, h - 1]
        margin = radius * 2 + 2
        x0, y0 = np.maximum(np.floor(points.min(0) - margin), 0).astype(int)
        x1, y1 = np.minimum(np.ceil(points.max(0) + margin + 1), [w, h]).astype(int)
        canvas = np.zeros((y1 - y0, x1 - x0), np.float32)
        pts = np.rint(points - [x0, y0]).astype(np.int32)
        rr = max(1, round(radius))
        if len(pts) > 1:
            cv2.polylines(canvas, [pts], False, 1.0, max(1, 2 * rr), lineType=cv2.LINE_8)
        for point in pts:
            cv2.circle(canvas, tuple(point), rr, 1.0, -1)
        brush = np.clip(blur(canvas, max(0.5, radius * 0.2)), 0, 1)
        target = result[y0:y1, x0:x1]
        if stroke["mode"] == "protect":
            target *= 1 - brush
        else:
            target += (baseline[y0:y1, x0:x1] - target) * brush
    return np.clip(result, 0, 1).astype(np.float32)


def _body_regions(rgb: np.ndarray, faces: list[np.ndarray], parts: dict) -> list:
    if not faces or "body_skin" not in parts:
        return []
    subject = 1 - parts["background"] if "background" in parts else None
    people = skin.body_regions(rgb, faces, subject, parts.get("clothes"))
    # The learned class prevents skin-colored objects from entering body edits.
    for person in people:
        probability = _resize_map(parts["body_skin"], person.neck.shape)
        gate = np.clip((probability - 0.2) / 0.6, 0, 1)
        person.neck *= gate
        person.body *= gate
    return people


def compose_cpu(original: np.ndarray, changed: np.ndarray, p: dict, field: np.ndarray | None) -> np.ndarray:
    """Reference linear-light composition, shared by CPU and automatic fallback."""
    if p["opacity"] == 0:
        return original.copy()
    lin = srgb_to_linear(np.clip(changed, 0, 1)).astype(np.float32)
    if p["exposure"]:
        lin *= np.float32(2.0 ** p["exposure"])
    if field is not None:
        lin *= np.exp2(p["relight"] * field)[..., None]
    if p["warmth"]:
        gains = np.exp2(np.array([0.16, 0.0, -0.16], np.float32) * p["warmth"])
        gains /= float(np.dot(gains, _LUMA))
        lin *= gains
    if p["saturation"] != 1:
        luminance = np.sum(lin * _LUMA, axis=2, keepdims=True)
        lin = luminance + p["saturation"] * (lin - luminance)
    lin = np.maximum(lin, 0)
    if p["opacity"] < 1:
        source_lin = srgb_to_linear(original.astype(np.float32) / 255.0)
        lin = source_lin + p["opacity"] * (lin - source_lin)
    return np.rint(np.clip(linear_to_srgb(np.maximum(lin, 0)), 0, 1) * 255).astype(np.uint8)


class Engine:
    def __init__(self, models_dir: Path):
        self.models_dir = Path(models_dir).resolve()
        configured = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
        if configured and not self.models_dir.is_relative_to(Path(configured).expanduser().resolve()):
            raise ValueError("模型目录不在此安装配置允许的存储目录内")
        self._face_adapter = None
        self._parts_adapter = None
        self._verified: dict[str, str] = {}
        self._lock = threading.RLock()
        # Optional isolated CUDA compositor. Model inference and skin tools stay CPU.
        self.compositor = None

    def status(self) -> dict:
        records, errors = {}, []
        for model_id, spec in MODEL_SPECS.items():
            path = (self.models_dir / model_id / spec["filename"]).resolve()
            exists = path.is_file() and path.is_relative_to(self.models_dir)
            records[model_id] = {**spec, "path": str(path), "exists": exists, "verified": self._verified.get(model_id) == spec["sha256"]}
            if not exists:
                errors.append(f"缺少模型：{model_id}；请在设置中的“处理模型”下载并校验")
        return {"ready": not errors, "loaded": self._face_adapter is not None and self._parts_adapter is not None, "models": records, "errors": errors}

    def _ensure_models(self) -> None:
        if self._face_adapter is not None and self._parts_adapter is not None:
            return
        for model_id, spec in MODEL_SPECS.items():
            path = (self.models_dir / model_id / spec["filename"]).resolve()
            if not path.is_relative_to(self.models_dir) or not path.is_file():
                raise RuntimeError(f"缺少模型 {model_id}：{path}。引擎不会自动联网下载。")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != spec["sha256"]:
                raise RuntimeError(f"模型校验失败：{model_id}，SHA256 与固定版本不符")
            self._verified[model_id] = actual
        from .vendor.rapidretouch_engine.adapters.mediapipe_face import Adapter as FaceAdapter
        from .vendor.rapidretouch_engine.adapters.mediapipe_parts import Adapter as PartsAdapter

        def manifest(model_id):
            spec = MODEL_SPECS[model_id]
            return SimpleNamespace(id=model_id, source={"filename": spec["filename"]}, extra={"input_edge": spec["input_edge"]})

        try:
            self._face_adapter = FaceAdapter(manifest("face_landmarker"), self.models_dir, "cpu")
            self._parts_adapter = PartsAdapter(manifest("selfie_multiclass"), self.models_dir, "cpu")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._face_adapter is not None:
                self._face_adapter.landmarker.close()
            if self._parts_adapter is not None:
                self._parts_adapter.segmenter.close()
            self._face_adapter = self._parts_adapter = None

    def analyze(self, rgb_uint8: np.ndarray) -> dict:
        started = perf_counter()
        rgb_uint8 = _rgb_input(rgb_uint8)
        source_shape = list(rgb_uint8.shape[:2])
        scale = min(1.0, ANALYSIS_EDGE / max(source_shape))
        if scale < 1:
            rgb_uint8 = cv2.resize(rgb_uint8, (max(16, round(source_shape[1] * scale)), max(16, round(source_shape[0] * scale))), interpolation=cv2.INTER_AREA)
        rgb = rgb_uint8.astype(np.float32) / 255.0
        times, warnings = {}, []
        with self._lock:
            step = perf_counter()
            self._ensure_models()
            times["load_models"] = (perf_counter() - step) * 1000
            step = perf_counter()
            faces = self._face_adapter.predict(rgb)
            times["face_landmarks"] = (perf_counter() - step) * 1000
            step = perf_counter()
            parts = self._parts_adapter.predict(rgb) if faces else {}
            times["person_parts"] = (perf_counter() - step) * 1000
        shape = rgb.shape[:2]
        area = np.zeros(shape, np.float32)
        step = perf_counter()
        if not faces:
            warnings.append("未检测到人脸：已跳过自动美肤与面部局部提亮，可使用全局曝光和色彩调整。")
        for index, face in enumerate(faces):
            lm = face * np.array([shape[1], shape[0]], np.float32)
            if skin.face_width(lm) < 40:
                warnings.append(f"第 {index + 1} 张人脸在分析图中过小，已跳过该脸自动美肤。")
                continue
            try:
                weight = skin.face_area(rgb, [face], head_hair=parts.get("hair"), ai_skin=parts.get("face_skin"))
                geometry = skin.face_region(shape, lm, outline=skin.profile_outline(lm, skin.SKIN_FOREHEAD_REACH))
                # Suppress the blur spill into eye/lip/eyebrow exclusion zones.
                area = np.maximum(area, weight * geometry)
            except (ValueError, IndexError, cv2.error, np.linalg.LinAlgError) as error:
                warnings.append(f"第 {index + 1} 张人脸区域分析失败，已跳过：{type(error).__name__}")
        area = np.clip(area, 0, 1).astype(np.float32)
        all_skin = area.copy()
        if faces:
            try:
                for person in _body_regions(rgb, faces, parts):
                    all_skin = np.maximum(all_skin, _resize_map(np.maximum(person.neck, person.body), shape))
            except (ValueError, IndexError, cv2.error, np.linalg.LinAlgError) as error:
                warnings.append(f"身体皮肤区域分析失败，仅保留面部区域：{type(error).__name__}")
        times["skin_masks"] = (perf_counter() - step) * 1000
        times["total"] = (perf_counter() - started) * 1000
        return {
            "faces": faces, "parts": parts, "mask": area, "face_mask": area,
            "skin_mask": np.clip(all_skin, 0, 1).astype(np.float32),
            "face_count": len(faces), "shape": list(shape), "source_shape": source_shape,
            "warnings": warnings, "timings_ms": times, "models": self.status()["models"],
            "algorithm": dict(ALGORITHM), "analysis_long_edge_limit": ANALYSIS_EDGE,
        }

    def render(self, rgb_uint8: np.ndarray, params: dict, analysis: dict, strokes: list | None = None) -> tuple[np.ndarray, dict]:
        started = perf_counter()
        original = _rgb_input(rgb_uint8)
        p = validate_params(params)
        clean_strokes = validate_strokes(strokes)
        faces = _faces(analysis)
        warnings = list(analysis.get("warnings", []))
        times: dict[str, float] = {}
        src = original.astype(np.float32) / 255.0
        step = perf_counter()
        effect_mask = mask_for(analysis, src.shape[:2], clean_strokes, p["region"])
        times["mask"] = (perf_counter() - step) * 1000
        changed = src
        beauty_active = faces and p["opacity"] > 0 and any(p[k] > 0 for k in ("smooth", "blemishes", "even", "texture", "acne")) and bool(np.any(effect_mask > 0))
        parts = analysis.get("parts", {})
        acne_found = False
        if beauty_active:
            step = perf_counter()
            # Upstream's amount controls detection, not correction strength.
            # Keep detection fixed, then mix only its repair delta. The
            # canonical full-size render is also the source of previews.
            if p["acne"] > 0:
                spots = skin.acne_masks(src, faces, ACNE_DETECTION_SENSITIVITY,
                                        head_hair=parts.get("hair"), ai_skin=parts.get("face_skin"))
                acne_found = any(np.any(spot > 0) for _, spot in spots)
                if acne_found:
                    healed = skin.apply(src, faces, {"acne": ACNE_DETECTION_SENSITIVITY}, acne=spots,
                                        head_hair=parts.get("hair"), ai_skin=parts.get("face_skin"))
                    changed = src + p["acne"] * (healed - src)
            times["acne"] = (perf_counter() - step) * 1000
            step = perf_counter()
            region_params = {k: p[k] for k in ("smooth", "blemishes", "even", "texture")}
            if any(region_params.values()):
                changed = skin.apply(changed, faces, region_params, head_hair=parts.get("hair"), ai_skin=parts.get("face_skin"))
            times["face_skin"] = (perf_counter() - step) * 1000
            if p["region"] == "skin":
                step = perf_counter()
                people = _body_regions(src, faces, parts)
                changed = skin.apply_body(changed, people, region_params, region_params)
                times["body_skin"] = (perf_counter() - step) * 1000
            # Restrict every beauty delta, including Lab round-trip error, to
            # the displayed, manually corrected selection.
            changed = src + (changed - src) * effect_mask[..., None]
        else:
            times["face_skin"] = 0.0
        step = perf_counter()
        field = None
        if p["relight"] != 0 and faces and p["opacity"] > 0:
            h, w = src.shape[:2]
            yy, xx = np.ogrid[:h, :w]
            field = np.zeros((h, w), np.float32)
            for face in faces:
                pts = face * np.array([w, h], np.float32)
                centre = (pts[10] + pts[152]) * 0.5
                fw = max(skin.face_width(pts), 1.0)
                radial = np.exp(-0.5 * (((xx - centre[0]) / (0.48 * fw)) ** 2 + ((yy - centre[1]) / (0.65 * fw)) ** 2))
                field = np.maximum(field, radial.astype(np.float32))
            field *= mask_for(analysis, (h, w), clean_strokes, "face")
        color_active = p["exposure"] != 0 or p["warmth"] != 0 or p["saturation"] != 1 or field is not None
        device_info = {"preference": "cpu", "actual_device": "cpu", "gpu_name": None,
                       "stage_note": "CPU 人脸识别、修肤、调色合成与文件压缩。"}
        if p["opacity"] == 0 or (not beauty_active and not color_active):
            output = original.copy()
            if self.compositor is not None:
                device_info = self.compositor.passthrough()
            device_info["stage_note"] = "当前调整不改变像素，直接保留原图；未运行 GPU 合成。"
        elif self.compositor is not None:
            output, device_info = self.compositor.compose(original, changed, p, field)
            if device_info.get("warning"):
                warnings.append(device_info["warning"])
        else:
            output = compose_cpu(original, changed, p, field)
        times["color_and_radial_exposure"] = (perf_counter() - step) * 1000
        times["total"] = (perf_counter() - started) * 1000
        metadata = {
            "params": p, "face_count": len(faces), "warnings": warnings,
            "processing_device": device_info,
            "timings_ms": times, "algorithm": dict(ALGORITHM), "models": analysis.get("models", self.status()["models"]),
            "analysis_shape": analysis.get("shape"), "output_shape": list(original.shape[:2]),
            "mask_coverage": float(np.mean(effect_mask > 0.1)), "stroke_count": len(clean_strokes),
            "acne": {"strength": p["acne"], "detection_sensitivity": ACNE_DETECTION_SENSITIVITY,
                     "spots_found": acne_found, "scope": "face_skin_with_protection_and_local_rectangle"},
            "color_processing": "Input/output encoded sRGB uint8 RGB; skin algorithms use Lab; exposure/white-balance gains/saturation/overall opacity use linear RGB.",
            "brush_scope": "Skin beauty and face-local radial exposure only; global exposure/color are not masked.",
            "relight_ev_range_before_opacity": [float((p["relight"] * field).min()), float((p["relight"] * field).max())] if field is not None else [0.0, 0.0],
            "limitations": ["分割模型的原生分辨率为 256×256，细发与边界仍需检查。", "直接按不同尺寸渲染时，细节效果可能略有差异；应用预览由原尺寸结果缩小。", "自动保护五官和须发不等于身份或纹理保真保证。", "去痘修复检测到的独立面部痘点，可能漏检连片痘痕或误判雀斑；可框选并用保护笔刷保留特征。"],
        }
        return output, metadata
