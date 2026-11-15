"""Photo-aware, scoped preset resolution without downloads or inference.

The resolver works from the unmodified source pixels, never a previous preview.
Repeated application is therefore idempotent. It resolves only the selected
operation's parameters; the existing CPU/CUDA renderer remains authoritative.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from copy import deepcopy
import math

import cv2
import numpy as np

from .engine import validate_params
from .presets import GROUP_LABELS, NEUTRAL, PARAMETER_GROUPS
from .vendor.rapidretouch_engine.tools.colour import srgb_to_linear

PIPELINE_VERSION = "scoped-photo-aware-presets-v1"
_LUMA = np.array([.2126, .7152, .0722], np.float32)


def _mask(values):
    value = np.asarray(values)
    if (value.ndim != 2 or not value.size
            or not (np.issubdtype(value.dtype, np.number) or np.issubdtype(value.dtype, np.bool_))
            or not np.isfinite(value).all()):
        raise ValueError("预设分析选区必须是有限数值的二维蒙版。")
    return np.clip(value.astype(np.float32), 0, 1)


def _sample_mask(value, shape, bounds, stride):
    y0, y1, x0, x1 = bounds
    yy, xx = np.meshgrid(np.arange(y0, y1, stride), np.arange(x0, x1, stride), indexing="ij")
    map_x = ((xx + .5) * value.shape[1] / shape[1] - .5).astype(np.float32)
    map_y = ((yy + .5) * value.shape[0] / shape[0] - .5).astype(np.float32)
    return cv2.remap(value, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _rect_bounds(rect, shape):
    if set(rect) != {"x", "y", "width", "height"}:
        raise ValueError("预设选区矩形需要 x、y、width、height。")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in rect.values()):
        raise ValueError("预设选区矩形必须使用有限数值。")
    x, y, width, height = (rect[key] for key in ("x", "y", "width", "height"))
    if not (0 <= x < 1 and 0 <= y < 1 and width > 0 and height > 0
            and x + width <= 1 + 1e-12 and y + height <= 1 + 1e-12):
        raise ValueError("预设选区矩形必须位于图片内。")
    h, w = shape
    return max(0, int(y*h)), min(h, math.ceil((y+height)*h)), max(0, int(x*w)), min(w, math.ceil((x+width)*w))


def _values_stats(linear, selected):
    pixels = linear[selected]
    if not pixels.size:
        return None
    luminance = pixels @ _LUMA
    return {
        "sample_count": int(len(pixels)),
        "median_luminance": float(np.median(luminance)),
        "luminance_p95": float(np.percentile(luminance, 95)),
        "channel_p98": float(np.percentile(np.max(pixels, axis=1), 98)),
        "clipped_fraction": float(np.mean(np.max(pixels, axis=1) >= .995)),
        "near_black_fraction": float(np.mean(luminance <= .001)),
    }


def source_statistics(rgb, analysis=None, local_mask=None):
    """Sample a source image and optional selection without changing pixels."""
    source = np.asarray(rgb)
    if source.ndim != 3 or source.shape[2] != 3 or source.dtype != np.uint8 or min(source.shape[:2]) < 1:
        raise ValueError("预设分析需要非空的 RGB uint8 原片。")
    full_shape = source.shape[:2]
    selection = None
    # Crop first so a small local rectangle is not lost while reducing a large photo.
    if isinstance(local_mask, dict):
        y0, y1, x0, x1 = _rect_bounds(local_mask, full_shape)
    elif local_mask is not None:
        selection = _mask(local_mask)
        if not np.any(selection > .05):
            raise ValueError("当前选区为空，请重新框选后应用预设。")
        ys, xs = np.where(selection > .05)
        scale_y, scale_x = full_shape[0] / selection.shape[0], full_shape[1] / selection.shape[1]
        y0, y1 = int(ys.min()*scale_y), min(full_shape[0], math.ceil((ys.max()+1)*scale_y))
        x0, x1 = int(xs.min()*scale_x), min(full_shape[1], math.ceil((xs.max()+1)*scale_x))
    else:
        y0, y1, x0, x1 = 0, full_shape[0], 0, full_shape[1]
    cropped = source[y0:y1, x0:x1]
    stride = max(1, math.ceil(max(cropped.shape[:2]) / 768))
    sampled = cropped[::stride, ::stride].astype(np.float32) / 255
    bounds = y0, y1, x0, x1
    sampled_selection = np.ones(sampled.shape[:2], bool) if selection is None else _sample_mask(selection, full_shape, bounds, stride) > .05
    linear = srgb_to_linear(sampled).astype(np.float32)
    scene = _values_stats(linear, sampled_selection)
    if scene is None:
        raise ValueError("当前选区没有可分析的像素，请重新框选。")
    face = None
    cached = analysis or {}
    face_mask = cached.get("face_mask", cached.get("mask"))
    if int(cached.get("face_count", len(cached.get("faces", [])))) > 0 and face_mask is not None:
        resized = _sample_mask(_mask(face_mask), full_shape, bounds, stride)
        face = _values_stats(linear, sampled_selection & (resized > .2))
    return {"scope": "selection" if local_mask is not None else "image", "scene": scene, "face": face}


def _ev(target, measured):
    return math.log2(target / max(measured, .0001))


def _positive_limit(stats, maximum):
    # A 98th-percentile RGB ceiling protects broad highlights without a single
    # tiny specular pixel blocking every correction. It cannot recover clipping.
    return min(maximum, max(0., _ev(.94, stats["channel_p98"])))


def _adaptive_light(strategy, template, current, stats, keys, warnings):
    scene, face = stats["scene"], stats["face"]
    strength = template["opacity"]
    values = {}
    median = scene["median_luminance"]
    if "exposure" in keys:
        if strategy in ("balance", "lift", "backlight", "face_lift"):
            desired = _ev(.18, median)
            minimum = -1. if strategy == "balance" else 0.
            maximum = .8 if strategy == "backlight" else 1.
            desired = float(np.clip(desired, minimum, maximum)) * strength
            if desired > 0:
                limit = _positive_limit(scene, maximum)
                if desired > limit + .01:
                    warnings.append("高光余量有限，已限制全图提亮；可改用面部补光或局部调整。")
                desired = min(desired, limit)
            values["exposure"] = round(desired, 4)
        elif strategy in ("protect_highlights", "face_reduce"):
            desired = min(0., _ev(.24, median), _ev(.94, scene["channel_p98"]))
            values["exposure"] = round(float(np.clip(desired, -1., 0.)) * strength, 4)
    if "relight" in keys:
        if face is None:
            values["relight"] = 0.
            warnings.append("当前范围未检测到可用面部选区，面部补光未启用；可框选后调整曝光。")
        else:
            exposure = values.get("exposure", current["exposure"])
            desired = _ev(.22, face["median_luminance"]) - exposure
            if strategy == "face_reduce":
                maximum = min(.65, max(.12, abs(template["relight"]) * 1.6))
                desired = float(np.clip(desired, -maximum, 0.))
            else:
                maximum = min(.9 if strategy == "backlight" else .65, max(.18, template["relight"] * 1.5))
                desired = float(np.clip(desired, 0., maximum))
                limit = max(0., _ev(.94, face["channel_p98"]) - exposure)
                if desired > limit + .01:
                    warnings.append("面部亮区接近上限，已限制补光强度。")
                desired = min(desired, limit)
            values["relight"] = round(desired * strength, 4)
    if scene["near_black_fraction"] > .98:
        warnings.append("当前范围接近纯黑，曝光调整无法恢复原片未记录的细节。")
    if scene["clipped_fraction"] > .02:
        warnings.append("当前范围已有通道过曝，压暗只能降低亮度，无法恢复丢失细节。")
    return values


def resolve_preset(preset, current_params, rgb, analysis=None, local_mask=None):
    """Return concrete scoped parameters plus user-facing, JSON-safe feedback.

    Callers supply a trusted built-in or persisted user recipe. The function
    does not mutate the recipe, current parameters, source image or analysis.
    A user recipe remains a full snapshot for compatibility and is identified
    as such in apply_mode and summary; all built-ins use independent operations.
    """
    if not isinstance(preset, dict) or not isinstance(preset.get("params"), dict):
        raise ValueError("预设必须包含有效参数。")
    current = validate_params(current_params)
    template = validate_params(preset["params"])
    identifier = str(preset.get("id", ""))
    name = str(preset.get("name", "预设"))
    warnings = []
    stats = source_statistics(rgb, analysis, local_mask)
    groups = list(preset.get("apply_groups", PARAMETER_GROUPS))
    if not groups or any(group not in PARAMETER_GROUPS for group in groups):
        raise ValueError("预设处理分组无效。")
    keys = list(preset.get("apply_keys", template))
    if any(key not in template for key in keys):
        raise ValueError("预设含不支持的参数。")
    mode = "snapshot" if not preset.get("builtin", False) else "scoped"
    if identifier == "original":
        result = dict(NEUTRAL)
        groups, keys = list(PARAMETER_GROUPS), list(result)
        summary = "已重置当前范围的修肤、明暗与色彩；总强度为 100%，可直接继续调整。"
    elif mode == "snapshot":
        result = dict(template)
        groups, keys = list(PARAMETER_GROUPS), list(result)
        summary = f"已应用《{name}》的完整参数快照，替换当前范围的修肤、明暗与色彩。"
    else:
        result = dict(current)
        strength = template["opacity"]
        for key in keys:
            value = template[key]
            if key in ("region", "engine", "acne_sensitivity"):
                result[key] = value
            elif key == "saturation":
                result[key] = 1 + (value - 1) * strength
            elif key != "opacity":
                result[key] = value * strength
        adaptive = preset.get("adaptive")
        if adaptive:
            result.update(_adaptive_light(adaptive, template, current, stats, keys, warnings))
            if all(abs(result[key]) < .005 for key in keys if key in ("exposure", "relight")) and not warnings:
                warnings.append("当前明暗已接近此预设的目标，未额外增减曝光；仍可手动微调。")
        if result["opacity"] == 0:
            result["opacity"] = 1.
            warnings.append("原总强度为 0，已恢复到 100% 以显示此次调整。")
        if "skin" in groups and stats["face"] is None:
            warnings.append("当前范围未检测到可用面部皮肤，修肤可能不生效；可使用点修处理局部瑕疵。")
        operation = "、".join(GROUP_LABELS[group] for group in groups)
        specifics = []
        if "exposure" in keys:
            specifics.append(f"曝光 {result['exposure']:+.2f} EV")
        if "relight" in keys:
            specifics.append(f"面部补光 {result['relight']:+.2f} EV")
        if "acne" in keys and result["acne"] > 0:
            specifics.append(f"去痘强度 {result['acne']:.0%}")
        summary = f"已应用《{name}》：{operation}" + (" · " + "，".join(specifics) if specifics else "") + "；其他处理组保持原值。"
        if result["opacity"] < .999:
            summary += f" 当前总强度 {result['opacity']:.0%}。"
    result = validate_params(result)
    return {"params": result, "pipeline": PIPELINE_VERSION, "preset_id": identifier,
            "apply_mode": mode, "apply_groups": groups, "apply_keys": keys,
            "changed_groups": groups, "group_labels": dict(GROUP_LABELS),
            "operations": deepcopy(preset.get("operations", [])), "adaptive": preset.get("adaptive", False),
            "summary": summary, "warnings": list(dict.fromkeys(warnings)), "analysis": stats}
