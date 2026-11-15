"""Local, non-generative geometry for the independent PortraitLab V2 editor.

SPDX-License-Identifier: AGPL-3.0-only
No model download, file write, or inference occurs in this module. Coordinates
are pixel centres normalized by (W-1,H-1); brush radii use the shorter image edge.
Manual strokes/protection use displayed-image coordinates at gesture time.
All regions sample one inverse map and one source image, so tiles and export
share the same calculation. The caller owns state history and atomic commits.
"""
from __future__ import annotations

from copy import deepcopy
import math
from numbers import Integral, Real

import numpy as np

from portraitlab import face_shape as legacy

ALGORITHM = "portraitlab-v2-compact-geometry-1"
PARAMETERS = (*legacy.PARAMETERS, "forehead_height", "forehead_width")
DEFAULT_GEOMETRY = {"enabled": True, "faces": [], "strokes": [],
                    "protection": [], "edge_protection": .01}
PARAMETER_LABELS = {
    "width_left": "左侧脸宽", "width_right": "右侧脸宽",
    "cheek_left": "左侧面颊", "cheek_right": "右侧面颊",
    "jaw_left": "左侧下颌", "jaw_right": "右侧下颌",
    "chin_length": "下巴长度", "chin_width": "下巴宽度",
    "forehead_height": "额头高度", "forehead_width": "额头宽度",
}
TILE = 256


def _number(value, low, high, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} 必须是有限数值。")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} 必须是有限数值。") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} 必须在 {low:g} 到 {high:g} 之间。")
    return number


def _dimensions(image_size):
    if (not isinstance(image_size, (tuple, list)) or len(image_size) != 2
            or any(isinstance(v, bool) or not isinstance(v, Integral) or v < 2 for v in image_size)):
        raise ValueError("image_size 须为至少 2×2 的 (width,height)。")
    return int(image_size[0]), int(image_size[1])


def _bounds(bounds, width, height):
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, Integral) for v in bounds)):
        raise ValueError("bounds 须为整数 [x0,y0,x1,y1]。")
    x0, y0, x1, y1 = map(int, bounds)
    if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
        raise ValueError("塑形图块超出图像范围。")
    return x0, y0, x1, y1


def validate_geometry(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_GEOMETRY):
        raise ValueError("塑形参数包含未知字段。")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("塑形开关必须为布尔值。")
    faces = value.get("faces", [])
    if not isinstance(faces, list) or len(faces) > legacy.MAX_FACES:
        raise ValueError("最多配置 16 张人脸。")
    reduced, extras = [], []
    for face in faces:
        if not isinstance(face, dict) or set(face) - {"face_index", *PARAMETERS}:
            raise ValueError("人脸塑形参数包含未知字段。")
        reduced.append({key: val for key, val in face.items() if key not in ("forehead_height", "forehead_width")})
        extras.append({key: _number(face.get(key, 0), -1, 1, key)
                       for key in ("forehead_height", "forehead_width")})
    clean = legacy.validate_face_shape({"faces": reduced, "strokes": value.get("strokes", [])})
    for face, extra in zip(clean["faces"], extras):
        face.update(extra)
    protection = value.get("protection", [])
    if not isinstance(protection, list) or len(protection) > 256:
        raise ValueError("最多保留 256 个局部保护圆。")
    guards = []
    for guard in protection:
        if not isinstance(guard, dict) or set(guard) - {"x", "y", "radius", "strength"}:
            raise ValueError("局部保护须指定 x、y、radius，可选 strength。")
        guards.append({
            "x": _number(guard.get("x"), 0, 1, "保护 x"),
            "y": _number(guard.get("y"), 0, 1, "保护 y"),
            "radius": _number(guard.get("radius"), .001, .5, "保护半径"),
            "strength": _number(guard.get("strength", 1), 0, 1, "保护强度"),
        })
    return {"enabled": enabled, **clean, "protection": guards,
            "edge_protection": _number(value.get("edge_protection", .01), 0, .1, "边缘保护")}


def geometry_active(value=None):
    state = validate_geometry(value)
    return bool(state["enabled"] and (
        any(any(face[key] for key in PARAMETERS) for face in state["faces"])
        or any(stroke["strength"] and (stroke["mode"] == "restore" or stroke["dx"] or stroke["dy"])
               for stroke in state["strokes"])))


