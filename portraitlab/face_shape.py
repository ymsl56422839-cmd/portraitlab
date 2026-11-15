"""Reversible compact-support portrait geometry, without generation or models.

SPDX-License-Identifier: AGPL-3.0-only
Coordinates address pixel centres, normalized by (width - 1, height - 1).
All edits compose as inverse coordinate maps; source pixels are sampled once.
The same operations may warp masks/depth and drive editor coordinate conversion.
Importing or using this module never downloads or writes files.
"""
from __future__ import annotations

import math
from numbers import Integral, Real

import cv2
import numpy as np

ALGORITHM = "portraitlab-compact-face-shape-v1"
PARAMETERS = ("width_left", "width_right", "cheek_left", "cheek_right",
              "jaw_left", "jaw_right", "chin_length", "chin_width")
DEFAULT_FACE_SHAPE = {"faces": [], "strokes": []}
MAX_FACES, MAX_STROKES, MAX_OPERATIONS = 16, 128, 320
MIN_RADIUS, MAX_RADIUS = .005, .25
TILE = 256
_OP_FIELDS = {"kind", "cx", "cy", "rx", "ry", "dx", "dy", "strength"}
_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288,
         397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136,
         172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109)


def _number(value, low, high, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} 必须是有限数值。")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} 必须是有限数值。") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} 必须在 {low:g} 到 {high:g} 之间。")
    return number


