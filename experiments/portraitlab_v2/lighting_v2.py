"""Deterministic, image-preserving subject/background lighting for V2.

SPDX-License-Identifier: AGPL-3.0-only
A real local Depth Anything V2 Small model supplies relative depth; normals are
derived from that depth. Rendering changes existing RGB values only. It does
not recover reflectance, remove baked-in shadows or synthesize cast shadows.
There are no downloads or writes on import. Geometry preparation is explicit.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path
from time import perf_counter

# The portable AI interpreter and core runtime use matching CPython/NumPy.
# Make core OpenCV/Pillow available before importing either, including CLI use.
_core_libraries = Path(sys.executable).resolve().parent.parent/"runtime"/"Lib"/"site-packages"
if _core_libraries.is_dir() and str(_core_libraries) not in sys.path:
    sys.path.append(str(_core_libraries))

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from portraitlab.lighting import DEFAULT_LIGHT as _OLD_LIGHT, validate_lighting as _old_validate

ALGORITHM = "portraitlab-v2-subject-background-lighting-1"
GEOMETRY_VERSION = "dav2-small-native-float-normals-v2"
LIMITATIONS = ("Relative single-view geometry and depth-derived normals, not measured geometry or physical inverse rendering. "
               "The renderer cannot remove original illumination, reconstruct clipped detail, or create true occlusion/cast shadows.")
DEFAULT_LIGHT = deepcopy(_OLD_LIGHT)
DEFAULT_GROUP = {"enabled": True, "ambient": 1., "lights": []}
DEFAULT_LIGHTING = {"schema": 2, "enabled": False, "strength": 1.,
                    "subject": deepcopy(DEFAULT_GROUP), "background": deepcopy(DEFAULT_GROUP)}
SCOPES = ("subject", "background")
LIGHT_TYPES = ("directional", "point", "spot", "area")
TILE = 256


def _number(value, low, high, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} 必须是有限数值。")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} 必须是有限数值。") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low:g} 到 {high:g} 之间。")
    return value


def _size(image_size):
    if (not isinstance(image_size, (tuple, list)) or len(image_size) != 2
            or any(isinstance(v, bool) or not isinstance(v, Integral) or v < 2 for v in image_size)):
        raise ValueError("image_size 须为至少 2×2 的 (width,height)。")
    return int(image_size[0]), int(image_size[1])


def _bounds(bounds, size):
    width, height = size
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, Integral) for v in bounds)):
        raise ValueError("bounds 须为整数 [x0,y0,x1,y1]。")
    x0, y0, x1, y1 = map(int, bounds)
    if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
        raise ValueError("布光图块超出照片。")
    return x0, y0, x1, y1


def validate_light(value):
    # Retain the existing strict, pinned four-light schema for each group.
    try:
        return _old_validate({"lights": [value]})["lights"][0]
    except (OverflowError, TypeError) as error:
        raise ValueError("光源参数须为有限的有效数值。") from error


def validate_lighting(value=None):
    if value is None:
        return deepcopy(DEFAULT_LIGHTING)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIGHTING):
        raise ValueError("V2 布光参数包含未知字段。")
    result = {**deepcopy(DEFAULT_LIGHTING), **deepcopy(value)}
    if type(result["schema"]) is not int or result["schema"] != 2:
        raise ValueError("V2 布光 schema 必须为 2。")
    if type(result["enabled"]) is not bool:
        raise ValueError("布光开关须为布尔值。")
    result["strength"] = _number(result["strength"], 0, 1, "整体强度")
    for scope in SCOPES:
        group = result[scope]
        if not isinstance(group, dict) or set(group) - set(DEFAULT_GROUP):
            raise ValueError(f"{scope} 仅接受 enabled、ambient、lights。")
        group = {**deepcopy(DEFAULT_GROUP), **group}
        if type(group["enabled"]) is not bool:
            raise ValueError("区域布光开关须为布尔值。")
        try:
            old = _old_validate({"ambient": group["ambient"], "lights": group["lights"]})
        except (OverflowError, TypeError) as error:
            raise ValueError("区域布光参数须为有限的有效数值。") from error
        result[scope] = {"enabled": group["enabled"], "ambient": old["ambient"], "lights": old["lights"]}
    return result


def lighting_active(value=None):
    state = validate_lighting(value)
    return bool(state["enabled"] and state["strength"] and any(
        group["enabled"] and (group["ambient"] != 1 or
            any(light["enabled"] and light["intensity"] > 0 for light in group["lights"]))
        for group in (state["subject"], state["background"])))


def _preset(identifier, name, description, ambient, *lights):
    values = [{**deepcopy(DEFAULT_LIGHT), **light, "id": f"light-{index+1}"} for index, light in enumerate(lights)]
    return {"id": identifier, "name": name, "description": description,
            "ambient": ambient, "lights": values,
            "limitations": "提供此布光方向的图像调整；不保证重建真实摄影阴影。"}


LIGHT_PRESETS = [
    _preset("soft", "清晖柔光", "宽阔柔光轻提主体，保留自然明暗。", .86,
            {"type": "area", "x": .35, "y": .2, "z": 1.1, "radius": 1.2, "intensity": .55, "softness": .9}),
    _preset("window", "临窗清光", "侧上方柔光形成克制的面部层次。", .73,
            {"type": "area", "x": -.05, "y": .22, "z": 1., "radius": 1., "intensity": .85, "temperature": 6200, "softness": .8}),
    _preset("side", "侧光塑形", "侧向主光与正面轻补光。", .55,
            {"azimuth": -155, "elevation": 30, "intensity": .85, "temperature": 6000, "softness": .25},
            {"azimuth": -20, "elevation": 65, "intensity": .2, "softness": .8}),
    _preset("rembrandt", "伦勃朗光向", "侧上方主光与微弱补光；三角阴影受原片和估计几何限制。", .5,
            {"azimuth": -140, "elevation": 38, "intensity": .95, "temperature": 5600, "softness": .18},
            {"azimuth": -10, "elevation": 65, "intensity": .12, "softness": .75}),
    _preset("butterfly", "蝶形柔光", "正上方光向与低位轻补，保持五官原有结构。", .62,
            {"azimuth": -90, "elevation": 50, "intensity": .62, "softness": .4},
            {"type": "area", "x": .5, "y": 1.1, "z": 1.2, "radius": .8, "intensity": .15, "softness": .9}),
    _preset("clamshell", "上下映光", "上方主光、下方补光，舒展局部反差。", .56,
            {"type": "area", "x": .5, "y": -.05, "z": 1., "radius": .9, "intensity": .8, "softness": .9},
            {"type": "area", "x": .5, "y": 1.12, "z": 1., "radius": .8, "intensity": .4, "softness": .9}),
    _preset("rim", "轮廓微光", "两侧低角度补光强调边缘起伏，不生成新轮廓或遮挡阴影。", .66,
            {"azimuth": 175, "elevation": 15, "intensity": .85, "softness": .18},
            {"azimuth": 5, "elevation": 18, "intensity": .65, "softness": .18}),
    _preset("warmcool", "冷暖相映", "暖主光与冷补光分离，保留原有构图。", .48,
            {"azimuth": -145, "elevation": 45, "intensity": .75, "temperature": 3200, "softness": .4},
            {"azimuth": -25, "elevation": 38, "intensity": .65, "temperature": 10500, "softness": .45}),
    _preset("golden", "暮色斜照", "低角度暖光适合轻度夕照氛围。", .65,
            {"azimuth": -145, "elevation": 27, "intensity": .85, "temperature": 3800, "softness": .3}),
    _preset("spot", "聚光留影", "可拖动聚光位置与照射目标，束宽和柔度独立可调。", .65,
            {"type": "spot", "x": .25, "y": .12, "z": 1.1, "radius": 1.2,
             "target_x": .5, "target_y": .45, "beam_angle": 80, "intensity": 1.3, "softness": .7}),
    _preset("lowkey", "幽光侧影", "降低所选区域环境增益，以单盏侧光保留明暗层次。", .32,
            {"azimuth": -155, "elevation": 35, "intensity": .95, "temperature": 5600, "softness": .2}),
    _preset("balanced", "素光匀照", "均衡两侧柔光，适合轻度整理已有光线。", .72,
            {"type": "area", "x": .1, "y": .3, "z": 1.2, "radius": 1.1, "intensity": .45, "softness": .9},
            {"type": "area", "x": .9, "y": .3, "z": 1.2, "radius": 1.1, "intensity": .4, "softness": .9}),
]
PRESETS = LIGHT_PRESETS


def preset_lighting(identifier, scope="subject", current=None):
    if scope not in SCOPES:
        raise ValueError("预设范围须为 subject 或 background。")
    preset = next((item for item in LIGHT_PRESETS if item["id"] == identifier), None)
    if preset is None:
        raise ValueError("布光预设不存在。")
    state = validate_lighting(current)
    state["enabled"] = True
    state[scope] = {"enabled": True, "ambient": preset["ambient"], "lights": deepcopy(preset["lights"])}
    return validate_lighting(state)


def light_handle(light, image_size):
    """Normalized canvas location. Directional lights use a front hemisphere."""
    light = validate_light(light)
    width, height = _size(image_size)
    if light["type"] != "directional":
        return {"x": light["x"], "y": light["y"]}
    azimuth, elevation = math.radians(light["azimuth"]), math.radians(light["elevation"])
    radius = .45*math.cos(elevation)
    return {"x": .5+radius*math.cos(azimuth)/(width/height),
            "y": .5+radius*math.sin(azimuth)}


def move_light(value, scope, light_id, x, y, image_size):
    if scope not in SCOPES:
        raise ValueError("光源范围须为 subject 或 background。")
    x, y = _number(x, -1, 2, "光源 x"), _number(y, -1, 2, "光源 y")
    width, height = _size(image_size)
    state = validate_lighting(value)
    light = next((item for item in state[scope]["lights"] if item["id"] == light_id), None)
    if light is None:
        raise ValueError("光源不存在。")
    light["x"], light["y"] = x, y
    if light["type"] == "directional":
        dx, dy = (x-.5)*width/height, y-.5
        radius = math.hypot(dx, dy)
        if radius > 1e-9:
            light["azimuth"] = math.degrees(math.atan2(dy, dx))
        light["elevation"] = math.degrees(math.acos(np.clip(radius/.45, 0, math.cos(math.radians(5)))))
    return validate_lighting(state)


@dataclass(frozen=True)
class LightingFields:
    normal: np.ndarray
    depth: np.ndarray
    subject_mask: np.ndarray


def prepare_fields(analysis):
    """Validate cached fields once; reuse LightingFields across tile requests."""
    if isinstance(analysis, LightingFields):
        return analysis
    if not isinstance(analysis, dict) and not hasattr(analysis, "files"):
        raise ValueError("布光需要真实深度、法线与主体蒙版。请先分析照片。")
    def get(name, alias=None):
        if name in analysis:
            return analysis[name]
        if alias and alias in analysis:
            return analysis[alias]
        raise ValueError(f"缺少布光分析字段 {name}；不会使用虚拟平面替代。")
    normal = np.asarray(get("normal"))
    if normal.ndim != 3 or normal.shape[2] != 3 or min(normal.shape[:2]) < 2 or not np.isfinite(normal).all():
        raise ValueError("法线须为有限的 H×W×3 数组。")
    normal = normal.astype(np.float32)/127.5-1 if normal.dtype == np.uint8 else normal.astype(np.float32)
    if np.any(np.abs(normal) > 1.00001) or np.any(np.linalg.norm(normal, axis=2) < 1e-5):
        raise ValueError("法线须为非零的 [-1,1] 向量。")
    fields = []
    for name, alias in (("depth", None), ("subject_mask", "mask")):
        raw = np.asarray(get(name, alias))
        if raw.ndim != 2 or min(raw.shape) < 2 or not np.isfinite(raw).all():
            raise ValueError(f"{name} 须为有限二维数组。")
        array = raw.astype(np.float32)/(255 if raw.dtype == np.uint8 else 1)
        if np.any((array < 0) | (array > 1)):
            raise ValueError(f"{name} 须在 0 到 1 之间。")
        fields.append(array)
    return LightingFields(normal, fields[0], fields[1])


def _sample(field, xs, ys, size):
    width, height = size
    fh, fw = field.shape[:2]
    x = np.clip((xs+.5)*fw/width-.5, 0, fw-1)
    y = np.clip((ys+.5)*fh/height-.5, 0, fh-1)
    ix, iy = np.floor(x).astype(np.intp), np.floor(y).astype(np.intp)
    jx, jy = np.minimum(ix+1, fw-1), np.minimum(iy+1, fh-1)
    wx, wy = x-ix, y-iy
    if field.ndim == 3:
        wx, wy = wx[None, :, None], wy[:, None, None]
    else:
        wx, wy = wx[None, :], wy[:, None]
    top = field[iy[:, None], ix]*(1-wx)+field[iy[:, None], jx]*wx
    bottom = field[jy[:, None], ix]*(1-wx)+field[jy[:, None], jx]*wx
    return (top*(1-wy)+bottom*wy).astype(np.float32)


def _normalize(vector):
    return vector/np.maximum(np.linalg.norm(vector, axis=-1, keepdims=True), 1e-8)


def _group_active(group):
    """Whether this validated group can differ from an identity gain."""
    return bool(group['enabled'] and (group['ambient']!=1 or
        any(light['enabled'] and light['intensity']>0 for light in group['lights'])))


def _gain(normal, position, group, aspect):
    if not group["enabled"]:
        return np.ones_like(position)
    gain = np.full_like(position, group["ambient"])
    for light in group["lights"]:
        if not light["enabled"] or not light["intensity"]:
            continue
        temperature = math.log(light["temperature"]/6500)
        tint = np.array([math.exp(-.35*temperature), math.exp(-.04*abs(temperature)),
                         math.exp(.45*temperature)], np.float32)
        wrap = light["softness"]*.35
        if light["type"] == "directional":
            azimuth, elevation = math.radians(light["azimuth"]), math.radians(light["elevation"])
            direction = np.array([math.cos(azimuth)*math.cos(elevation),
                                  math.sin(azimuth)*math.cos(elevation), math.sin(elevation)], np.float32)
            amount = np.maximum(0, (np.sum(normal*direction, axis=-1)+wrap)/(1+wrap))
        else:
            origin = np.array([(light["x"]-.5)*aspect, light["y"]-.5, light["z"]], np.float32)
            offsets = [(0, 0)]
            if light["type"] == "area":
                radius = light["radius"]/2
                offsets += [(-radius, -radius), (-radius, radius), (radius, -radius), (radius, radius)]
            amount = np.zeros(position.shape[:2], np.float32)
            for dx, dy in offsets:
                delta = origin+np.array([dx, dy, 0], np.float32)-position
                distance2 = np.sum(delta*delta, axis=-1)
                direction = delta/np.sqrt(np.maximum(distance2, 1e-8))[..., None]
                reach = 1. if light["type"] == "area" else light["radius"]**2
                attenuation = 1/(1+distance2/reach)
                contribution = np.maximum(0, (np.sum(normal*direction, axis=-1)+wrap)/(1+wrap))*attenuation
                if light["type"] == "spot":
                    target = np.array([(light["target_x"]-.5)*aspect, light["target_y"]-.5, 0], np.float32)
                    axis = _normalize(target-origin)
                    cosine = np.sum(-direction*axis, axis=-1)
                    outer = math.cos(math.radians(light["beam_angle"]/2))
                    inner = math.cos(math.radians(light["beam_angle"]/2*(1-.8*light["softness"])))
                    if inner-outer < 1e-6:
                        cone = (cosine >= outer).astype(np.float32)
                    else:
                        a = np.clip((cosine-outer)/(inner-outer), 0, 1)
                        cone = a*a*(3-2*a)
                    contribution *= cone
                amount += contribution/len(offsets)
        gain += (amount*light["intensity"])[..., None]*tint
    return gain


def render_region(rgb_patch, analysis, value=None, bounds=None, image_size=None, *, linear_input=False):
    """Render the same native-pixel math for preview tiles and final export.

    With linear_input=True, input and output are float32/float64 linear RGB;
    values above 1 are retained for the caller's final tone/encoding stage.
    Default input/output is RGB8 sRGB. Both groups start from the same source,
    so independently adjusting subject/background cannot compound two renders.
    """
    if not isinstance(rgb_patch, np.ndarray) or rgb_patch.ndim != 3 or rgb_patch.shape[2] != 3:
        raise ValueError("布光输入须为 RGB 数组。")
    if type(linear_input) is not bool:
        raise ValueError("linear_input 必须为布尔值。")
    if linear_input:
        if rgb_patch.dtype not in (np.float32, np.float64) or not np.isfinite(rgb_patch).all() or np.any(rgb_patch < 0):
            raise ValueError("线性布光输入须为有限非负浮点 RGB。")
    elif rgb_patch.dtype != np.uint8:
        raise ValueError("默认布光输入须为 uint8 sRGB；浮点线性输入请设置 linear_input=True。")
    size = _size(image_size or (rgb_patch.shape[1], rgb_patch.shape[0]))
    bounds = _bounds(bounds or (0, 0, *size), size)
    x0, y0, x1, y1 = bounds
    if rgb_patch.shape[:2] != (y1-y0, x1-x0):
        raise ValueError("布光图块尺寸与 bounds 不一致。")
    state = validate_lighting(value)
    if not lighting_active(state):
        return rgb_patch.copy()
    fields = prepare_fields(analysis)
    width, height = size
    xs = np.arange(x0, x1, dtype=np.float32)
    ys = np.arange(y0, y1, dtype=np.float32)
    mask = np.clip(_sample(fields.subject_mask, xs, ys, size), 0, 1)
    # A group with zero coverage contributes exactly zero. Check the sampled
    # mask, not a downsampled bounding box: every soft-edge value must still run
    # the original lighting math. Avoid normal/depth sampling for identity tiles.
    if ((not _group_active(state['background']) and not np.any(mask)) or
            (not _group_active(state['subject']) and np.all(mask==1))):
        return rgb_patch.copy()
    normal = _normalize(_sample(fields.normal, xs, ys, size))
    depth = _sample(fields.depth, xs, ys, size)
    position = np.empty((*depth.shape, 3), np.float32)
    position[..., 0] = (((xs+.5)/width-.5)*(width/height))[None, :]
    position[..., 1] = ((ys+.5)/height-.5)[:, None]
    position[..., 2] = depth*.25
    subject = _gain(normal, position, state["subject"], width/height)
    background = _gain(normal, position, state["background"], width/height)
    gain = 1+((subject-1)*mask[..., None]+(background-1)*(1-mask[..., None]))*state["strength"]
    if linear_input:
        source = rgb_patch.astype(np.float32, copy=False)
        result = np.maximum(source*gain, 0).astype(rgb_patch.dtype)
    else:
        encoded = rgb_patch.astype(np.float32)/255
        source = np.where(encoded <= .04045, encoded/12.92, ((encoded+.055)/1.055)**2.4)
        lit = np.maximum(source*gain, 0)
        encoded = np.where(lit <= .0031308, lit*12.92, 1.055*lit**(1/2.4)-.055)
        result = np.floor(np.clip(encoded, 0, 1)*255+.5).astype(np.uint8)
    # Exact identity where the requested operations have no net influence.
    unchanged = np.all(gain == 1, axis=-1)
    result[unchanged] = rgb_patch[unchanged]
    return result


def render_lighting(rgb, analysis, value=None, *, linear_input=False):
    if not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("布光输入须为 RGB 数组。")
    height, width = rgb.shape[:2]
    state = validate_lighting(value)
    if not lighting_active(state):
        return render_region(rgb, analysis, state, linear_input=linear_input)
    fields = prepare_fields(analysis)
    result = np.empty_like(rgb)
    for top in range(0, height, TILE):
        bottom = min(top+TILE, height)
        for left in range(0, width, TILE):
            right = min(left+TILE, width)
            result[top:bottom, left:right] = render_region(
                rgb[top:bottom, left:right], fields, state, (left, top, right, bottom), (width, height),
                linear_input=linear_input)
    return result


def subject_mask_from_analysis(analysis):
    """Produce a person mask from real semantic probabilities; never face-only fallback."""
    if not isinstance(analysis, dict):
        raise ValueError("缺少语义分析。")
    parts = analysis.get("parts", {})
    if "background" not in parts:
        raise ValueError("语义模型没有返回背景概率；无法独立区分主体与背景。")
    background = np.asarray(parts["background"], np.float32)
    if background.ndim != 2 or min(background.shape) < 2 or not np.isfinite(background).all():
        raise ValueError("背景概率图无效。")
    if np.any((background < 0) | (background > 1)):
        raise ValueError("背景概率须在 0 到 1 之间。")
    person = np.clip(((1-background)-.1)/.8, 0, 1)
    return (person*person*(3-2*person)).astype(np.float32)


def infer_geometry(rgb, models, device="auto", edge=1024):
    """Run the pinned real Small depth network; no silent inference fallback."""
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("深度分析需要 uint8 RGB。")
    if min(rgb.shape[:2]) < 2 or rgb.shape[0]*rgb.shape[1] > 80_000_000:
        raise ValueError("深度分析输入须至少 2×2，最多 8000 万像素。")
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("device 须为 auto、cpu 或 cuda。")
    if type(edge) is not int or not 128 <= edge <= 1536:
        raise ValueError("分析长边须在 128 到 1536 之间。")
    from portraitlab.lighting_geometry import manifest, model_status
    spec, ready = manifest(), model_status(Path(models))
    if not ready["ready"]:
        raise ValueError("Depth Anything V2 Small 本地模型缺失或校验失败，未使用替代深度。")
    import torch
    import torch.nn.functional as functional
    from portraitlab.vendor.depth_anything_v2.dpt import DepthAnythingV2
    started = perf_counter()
    available = torch.cuda.is_available()
    if device == "cuda" and not available:
        raise RuntimeError("所选 NVIDIA GPU 不可用；请选择 CPU 后重新分析。")
    actual = "cuda" if device in ("auto", "cuda") and available else "cpu"
    torch.set_num_threads(min(8, max(1, os.cpu_count() or 1)))
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    height, width = rgb.shape[:2]
    factor = min(1., edge/max(width, height))
    aw, ah = max(2, round(width*factor)), max(2, round(height*factor))
    small = cv2.resize(rgb, (aw, ah), interpolation=cv2.INTER_AREA) if (aw, ah) != (width, height) else rgb
    scale = min(518/min(ah, aw), 1036/max(ah, aw))
    ih, iw = max(14, round(ah*scale/14)*14), max(14, round(aw*scale/14)*14)
    resized = cv2.resize(small.astype(np.float32)/255, (iw, ih), interpolation=cv2.INTER_CUBIC)
    normalized = (resized-np.array([.485, .456, .406], np.float32))/np.array([.229, .224, .225], np.float32)
    weights = torch.load(Path(models)/spec["files"][0]["relative_path"], map_location="cpu", weights_only=True)
    model = DepthAnythingV2(encoder="vits", features=64, out_channels=[48, 96, 192, 384])
    model.load_state_dict(weights, strict=True)
    del weights
    model = model.to(actual).eval()
    tensor = torch.from_numpy(np.ascontiguousarray(normalized.transpose(2, 0, 1))).unsqueeze(0).to(actual)
    if actual == "cuda":
        torch.cuda.synchronize()
    loaded = perf_counter()
    try:
        with torch.inference_mode():
            prediction = model(tensor)
            prediction = functional.interpolate(prediction[:, None], (ah, aw), mode="bilinear", align_corners=True)[0, 0]
            raw = prediction.float().cpu().numpy().copy()
        if actual == "cuda":
            torch.cuda.synchronize()
    except (RuntimeError, torch.cuda.OutOfMemoryError) as error:
        raise RuntimeError(f"真实深度推理失败（{actual}）；未切换算法或设备：{error}") from error
    finally:
        del tensor, model
    inferred = perf_counter()
    if not np.isfinite(raw).all():
        raise ValueError("深度模型返回了无效数值。")
    low, high = np.percentile(raw, [1, 99])
    if high-low <= 1e-8:
        raise ValueError("深度模型未解析出有效起伏；未用平面伪造结果。")
    depth = np.clip((raw-low)/(high-low), 0, 1).astype(np.float32)
    depth = cv2.GaussianBlur(depth, (0, 0), max(.8, max(aw, ah)/512*1.2))
    # Respect the original aspect ratio even when analysis-size rounding differs.
    dzdy, dzdx = np.gradient(depth*.25, 1/ah, (width/height)/aw)
    normal = _normalize(np.stack((-dzdx, -dzdy, np.ones_like(depth)), axis=-1)).astype(np.float32)
    metadata = {"algorithm": ALGORITHM, "geometry_version": GEOMETRY_VERSION,
                "model": spec["name"], "model_revision": spec["revision"],
                "code_revision": spec["code_revision"], "model_sha256": spec["files"][0]["sha256"],
                "code_license": spec["code_license"], "weights_license": spec["weights_license"],
                "device_preference": device, "actual_device": actual,
                "gpu_name": torch.cuda.get_device_name(0) if actual == "cuda" else None,
                "torch": torch.__version__, "source_size": [width, height],
                "analysis_size": [aw, ah], "inference_size": [iw, ih],
                "depth_encoding": "float32 relative inverse depth 0..1, near=1; robust 1..99 percentile",
                "normal_encoding": "float32 normalized xyz; x right, y down, z toward viewer",
                "depth_is_metric": False, "fallback_used": False,
                "renderer_device": "cpu", "renderer_deterministic": True,
                "timings_seconds": {"loading": loaded-started, "inference": inferred-loaded,
                                    "total": perf_counter()-started},
                "limitations": LIMITATIONS}
    return {"normal": normal, "depth": depth}, metadata


def _load_rgb(path):
    path = Path(path)
    if path.suffix.lower() == ".npy":
        return np.load(path, allow_pickle=False)
    from PIL import Image, ImageCms, ImageOps
    with Image.open(path) as image:
        icc = image.info.get("icc_profile")
        image = ImageOps.exif_transpose(image)
        if icc:
            image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                              ImageCms.createProfile("sRGB"), outputMode="RGB")
        return np.asarray(image.convert("RGB")).copy()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_request(request):
    """Explicit request; cache_root/worker-cache environment use short paths."""
    if (not isinstance(request, dict) or {"input", "models", "output"}-set(request)
            or set(request)-{"input", "models", "output", "subject_mask", "device", "edge", "cache_root"}):
        raise ValueError("深度请求须有 input、models、output，可选 subject_mask、device、edge、cache_root。")
    output = Path(request["output"]).resolve()
    boundary = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if boundary and not output.is_relative_to(Path(boundary).resolve()):
        raise ValueError("深度输出超出已配置的工作目录。")
    if output.exists() and any(output.iterdir()):
        raise ValueError("深度输出目录须为空或尚未建立。")
    for key in ("input", "models", "subject_mask"):
        if request.get(key) and Path(request[key]).resolve().is_relative_to(output):
            raise ValueError("输入和模型不能位于输出目录中。")
    output.mkdir(parents=True, exist_ok=True)
    if __package__:
        from .neural_candidate import configure_worker_cache
    else:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from neural_candidate import configure_worker_cache
    cache = configure_worker_cache(output, request.get("cache_root"))
    rgb = _load_rgb(request["input"])
    arrays, metadata = infer_geometry(rgb, request["models"], request.get("device", "auto"), request.get("edge", 1024))
    metadata["cache_root"] = str(cache)
    if request.get("subject_mask"):
        mask = np.load(request["subject_mask"], allow_pickle=False)
        arrays["subject_mask"] = prepare_fields({**arrays, "subject_mask": mask}).subject_mask
        metadata["subject_mask_sha256"] = _sha256(request["subject_mask"])
    else:
        metadata["subject_mask_required_before_rendering"] = True
    metadata["source_sha256"] = _sha256(request["input"])
    archive = output/"geometry.npz"
    temporary = output/"geometry.npz.part"
    try:
        with temporary.open("wb") as stream:
            np.savez_compressed(stream, **arrays)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    metadata["geometry_sha256"] = _sha256(archive)
    temporary = output/"geometry.json.part"
    try:
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
        os.replace(temporary, output/"geometry.json")
    finally:
        temporary.unlink(missing_ok=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    # Embedded AI Python can reuse the matching core runtime's OpenCV/Pillow.
    sibling = Path(sys.executable).resolve().parent.parent/"runtime"/"Lib"/"site-packages"
    if sibling.is_dir() and str(sibling) not in sys.path:
        sys.path.append(str(sibling))
    try:
        request = json.loads(args.request.read_text(encoding="utf-8-sig"))
        with redirect_stdout(sys.stderr):
            metadata = run_request(request)
        print(json.dumps({"ok": True, "output": str(Path(request["output"]).resolve()),
                          "metadata": metadata}, ensure_ascii=False, allow_nan=False), flush=True)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error), "error_type": type(error).__name__},
                         ensure_ascii=False), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

