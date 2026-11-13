"""Deterministic image-preserving lighting, shared with web/lighting-renderer.js.

SPDX-License-Identifier: AGPL-3.0-only

Coordinates: x right, y down, z toward the viewer. Pixel centers are
((x+.5)/W, (y+.5)/H); world XY is ((u-.5)*W/H, v-.5) and Z=depth*.25.
Normals are RGB8 encoded [-1,1], renormalized after bilinear sampling.
This adds illumination to an existing photograph; it does not recover true
reflectance, erase baked-in shadows, or generate occlusion/cast shadows.
"""
from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real
import re
import numpy as np

ALGORITHM_VERSION = 'image-preserving-lighting-v1'
DEFAULT_LIGHT = {
    'id': 'light-1', 'type': 'directional', 'enabled': True,
    'x': .25, 'y': .25, 'z': 1., 'azimuth': -135., 'elevation': 45.,
    'intensity': .8, 'temperature': 6500., 'radius': .5, 'softness': .5,
    'beam_angle': 60., 'target_x': .5, 'target_y': .5,
}
DEFAULT_LIGHTING = {'schema': 1, 'enabled': False, 'scope': 'subject',
                    'strength': 1., 'ambient': 1., 'lights': []}
_RANGES = {'x': (-1, 2), 'y': (-1, 2), 'z': (.1, 3),
           'azimuth': (-180, 180), 'elevation': (5, 90),
           'intensity': (0, 3), 'temperature': (2000, 12000),
           'radius': (.02, 2), 'softness': (0, 1), 'beam_angle': (5, 120),
           'target_x': (-1, 2), 'target_y': (-1, 2)}


def _number(value, name, low, high):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f'{name} 必须是有限数值。')
    if not low <= value <= high:
        raise ValueError(f'{name} 必须在 {low} 到 {high} 之间。')
    return float(value)