def validate_face_shape(value=None):
    """Canonical independent JSON-compatible state. Positive widths widen."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"faces", "strokes"}:
        raise ValueError("面部塑形只接受 faces 和 strokes。")
    faces, strokes = value.get("faces", []), value.get("strokes", [])
    if not isinstance(faces, list) or len(faces) > MAX_FACES:
        raise ValueError(f"面部塑形最多保留 {MAX_FACES} 张人脸。")
    if not isinstance(strokes, list) or len(strokes) > MAX_STROKES:
        raise ValueError(f"手动塑形最多保留 {MAX_STROKES} 笔。")
    result = {"faces": [], "strokes": []}
    seen = set()
    for item in faces:
        if not isinstance(item, dict) or set(item) - {"face_index", *PARAMETERS}:
            raise ValueError("人脸塑形参数包含无效字段。")
        index = item.get("face_index")
        if isinstance(index, (bool, np.bool_)) or not isinstance(index, Integral) or not 0 <= index < 64:
            raise ValueError("face_index 必须是 0 到 63 的整数。")
        index = int(index)
        if index in seen:
            raise ValueError("同一张人脸不能有重复的塑形参数。")
        seen.add(index)
        result["faces"].append({"face_index": index, **{key: _number(item.get(key, 0), -1, 1, key) for key in PARAMETERS}})
    for item in strokes:
        fields = {"mode", "x", "y", "dx", "dy", "radius", "strength"}
        if not isinstance(item, dict) or set(item) - fields or {"mode", "x", "y", "radius"} - set(item):
            raise ValueError("塑形笔刷必须指定 mode、x、y、radius。")
        mode = item["mode"]
        if not isinstance(mode, str) or mode not in {"push", "restore"}:
            raise ValueError("塑形笔刷仅支持推移 push 或恢复 restore。")
        clean = {"mode": mode,
                 "x": _number(item["x"], 0, 1, "x"), "y": _number(item["y"], 0, 1, "y"),
                 "dx": _number(item.get("dx", 0), -1, 1, "dx"), "dy": _number(item.get("dy", 0), -1, 1, "dy"),
                 "radius": _number(item["radius"], MIN_RADIUS, MAX_RADIUS, "radius"),
                 "strength": _number(item.get("strength", 1), 0, 1, "strength")}
        if mode == "restore":
            clean["dx"] = clean["dy"] = 0.0
        result["strokes"].append(clean)
    return result


def has_shape(value=None):
    state = validate_face_shape(value)
    return any(any(face[p] != 0 for p in PARAMETERS) for face in state["faces"]) or any(
        s["strength"] and (s["mode"] == "restore" or s["dx"] or s["dy"]) for s in state["strokes"])


def _operations(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_OPERATIONS:
        raise ValueError("塑形操作列表无效或过长。")
    clean = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != _OP_FIELDS or raw.get("kind") not in ("push", "restore"):
            raise ValueError("塑形坐标操作字段无效。")
        op = {"kind": raw["kind"]}
        for key in ("cx", "cy", "dx", "dy"):
            op[key] = _number(raw[key], -4, 4, key)
        for key in ("rx", "ry"):
            op[key] = _number(raw[key], 1e-8, 4, key)
        op["strength"] = _number(raw["strength"], 0, 1, "strength")
        # This also guards coordinate APIs against externally fabricated foldovers.
        if op["kind"] == "push" and math.hypot(op["dx"] / op["rx"], op["dy"] / op["ry"]) > .20000001:
            raise ValueError("单笔塑形位移过大，请分次轻推。")
        clean.append(op)
    return clean


def _inverse(points, operations):
    shape = points.shape
    q = np.asarray(points, dtype=np.float64).reshape(-1, 2).copy()
    weight = np.ones(len(q), np.float64)
    result = np.zeros_like(q)
    for op in reversed(operations):
        if not op["strength"]:
            continue
        chosen = ((np.abs(q[:, 0] - op["cx"]) < op["rx"])
                  & (np.abs(q[:, 1] - op["cy"]) < op["ry"]) & (weight != 0))
        ids = np.flatnonzero(chosen)
        if not len(ids):
            continue
        r2 = ((q[ids, 0] - op["cx"]) / op["rx"]) ** 2 + ((q[ids, 1] - op["cy"]) / op["ry"]) ** 2
        ids, r2 = ids[r2 < 1], r2[r2 < 1]
        if not len(ids):
            continue
        alpha = (1 - r2) ** 3 * op["strength"]
        if op["kind"] == "restore":
            result[ids] += (weight[ids] * alpha)[:, None] * q[ids]
            weight[ids] *= 1 - alpha
        else:
            q[ids, 0] -= op["dx"] * alpha
            q[ids, 1] -= op["dy"] * alpha
    return (result + weight[:, None] * q).reshape(shape)


def map_points(points, operations, inverse=True):
    """Map (...,2) normalized points; inverse means display -> source.

    Does not clip points at image borders: callers clamp user input explicitly;
    image sampling uses border replication. This preserves reversible geometry.
    """
    values = np.asarray(points, dtype=np.float64)
    if values.ndim < 1 or values.shape[-1] != 2 or not np.isfinite(values).all():
        raise ValueError("塑形坐标必须是有限的 (...,2) 数组。")
    ops = _operations(operations)
    return _inverse(values, ops) if inverse else _forward(values, ops)


def _forward(points, operations):
    source = np.asarray(points, dtype=np.float64)
    view = source.copy()
    # Fast path is also the documented browser implementation.
    for _ in range(40):
        error = source - _inverse(view, operations)
        if not error.size or np.max(np.abs(error)) <= 1e-9:
            return view
        view += error
    # Strong accumulated edits can lose fixed-point contraction even while each
    # push is nonsingular. A bounded Newton fallback avoids dropping overlays.
    h = 1e-5
    for _ in range(20):
        mapped = _inverse(view, operations)
        error = source - mapped
        if not error.size or np.max(np.abs(error)) <= 1e-8:
            return view
        jx = (_inverse(view + [h, 0], operations) - mapped) / h
        jy = (_inverse(view + [0, h], operations) - mapped) / h
        determinant = jx[..., 0] * jy[..., 1] - jx[..., 1] * jy[..., 0]
        safe = np.abs(determinant) > 1e-8
        denominator = np.where(safe, determinant, 1)
        dx = (error[..., 0] * jy[..., 1] - error[..., 1] * jy[..., 0]) / denominator
        dy = (jx[..., 0] * error[..., 1] - jx[..., 1] * error[..., 0]) / denominator
        step = np.stack((dx, dy), axis=-1)
        step = np.where(safe[..., None], step, error)
        step = np.clip(step, -.1, .1)
        # Accept improvement per point, falling back to a small residual step.
        oldnorm = np.sum(error * error, axis=-1)
        proposal = view + step
        for _ in range(8):
            good = np.sum((source - _inverse(proposal, operations)) ** 2, axis=-1) < oldnorm
            if np.all(good):
                break
            step = np.where(good[..., None], step, step * .5)
            proposal = view + step
        view = proposal
    return view


def forward_points(points, operations):
    return map_points(points, operations, inverse=False)


def _descriptors(analysis, width, height):
    descriptors, frames = [], {}
    raw_faces = (analysis or {}).get("faces", [])
    dimensions = np.array([width - 1, height - 1], np.float64)
    for index, raw in enumerate(raw_faces):
        invalid = {"face_index": index, "bounds": None, "anchors": {}, "valid": False}
        try:
            p = np.asarray(raw, dtype=np.float64)
            if p.ndim != 2 or p.shape[0] < 455 or p.shape[1] < 2 or not np.isfinite(p[:, :2]).all():
                raise ValueError()
            p = np.clip(p[:, :2], 0, 1)
            px = p * dimensions
            outline = px[list(_OVAL)]
            low, high = outline.min(axis=0), outline.max(axis=0)
            fw, fh = high - low
            if fw < 8 or fh < 8 or not .15 <= fw / fh <= 2.5:
                raise ValueError()
            v = px[152] - px[10]
            length = float(np.linalg.norm(v))
            if length < 8:
                raise ValueError()
            v /= length
            u = np.array([v[1], -v[0]])
            if u[0] < 0:
                u *= -1
            def pair(a, b):
                candidates = [px[a], px[b]]
                return sorted(candidates, key=lambda q: float(q @ u))
            left, right = pair(234, 454)
            cheek_left, cheek_right = pair(123, 352)
            jaw_left, jaw_right = pair(172, 397)
            anchors_px = {"forehead": px[10], "chin": px[152], "left": left, "right": right,
                          "cheek_left": cheek_left, "cheek_right": cheek_right,
                          "jaw_left": jaw_left, "jaw_right": jaw_right}
            anchors = {name: (point / dimensions).tolist() for name, point in anchors_px.items()}
            descriptor = {"face_index": index, "bounds": [*(low / dimensions).tolist(), *(high / dimensions).tolist()],
                          "anchors": anchors, "valid": True}
            frames[index] = {"fw": float(fw), "fh": float(fh), "u": u, "v": v, "anchors": anchors_px}
            descriptors.append(descriptor)
        except (ValueError, TypeError, IndexError, OverflowError):
            invalid["reason"] = "关键点不完整、范围过小或几何无效，不能使用参数塑形。"
            descriptors.append(invalid)
    return descriptors, frames


def build_operations(value, analysis, width, height):
    """Return (operations, metadata), without creating any image-sized arrays."""
    if any(isinstance(n, bool) or not isinstance(n, Integral) or n < 2 for n in (width, height)):
        raise ValueError("塑形图像宽高须为至少 2 的整数。")
    state = validate_face_shape(value)
    descriptors, frames = _descriptors(analysis, width, height)
    ops, warnings = [], []
    dimensions = np.array([width - 1, height - 1], np.float64)
    meta = {"algorithm": ALGORITHM, "operations": ops, "faces": descriptors, "warnings": warnings,
            "coordinates": "pixel centres normalized by width-1,height-1",
            "parameter_direction": "positive widens/lengthens; negative narrows/shortens",
            "automatic_face_selection": False, "stroke_clamps": [], "applied_faces": []}
    def append(kind, source_centre, radii, delta, strength):
        centre = _forward(np.asarray(source_centre, np.float64) / dimensions, ops)
        radius = np.maximum(np.asarray(radii, np.float64), 1.) / dimensions
        movement = np.asarray(delta, np.float64) / dimensions
        ratio = float(np.linalg.norm(movement / radius))
        clipped = ratio > .2
        if clipped:
            movement *= .2 / ratio
        ops.append({"kind": kind, "cx": float(centre[0]), "cy": float(centre[1]),
                    "rx": float(radius[0]), "ry": float(radius[1]),
                    "dx": float(movement[0]), "dy": float(movement[1]), "strength": float(strength)})
        return clipped
    for face in state["faces"]:
        if not any(face[p] for p in PARAMETERS):
            continue
        index = face["face_index"]
        if index not in frames:
            warnings.append(f"第 {index + 1} 张人脸关键点不可用，已跳过该脸的参数塑形。")
            continue
        frame = frames[index]
        fw, fh, u, v, anchors = (frame[k] for k in ("fw", "fh", "u", "v", "anchors"))
        for side, sign in (("left", -1), ("right", 1)):
            for control, anchor, rx, ry, amount in (
                ("width", side, .40, .42, .035),
                ("cheek", "cheek_" + side, .28, .22, .025),
                ("jaw", "jaw_" + side, .30, .25, .025)):
                value = face[control + "_" + side]
                if value:
                    append("push", anchors[anchor], (rx * fw, ry * fh), u * (sign * fw * amount * value), 1.)
        if face["chin_width"]:
            for sign in (-1, 1):
                centre = anchors["chin"] + sign * u * .13 * fw - v * .06 * fh
                append("push", centre, (.20 * fw, .18 * fh), u * (sign * fw * .02 * face["chin_width"]), 1.)
        if face["chin_length"]:
            append("push", anchors["chin"], (.30 * fw, .20 * fh), v * (fh * .03 * face["chin_length"]), 1.)
        meta["applied_faces"].append(index)
    for index, stroke in enumerate(state["strokes"]):
        if not stroke["strength"] or (stroke["mode"] == "push" and not (stroke["dx"] or stroke["dy"])):
            continue
        r = stroke["radius"] * min(width, height)
        clipped = append(stroke["mode"], np.array([stroke["x"], stroke["y"]]) * dimensions,
                         (r, r), np.array([stroke["dx"], stroke["dy"]]) * dimensions, stroke["strength"])
        if clipped:
            meta["stroke_clamps"].append(index)
    if meta["stroke_clamps"]:
        warnings.append("部分推移距离超过单笔上限，已限制位移；需要更大调整时请分次轻推。")
    _operations(ops)
    return ops, meta


def warp_image(array, operations):
    """Warp RGB, scalar depth, or mask once; return an independent same-dtype array.

    Native sampling is tiled and reads only each required source crop, avoiding
    full-image float coordinate fields and OpenCV's 32767px remap dimension limit.
    Mask/depth and encoded normals use the identical spatial map. Consumers must
    renormalize normal vectors after interpolation.
    """
    if (not isinstance(array, np.ndarray) or array.ndim not in (2, 3)
            or min(array.shape[:2]) < 2 or (array.ndim == 3 and not 1 <= array.shape[2] <= 4)
            or array.dtype not in (np.uint8, np.uint16, np.int16, np.float32, np.float64, np.bool_)):
        raise ValueError("塑形输入必须是非空图像、深度或蒙版数组。")
    ops = _operations(operations)
    output = array.copy()
    if not ops:
        return output
    h, w = array.shape[:2]
    low = np.min([[op["cx"] - op["rx"], op["cy"] - op["ry"]] for op in ops], axis=0)
    high = np.max([[op["cx"] + op["rx"], op["cy"] + op["ry"]] for op in ops], axis=0)
    x0 = max(0, int(math.floor(low[0] * (w - 1))))
    y0 = max(0, int(math.floor(low[1] * (h - 1))))
    x1 = min(w, int(math.ceil(high[0] * (w - 1))) + 1)
    y1 = min(h, int(math.ceil(high[1] * (h - 1))) + 1)
    dimensions = np.array([w - 1, h - 1], np.float64)
    for ty in range(y0, y1, TILE):
        ey = min(y1, ty + TILE)
        for tx in range(x0, x1, TILE):
            ex = min(x1, tx + TILE)
            xx, yy = np.meshgrid(np.arange(tx, ex, dtype=np.float64), np.arange(ty, ey, dtype=np.float64))
            target = np.stack((xx, yy), axis=-1) / dimensions
            source = _inverse(target, ops)
            active = np.any(np.abs(source - target) > 1e-12, axis=-1)
            if not active.any():
                continue
            mapped = source * dimensions
            # Border replication is explicit here, so scalar and RGB paths agree.
            mx = np.clip(mapped[..., 0], 0, w - 1)
            my = np.clip(mapped[..., 1], 0, h - 1)
            sx = max(0, int(np.floor(mx[active].min())) - 1)
            sy = max(0, int(np.floor(my[active].min())) - 1)
            se = min(w, int(np.ceil(mx[active].max())) + 2)
            sb = min(h, int(np.ceil(my[active].max())) + 2)
            patch = array[sy:sb, sx:se]
            if max(patch.shape[:2]) >= 32767:
                raise ValueError("本次形变跨度过大，请缩小作用范围或分次处理。")
            sampled = cv2.remap(patch.astype(np.float32) if patch.dtype == np.bool_ else patch,
                                (mx - sx).astype(np.float32), (my - sy).astype(np.float32),
                                cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            if array.dtype == np.bool_:
                sampled = sampled >= .5
            if array.ndim == 3 and array.shape[2] == 1 and sampled.ndim == 2:
                sampled = sampled[..., None]
            output[ty:ey, tx:ex][active] = sampled[active]
    return output


def apply_face_shape(rgb_uint8, value=None, analysis=None):
    if (not isinstance(rgb_uint8, np.ndarray) or rgb_uint8.dtype != np.uint8
            or rgb_uint8.ndim != 3 or rgb_uint8.shape[2] != 3 or min(rgb_uint8.shape[:2]) < 2):
        raise ValueError("面部塑形输入必须是 uint8 RGB 图像。")
    h, w = rgb_uint8.shape[:2]
    ops, meta = build_operations(value, analysis, w, h)
    output = warp_image(rgb_uint8, ops)
    meta["changed_pixels"] = sum(int(np.count_nonzero(np.any(output[y:y + TILE] != rgb_uint8[y:y + TILE], axis=2)))
                                 for y in range(0, h, TILE))
    meta["active"] = bool(ops)
    meta["applied"] = bool(meta["changed_pixels"])
    meta["size"] = [w, h]
    return output, meta
