"""Relocation smoke test: embedded imports, EXE startup, processing and shutdown.

Always uses a new copy, so the release folder receives no photos, caches or logs.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import uuid
import zipfile
from PIL import Image
from build_portable import checked, mkdir, sha, write_json


def call(url, token=None, data=None, content_type="application/json"):
    headers = {}
    if token:
        headers["X-PortraitLab-Token"] = token
    if data is not None:
        headers["Content-Type"] = content_type
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--copy-to", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18875)
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--ai-generate", action="store_true", help="Also run a 256px/10-step CPU generation from the relocated runtime")
    parser.add_argument("--archives", type=Path, help="Extract and verify actual release ZIPs instead of copying runtimes; models are copied from --package for offline testing")
    args = parser.parse_args()
    source, moved = Path(args.package).resolve(), checked(args.copy_to)
    if moved.exists():
        raise ValueError("Relocation test requires a new destination; it never deletes existing folders")
    ignore = shutil.ignore_patterns("Cache", "AI", "ic-light") if args.core_only else shutil.ignore_patterns("Cache")
    if args.archives:
        release = Path(args.archives).resolve()
        manifest = json.loads((release / "release-packages.json").read_text(encoding="utf-8"))
        mkdir(moved)
        for record in manifest["archives"]:
            if args.core_only and record["component"] != "core":
                continue
            path = release / record["file"]
            if path.stat().st_size != record["bytes"] or sha(path) != record["sha256"]:
                raise ValueError("Release archive checksum mismatch: " + str(path))
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    relative = Path(member.filename)
                    if relative.parts[0] != manifest["top_level_directory"]:
                        raise ValueError("Unexpected ZIP top-level directory")
                    target = checked(moved.joinpath(*relative.parts[1:]))
                    if not target.is_relative_to(moved):
                        raise ValueError("Archive path traversal")
                    if member.is_dir():
                        mkdir(target)
                    else:
                        mkdir(target.parent)
                        with archive.open(member) as archived, target.open("xb") as extracted:
                            shutil.copyfileobj(archived, extracted, 1024 * 1024)
        shutil.copytree(source / "Data/models", moved / "Data/models", ignore=ignore)
    else:
        shutil.copytree(source, moved, ignore=ignore)
    env = os.environ.copy()
    env.pop("PORTRAITLAB_AI_PYTHON", None)
    env.pop("PORTRAITLAB_RELIGHT_MODELS", None)
    env.update({"TEMP": str(mkdir(moved / "Cache/temp")), "TMP": str(moved / "Cache/temp"),
                "MPLCONFIGDIR": str(mkdir(moved / "Cache/matplotlib")), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHOME": str(moved / "deliberately-missing-python-home"),
                "PYTHONPATH": str(moved / "deliberately-missing-python-path"),
                "PORTRAITLAB_DATA": str(moved / "Data"), "PORTRAITLAB_CACHE": str(moved / "Cache")})
    for key, folder in (("HF_HOME", "huggingface"), ("TORCH_HOME", "torch"), ("CUDA_CACHE_PATH", "cuda"), ("TORCH_EXTENSIONS_DIR", "torch-extensions"), ("TORCHINDUCTOR_CACHE_DIR", "torch-inductor"), ("TRITON_CACHE_DIR", "triton"), ("XDG_CACHE_HOME", "xdg")):
        env[key] = str(mkdir(moved / "Cache" / folder))
    probe = '''import sys,json,pathlib,numpy,cv2,fastapi,PIL,mediapipe
root=pathlib.Path(sys.executable).resolve().parent.parent
modules={m.__name__:str(pathlib.Path(m.__file__).resolve()) for m in (numpy,cv2,fastapi,PIL,mediapipe)}
assert all(pathlib.Path(p).is_relative_to(root) for p in modules.values()), modules
assert all(pathlib.Path(p).is_relative_to(root) for p in sys.path), sys.path
assert pathlib.Path(sys.base_prefix).resolve()==pathlib.Path(sys.executable).resolve().parent
print(json.dumps({"executable":sys.executable,"base_prefix":sys.base_prefix,"sys_path":sys.path,"modules":modules}))'''
    verified = subprocess.run([str(moved / "runtime/python.exe"), "-I", "-B", "-X", "utf8", "-c", probe],
                              cwd=moved, env=env, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = {"relocated_root": str(moved), "runtime": json.loads(verified.stdout), "core_only": args.core_only,
              "archive_source": str(args.archives.resolve()) if args.archives else None,
              "offline_model_fixture_source": str(source / "Data/models") if args.archives else None}
    if not args.core_only:
        ai_probe = '''import sys,json,pathlib,torch,diffusers,transformers,safetensors,tokenizers
root=pathlib.Path(sys.executable).resolve().parent.parent
modules={m.__name__:str(pathlib.Path(m.__file__).resolve()) for m in (torch,diffusers,transformers,safetensors,tokenizers)}
assert all(pathlib.Path(p).is_relative_to(root) for p in modules.values()), modules
assert all(pathlib.Path(p).is_relative_to(root) for p in sys.path), sys.path
assert pathlib.Path(sys.base_prefix).resolve()==pathlib.Path(sys.executable).resolve().parent
print(json.dumps({"executable":sys.executable,"base_prefix":sys.base_prefix,"sys_path":sys.path,"modules":modules}))'''
        verified_ai = subprocess.run([str(moved / "AI/python.exe"), "-I", "-B", "-X", "utf8", "-c", ai_probe],
                                     cwd=moved, env=env, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        result["ai_runtime"] = json.loads(verified_ai.stdout)
        verification = mkdir(moved / "Data/verification")
        def ai_action(action, **parameters):
            request = verification / (action + "-request.json")
            response = verification / (action + "-result.json")
            write_json(request, {"action": action, "models_dir": str(moved / "Data/models/ic-light"), **parameters})
            started = time.monotonic()
            process = subprocess.run([str(moved / "AI/python.exe"), "-I", "-B", "-X", "utf8", str(moved / "app/portraitlab/relight.py"),
                                      "--request", str(request), "--result", str(response)], cwd=moved,
                                     env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            checked(verification / (action + "-worker.log")).write_text(process.stdout + process.stderr, encoding="utf-8")
            process.check_returncode()
            value = json.loads(response.read_text(encoding="utf-8"))
            if not value.get("ok"):
                raise RuntimeError(value)
            return {"result": value, "wall_seconds": time.monotonic() - started}
        result["ai_probe"] = ai_action("probe")
        if args.ai_generate:
            input_path, mask_path = verification / "输入图片.png", verification / "主体蒙版.png"
            Image.new("RGB", (256, 256), (100, 120, 140)).save(checked(input_path))
            Image.new("L", (256, 256), 255).save(checked(mask_path))
            output_path = verification / "生成结果.png"
            result["ai_generation"] = ai_action("generate", input_path=str(input_path), mask_path=str(mask_path),
                output_path=str(output_path), width=256, height=256, steps=10, device="cpu", direction="left")
            with Image.open(output_path) as rendered:
                if rendered.size != (256, 256):
                    raise RuntimeError("AI output size differs from the requested size")
    launcher_env = env.copy()
    unexpected_cache = moved / "deliberately-wrong-inherited-cache"
    launcher_env["PORTRAITLAB_CACHE"] = str(unexpected_cache)
    launcher = subprocess.Popen([str(moved / "PortraitLab.exe"), "--headless", "--no-browser", "--port", str(args.port),
                                 "--cache", str(moved / "Cache")],
                                cwd=moved, env=launcher_env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = "http://127.0.0.1:" + str(args.port)
    token = None
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if launcher.poll() is not None:
                raise RuntimeError("Portable launcher exited before readiness; see relocated Data/logs")
            try:
                boot = call(base + "/api/bootstrap")
                if Path(boot["storage"]["data_dir"]).resolve() == (moved / "Data").resolve():
                    token = boot["token"]
                    break
            except Exception:
                pass
            time.sleep(.3)
        if not token:
            raise RuntimeError("Portable service did not become ready")
        result["version"] = boot["version"]
        result["models"] = boot["models"]
        if not args.core_only:
            api_ai = call(base + "/api/relight/status?probe=true", token)
            if not api_ai.get("runtime_ready") or not api_ai.get("ready") or not api_ai.get("hardware_checked"):
                raise RuntimeError("Packaged server could not start its bundled AI worker: " + str(api_ai))
            if not (moved / "Cache/ai").is_dir():
                raise RuntimeError("Server AI cache did not use the explicit --cache directory")
            result["api_ai"] = {key: api_ai.get(key) for key in ("runtime_ready", "ready", "hardware_checked", "cuda_available", "torch", "gpu_name")}
        if unexpected_cache.exists() or (moved / "Data/runtime-cache").exists() or not (moved / "Cache/temp").is_dir():
            raise RuntimeError("Server failed to honor the launcher's explicit --cache setting")
        result["cache_override_verified"] = True
        image = Image.new("RGB", (64, 48), (100, 120, 140))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        boundary = "portraitlab" + uuid.uuid4().hex
        raw = ("--" + boundary + '\r\nContent-Disposition: form-data; name="files"; filename="portable-smoke.png"\r\nContent-Type: image/png\r\n\r\n').encode() + payload.getvalue() + ("\r\n--" + boundary + "--\r\n").encode()
        imported = call(base + "/api/import", token, raw, "multipart/form-data; boundary=" + boundary)
        if imported.get("errors") or len(imported.get("photos", [])) != 1:
            raise RuntimeError(str(imported))
        photo_id = imported["photos"][0]["id"]
        preview = call(base + "/api/photos/" + photo_id + "/preview", token,
                       {"params": {"exposure": .25}, "strokes": []})
        result["preview_status"] = preview["photo"]["status"]
        result["face_count"] = preview["photo"]["face_count"]
        result["launcher_pid"] = launcher.pid
        call(base + "/api/shutdown", token, {})
        launcher.wait(timeout=25)
        result["launcher_exit_code"] = launcher.returncode
        try:
            call(base + "/api/bootstrap")
            raise RuntimeError("Service remained reachable after shutdown")
        except urllib.error.URLError:
            result["shutdown_verified"] = True
        result["ok"] = result["preview_status"] == "ready" and launcher.returncode == 0
        if not result["ok"]:
            raise RuntimeError(str(result))
    finally:
        if launcher.poll() is None:
            if token:
                try:
                    call(base + "/api/shutdown", token, {})
                    launcher.wait(timeout=20)
                except Exception:
                    pass
            if launcher.poll() is None:
                # Only the launcher created by this test is terminated; its Job Object owns its children.
                launcher.terminate()
                launcher.wait(timeout=10)
    write_json(args.report, result)
    print(json.dumps({"ok": result["ok"], "report": str(args.report), "relocated_root": str(moved)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