def build_operations(value, analysis, width, height):
    """Return immutable-by-convention inverse-map operations and UI metadata.

    analysis['faces'] must contain actual normalized landmark arrays, not face
    bounding-box records. Nonzero face parameters with missing landmarks fail
    explicitly. Manual push/restore can be used without an analysis model.
    """
    width, height = _dimensions((width, height))
    state = validate_geometry(value)
    descriptors, frames = legacy._descriptors(analysis or {}, width, height)
    meta = {"algorithm": ALGORITHM, "faces": descriptors, "operations": [],
            "applied_faces": [], "warnings": [], "stroke_clamps": [],
            "active": False, "state": deepcopy(state), "size": [width, height],
            "coordinates": "pixel centres normalized by width-1,height-1; strokes/protection in display coordinates; radius/shorter-edge",
            "parameter_direction": "positive widens/lengthens; forehead_height positive moves upward",
            "preservation": "Compact support, image-edge taper and user protection circles; nearby hair/background inside the support may move.",
            "limitations": "Local 2D deformation, not a 3D face reconstruction. Strong accumulated edits require visual review."}
    if not state["enabled"]:
        return [], meta
    for face in state["faces"]:
        if any(face[key] for key in PARAMETERS) and face["face_index"] not in frames:
            raise ValueError(f"人物 {face['face_index']+1} 缺少有效五官关键点，无法执行参数塑形；可使用手动推拉。")
    old_faces = [{key: val for key, val in face.items() if key in {"face_index", *legacy.PARAMETERS}}
                 for face in state["faces"]]
    ops, old_meta = legacy.build_operations({"faces": old_faces, "strokes": []},
                                             analysis or {}, width, height)
    meta["warnings"].extend(old_meta["warnings"])
    dimensions = np.array([width-1, height-1], np.float64)

    def append(kind, source_center, radii, delta, strength, display_center=False):
        center = np.asarray(source_center, np.float64) / dimensions
        if not display_center:
            center = legacy._forward(center, ops)
        radius = np.maximum(np.asarray(radii, np.float64), 1.) / dimensions
        movement = np.asarray(delta, np.float64) / dimensions
        ratio = float(np.linalg.norm(movement / radius))
        clipped = ratio > .2
        if clipped:
            movement *= .2 / ratio
        ops.append({"kind": kind, "cx": float(center[0]), "cy": float(center[1]),
                    "rx": float(radius[0]), "ry": float(radius[1]),
                    "dx": float(movement[0]), "dy": float(movement[1]), "strength": float(strength)})
        return clipped

    for face in state["faces"]:
        if not any(face[key] for key in PARAMETERS):
            continue
        index = face["face_index"]
        frame = frames[index]
        fw, fh, u, v, anchors = (frame[key] for key in ("fw", "fh", "u", "v", "anchors"))
        if face["forehead_height"]:
            append("push", anchors["forehead"], (.36*fw, .23*fh),
                   -v * (.03*fh*face["forehead_height"]), 1.)
        if face["forehead_width"]:
            for sign in (-1, 1):
                center = anchors["forehead"] + sign*u*.26*fw + v*.07*fh
                append("push", center, (.27*fw, .24*fh),
                       u * (sign*.025*fw*face["forehead_width"]), 1.)
        meta["applied_faces"].append(index)
    for index, stroke in enumerate(state["strokes"]):
        if not stroke["strength"] or (stroke["mode"] == "push" and not (stroke["dx"] or stroke["dy"])):
            continue
        radius = stroke["radius"] * min(width, height)
        if append(stroke["mode"], np.array([stroke["x"], stroke["y"]])*dimensions,
                  (radius, radius), np.array([stroke["dx"], stroke["dy"]])*dimensions,
                  stroke["strength"], display_center=True):
            meta["stroke_clamps"].append(index)
    if meta["stroke_clamps"]:
        meta["warnings"].append("部分推移超过半径的 20%，已限制单笔位移；需要更大调整请分次轻推。")
    ops = legacy._operations(ops)
    meta.update(operations=deepcopy(ops), active=bool(ops))
    return ops, meta


def _smooth(value):
    value = np.clip(value, 0, 1)
    return value*value*(3-2*value)


