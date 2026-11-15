"""Reversible point/continuous donor healing; no model or filesystem activity.

Adapted from RapidRetouch Patch, Copyright Chris Cork, AGPL-3.0-only,
commit d6f9a848fa5ba5628239b568777c5be3e7851a8e. See
licenses/SPOT_REPAIR_NOTICE.txt for provenance and PortraitLab changes.
Inputs/outputs are sRGB uint8 RGB. Coordinates address pixel centres using
(width - 1, height - 1); radius is a fraction of the image's shorter dimension.
"""
from __future__ import annotations

import math
from numbers import Real

import cv2
import numpy as np

from .vendor.rapidretouch_engine.tools.colour import linear_to_srgb, srgb_to_linear
from .vendor.rapidretouch_engine.tools.filters import blur, masked_blur

MAX_REPAIRS = 100
MIN_RADIUS = .001
MAX_RADIUS = .25
TONE_EDGE = 512
TILE_EDGE = 256
ALGORITHM = "rapidretouch-donor-heal-circle-v1"
_FIELDS = ("x", "y", "source_x", "source_y", "radius", "strength")
BRUSH_ALGORITHM = "portraitlab-continuous-donor-heal-v2"
MAX_STROKE_POINTS = 1000
MAX_TOTAL_POINTS = 16384
_BRUSH_FIELDS = {"mode", "points", "hardness"}


def _number(value, index, key, low=0., high=1.):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"第 {index} 笔修补的 {key} 必须是有限数值。")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"第 {index} 笔修补的 {key} 必须是有限数值。") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"第 {index} 笔修补的 {key} 必须在 {low:g} 到 {high:g} 之间。")
    return value


