"""One-shot Depth Anything V2 Small worker. SPDX-License-Identifier: AGPL-3.0-only.

Runs inside the existing AI Python, with no dependency upgrades or network calls.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter
import uuid

sys.dont_write_bytecode = True
APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
# The portable app intentionally keeps OpenCV in its core runtime while Torch
# lives in AI/. Both embedded interpreters are CPython 3.12 with the same NumPy.
# Reuse that sibling library instead of installing/upgrading either environment.
core_libraries = Path(sys.executable).resolve().parent.parent/'runtime/Lib/site-packages'
if core_libraries.is_dir():
    sys.path.append(str(core_libraries))


def write_png(path, values):
    from PIL import Image
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        Image.fromarray(values).save(temporary, format='PNG')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def infer(args):
    started = perf_counter()
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    import torch
    import torch.nn.functional as F
    from portraitlab.lighting_geometry import manifest, model_status, geometry_signature, file_sha256, GEOMETRY_VERSION
    from portraitlab.vendor.depth_anything_v2.dpt import DepthAnythingV2

    torch.set_num_threads(min(8, max(1, os.cpu_count() or 1)))
    spec = manifest()
    ready = model_status(args.models)
    if not ready['ready']:
        raise RuntimeError('请先下载并校验布光分析模型。')
    weights_path = Path(args.models)/spec['files'][0]['relative_path']
    with Image.open(args.input) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        source_size = image.size
        image.thumbnail((args.edge, args.edge), Image.Resampling.LANCZOS)
        rgb = np.asarray(image)
    height, width = rgb.shape[:2]
    # Same ImageNet normalization as upstream. Bound long-edge token count for
    # panoramas instead of allowing the 518px short-edge rule to grow unbounded.
    scale = min(518/min(height, width), 1036/max(height, width))
    inference_h = max(14, round(height*scale/14)*14)
    inference_w = max(14, round(width*scale/14)*14)
    resized = cv2.resize(rgb.astype(np.float32)/255., (inference_w, inference_h), interpolation=cv2.INTER_CUBIC)
    normalized = (resized-np.array([.485, .456, .406], np.float32))/np.array([.229, .224, .225], np.float32)
    tensor_cpu = torch.from_numpy(np.ascontiguousarray(normalized.transpose(2, 0, 1))).unsqueeze(0)
    weights = torch.load(weights_path, map_location='cpu', weights_only=True)
    if not isinstance(weights, dict):
        raise RuntimeError('固定模型文件不含预期权重字典。')
    load_finished = perf_counter()
    requested = args.device
    if requested == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('当前 NVIDIA GPU 不可用，请选择自动或 CPU。')
    actual = 'cuda' if requested in ('auto', 'cuda') and torch.cuda.is_available() else 'cpu'
    fallback = None
    for attempt in range(2):
        model = DepthAnythingV2(encoder='vits', features=64, out_channels=[48, 96, 192, 384])
        model.load_state_dict(weights, strict=True)
        model.eval()
        try:
            model = model.to(actual)
            tensor = tensor_cpu.to(actual)
            if actual == 'cuda':
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
            inference_started = perf_counter()
            with torch.inference_mode():
                depth_tensor = model(tensor)
                depth_tensor = F.interpolate(depth_tensor[:, None], (height, width), mode='bilinear', align_corners=True)[0, 0]
                raw = depth_tensor.float().cpu().numpy()
            if actual == 'cuda':
                torch.cuda.synchronize()
            inference_seconds = perf_counter()-inference_started
            break
        except (RuntimeError, torch.cuda.OutOfMemoryError) as exc:
            if requested != 'auto' or actual != 'cuda':
                raise
            fallback = f'GPU 分析不可用，已使用 CPU：{type(exc).__name__}'
            del model
            if 'tensor' in locals():
                del tensor
            gc.collect()
            torch.cuda.empty_cache()
            actual = 'cpu'
    if not np.isfinite(raw).all():
        raise RuntimeError('深度分析返回无效数值。')
    low, high = np.percentile(raw, [1, 99])
    if high-low <= 1e-8:
        depth = np.zeros_like(raw, dtype=np.float32)
    else:
        depth = np.clip((raw-low)/(high-low), 0, 1).astype(np.float32)
    depth = cv2.GaussianBlur(depth, (0, 0), max(.8, max(width, height)/512*1.2))
    # P.z=depth*.25, pixel spacing in both world XY axes is 1/imageHeight.
    dzdy, dzdx = np.gradient(depth*.25, 1/height, 1/height)
    normal = np.stack([-dzdx, -dzdy, np.ones_like(depth)], axis=-1)
    normal /= np.maximum(np.linalg.norm(normal, axis=-1, keepdims=True), 1e-8)
    normal8 = np.floor(np.clip(normal*.5+.5, 0, 1)*255+.5).astype(np.uint8)
    depth8 = np.floor(np.clip(depth, 0, 1)*255+.5).astype(np.uint8)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_png(output/'normal.png', normal8)
    write_png(output/'depth.png', depth8)
    result = {
        'ok': True, 'model': spec['name'], 'model_sha256': spec['files'][0]['sha256'],
        'source_sha256': file_sha256(Path(args.input)), 'geometry_signature': geometry_signature(),
        'geometry_version': GEOMETRY_VERSION, 'device_preference': requested, 'actual_device': actual,
        'gpu_name': torch.cuda.get_device_name(0) if actual == 'cuda' else None,
        'torch': torch.__version__, 'fallback_reason': fallback,
        'source_size': list(source_size), 'analysis_size': [width, height],
        'inference_size': [inference_w, inference_h], 'depth_is_metric': False,
        'depth_encoding': 'R8 relative inverse depth; near=1; robust 1-99 percentile and Gaussian smoothing',
        'normal_encoding': 'RGB8 [-1,1]; x right, y down, z toward viewer; derived from relative depth',
        'normal_algorithm': 'normalize(-d(depth*.25)/dx,-d(depth*.25)/dy,1); pixel spacing=1/imageHeight',
        'normal_sha256': file_sha256(output/'normal.png'), 'depth_sha256': file_sha256(output/'depth.png'),
        'peak_cuda_allocated_bytes': torch.cuda.max_memory_allocated() if actual == 'cuda' else 0,
        'timings_seconds': {'initialization': load_finished-started, 'inference': inference_seconds,
                            'total': perf_counter()-started},
        'limitations': 'Estimated single-view geometry; does not recover true reflectance or remove original shadows.'
    }
    (output/'geometry.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--edge', type=int, default=1024)
    args = parser.parse_args()
    if not 128 <= args.edge <= 1024:
        parser.error('--edge must be between 128 and 1024')
    try:
        print(json.dumps(infer(args), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'error_type': type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
