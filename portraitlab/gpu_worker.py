"""Isolated persistent CUDA color compositor. SPDX-License-Identifier: AGPL-3.0-only.

The regular application never imports torch. This process accepts only paths
inside its server-selected scratch directory. CUDA memory is bounded by tiles.
Image file compression is still performed by Pillow in the parent process.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np

MAX_TILE_PIXELS = 262144
MAX_IMAGE_PIXELS = 80_000_000


def probe():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用；请检查 NVIDIA 显卡驱动与 CUDA 版 AI 运行组件。")
    # is_available alone cannot detect broken drivers/unsupported kernels.
    check = (torch.arange(16, device="cuda", dtype=torch.float32) * 2).sum()
    if check.item() != 240:
        raise RuntimeError("CUDA 实际计算校验失败。")
    torch.cuda.synchronize()
    return {"cuda_available": True, "gpu_name": torch.cuda.get_device_name(0),
            "torch_version": str(torch.__version__), "cuda_version": torch.version.cuda,
            "max_tile_pixels": MAX_TILE_PIXELS}


def checked_path(raw, root):
    path = Path(raw).resolve()
    if not path.is_relative_to(root) or path.parent == root:
        raise ValueError("GPU 数据路径必须位于本进程专用作业目录内。")
    return path


def compose(request, root):
    import torch
    original_path = checked_path(request["original"], root)
    changed_path = checked_path(request["changed"], root)
    output_path = checked_path(request["output"], root)
    field_path = checked_path(request["field"], root) if request.get("field") else None
    if len({p.parent for p in (original_path, changed_path, output_path)}) != 1 or (field_path and field_path.parent != output_path.parent):
        raise ValueError("GPU 输入输出必须属于同一个作业。")
    if output_path.exists():
        raise ValueError("GPU 输出已存在。")
    original = np.load(original_path, mmap_mode="r", allow_pickle=False)
    changed = np.load(changed_path, mmap_mode="r", allow_pickle=False)
    field = np.load(field_path, mmap_mode="r", allow_pickle=False) if field_path else None
    if original.ndim != 3 or original.shape[2] != 3 or original.dtype != np.uint8:
        raise ValueError("GPU 输入须为 uint8 RGB。")
    if original.shape[0] * original.shape[1] > MAX_IMAGE_PIXELS or changed.shape != original.shape or changed.dtype != np.float32:
        raise ValueError("GPU 输入尺寸或精度无效。")
    if field is not None and (field.shape != original.shape[:2] or field.dtype != np.float32):
        raise ValueError("GPU 面部曝光场无效。")
    p = request["params"]
    for key, low, high in (("exposure", -2, 2), ("relight", -1, 1), ("warmth", -1, 1), ("saturation", 0, 2), ("opacity", 0, 1)):
        value = p[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("GPU 合成参数无效。")
    output = np.lib.format.open_memmap(output_path, mode="w+", dtype=np.uint8, shape=original.shape)
    src, edited, result = original.reshape(-1, 3), changed.reshape(-1, 3), output.reshape(-1, 3)
    flat_field = field.reshape(-1) if field is not None else None
    tiles = 0
    try:
        if p["opacity"] == 0:
            result[:] = src
            return {"actual_device": "cpu", "gpu_name": None, "tiles": 0}
        with torch.inference_mode():
            luma = torch.tensor([.2126, .7152, .0722], dtype=torch.float32, device="cuda")
            gains = torch.exp2(torch.tensor([.16, 0., -.16], dtype=torch.float32, device="cuda") * p["warmth"])
            gains = gains / (gains * luma).sum()

            def to_linear(value):
                return torch.where(value <= .04045, value / 12.92, ((value + .055) / 1.055).pow(2.4))

            for start in range(0, src.shape[0], MAX_TILE_PIXELS):
                stop = min(start + MAX_TILE_PIXELS, src.shape[0])
                # Copy each tile only: no full-size source/output tensor on GPU.
                value = torch.from_numpy(np.array(edited[start:stop])).to("cuda").clamp(0, 1)
                lin = to_linear(value)
                if p["exposure"]:
                    lin *= np.float32(2.0 ** p["exposure"]).item()
                if flat_field is not None:
                    radial = torch.from_numpy(np.array(flat_field[start:stop])).to("cuda")
                    lin *= torch.exp2(p["relight"] * radial).unsqueeze(1)
                if p["warmth"]:
                    lin *= gains
                if p["saturation"] != 1:
                    luminance = (lin * luma).sum(dim=1, keepdim=True)
                    lin = luminance + p["saturation"] * (lin - luminance)
                lin = lin.clamp_min(0)
                if p["opacity"] < 1:
                    source = torch.from_numpy(np.array(src[start:stop])).to(device="cuda", dtype=torch.float32) / 255.
                    source_lin = to_linear(source)
                    lin = source_lin + p["opacity"] * (lin - source_lin)
                lin = lin.clamp_min(0)
                encoded = torch.where(lin <= .0031308, lin * 12.92, 1.055 * lin.pow(1 / 2.4) - .055)
                result[start:stop] = (encoded.clamp(0, 1) * 255).round().to(torch.uint8).cpu().numpy()
                tiles += 1
            torch.cuda.synchronize()
            return {"actual_device": "cuda", "gpu_name": torch.cuda.get_device_name(0), "tiles": tiles,
                    "max_tile_pixels": MAX_TILE_PIXELS, "torch_version": str(torch.__version__)}
    finally:
        output.flush()
        # Mappings are closed before acknowledging, allowing Windows cleanup.
        for array in (output, original, changed, field):
            if array is not None:
                array._mmap.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    root = parser.parse_args().root.resolve()
    if not root.is_dir():
        raise ValueError("GPU 作业根目录不存在。")
    for line in sys.stdin:
        identifier = None
        try:
            if len(line) > 16384:
                raise ValueError("GPU 请求过大。")
            request = json.loads(line)
            identifier = request.get("id")
            action = request.get("action")
            if action == "close":
                break
            if action not in ("probe", "compose"):
                raise ValueError("未知 GPU 请求。")
            result = probe() if action == "probe" else compose(request, root)
            response = {"id": identifier, "ok": True, **result}
        except Exception as exc:
            # A failed CUDA context is discarded by the parent before reuse.
            response = {"id": identifier, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