def _inverse(points, operations, state, image_size):
    source = legacy._inverse(points, operations)
    if not operations:
        return source
    width, height = image_size
    px = points[..., 0]*(width-1)
    py = points[..., 1]*(height-1)
    weight = np.ones(points.shape[:-1], np.float64)
    edge = state["edge_protection"]*min(width, height)
    if edge > 0:
        distance = np.minimum(np.minimum(px, width-1-px), np.minimum(py, height-1-py))
        weight *= _smooth(distance/edge)
    for guard in state["protection"]:
        radius = guard["radius"]*min(width, height)
        distance = np.hypot(px-guard["x"]*(width-1), py-guard["y"]*(height-1))
        # Inner 60% is an exact protected core; the remaining 40% blends outward.
        protected = 1-_smooth((distance/radius-.6)/.4)
        weight *= 1-protected*guard["strength"]
    return points+(source-points)*weight[..., None]


def map_points(points, operations, *, image_size, value=None, inverse=True):
    """Map normalized coordinates. Forward is for overlays, not a second resample."""
    size = _dimensions(image_size)
    state, ops = validate_geometry(value), legacy._operations(operations)
    points = np.asarray(points, np.float64)
    if points.ndim < 1 or points.shape[-1] != 2 or not np.isfinite(points).all():
        raise ValueError("塑形坐标须为有限 (...,2) 数组。")
    if inverse:
        return _inverse(points, ops, state, size)
    view = points.copy()
    for _ in range(60):
        error = points-_inverse(view, ops, state, size)
        if not error.size or np.max(np.abs(error)) <= 1e-9:
            return view
        view += np.clip(error, -.025, .025)
    if np.max(np.abs(points-_inverse(view, ops, state, size))) > 1e-5:
        raise ValueError("累积形变过强，坐标反解未收敛；请撤销或减小塑形。")
    return view


def _read_source(source, bounds, image_size):
    x0, y0, x1, y1 = bounds
    block = source(bounds) if callable(source) else source[y0:y1, x0:x1]
    if (not isinstance(block, np.ndarray) or block.ndim not in (2, 3)
            or block.shape[:2] != (y1-y0, x1-x0)
            or (block.ndim == 3 and not 1 <= block.shape[2] <= 4)
            or block.dtype not in (np.uint8, np.uint16, np.int16, np.float32, np.float64, np.bool_)):
        raise ValueError("源图读取函数必须返回与请求范围一致的有效图像数组。")
    if np.issubdtype(block.dtype, np.floating) and not np.isfinite(block).all():
        raise ValueError("塑形源图包含非有限浮点值。")
    return block


def _bilinear(patch, mx, my, sx, sy):
    """Global fractional coordinates keep arbitrary tile boundaries identical.

    OpenCV's local float maps can straddle different 1/32-pixel table bins when
    crop origins differ. This sampler does not quantize the inverse coordinates.
    Inactive pixels may lie outside the crop; they are clamped and never written.
    """
    gx, gy = np.floor(mx), np.floor(my)
    fx, fy = mx-gx, my-gy
    x0 = np.clip(gx.astype(np.intp)-sx, 0, patch.shape[1]-1)
    y0 = np.clip(gy.astype(np.intp)-sy, 0, patch.shape[0]-1)
    x1 = np.clip(gx.astype(np.intp)+1-sx, 0, patch.shape[1]-1)
    y1 = np.clip(gy.astype(np.intp)+1-sy, 0, patch.shape[0]-1)
    if patch.ndim == 3:
        fx, fy = fx[..., None], fy[..., None]
    values = (patch[y0, x0]*(1-fx)*(1-fy)+patch[y0, x1]*fx*(1-fy)
              +patch[y1, x0]*(1-fx)*fy+patch[y1, x1]*fx*fy)
    if patch.dtype == np.bool_:
        return values >= .5
    if np.issubdtype(patch.dtype, np.integer):
        limits = np.iinfo(patch.dtype)
        values = np.clip(np.rint(values), limits.min, limits.max)
    return values.astype(patch.dtype)


