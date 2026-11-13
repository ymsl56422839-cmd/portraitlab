"""IC-Light v1 FC worker for PortraitLab.

Adapted from lllyasviel/IC-Light/gradio_demo.py, commit
bcf3f29ca85be8a4686215f477b546f5030be8b7 (Apache-2.0).
Changes: supplied MediaPipe mask instead of BRIA; pinned local-only models;
JSON protocol, explicit paths, fixed output size, sequential CPU offload.
IC-Light and base weights retain their separate CreativeML OpenRAIL-M terms.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import uuid

MANIFEST = Path(__file__).resolve().parents[1] / "models" / "relight-manifest.json"
DEFAULTS = {"prompt": "portrait photograph, detailed face, soft studio lighting", "negative_prompt": "lowres, bad anatomy, bad hands, cropped, worst quality", "direction": "left", "seed": 12345, "steps": 20, "denoise": 0.5, "cfg": 2.0, "width": 512, "height": 768, "low_vram": True, "device": "auto"}


def destination(value):
    path = Path(value).expanduser().resolve()
    root = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if root and not path.is_relative_to(Path(root).resolve()):
        raise ValueError(f"Path is outside PORTRAITLAB_STORAGE_ROOT: {path}")
    return path


def write_json(path, value):
    path = destination(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination(path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp"))
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, destination(path))
    finally:
        if temporary.exists():
            destination(temporary).unlink()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def publish_new_file(temporary, output_path):
    """Never replace an existing file, including on filesystems without hardlinks."""
    temporary, output_path = destination(temporary), destination(output_path)
    try:
        os.link(temporary, output_path)
    except FileExistsError:
        raise
    except OSError:
        # exFAT/FAT32 and some network drives do not support hardlinks.
        created = False
        try:
            with output_path.open("xb") as output:
                created = True
                with temporary.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        except BaseException:
            if created:
                output_path.unlink(missing_ok=True)
            raise


class Progress:
    def __init__(self, path=None):
        self.path = destination(path) if path else None

    def __call__(self, phase, current=0, total=1, message=""):
        value = {"phase": phase, "current": current, "total": total, "message": message, "updated_at": time.time()}
        if self.path:
            write_json(self.path, value)


def model_status(models_dir):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = []
    for item in manifest["files"]:
        path = destination(models_dir / item["path"])
        exists = path.is_file()
        size_ok = exists and path.stat().st_size == item["bytes"]
        files.append({**item, "relative_path": item["path"], "path": str(path), "exists": exists, "size_ok": size_ok, "hash_verified": False})
    return {"ok": True, "action": "status", "ready": all(f["size_ok"] for f in files), "files": files, "estimated_bytes": manifest["total_bytes"], "models_dir": str(models_dir), "cuda_checked": False, "note": "Status checks file presence/size only; generation verifies all SHA256 values."}


def download_models(models_dir, progress):
    status = model_status(models_dir)
    total, done = status["estimated_bytes"], 0
    for item in status["files"]:
        path = destination(item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size == item["bytes"] and sha256(path) == item["sha256"]:
            done += item["bytes"]
            progress("download", done, total, f"Verified {item['relative_path']}")
            continue
        part = destination(str(path) + ".part")
        for attempt in range(4):
            try:
                offset = part.stat().st_size if part.exists() else 0
                request = urllib.request.Request(item["url"], headers={"User-Agent": "PortraitLab/0.2", **({"Range": f"bytes={offset}-"} if offset else {})})
                with urllib.request.urlopen(request, timeout=90) as response:
                    if offset and response.status != 206:
                        offset = 0
                    mode = "ab" if offset else "wb"
                    last = 0.0
                    with part.open(mode) as output:
                        while block := response.read(4 * 1024 * 1024):
                            output.write(block)
                            offset += len(block)
                            if time.monotonic() - last > 0.7:
                                progress("download", done + offset, total, item["relative_path"])
                                last = time.monotonic()
                if part.stat().st_size != item["bytes"] or sha256(part) != item["sha256"]:
                    part.unlink()
                    raise RuntimeError(f"Downloaded file failed checksum: {item['relative_path']}")
                os.replace(part, path)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)
        done += item["bytes"]
    progress("complete", total, total, "Pinned models downloaded and verified")
    result = model_status(models_dir)
    result["action"] = "download"
    for item in result["files"]:
        item["hash_verified"] = True
    return result


def setup_runtime(models_dir):
    cache = destination(os.environ.get("HF_HOME", str(models_dir.parent / "_relight_cache")))
    temporary = destination(os.environ.get("TEMP", str(cache / "temp")))
    for path in (cache, temporary):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update({"HF_HOME": str(cache), "HF_HUB_CACHE": str(cache / "hub"), "HUGGINGFACE_HUB_CACHE": str(cache / "hub"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"})
    sys.dont_write_bytecode = True
    for key, name in (("CUDA_CACHE_PATH", "cuda"), ("TORCH_EXTENSIONS_DIR", "torch-extensions"),
                      ("TORCHINDUCTOR_CACHE_DIR", "inductor"), ("TRITON_CACHE_DIR", "triton")):
        target = destination(os.environ.get(key, str(cache / name)))
        target.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(target)


def probe(models_dir):
    setup_runtime(models_dir)
    import torch
    available = torch.cuda.is_available()
    capability = list(torch.cuda.get_device_capability(0)) if available else None
    architectures = torch.cuda.get_arch_list() if available else []
    total_memory = torch.cuda.get_device_properties(0).total_memory if available else None
    return {"ok": True, "action": "probe", "cuda_available": available, "torch": torch.__version__, "torch_cuda": torch.version.cuda, "cpu_supported": True, "cpu_tested_by_probe": False, "gpu_name": torch.cuda.get_device_name(0) if available else None, "gpu_total_bytes": total_memory, "capability": capability, "cuda_arch_list": architectures, "native_arch_listed": f"sm_{capability[0]}{capability[1]}" in architectures if available else False, "bf16_supported": torch.cuda.is_bf16_supported() if available else False, "recommended_start": {"width": 384, "height": 512, "steps": 20, "low_vram": True, "gpu_memory_target_bytes": 8 * 1024 ** 3, "meets_gpu_memory_target": total_memory >= 8 * 1024 ** 3 if available else False, "note": "8 GB is a conservative deployment target, not a benchmark on this device. Start small; other applications and drivers also use GPU memory."}}


def validate(request):
    params = {key: request.get(key, value) for key, value in DEFAULTS.items()}
    for key in ("prompt", "negative_prompt"):
        if not isinstance(params[key], str) or len(params[key]) > 3000:
            raise ValueError(f"{key} must be text of at most 3000 characters")
    if not params["prompt"].strip():
        raise ValueError("prompt cannot be empty")
    for key, lo, hi in (("steps", 10, 50), ("seed", 0, 4294967295), ("width", 256, 1024), ("height", 256, 1024)):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{key} must be an integer in {lo}..{hi}")
    if params["width"] % 64 or params["height"] % 64:
        raise ValueError("width and height must be multiples of 64")
    for key, lo, hi in (("denoise", 0.1, 0.9), ("cfg", 1, 10)):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{key} must be finite and in {lo}..{hi}")
    if params["direction"] not in ("none", "left", "right", "top", "bottom"):
        raise ValueError("Invalid direction")
    if params["device"] not in ("auto", "cuda", "cpu") or not isinstance(params["low_vram"], bool):
        raise ValueError("device must be auto/cuda/cpu; low_vram must be boolean")
    return params


def generate(request, models_dir, progress):
    started = time.perf_counter()
    params = validate(request)
    input_path = Path(request["input_path"]).resolve(strict=True)
    mask_path = Path(request["mask_path"]).resolve(strict=True)
    output_path = destination(request["output_path"])
    if output_path.exists() or output_path in (input_path, mask_path):
        raise ValueError("Output must be a new file; source files are never overwritten")
    if output_path.suffix.lower() != ".png":
        raise ValueError("Output must be PNG")
    status = model_status(models_dir)
    if not status["ready"]:
        raise RuntimeError("IC-Light models are missing/incomplete; run explicit action=download first")
    progress("verify_models", 0, len(status["files"]), "Checking pinned model SHA256 values")
    for index, item in enumerate(status["files"]):
        if sha256(item["path"]) != item["sha256"]:
            raise RuntimeError(f"Model checksum mismatch: {item['relative_path']}")
        item["hash_verified"] = True
        progress("verify_models", index + 1, len(status["files"]), item["relative_path"])
    setup_runtime(models_dir)
    import numpy as np
    from PIL import Image, ImageOps
    import torch
    import safetensors.torch as sf
    from transformers import CLIPTokenizer, CLIPTextModel
    from diffusers import AutoencoderKL, UNet2DConditionModel, DPMSolverMultistepScheduler, StableDiffusionPipeline, StableDiffusionImg2ImgPipeline
    from diffusers.models.attention_processor import AttnProcessor2_0

    available = torch.cuda.is_available()
    if params["device"] == "cuda" and not available:
        raise RuntimeError("CUDA was requested but no supported NVIDIA CUDA device is available")
    device = torch.device("cuda" if available and params["device"] != "cpu" else "cpu")
    low = params["low_vram"] and device.type == "cuda"
    dtype = torch.float16 if device.type == "cuda" else torch.float32
    vae_dtype = torch.bfloat16 if device.type == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
    timings = {"verify_and_import_seconds": time.perf_counter() - started}
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats()
    progress("load_models", 0, 1, f"Loading IC-Light on {device}")
    step_time = time.perf_counter()
    base = models_dir / "base"
    tokenizer = CLIPTokenizer.from_pretrained(base / "tokenizer", local_files_only=True)
    text_encoder = CLIPTextModel.from_pretrained(base / "text_encoder", local_files_only=True, use_safetensors=True, torch_dtype=dtype)
    vae = AutoencoderKL.from_pretrained(base / "vae", local_files_only=True, use_safetensors=True, torch_dtype=vae_dtype)
    unet = UNet2DConditionModel.from_pretrained(base / "unet", local_files_only=True, use_safetensors=True, torch_dtype=torch.float32)
    with torch.no_grad():
        original = unet.conv_in
        new_input = torch.nn.Conv2d(8, original.out_channels, original.kernel_size, original.stride, original.padding)
        new_input.weight.zero_()
        new_input.weight[:, :4].copy_(original.weight)
        new_input.bias.copy_(original.bias)
        unet.conv_in = new_input
        offsets = sf.load_file(str(models_dir / "iclight_sd15_fc.safetensors"), device="cpu")
        current = unet.state_dict()
        if set(offsets) != set(current):
            raise RuntimeError("IC-Light offset keys do not match the pinned base UNet")
        for key in current:
            current[key].add_(offsets[key].to(current[key]))
        unet.load_state_dict(current, strict=True)
        del offsets, current, original, new_input
    unet.to(dtype=dtype)
    unet_forward = unet.forward

    def foreground_forward(sample, timestep, encoder_hidden_states, **kwargs):
        condition = kwargs["cross_attention_kwargs"]["concat_conds"].to(sample)
        condition = torch.cat([condition] * (sample.shape[0] // condition.shape[0]), dim=0)
        kwargs["cross_attention_kwargs"] = {}
        return unet_forward(torch.cat([sample, condition], dim=1), timestep, encoder_hidden_states, **kwargs)

    unet.forward = foreground_forward
    unet.set_attn_processor(AttnProcessor2_0())
    vae.set_attn_processor(AttnProcessor2_0())
    if params["low_vram"]:
        unet.set_attention_slice("auto")
        vae.enable_tiling()
        vae.enable_slicing()

    # Explicit placement avoids stacking Accelerate hooks on shared modules.
    class TextPipeline(StableDiffusionPipeline):
        @property
        def _execution_device(self):
            return device

    class ImagePipeline(StableDiffusionImg2ImgPipeline):
        @property
        def _execution_device(self):
            return device

    def scheduler():
        return DPMSolverMultistepScheduler(num_train_timesteps=1000, beta_start=0.00085, beta_end=0.012, algorithm_type="sde-dpmsolver++", use_karras_sigmas=True, steps_offset=1)

    common = dict(vae=vae, text_encoder=text_encoder, tokenizer=tokenizer, unet=unet, safety_checker=None, requires_safety_checker=False, feature_extractor=None, image_encoder=None)
    txt_pipe = TextPipeline(**common, scheduler=scheduler())
    img_pipe = ImagePipeline(**common, scheduler=scheduler())
    txt_pipe.set_progress_bar_config(disable=True)
    img_pipe.set_progress_bar_config(disable=True)
    if not low:
        for model in (unet, text_encoder, vae):
            model.to(device)

    def place(model):
        if low:
            for other in (unet, text_encoder, vae):
                if other is not model:
                    other.to("cpu")
            torch.cuda.empty_cache()
            model.to(device)

    timings["load_models_seconds"] = time.perf_counter() - step_time
    with Image.open(input_path) as picture:
        image = np.asarray(ImageOps.exif_transpose(picture).convert("RGB"))
    with Image.open(mask_path) as picture:
        alpha = np.asarray(picture.convert("L"), dtype=np.float32) / 255.0
    if alpha.shape != image.shape[:2] or float(alpha.max()) < 0.01:
        raise ValueError("Foreground mask must match the input and contain a foreground")
    foreground = np.clip(127 + (image.astype(np.float32) - 127) * alpha[..., None], 0, 255).astype(np.uint8)
    width, height = params["width"], params["height"]
    scale = max(width / image.shape[1], height / image.shape[0])
    resized = (round(image.shape[1] * scale), round(image.shape[0] * scale))
    left, top = (resized[0] - width) // 2, (resized[1] - height) // 2
    foreground = np.asarray(Image.fromarray(foreground).resize(resized, Image.Resampling.LANCZOS).crop((left, top, left + width, top + height)))

    def image_tensor(array):
        return (torch.from_numpy(np.array(array, copy=True)).float() / 127.0 - 1.0).permute(2, 0, 1)[None].to(device=device, dtype=vae_dtype)

    def pixels(tensor):
        return np.clip(tensor[0].movedim(0, -1).float().cpu().numpy() * 127.5 + 127.5, 0, 255).astype(np.uint8)

    def encode(text):
        ids = tokenizer(text, truncation=False, add_special_tokens=False)["input_ids"]
        n = tokenizer.model_max_length
        chunks = [[tokenizer.bos_token_id] + ids[i:i + n - 2] + [tokenizer.eos_token_id] for i in range(0, max(1, len(ids)), n - 2)]
        chunks = [chunk + [tokenizer.eos_token_id] * (n - len(chunk)) for chunk in chunks]
        return text_encoder(torch.tensor(chunks, dtype=torch.int64, device=device)).last_hidden_state

    def callback(phase):
        return lambda number, timestep, latent: progress(phase, number + 1, params["steps"], f"{phase}: diffusion step {number + 1}")

    with torch.inference_mode():
        step_time = time.perf_counter()
        place(text_encoder)
        cond = encode(params["prompt"] + ", best quality")
        negative = encode(params["negative_prompt"])
        length = max(len(cond), len(negative))
        cond = cond.repeat(math.ceil(length / len(cond)), 1, 1)[:length].reshape(1, -1, cond.shape[-1])
        negative = negative.repeat(math.ceil(length / len(negative)), 1, 1)[:length].reshape(1, -1, negative.shape[-1])
        place(vae)
        concat = (vae.encode(image_tensor(foreground)).latent_dist.mode() * vae.config.scaling_factor).to(dtype=dtype)
        gradient = None
        direction = params["direction"]
        if direction != "none":
            if direction in ("left", "right"):
                ramp = np.linspace(255, 0, width) if direction == "left" else np.linspace(0, 255, width)
                gradient = np.tile(ramp, (height, 1))
            else:
                ramp = np.linspace(255, 0, height) if direction == "top" else np.linspace(0, 255, height)
                gradient = np.tile(ramp[:, None], (1, width))
            bg = np.stack([gradient] * 3, axis=-1).astype(np.uint8)
            initial = (vae.encode(image_tensor(bg)).latent_dist.mode() * vae.config.scaling_factor).to(dtype=dtype)
        generator = torch.Generator(device=device).manual_seed(params["seed"])
        args = dict(prompt_embeds=cond, negative_prompt_embeds=negative, width=width, height=height, num_images_per_prompt=1, generator=generator, output_type="latent", guidance_scale=params["cfg"], cross_attention_kwargs={"concat_conds": concat}, callback_steps=1)
        place(unet)
        progress("stage1", 0, params["steps"], "Generating foreground-conditioned lighting")
        if gradient is None:
            latent = txt_pipe(**args, num_inference_steps=params["steps"], callback=callback("stage1")).images
        else:
            latent = img_pipe(**args, image=initial, strength=0.9, num_inference_steps=round(params["steps"] / 0.9), callback=callback("stage1")).images
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings["conditioning_and_stage1_seconds"] = time.perf_counter() - step_time
        step_time = time.perf_counter()
        place(vae)
        intermediate = pixels(vae.decode(latent.to(vae_dtype) / vae.config.scaling_factor).sample)
        latent = (vae.encode(image_tensor(intermediate)).latent_dist.mode() * vae.config.scaling_factor).to(dtype=dtype)
        place(unet)
        progress("stage2", 0, params["steps"], "Refining generated lighting")
        latent = img_pipe(**args, image=latent, strength=params["denoise"], num_inference_steps=round(params["steps"] / params["denoise"]), callback=callback("stage2")).images
        place(vae)
        output = pixels(vae.decode(latent.to(vae_dtype) / vae.config.scaling_factor).sample)
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings["stage2_and_decode_seconds"] = time.perf_counter() - step_time
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination(output_path.with_name(output_path.stem + "." + uuid.uuid4().hex + ".tmp"))
    try:
        Image.fromarray(output).save(temporary, format="PNG")
        # Publish without replacing an existing image even under a race.
        publish_new_file(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    timings["total_seconds"] = time.perf_counter() - started
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    info = {"implementation": manifest["implementation"], "checkpoint_license": manifest["checkpoint_license"], "base_license": manifest["base_license"], "model_files": status["files"], "params": params, "timings": timings, "device": str(device), "gpu_name": torch.cuda.get_device_name(0) if device.type == "cuda" else None, "torch": torch.__version__, "torch_cuda": torch.version.cuda, "memory_mode": "component_cpu_offload_attention_slicing_vae_tiling" if low else ("cuda_resident_sdpa" if device.type == "cuda" else "cpu_float32"), "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated() if device.type == "cuda" else 0, "peak_cuda_reserved_bytes": torch.cuda.max_memory_reserved() if device.type == "cuda" else 0, "input_sha256": sha256(input_path), "mask_sha256": sha256(mask_path), "output_sha256": sha256(output_path), "input_size": [image.shape[1], image.shape[0]], "output_size": [width, height], "resize_before_center_crop": list(resized), "crop_box": [left, top, left + width, top + height], "source_image_preserved": True, "matting": "Supplied foreground mask; no BRIA model", "warning": "Generative output may change identity, texture, clothing and background. Direction biases initial latent; it is not a physically exact light position."}
    progress("complete", 1, 1, "Generated a new IC-Light image")
    return {"ok": True, "action": "generate", "output_path": str(output_path), "width": width, "height": height, "metadata": info}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--progress")
    args = parser.parse_args()
    progress = Progress(args.progress)
    result_path = destination(args.result)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
        models_dir = destination(request.get("models_dir") or os.environ.get("PORTRAITLAB_RELIGHT_MODELS") or Path(__file__).resolve().parents[1] / "runtime/models/ic-light")
        action = request.get("action", "status")
        if action == "status":
            result = model_status(models_dir)
        elif action == "download":
            result = download_models(models_dir, progress)
        elif action == "probe":
            result = probe(models_dir)
        elif action == "generate":
            result = generate(request, models_dir, progress)
        else:
            raise ValueError("action must be status, probe, download or generate")
        write_json(result_path, result)
        print(json.dumps({"ok": True, "action": action, "result_path": str(result_path)}))
        return 0
    except Exception as error:
        result = {"ok": False, "error": str(error), "error_type": type(error).__name__}
        write_json(result_path, result)
        progress("error", 0, 1, str(error))
        print(json.dumps(result, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