def validate_lighting(value: dict | None) -> dict:
    if value is None:
        return deepcopy(DEFAULT_LIGHTING)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIGHTING):
        raise ValueError('布光参数包含未知字段。')
    result = {**DEFAULT_LIGHTING, **value}
    if type(result['schema']) is not int or result['schema'] != 1:
        raise ValueError('不支持的布光参数版本。')
    if type(result['enabled']) is not bool:
        raise ValueError('布光开关必须为布尔值。')
    if not isinstance(result['scope'], str) or result['scope'] not in ('subject', 'background', 'all'):
        raise ValueError('布光范围必须为 subject、background 或 all。')
    result['strength'] = _number(result['strength'], '整体强度', 0, 1)
    result['ambient'] = _number(result['ambient'], '环境光', 0, 1.5)
    if not isinstance(result['lights'], list) or len(result['lights']) > 4:
        raise ValueError('最多同时使用 4 个光源。')
    lights, seen = [], set()
    for item in result['lights']:
        if not isinstance(item, dict) or set(item) - set(DEFAULT_LIGHT):
            raise ValueError('光源参数包含未知字段。')
        light = {**DEFAULT_LIGHT, **item}
        identifier = light['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in seen:
            raise ValueError('光源编号必须唯一，且仅包含字母、数字、短横线和下划线。')
        seen.add(identifier)
        if not isinstance(light['type'], str) or light['type'] not in ('directional', 'point', 'spot', 'area'):
            raise ValueError('不支持的光源类型。')
        if type(light['enabled']) is not bool:
            raise ValueError('光源开关必须为布尔值。')
        for key, (low, high) in _RANGES.items():
            light[key] = _number(light[key], key, low, high)
        lights.append(light)
    result['lights'] = lights
    return result


def lighting_active(value: dict | None) -> bool:
    p = validate_lighting(value)
    return bool(p['enabled'] and p['strength'] > 0 and any(light['enabled'] and light['intensity'] > 0 for light in p['lights']))


def _preset(identifier, name, description, ambient, *lights):
    return {'id': identifier, 'name': name, 'description': description,
            'lighting': {'schema': 1, 'enabled': True, 'scope': 'subject',
                         'strength': 1., 'ambient': ambient,
                         'lights': [{**DEFAULT_LIGHT, **light, 'id': f'light-{i+1}'} for i, light in enumerate(lights)]}}


LIGHT_PRESETS = [
    _preset('window', '窗边柔光', '侧上方大面积柔光，保留自然明暗。', .78,
            {'type': 'area', 'x': -.15, 'y': .2, 'z': 1., 'radius': 1.1, 'intensity': .75, 'temperature': 6200, 'softness': .8}),
    _preset('butterfly', '蝶形布光', '正上方主光，适合正面人像塑形。', .63,
            {'azimuth': -90, 'elevation': 48, 'intensity': .65, 'softness': .35}),
    _preset('side', '侧面塑形', '侧面主光与轻补光，强调表面起伏。', .54,
            {'azimuth': -160, 'elevation': 32, 'intensity': .9, 'temperature': 6000, 'softness': .25},
            {'azimuth': -15, 'elevation': 60, 'intensity': .2, 'softness': .8}),
    _preset('clamshell', '上下柔光', '上方柔光配下方补光，减弱局部反差。', .56,
            {'type': 'area', 'x': .5, 'y': -.05, 'z': 1., 'radius': .9, 'intensity': .8, 'softness': .9},
            {'type': 'area', 'x': .5, 'y': 1.15, 'z': 1., 'radius': .8, 'intensity': .45, 'softness': .9}),
    _preset('edge', '侧缘补光', '两侧低角度光，突出轮廓的明暗变化。', .65,
            {'azimuth': 175, 'elevation': 16, 'intensity': .8, 'softness': .2},
            {'azimuth': 5, 'elevation': 20, 'intensity': .65, 'softness': .2}),
    _preset('warmcool', '冷暖双色', '暖色主光与冷色补光形成色温层次。', .45,
            {'azimuth': -145, 'elevation': 45, 'intensity': .8, 'temperature': 3000, 'softness': .45},
            {'azimuth': -25, 'elevation': 38, 'intensity': .7, 'temperature': 11000, 'softness': .45}),
    _preset('warm', '暖日斜照', '低角度暖光，模拟日落时的侧光方向。', .6,
            {'azimuth': -140, 'elevation': 25, 'intensity': .9, 'temperature': 3800, 'softness': .25}),
    _preset('spot', '聚光塑形', '聚光灯照向主体，光束边缘柔和过渡。', .55,
            {'type': 'spot', 'x': .2, 'y': .1, 'z': 1.3, 'target_x': .5, 'target_y': .45,
             'beam_angle': 80, 'intensity': 1.5, 'softness': .7}),
    _preset('bright', '明净柔光', '宽阔的正面柔光，轻提主体亮度。', .87,
            {'type': 'area', 'x': .5, 'y': .35, 'z': 1.2, 'radius': 1.4, 'intensity': .5, 'softness': 1.}),
    _preset('lowkey', '暗调侧光', '降低主体环境光，以一盏侧光刻画层次。', .3,
            {'azimuth': -155, 'elevation': 35, 'intensity': .95, 'temperature': 5600, 'softness': .2}),
]
PRESETS = LIGHT_PRESETS


def _sample_rows(source, width, height, start, stop):
    """Float bilinear sampling at pixel centers, matching GL LINEAR textures."""
    sh, sw = source.shape[:2]
    x = np.clip((np.arange(width, dtype=np.float32)+.5)*sw/width-.5, 0, sw-1)
    y = np.clip((np.arange(start, stop, dtype=np.float32)+.5)*sh/height-.5, 0, sh-1)
    x0, y0 = np.floor(x).astype(np.intp), np.floor(y).astype(np.intp)
    x1, y1 = np.minimum(x0+1, sw-1), np.minimum(y0+1, sh-1)
    wx, wy = x-x0, y-y0
    if source.ndim == 3:
        wx, wy = wx[None, :, None], wy[:, None, None]
    else:
        wx, wy = wx[None, :], wy[:, None]
    top = source[y0[:, None], x0]*(1-wx)+source[y0[:, None], x1]*wx
    bottom = source[y1[:, None], x0]*(1-wx)+source[y1[:, None], x1]*wx
    return (top*(1-wy)+bottom*wy).astype(np.float32)


def _normalize(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-8)


def render_lighting(rgb: np.ndarray, normal: np.ndarray, depth: np.ndarray,
                    mask: np.ndarray, lighting: dict | None) -> np.ndarray:
    p = validate_lighting(lighting)
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('布光输入必须是 uint8 RGB 图像。')
    if not lighting_active(p):
        return rgb.copy()
    if not isinstance(normal, np.ndarray) or normal.dtype != np.uint8 or normal.ndim != 3 or normal.shape[2] != 3:
        raise ValueError('布光需要经过分析的 RGB8 法线图。')
    for name, field in [('深度', depth), ('主体选区', mask)]:
        if not isinstance(field, np.ndarray) or field.ndim != 2 or not field.size or not np.isfinite(field).all():
            raise ValueError(f'{name}图必须是有限二维数组。')
    if depth.dtype != np.uint8:
        raise ValueError('深度图必须是 R8 图像。')
    maskf = mask.astype(np.float32) / (255 if mask.dtype == np.uint8 else 1)
    if maskf.min() < 0 or maskf.max() > 1:
        raise ValueError('主体选区必须在 0 到 1 之间。')
    normals, depths = normal.astype(np.float32)/255., depth.astype(np.float32)/255.
    height, width = rgb.shape[:2]
    aspect = width/height
    xs = ((np.arange(width, dtype=np.float32)+.5)/width-.5)*aspect
    result = np.empty_like(rgb)
    active = [light for light in p['lights'] if light['enabled'] and light['intensity'] > 0]
    for start in range(0, height, 128):
        stop = min(start+128, height)
        n = _normalize(_sample_rows(normals, width, height, start, stop)*2-1)
        z = _sample_rows(depths, width, height, start, stop)*.25
        ys = (np.arange(start, stop, dtype=np.float32)+.5)/height-.5
        pos = np.empty((*z.shape, 3), np.float32)
        pos[..., 0], pos[..., 1], pos[..., 2] = xs[None, :], ys[:, None], z
        gain = np.full_like(pos, p['ambient'])
        for light in active:
            t = math.log(light['temperature']/6500.)
            tint = np.array([math.exp(-.35*t), math.exp(-.04*abs(t)), math.exp(.45*t)], np.float32)
            wrap = light['softness']*.35
            if light['type'] == 'directional':
                az, el = math.radians(light['azimuth']), math.radians(light['elevation'])
                direction = np.array([math.cos(az)*math.cos(el), math.sin(az)*math.cos(el), math.sin(el)], np.float32)
                amount = np.maximum(0, (np.sum(n*direction, axis=-1)+wrap)/(1+wrap))
            else:
                q = np.array([(light['x']-.5)*aspect, light['y']-.5, light['z']], np.float32)
                offsets = [(0, 0)]
                if light['type'] == 'area':
                    r = light['radius']/2
                    offsets += [(-r, -r), (-r, r), (r, -r), (r, r)]
                amount = np.zeros(z.shape, np.float32)
                for dx, dy in offsets:
                    delta = q+np.array([dx, dy, 0], np.float32)-pos
                    distance2 = np.sum(delta*delta, axis=-1)
                    direction = delta / np.sqrt(np.maximum(distance2, 1e-8))[..., None]
                    contribution = np.maximum(0, (np.sum(n*direction, axis=-1)+wrap)/(1+wrap))/(1+distance2)
                    if light['type'] == 'spot':
                        target = np.array([(light['target_x']-.5)*aspect, light['target_y']-.5, 0], np.float32)
                        axis = _normalize(target-q)
                        cosine = np.sum(-direction*axis, axis=-1)
                        outer = math.cos(math.radians(light['beam_angle']/2))
                        inner = math.cos(math.radians(light['beam_angle']/2*(1-.8*light['softness'])))
                        if inner-outer < 1e-6:
                            cone = (cosine >= outer).astype(np.float32)
                        else:
                            a = np.clip((cosine-outer)/(inner-outer), 0, 1)
                            cone = a*a*(3-2*a)
                        contribution *= cone
                    amount += contribution/len(offsets)
            gain += (amount*light['intensity'])[..., None]*tint
        alpha = np.ones(z.shape, np.float32) if p['scope'] == 'all' else _sample_rows(maskf, width, height, start, stop)
        if p['scope'] == 'background':
            alpha = 1-alpha
        alpha *= p['strength']
        source = rgb[start:stop].astype(np.float32)/255.
        linear = np.where(source <= .04045, source/12.92, ((source+.055)/1.055)**2.4)
        lit = np.maximum(linear*(1+(gain-1)*alpha[..., None]), 0)
        encoded = np.where(lit <= .0031308, lit*12.92, 1.055*lit**(1/2.4)-.055)
        result[start:stop] = np.floor(np.clip(encoded, 0, 1)*255+.5).astype(np.uint8)
        outside = alpha == 0
        result[start:stop][outside] = rgb[start:stop][outside]
    return result