def _support_rectangles(operations):
    """Conservative normalized support, including a floating-point margin.

    Reverse mapping visits operations in reverse order. A starting point
    outside every support rectangle remains unchanged at each visit, hence
    never enters another operation's support. This induction remains true for
    chained pushes and restores; edge/protection weights only reduce movement.
    The union is sufficient, although it need not be the minimal affected area.
    """
    rectangles=[]
    epsilon=16*np.finfo(np.float64).eps
    for op in operations:
        if not op['strength']:continue
        cx,cy,rx,ry=(op[key] for key in ('cx','cy','rx','ry'))
        # Expand rather than risk rejecting boundary pixels because the legacy
        # abs(q-centre)<radius comparison rounded differently from centre±radius.
        ex=epsilon*(1+abs(cx)+rx);ey=epsilon*(1+abs(cy)+ry)
        rectangles.append((cx-rx-ex,cy-ry-ey,cx+rx+ex,cy+ry+ey))
    return rectangles


def _intersects_support(bounds,rectangles,image_size):
    """Check actual pixel-centre extrema; bounds are native half-open pixels."""
    x0,y0,x1,y1=bounds;width,height=image_size
    left,right=x0/(width-1),(x1-1)/(width-1)
    top,bottom=y0/(height-1),(y1-1)/(height-1)
    return any(right>=a and left<=c and bottom>=b and top<=d for a,b,c,d in rectangles)


def render_region(source, bounds, value=None, analysis=None, operations=None, *, image_size=None):
    """Inverse-sample a native-size region, without full-image coordinate fields.

    source is a full image or callable read([x0,y0,x1,y1]); a callable requires
    image_size=(W,H). This lets the caller supply an already-retouched/relit
    source, keeping geometry the final spatial stage and avoiding repeated warp.
    """
    if callable(source):
        width, height = _dimensions(image_size)
    else:
        if not isinstance(source, np.ndarray) or source.ndim not in (2, 3):
            raise ValueError("source 须为全图数组或区域读取函数。")
        width, height = _dimensions((source.shape[1], source.shape[0]))
        if image_size is not None and tuple(image_size) != (width, height):
            raise ValueError("全图数组尺寸与 image_size 不一致。")
    bounds = _bounds(bounds, width, height)
    state = validate_geometry(value)
    if not state["enabled"] or (operations is None and not geometry_active(state)):
        return _read_source(source, bounds, (width, height)).copy()
    ops = (build_operations(state, analysis, width, height)[0] if operations is None
           else legacy._operations(operations))
    supports=_support_rectangles(ops)
    if not _intersects_support(bounds,supports,(width,height)):
        return _read_source(source,bounds,(width,height)).copy()
    x0, y0, x1, y1 = bounds
    output = _read_source(source, bounds, (width, height)).copy()
    if not state["enabled"] or not ops:
        return output
    dimensions = np.array([width-1, height-1], np.float64)
    for top in range(y0, y1, TILE):
        bottom = min(top+TILE, y1)
        for left in range(x0, x1, TILE):
            right = min(left+TILE, x1)
            if not _intersects_support((left,top,right,bottom),supports,(width,height)):
                continue
            xx, yy = np.meshgrid(np.arange(left, right, dtype=np.float64),
                                 np.arange(top, bottom, dtype=np.float64))
            target = np.stack((xx, yy), axis=-1)/dimensions
            mapped = _inverse(target, ops, state, (width, height))
            active = np.any(np.abs(mapped-target) > 1e-12, axis=-1)
            if not active.any():
                continue
            coordinates = mapped*dimensions
            mx = np.clip(coordinates[..., 0], 0, width-1)
            my = np.clip(coordinates[..., 1], 0, height-1)
            sx = max(0, int(np.floor(mx[active].min()))-1)
            sy = max(0, int(np.floor(my[active].min()))-1)
            ex = min(width, int(np.ceil(mx[active].max()))+2)
            ey = min(height, int(np.ceil(my[active].max()))+2)
            patch = _read_source(source, (sx, sy, ex, ey), (width, height))
            sampled = _bilinear(patch, mx, my, sx, sy)
            output[top-y0:bottom-y0, left-x0:right-x0][active] = sampled[active]
    return output


def render_geometry(rgb, value=None, analysis=None):
    if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3
            or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 2):
        raise ValueError("塑形输入须为 uint8 RGB。")
    height, width = rgb.shape[:2]
    operations, meta = build_operations(value, analysis, width, height)
    result = render_region(rgb, (0, 0, width, height), value, analysis, operations)
    meta["applied"] = bool(np.any(result != rgb))
    return result, meta


# Clear compatibility names for callers that previously used face_shape.
validate_face_shape = validate_geometry
apply_face_shape = render_geometry
has_shape = geometry_active

