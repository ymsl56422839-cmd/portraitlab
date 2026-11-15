"""User-marked, native-resolution local repair for the isolated V2 prototype.

SPDX-License-Identifier: AGPL-3.0-only
No detector or semantic mask runs. Brush support is a union of round capsules;
feathering stays inside that support. Disjoint 256px result blocks use 576px
model context and blend 64px overlapping predictions before producing deltas.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import time

import numpy as np

if __package__:
    from . import neural_candidate as core
else:
    _module_dir = str(Path(__file__).resolve().parent)
    if _module_dir not in sys.path:
        sys.path.insert(0, _module_dir)
    import neural_candidate as core

BLOCK = 256
BLEND = 64
MAX_STROKES = 100
MAX_POINTS = 5000
MAX_MASK_PIXELS = 1_000_000
MAX_BLOCKS = 64
MAX_RASTER_PIXELS = 100_000_000
MAX_TILE_VISITS = 100_000


def validate_strokes(strokes, width, height):
    """Normalize original-image pixel coordinates without changing their scale."""
    if type(width) is not int or type(height) is not int or min(width, height) < 2:
        raise ValueError('图片宽高须为至少 2 像素的整数。')
    if not isinstance(strokes, list) or not 1 <= len(strokes) <= MAX_STROKES:
        raise ValueError('每次请标记 1～100 笔，更多标记请分次修补。')
    count, normalized = 0, []
    for stroke in strokes:
        if not isinstance(stroke, dict):
            raise ValueError('笔触数据无效，请重新标记。')
        try:
            radius = core._number(stroke.get('radius'), 2, 80, 'radius')
        except ValueError:
            raise ValueError('画笔半径须为 2～80 像素之间的有效数值。') from None
        points = stroke.get('points')
        if not isinstance(points, list) or not points:
            raise ValueError('每一笔至少需要一个标记点。')
        count += len(points)
        if count > MAX_POINTS:
            raise ValueError('单次标记最多 5000 个采样点，请分次修补。')
        result = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('标记点坐标无效，请重新标记。')
            try:
                x = core._number(point[0], 0, width, 'x')
                y = core._number(point[1], 0, height, 'y')
            except ValueError:
                raise ValueError('标记点须位于图片内，且坐标须为有效数值。') from None
            if x >= width or y >= height:
                raise ValueError('标记点超出图片边界，请在图片内标记。')
            result.append([x, y])
        normalized.append({'radius': radius, 'points': result})
    return normalized


def _intersects_segment(a, b, rectangle):
    """Slab clipping avoids rasterizing the empty corners of long diagonals."""
    low, high = 0., 1.
    for start, end, minimum, maximum in ((a[0], b[0], rectangle[0], rectangle[2]),
                                         (a[1], b[1], rectangle[1], rectangle[3])):
        step = end - start
        if abs(step) < 1e-12:
            if start < minimum or start > maximum:
                return False
        else:
            first, last = sorted(((minimum - start) / step, (maximum - start) / step))
            low, high = max(low, first), min(high, last)
            if low > high:
                return False
    return True


def build_manual_mask(strokes, width, height):
    """Return sparse, disjoint native-size mask blocks; no full-image float mask."""
    strokes = validate_strokes(strokes, width, height)
    blocks, marked, raster_pixels, visits = {}, 0, 0, 0
    for stroke in strokes:
        radius, points = stroke['radius'], stroke['points']
        feather = min(6., max(1., radius * .25))
        segments = zip(points, points[1:]) if len(points) > 1 else [(points[0], points[0])]
        for a, b in segments:
            left = max(0, math.floor(min(a[0], b[0]) - radius))
            top = max(0, math.floor(min(a[1], b[1]) - radius))
            right = min(width, math.ceil(max(a[0], b[0]) + radius))
            bottom = min(height, math.ceil(max(a[1], b[1]) + radius))
            dx, dy = b[0] - a[0], b[1] - a[1]
            length_squared = dx * dx + dy * dy
            for ty in range(top // BLOCK, (bottom - 1) // BLOCK + 1):
                for tx in range(left // BLOCK, (right - 1) // BLOCK + 1):
                    visits += 1
                    if visits > MAX_TILE_VISITS:
                        raise ValueError('标记路径过于复杂，请分成较小区域修补。')
                    x0, y0 = tx * BLOCK, ty * BLOCK
                    x1, y1 = min(width, x0 + BLOCK), min(height, y0 + BLOCK)
                    if not _intersects_segment(a, b, [x0-radius, y0-radius, x1+radius, y1+radius]):
                        continue
                    l, t, r, d = max(left, x0), max(top, y0), min(right, x1), min(bottom, y1)
                    raster_pixels += (r - l) * (d - t)
                    if raster_pixels > MAX_RASTER_PIXELS:
                        raise ValueError('标记点过密，请减少重复涂画或分次修补。')
                    # UI image coordinates refer to pixel edges; sample at centers.
                    yy, xx = np.ogrid[t+.5:d+.5, l+.5:r+.5]
                    if length_squared:
                        along = np.clip(((xx-a[0])*dx+(yy-a[1])*dy)/length_squared, 0, 1)
                        distance = np.sqrt((xx-a[0]-along*dx)**2+(yy-a[1]-along*dy)**2)
                    else:
                        distance = np.sqrt((xx-a[0])**2+(yy-a[1])**2)
                    inward = np.clip((radius-distance)/feather, 0, 1)
                    soft = (inward*inward*(3-2*inward)).astype(np.float32)
                    if not soft.any():
                        continue
                    key = (tx, ty)
                    if key not in blocks:
                        if len(blocks) >= MAX_BLOCKS:
                            raise ValueError('标记跨越超过 64 个处理块，请分区修补。')
                        blocks[key] = np.zeros((y1-y0, x1-x0), np.float32)
                    target = blocks[key][t-y0:d-y0, l-x0:r-x0]
                    before = np.count_nonzero(target)
                    np.maximum(target, soft, out=target)
                    marked += np.count_nonzero(target) - before
                    if marked > MAX_MASK_PIXELS:
                        raise ValueError('标记面积超过 1000000 像素（100 万），请分区修补。')
    return strokes, blocks


def _mask_crop(blocks, bounds):
    x0, y0, x1, y1 = bounds
    result = np.zeros((y1-y0, x1-x0), np.float32)
    for (tx, ty), mask in blocks.items():
        bx, by = tx*BLOCK, ty*BLOCK
        l, t, r, b = max(x0, bx), max(y0, by), min(x1, bx+mask.shape[1]), min(y1, by+mask.shape[0])
        if l < r and t < b:
            result[t-y0:b-y0, l-x0:r-x0] = mask[t-by:b-by, l-bx:r-bx]
    return result


def _validate_rgb(rgb):
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('输入须为 8 位 RGB 图片。')
    height, width = rgb.shape[:2]
    if min(height, width) < 2 or height*width > 80_000_000:
        raise ValueError('图片宽高至少为 2 像素，总像素不得超过 8000 万。')
    return height, width


def analyse_manual(rgb, strokes, predictor):
    height, width = _validate_rgb(rgb)
    started = time.perf_counter()
    strokes, blocks = build_manual_mask(strokes, width, height)
    raster_seconds = time.perf_counter()-started
    sums = {key: np.zeros((*mask.shape, 3), np.float32) for key, mask in blocks.items()}
    weights = {key: np.zeros(mask.shape, np.float32) for key, mask in blocks.items()}
    inference_started = time.perf_counter()
    # Output blocks stay disjoint. Only predictions overlap, then blend smoothly.
    for tx, ty in sorted(blocks, key=lambda key: (key[1], key[0])):
        bx, by = tx*BLOCK, ty*BLOCK
        l, t, r, b = max(0, bx-BLEND), max(0, by-BLEND), min(width, bx+BLOCK+BLEND), min(height, by+BLOCK+BLEND)
        cx, cy = (l+r)//2, (t+b)//2
        context = [max(0, cx-288), max(0, cy-288), min(width, cx+288), min(height, cy+288)]
        x0, y0, x1, y1 = context
        prediction = np.asarray(predictor.repair(rgb[y0:y1, x0:x1], _mask_crop(blocks, context),
                                                  [l-x0, t-y0, r-x0, b-y0]), dtype=np.float32)
        if prediction.shape != (b-t, r-l, 3) or not np.isfinite(prediction).all():
            raise ValueError('Repair network returned an invalid manual patch')
        prediction = np.clip(prediction, 0, 255)
        px, py = np.arange(l, r, dtype=np.float32)+.5, np.arange(t, b, dtype=np.float32)+.5
        wx = np.clip(np.minimum((px-(bx-BLEND))/BLEND, (bx+BLOCK+BLEND-px)/BLEND), 0, 1)
        wy = np.clip(np.minimum((py-(by-BLEND))/BLEND, (by+BLOCK+BLEND-py)/BLEND), 0, 1)
        blend = wy[:, None]*wx[None, :]
        for key, mask in blocks.items():
            ox, oy = key[0]*BLOCK, key[1]*BLOCK
            a, c, d, e = max(l, ox), max(t, oy), min(r, ox+mask.shape[1]), min(b, oy+mask.shape[0])
            if a >= d or c >= e:
                continue
            local = blend[c-t:e-t, a-l:d-l]
            sums[key][c-oy:e-oy, a-ox:d-ox] += prediction[c-t:e-t, a-l:d-l]*local[..., None]
            weights[key][c-oy:e-oy, a-ox:d-ox] += local
    repair_seconds = time.perf_counter()-inference_started
    candidates, patches = [], {}
    for key in sorted(blocks, key=lambda key: (key[1], key[0])):
        mask = blocks[key]
        ys, xs = np.nonzero(mask)
        a, b, c, d = int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1
        x0, y0 = key[0]*BLOCK+a, key[1]*BLOCK+b
        x1, y1 = key[0]*BLOCK+c, key[1]*BLOCK+d
        mask = mask[b:d, a:c].copy()
        target = sums[key][b:d, a:c]/np.maximum(weights[key][b:d, a:c, None], 1e-8)
        original = rgb[y0:y1, x0:x1].astype(np.float32)
        delta = (target-original)*mask[..., None]
        delta[mask == 0] = 0
        identifier = f'm{len(candidates)+1:04d}'
        candidates.append({'id': identifier, 'kind': 'manual', 'bounds': [x0, y0, x1, y1],
            'thumb_bounds': [max(0,x0-24), max(0,y0-24), min(width,x1+24), min(height,y1+24)],
            'area': int(np.count_nonzero(mask)), 'score': None, 'default_enabled': True,
            'requires_review': True, 'classification': 'user_marked_region', 'patch': identifier+'.npz',
            'changed_pixels_at_full_strength': int(np.count_nonzero(np.any(np.rint(original+delta)!=original,axis=2)))})
        patches[identifier] = {'delta': delta.astype(np.float32), 'mask': mask}
    if candidates:
        roi = [min(c['bounds'][0] for c in candidates), min(c['bounds'][1] for c in candidates),
               max(c['bounds'][2] for c in candidates), max(c['bounds'][3] for c in candidates)]
    else:
        raise ValueError('没有有效的标记像素，请重新标记。')
    metadata = {'schema_version': 1, 'kind': 'manual', 'model': core.MODEL_ID,
        'model_revision': core.MODEL_REVISION, 'source_commit': core.SOURCE_COMMIT,
        'source_rgb_sha256': core.rgb_fingerprint(rgb), 'image_size': [width,height], 'roi':roi,
        'default_strength': .7, 'default_enabled': True, 'strokes': strokes, 'candidates': candidates,
        'marked_pixels': sum(c['area'] for c in candidates), 'inference_blocks': len(blocks),
        'timings': {'raster_seconds': raster_seconds, 'candidate_repair_seconds': repair_seconds,
                    'analysis_seconds': time.perf_counter()-started},
        'warnings': ['手动标记直接指定修补范围；请检查纹理和边界后保留。'],
        'delta_contract': 'RGB float32 (target-original)*manual_mask; unscaled by user strength'}
    return metadata, patches


def run_manual_request(request, predictor=None):
    required = {'input','vendor','models','output','strokes'}
    if not isinstance(request, dict) or not required.issubset(request):
        raise ValueError('Manual request requires '+', '.join(sorted(required)))
    out = Path(request['output']).resolve()
    boundary = os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not out.is_relative_to(Path(boundary).resolve()):
        raise ValueError('Output is outside the configured writable storage root')
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Candidate output must be new or empty')
    for field in ('input','vendor','models'):
        if Path(request[field]).resolve().is_relative_to(out):
            raise ValueError('Inputs and model assets cannot be inside the output directory')
    out.mkdir(parents=True,exist_ok=True)
    core.configure_worker_cache(out,request.get('cache_root'))
    started=time.perf_counter()
    rgb=core._read_array(request['input'])
    _validate_rgb(rgb)
    # Validation happens before a directly supplied factory is constructed.
    validate_strokes(request['strokes'],int(rgb.shape[1]),int(rgb.shape[0]))
    predictor=predictor or core.ModelScopeLocalPredictor(request['vendor'],request['models'],request.get('device','auto'))
    metadata,patches=analyse_manual(rgb,request['strokes'],predictor)
    metadata.update(device=str(predictor.device),device_name=predictor.device_name,
                    model_sha256=core.MODEL_SHA256,input_sha256=core.sha256(request['input']))
    metadata['timings']['model_load_seconds']=predictor.load_seconds
    for candidate in metadata['candidates']:
        path=out/candidate['patch'];np.savez_compressed(path,**patches[candidate['id']])
        candidate['patch_sha256']=core.sha256(path)
    metadata['timings']['request_seconds']=time.perf_counter()-started
    (out/'SOURCE-LICENSE.txt').write_bytes((Path(request['vendor'])/'LICENSE').read_bytes())
    (out/'candidates.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return metadata