def validate_repairs(value) -> list[dict]:
    """Canonical old points or continuous strokes, without changing old records.

    New records add mode/points/hardness. Sampled mode has an explicit source;
    auto searches a local donor; restore restores this repair layer's input.
    A path is one history operation, not a list of independently opaque stamps.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_REPAIRS:
        raise ValueError("点修必须是数组，最多保留 100 笔。")
    result, total = [], 0
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or set(raw) - (set(_FIELDS) | _BRUSH_FIELDS):
            raise ValueError(f"第 {index} 笔点修包含无效字段。")
        if _BRUSH_FIELDS.intersection(raw):
            mode = raw.get("mode", "sampled")
            if not isinstance(mode, str) or mode not in {"sampled", "auto", "restore"}:
                raise ValueError(f"第 {index} 笔修补模式须为 sampled、auto 或 restore。")
            if {"x", "y", "radius"} - set(raw):
                raise ValueError(f"第 {index} 笔修补必须指定起点和半径。")
            clean = {key: _number(raw[key], index, key, MIN_RADIUS if key == "radius" else 0,
                                  MAX_RADIUS if key == "radius" else 1) for key in ("x", "y", "radius")}
            clean.update(mode=mode, strength=_number(raw.get("strength", 1), index, "strength"),
                         hardness=_number(raw.get("hardness", .65), index, "hardness"))
            if mode == "sampled" and {"source_x", "source_y"} - set(raw):
                raise ValueError(f"第 {index} 笔取样修补必须指定样本位置。")
            # Extraneous coordinates are checked but never used by auto/restore.
            for key in ("source_x", "source_y"):
                if key in raw:
                    number = _number(raw[key], index, key)
                    if mode == "sampled":
                        clean[key] = number
            points = raw.get("points", [[clean["x"], clean["y"]]])
            if not isinstance(points, list) or not 1 <= len(points) <= MAX_STROKE_POINTS:
                raise ValueError(f"第 {index} 笔路径须有 1～{MAX_STROKE_POINTS} 个坐标。")
            path = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError(f"第 {index} 笔路径坐标须为 [x,y]。")
                path.append([_number(point[0], index, "points.x"), _number(point[1], index, "points.y")])
            if max(abs(path[0][0] - clean["x"]), abs(path[0][1] - clean["y"])) > 1e-8:
                raise ValueError(f"第 {index} 笔路径首点必须与 x、y 一致。")
            clean["points"] = path
            total += len(path)
            if total > MAX_TOTAL_POINTS:
                raise ValueError(f"修补路径总坐标不能超过 {MAX_TOTAL_POINTS} 个。")
            result.append(clean)
            continue
        if set(_FIELDS[:-1]) - set(raw):
            raise ValueError(f"第 {index} 笔点修必须指定目标、供体和半径。")
        clean = {}
        for key in _FIELDS:
            number = raw.get(key, 1.0)
            if isinstance(number, bool) or not isinstance(number, Real):
                raise ValueError(f"第 {index} 笔点修的 {key} 必须是有限数值。")
            try:
                number = float(number)
            except (OverflowError, ValueError):
                raise ValueError(f"第 {index} 笔点修的 {key} 必须是有限数值。") from None
            if not math.isfinite(number):
                raise ValueError(f"第 {index} 笔点修的 {key} 必须是有限数值。")
            low, high = (MIN_RADIUS, MAX_RADIUS) if key == "radius" else (0., 1.)
            if not low <= number <= high:
                raise ValueError(f"第 {index} 笔点修的 {key} 必须在 {low:g} 到 {high:g} 之间。")
            clean[key] = float(number)
        result.append(clean)
        total += 1
        if total > MAX_TOTAL_POINTS:
            raise ValueError(f"修补路径总坐标不能超过 {MAX_TOTAL_POINTS} 个。")
    return result


def _sample(image, xs, ys):
    """Bilinear sampling from a view, never converting/copying the full image."""
    h, w = image.shape[:2]
    x0 = max(0, min(w - 1, math.floor(float(xs.min())) - 1))
    y0 = max(0, min(h - 1, math.floor(float(ys.min())) - 1))
    x1 = max(x0 + 1, min(w, math.ceil(float(xs.max())) + 2))
    y1 = max(y0 + 1, min(h, math.ceil(float(ys.max())) + 2))
    return cv2.remap(image[y0:y1, x0:x1], (xs - x0).astype(np.float32),
                     (ys - y0).astype(np.float32), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def _tone_fields(output, original, bounds, centre, offset, radius):
    """Bounded tone estimation; native donor texture is sampled separately."""
    x0, y0, x1, y1 = bounds
    rw, rh = x1 - x0, y1 - y0
    scale = min(1., TONE_EDGE / max(rw, rh))
    tw, th = max(1, round(rw * scale)), max(1, round(rh * scale))
    target = cv2.resize(output[y0:y1, x0:x1], (tw, th), interpolation=cv2.INTER_AREA)
    gx, gy = np.meshgrid(x0 + (np.arange(tw) + .5) * rw / tw - .5,
                         y0 + (np.arange(th) + .5) * rh / th - .5)
    dx, dy = offset
    h, w = original.shape[:2]
    sx0, sy0 = max(0, math.floor(x0 + dx)), max(0, math.floor(y0 + dy))
    sx1, sy1 = min(w, math.ceil(x1 + dx)), min(h, math.ceil(y1 + dy))
    # The requested source centre is in the image, so this context intersects it.
    crop = original[sy0:sy1, sx0:sx1]
    sw, sh = max(1, round(crop.shape[1] * tw / rw)), max(1, round(crop.shape[0] * th / rh))
    small = cv2.resize(crop, (sw, sh), interpolation=cv2.INTER_AREA)
    mx = ((gx + dx - sx0 + .5) * sw / crop.shape[1] - .5).astype(np.float32)
    my = ((gy + dy - sy0 + .5) * sh / crop.shape[0] - .5).astype(np.float32)
    donor = cv2.remap(small, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    target = srgb_to_linear(target.astype(np.float32) / 255)
    donor = srgb_to_linear(donor.astype(np.float32) / 255)
    # Exclude footprints touching the defect when downsampling the context.
    guard = .5 * math.hypot(rw / tw - 1, rh / th - 1)
    context = (np.hypot(gx - centre[0], gy - centre[1]) >= radius + guard).astype(np.float32)
    if not context.any():
        return None
    sigma = max(1., .5 * radius * min(tw / rw, th / rh))
    return masked_blur(target, context, sigma), blur(donor, sigma)


def _path_distance(gx, gy, points, cutoff):
    """Exact distance to finite line segments on a regular grid.

    Each segment touches only its padded box, avoiding an H*W*points temporary.
    Pixels outside cutoff remain infinity, which is sufficient for support and
    ring decisions. Duplicate points and a one-point stroke are valid.
    """
    result = np.full(gx.shape, np.inf, np.float64)
    xs, ys = gx[0], gy[:, 0]
    pairs = zip(points[:-1], points[1:]) if len(points) > 1 else [(points[0], points[0])]
    for a, b in pairs:
        ax, ay = a; bx, by = b
        x0 = int(np.searchsorted(xs, min(ax, bx) - cutoff, side="left"))
        x1 = int(np.searchsorted(xs, max(ax, bx) + cutoff, side="right"))
        y0 = int(np.searchsorted(ys, min(ay, by) - cutoff, side="left"))
        y1 = int(np.searchsorted(ys, max(ay, by) + cutoff, side="right"))
        if x0 >= x1 or y0 >= y1:
            continue
        xx, yy = gx[y0:y1, x0:x1], gy[y0:y1, x0:x1]
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy
        t = np.clip(((xx - ax) * vx + (yy - ay) * vy) / length2, 0, 1) if length2 else 0.
        distance = np.hypot(xx - ax - vx * t, yy - ay - vy * t)
        block = result[y0:y1, x0:x1]
        np.minimum(block, distance, out=block)
    return result


def _path_bounds(points, radius, shape, pad=0.):
    h, w = shape[:2]
    return (max(0, math.floor(float(points[:, 0].min()) - radius - pad)),
            max(0, math.floor(float(points[:, 1].min()) - radius - pad)),
            min(w, math.ceil(float(points[:, 0].max()) + radius + pad) + 1),
            min(h, math.ceil(float(points[:, 1].max()) + radius + pad) + 1))


def _context_grid(bounds, edge):
    x0, y0, x1, y1 = bounds
    rw, rh = x1 - x0, y1 - y0
    scale = min(1., edge / max(rw, rh))
    tw, th = max(1, round(rw * scale)), max(1, round(rh * scale))
    gx, gy = np.meshgrid(x0 + (np.arange(tw) + .5) * rw / tw - .5,
                         y0 + (np.arange(th) + .5) * rh / th - .5)
    return gx, gy, tw, th


def _choose_source(original, output, points, radius):
    """Conservative deterministic local donor search, not semantic detection.

    The user explicitly supplies the repair footprint. Candidates must contain
    the entire translated path, avoid its target, and resemble the surrounding
    tone and fine texture. The score has a rejection threshold; an unsuccessful
    search never falls back to arbitrary copying or generative filling.
    """
    bounds = _path_bounds(points, radius, original.shape, 1.25 * radius + 2)
    gx, gy, tw, th = _context_grid(bounds, 192)
    distance = _path_distance(gx, gy, points, radius * 2.25 + 3)
    footprint = distance < radius
    ring = (distance > radius * 1.2) & (distance < radius * 2.1)
    if footprint.sum() < 3 or ring.sum() < 12:
        return None, {"reason": "insufficient_context"}
    x0, y0, x1, y1 = bounds
    target = cv2.resize(output[y0:y1, x0:x1], (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    target_lab = cv2.cvtColor(target, cv2.COLOR_RGB2Lab)
    reference = np.median(target_lab[ring], axis=0)
    target_luma = target @ np.array([.2126, .7152, .0722], np.float32)
    fine_sigma = max(.65, radius * tw / (x1 - x0) * .18)
    target_fine = target_luma - cv2.GaussianBlur(target_luma, (0, 0), fine_sigma)
    reference_texture = float(np.percentile(np.abs(target_fine[ring]), 80))
    # A broad mixed-color/edge context is an unsafe skin donor target.
    if float(np.percentile(np.linalg.norm(target_lab[ring] - reference, axis=1), 85)) > 28:
        return None, {"reason": "heterogeneous_context"}
    h, w = original.shape[:2]
    best = None
    # Vertical/diagonal candidates also handle a horizontal painted blemish.
    for extent in (2.6, 4., 6.):
        for angle in np.arange(12, dtype=np.float64) * (math.pi / 6):
            dx, dy = float(math.cos(angle) * radius * extent), float(math.sin(angle) * radius * extent)
            translated = points + (dx, dy)
            if (translated[:, 0].min() - radius < 0 or translated[:, 1].min() - radius < 0
                    or translated[:, 0].max() + radius > w - 1 or translated[:, 1].max() + radius > h - 1):
                continue
            source_distance = _path_distance(gx + dx, gy + dy, points, radius * 1.15)
            if np.any(source_distance[footprint] < radius * 1.15):
                continue
            donor = _sample(original, gx + dx, gy + dy).astype(np.float32) / 255
            lab = cv2.cvtColor(donor, cv2.COLOR_RGB2Lab)
            middle = np.median(lab[footprint], axis=0)
            tone_distance = float(abs(middle[0] - reference[0]))
            chroma_distance = float(np.linalg.norm(middle[1:] - reference[1:]))
            luma = donor @ np.array([.2126, .7152, .0722], np.float32)
            fine = luma - cv2.GaussianBlur(luma, (0, 0), fine_sigma)
            texture = float(np.percentile(np.abs(fine[footprint]), 80))
            texture_ratio = (texture + .004) / (reference_texture + .004)
            patch_outliers = float(np.percentile(np.linalg.norm(lab[footprint] - middle, axis=1), 97))
            if tone_distance > 13 or chroma_distance > 10 or not .25 < texture_ratio < 3.5 or patch_outliers > 23:
                continue
            score = tone_distance * .35 + chroma_distance + abs(math.log(texture_ratio)) * 3 + patch_outliers * .15 + extent * .05
            if best is None or score < best[0]:
                best = (score, dx, dy, tone_distance, chroma_distance)
    if best is None:
        return None, {"reason": "no_matching_local_patch"}
    score, dx, dy, tone, chroma = best
    return (dx, dy), {"score": score, "tone_difference_Lab_L": tone,
                      "chroma_difference_Lab": chroma, "reason": None}


def _brush_tones(output, original, bounds, points, offset, radius):
    gx, gy, tw, th = _context_grid(bounds, TONE_EDGE)
    x0, y0, x1, y1 = bounds
    target = cv2.resize(output[y0:y1, x0:x1], (tw, th), interpolation=cv2.INTER_AREA)
    dx, dy = offset
    h, w = original.shape[:2]
    sx0, sy0 = max(0, math.floor(x0 + dx)), max(0, math.floor(y0 + dy))
    sx1, sy1 = min(w, math.ceil(x1 + dx)), min(h, math.ceil(y1 + dy))
    crop = original[sy0:sy1, sx0:sx1]
    # Match the target's area filtering; directly point-sampling a long stroke
    # into a 512px tone grid would alias the donor texture into the tone field.
    sw = max(1, round(crop.shape[1] * tw / (x1 - x0)))
    sh = max(1, round(crop.shape[0] * th / (y1 - y0)))
    small = cv2.resize(crop, (sw, sh), interpolation=cv2.INTER_AREA)
    mx = ((gx + dx - sx0 + .5) * sw / crop.shape[1] - .5).astype(np.float32)
    my = ((gy + dy - sy0 + .5) * sh / crop.shape[0] - .5).astype(np.float32)
    donor = cv2.remap(small, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    target = srgb_to_linear(target.astype(np.float32) / 255)
    donor = srgb_to_linear(donor.astype(np.float32) / 255)
    guard = .5 * math.hypot((x1 - x0) / tw - 1, (y1 - y0) / th - 1)
    context = (_path_distance(gx, gy, points, radius + guard) >= radius + guard).astype(np.float32)
    if not context.any():
        return None
    sigma = max(1., .5 * radius * min(tw / (x1 - x0), th / (y1 - y0)))
    return masked_blur(target, context, sigma), blur(donor, sigma)


def _apply_brush(output, original, repair, index):
    h, w = original.shape[:2]
    points = np.asarray(repair["points"], dtype=np.float64) * (w - 1, h - 1)
    radius, strength = repair["radius"] * min(h, w), repair["strength"]
    mode = repair["mode"]
    info = {"index": index, "mode": mode, "changed_pixels": 0, "target_clipped": False,
            "source_clipped": False, "no_op_reason": None, "path_points": len(points)}
    warnings = []
    if not strength:
        info["no_op_reason"] = "zero_strength"
        return info, warnings
    if mode == "sampled":
        dx, dy = repair["source_x"] * (w - 1) - points[0, 0], repair["source_y"] * (h - 1) - points[0, 1]
        if dx == 0 and dy == 0:
            info["no_op_reason"] = "same_source_and_target"
            return info, warnings
    elif mode == "auto":
        offset, search = _choose_source(original, output, points, radius)
        info["source_search"] = search
        if offset is None:
            info["no_op_reason"] = "no_suitable_source"
            warnings.append(f"第 {index + 1} 笔未找到相近且完整的纹理样本，已保留原貌；可缩小范围或改为手动取样。")
            return info, warnings
        dx, dy = offset
    else:
        dx, dy = 0., 0.
    if mode != "restore":
        info["source_x"] = float((points[0, 0] + dx) / max(1, w - 1))
        info["source_y"] = float((points[0, 1] + dy) / max(1, h - 1))
    for key, locations, label in (("target_clipped", points, "目标"),
                                   ("source_clipped", points + (dx, dy), "供体")):
        clipped = bool(locations[:, 0].min() - radius < 0 or locations[:, 1].min() - radius < 0
                       or locations[:, 0].max() + radius > w - 1 or locations[:, 1].max() + radius > h - 1)
        info[key] = clipped
        if clipped and (key == "target_clipped" or mode != "restore"):
            warnings.append(f"第 {index + 1} 笔{label}触及图像边缘，仅处理有效范围。")
    bounds = _path_bounds(points, radius, original.shape, radius + 3)
    tones = None if mode == "restore" else _brush_tones(output, original, bounds, points, (dx, dy), radius)
    if mode != "restore" and tones is None:
        info["no_op_reason"] = "no_context"
        warnings.append(f"第 {index + 1} 笔周边像素不足，已保留原貌；请缩小修补范围。")
        return info, warnings
    tx0, ty0, tx1, ty1 = _path_bounds(points, radius, original.shape)
    valid_count = 0
    for y0 in range(ty0, ty1, TILE_EDGE):
        y1 = min(ty1, y0 + TILE_EDGE)
        for x0 in range(tx0, tx1, TILE_EDGE):
            x1 = min(tx1, x0 + TILE_EDGE)
            gx, gy = np.meshgrid(np.arange(x0, x1, dtype=np.float64), np.arange(y0, y1, dtype=np.float64))
            distance = _path_distance(gx, gy, points, radius) / radius
            valid = (distance < 1) & (gx + dx >= 0) & (gx + dx <= w - 1) & (gy + dy >= 0) & (gy + dy <= h - 1)
            if not valid.any():
                continue
            valid_count += int(valid.sum())
            hardness = repair["hardness"]
            alpha = valid.astype(np.float32) if hardness == 1 else (
                .5 + .5 * np.cos(np.pi * np.clip((distance - hardness) / (1 - hardness), 0, 1))) * valid
            alpha = (alpha * strength).astype(np.float32)[..., None]
            before = output[y0:y1, x0:x1]
            target = srgb_to_linear(before.astype(np.float32) / 255)
            if mode == "restore":
                healed = srgb_to_linear(original[y0:y1, x0:x1].astype(np.float32) / 255)
            else:
                target_tone, source_tone = tones
                mx = ((gx - bounds[0] + .5) * target_tone.shape[1] / (bounds[2] - bounds[0]) - .5).astype(np.float32)
                my = ((gy - bounds[1] + .5) * target_tone.shape[0] / (bounds[3] - bounds[1]) - .5).astype(np.float32)
                tone = cv2.remap(target_tone, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                source_base = cv2.remap(source_tone, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                donor = srgb_to_linear(_sample(original, gx + dx, gy + dy).astype(np.float32) / 255)
                healed = np.clip(tone * (donor + 1e-4) / (source_base + 1e-4), 0, 1)
            mixed = alpha * healed + (1 - alpha) * target
            encoded = np.rint(np.clip(linear_to_srgb(mixed), 0, 1) * 255).astype(np.uint8)
            info["changed_pixels"] += int(np.count_nonzero(np.any(encoded[valid] != before[valid], axis=1)))
            before[valid] = encoded[valid]
    if not valid_count:
        info["no_op_reason"] = "no_valid_pixels"
        warnings.append(f"第 {index + 1} 笔没有有效像素，已保留原貌。")
    elif not info["changed_pixels"]:
        info["no_op_reason"] = "unchanged_pixels"
    return info, warnings


def apply_repairs(rgb_uint8, repairs):
    """Replay explicit repairs and return (independent uint8 output, metadata).

    Targets compose in list order; EVERY donor samples the immutable input to
    this call. Source coordinates outside the image are never applied. A finite
    circular feather changes only pixels strictly inside each target radius.
    Tone fields are at most 512px on their long edge, native texture is processed
    in 256px tiles, and the input is copied just once regardless of repair count.
    """
    repairs = validate_repairs(repairs)
    if (not isinstance(rgb_uint8, np.ndarray) or rgb_uint8.dtype != np.uint8
            or rgb_uint8.ndim != 3 or rgb_uint8.shape[2] != 3
            or min(rgb_uint8.shape[:2]) < 1):
        raise ValueError("点修输入必须是非空 uint8 RGB 图像。")
    output = rgb_uint8.copy()
    h, w = output.shape[:2]
    metadata = {"algorithm": BRUSH_ALGORITHM if any("mode" in r for r in repairs) else ALGORITHM,
                "source": "immutable-repair-layer-input" if any("mode" in r for r in repairs) else "explicit-input-donor",
                "requested": len(repairs), "applied": 0, "changed_pixels": 0,
                "repairs": [], "warnings": []}
    for index, repair in enumerate(repairs):
        if "mode" in repair:
            info, warnings = _apply_brush(output, rgb_uint8, repair, index)
            metadata["repairs"].append(info)
            metadata["warnings"].extend(warnings)
            metadata["applied"] += int(info["changed_pixels"] > 0)
            continue
        info = {"index": index, "changed_pixels": 0, "target_clipped": False,
                "source_clipped": False, "no_op_reason": None}
        metadata["repairs"].append(info)
        cx, cy = repair["x"] * (w - 1), repair["y"] * (h - 1)
        sx, sy = repair["source_x"] * (w - 1), repair["source_y"] * (h - 1)
        radius, strength = repair["radius"] * min(h, w), repair["strength"]
        dx, dy = sx - cx, sy - cy
        if not strength or (dx == 0 and dy == 0):
            info["no_op_reason"] = "zero_strength" if not strength else "same_source_and_target"
            continue
        for key, x, y, label in (("target_clipped", cx, cy, "目标"), ("source_clipped", sx, sy, "供体")):
            if x - radius < 0 or y - radius < 0 or x + radius > w - 1 or y + radius > h - 1:
                info[key] = True
                metadata["warnings"].append(f"第 {index + 1} 笔{label}范围触及图片边缘，仅处理目标和供体都有效的像素。")
        pad = radius + 3
        bounds = (max(0, math.floor(cx - radius - pad)), max(0, math.floor(cy - radius - pad)),
                  min(w, math.ceil(cx + radius + pad) + 1), min(h, math.ceil(cy + radius + pad) + 1))
        tones = _tone_fields(output, rgb_uint8, bounds, (cx, cy), (dx, dy), radius)
        if tones is None:
            info["no_op_reason"] = "no_context"
            metadata["warnings"].append(f"第 {index + 1} 笔没有足够的周边像素估计肤色，已保留原图。")
            continue
        target_tone, source_tone = tones
        tx0, ty0 = max(0, math.floor(cx - radius)), max(0, math.floor(cy - radius))
        tx1, ty1 = min(w, math.ceil(cx + radius) + 1), min(h, math.ceil(cy + radius) + 1)
        valid_count = 0
        for y0 in range(ty0, ty1, TILE_EDGE):
            y1 = min(ty1, y0 + TILE_EDGE)
            for x0 in range(tx0, tx1, TILE_EDGE):
                x1 = min(tx1, x0 + TILE_EDGE)
                # Keep support decisions at float64 precision; remap coordinates
                # alone are float32, so rounding cannot expand the declared mask.
                gx, gy = np.meshgrid(np.arange(x0, x1, dtype=np.float64), np.arange(y0, y1, dtype=np.float64))
                distance = np.hypot(gx - cx, gy - cy) / radius
                valid = ((distance < 1) & (gx + dx >= 0) & (gx + dx <= w - 1)
                         & (gy + dy >= 0) & (gy + dy <= h - 1))
                if not valid.any():
                    continue
                valid_count += int(np.count_nonzero(valid))
                alpha = .5 + .5 * np.cos(np.pi * np.clip((distance - .65) / .35, 0, 1))
                alpha = (alpha * valid * strength).astype(np.float32)[..., None]
                mx = ((gx - bounds[0] + .5) * target_tone.shape[1] / (bounds[2] - bounds[0]) - .5).astype(np.float32)
                my = ((gy - bounds[1] + .5) * target_tone.shape[0] / (bounds[3] - bounds[1]) - .5).astype(np.float32)
                tone = cv2.remap(target_tone, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                source_base = cv2.remap(source_tone, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                donor = srgb_to_linear(_sample(rgb_uint8, gx + dx, gy + dy).astype(np.float32) / 255)
                # Stabilised upstream texture ratio also handles a black donor.
                healed = np.clip(tone * (donor + 1e-4) / (source_base + 1e-4), 0, 1)
                before = output[y0:y1, x0:x1]
                target = srgb_to_linear(before.astype(np.float32) / 255)
                mixed = alpha * healed + (1 - alpha) * target
                encoded = np.rint(np.clip(linear_to_srgb(mixed), 0, 1) * 255).astype(np.uint8)
                info["changed_pixels"] += int(np.count_nonzero(np.any(encoded[valid] != before[valid], axis=1)))
                before[valid] = encoded[valid]
        if not valid_count:
            info["no_op_reason"] = "no_valid_pixels"
            metadata["warnings"].append(f"第 {index + 1} 笔没有有效的目标与供体重叠像素，已保留原图。")
        elif not info["changed_pixels"]:
            info["no_op_reason"] = "unchanged_pixels"
        else:
            metadata["applied"] += 1
    # Count final changed pixels in strips, without a full-size temporary mask.
    for y in range(0, h, TILE_EDGE):
        metadata["changed_pixels"] += int(np.count_nonzero(np.any(output[y:y + TILE_EDGE] != rgb_uint8[y:y + TILE_EDGE], axis=2)))
    return output, metadata
