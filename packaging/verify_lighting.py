"""Verify interactive lighting through an actual portable Windows package.

Run with the tested package's runtime/python.exe. Models and a local portrait
are supplied separately; they are copied into a new, isolated work directory.
No browser, downloads, user Data edits, or IC-Light models are needed.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid


CONTRACT = "canonical-full-resolution-retouch-v2-edit-tools"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def contained(path, root):
    path = Path(path).resolve()
    require(path.is_relative_to(root), "Verification path escaped its work directory: " + str(path))
    return path


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


class Service:
    def __init__(self, package, work, port):
        self.package, self.work, self.port = package, work, port
        self.data, self.cache = work / "Data", work / "Cache"
        self.base = "http://127.0.0.1:" + str(port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.process = self.token = None
        self.shutdown_checks = []

    def request(self, route, data=None, raw=False, content_type="application/json"):
        headers = {}
        if self.token:
            headers["X-PortraitLab-Token"] = self.token
        if data is not None:
            headers["Content-Type"] = content_type
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + route, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=180) as response:
                body = response.read()
                return (body, response.headers.get("Content-Type", "")) if raw else json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{route}: HTTP {error.code}: {detail}") from error

    def start(self):
        require(not listening(self.port), "Verification port is already in use; choose another --port")
        env = os.environ.copy()
        for key in ("PYTHONHOME", "PYTHONPATH", "PORTRAITLAB_RELIGHT_MODELS"):
            env.pop(key, None)
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                   PORTRAITLAB_DATA=str(self.data), PORTRAITLAB_CACHE=str(self.cache),
                   PORTRAITLAB_AI_PYTHON=str(self.package / "AI/python.exe"),
                   PORTRAITLAB_PORT=str(self.port), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        for variable, folder in (("TEMP", "temp"), ("TMP", "temp"), ("HF_HOME", "huggingface"),
                                 ("TORCH_HOME", "torch"), ("MPLCONFIGDIR", "matplotlib"),
                                 ("CUDA_CACHE_PATH", "cuda"), ("TORCH_EXTENSIONS_DIR", "torch-extensions"),
                                 ("TORCHINDUCTOR_CACHE_DIR", "torch-inductor"),
                                 ("TRITON_CACHE_DIR", "triton"), ("XDG_CACHE_HOME", "xdg")):
            path = self.cache / folder
            path.mkdir(parents=True, exist_ok=True)
            env[variable] = str(path)
        command = [str(self.package / "PortraitLab.exe"), "--headless", "--no-browser",
                   "--data", str(self.data), "--cache", str(self.cache), "--port", str(self.port)]
        self.process = subprocess.Popen(command, cwd=self.work, env=env,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.token = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "Launcher exited before readiness; inspect isolated Data/logs")
            try:
                boot = self.request("/api/bootstrap")
            except (OSError, ValueError, RuntimeError):
                time.sleep(.2)
                continue
            require(Path(boot["storage"]["data_dir"]).resolve() == self.data.resolve(),
                    "Launcher connected to an unexpected workspace")
            self.token = boot["token"]
            return boot
        raise TimeoutError("Portable launcher did not become ready")

    def stop(self, strict=True):
        if self.process is None:
            return
        process = self.process
        try:
            if process.poll() is None and self.token:
                self.request("/api/shutdown", {})
            process.wait(timeout=30)
            result = {"launcher_exit_code": process.returncode, "service_stopped": not listening(self.port)}
            self.shutdown_checks.append(result)
            if strict:
                require(process.returncode == 0 and result["service_stopped"], "Portable service did not shut down cleanly")
        finally:
            if process.poll() is None:
                # This exact test-owned launcher owns its child processes through a Job Object.
                process.terminate()
                process.wait(timeout=15)
            self.process = self.token = None

    def wait_job(self, route, identifier, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.request(route + identifier)["job"]
            if job["status"] not in ("queued", "running", "cancelling"):
                require(job["status"] == "completed", "Job failed: " + json.dumps(job, ensure_ascii=False))
                return job
            time.sleep(.2)
        raise TimeoutError("Verification job timed out: " + identifier)


def copy_models(package, source, target):
    registry = json.loads((package / "app/models/manifest.json").read_text(encoding="utf-8"))
    lighting = json.loads((package / "app/models/lighting-manifest.json").read_text(encoding="utf-8"))
    expected = [(entry["id"] + "/" + entry["filename"], entry["sha256"])
                for entry in registry["models"] if entry["id"] in ("face_landmarker", "selfie_multiclass")]
    expected += [(entry["relative_path"], entry["sha256"]) for entry in lighting["files"]]
    require(len(expected) == 3, "Unexpected model registry; review this verification script")
    for name, digest in expected:
        original, destination = contained(source / name, source), contained(target / name, target)
        require(original.is_file() and sha(original) == digest, "Model checksum mismatch: " + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
        require(sha(destination) == digest, "Copied model checksum mismatch: " + name)
    return [{"file": name, "sha256": digest} for name, digest in expected]


def verify_device(args, device):
    import numpy as np
    from PIL import Image
    from portraitlab import lighting

    work = args.work / device
    work.mkdir()
    service = Service(args.package, work, args.port)
    result = {"device_requested": device, "work": str(work), "scopes": []}
    result["models"] = copy_models(args.package, args.models, work / "Data/models")

    def image_bytes(route):
        data, content_type = service.request(route, raw=True)
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).copy(), content_type

    def export(photo, side, scope):
        output = work / "exports" / (scope + "-" + str(side))
        job = service.request("/api/jobs", {"ids": [photo["id"]], "format": "png",
                                          "max_side": side, "output_dir": str(output)})["job"]
        job = service.wait_job("/api/jobs/", job["id"])
        item = job["items"][0]
        require(item["status"] == "completed", "Export item failed: " + str(item))
        path = contained(item["path"], work)
        with Image.open(path) as image:
            require(image.format == "PNG", "Export is not PNG")
            return np.asarray(image.convert("RGB")).copy()

    try:
        boot = service.start()
        result["version"] = boot["version"]
        status = service.request("/api/lighting/status")
        require(status["ready"] and status["runtime_ready"], "Packaged lighting runtime or verified models are unavailable")
        require(all(status.get("capabilities", {}).get(key) is True
                    for key in ("canonical_preview", "refresh_geometry", "geometry_restore")),
                "Packaged lighting API capabilities do not match the interface")
        boundary = "portraitlab" + uuid.uuid4().hex
        raw = (("--" + boundary + '\r\nContent-Disposition: form-data; name="files"; filename="verification-portrait'
                + args.photo.suffix.lower() + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
               + args.photo.read_bytes() + ("\r\n--" + boundary + "--\r\n").encode())
        imported = service.request("/api/import", raw, content_type="multipart/form-data; boundary=" + boundary)
        require(not imported.get("errors") and len(imported["photos"]) == 1, "Verification portrait import failed")
        photo = imported["photos"][0]
        params = {**photo["params"], "even": .42, "smooth": .12, "blemishes": .12,
                  "texture": .04, "opacity": .8, "region": "skin", "engine": "classic"}
        photo = service.request(f"/api/photos/{photo['id']}/preview", {"params": params, "strokes": []})["photo"]
        started = time.monotonic()
        prepared_job = service.request("/api/lighting/prepare", {"photo_id": photo["id"], "device": device,
                                                               "refresh_geometry": True})["job"]
        prepared = service.wait_job("/api/lighting/jobs/", prepared_job["id"])["result"]
        result["prepare_seconds"] = time.monotonic() - started
        require(prepared["base_contract"] == CONTRACT, "Unexpected lighting base contract")
        require(prepared["meta"]["analysis"]["actual_device"] == device, "Requested analysis device was not used")
        result["analysis"] = prepared["meta"]["analysis"]
        geometry = work / "Data/lighting/geometry" / prepared["geometry_key"]
        base_path = work / "Data/lighting/retouch-base" / photo["id"] / (prepared["base_key"] + ".png")
        with Image.open(base_path) as image:
            full_base = np.asarray(image.convert("RGB")).copy()
        base, _ = image_bytes(prepared["base_url"])
        normal, _ = image_bytes(prepared["normal_url"])
        depth = np.asarray(Image.open(geometry / "depth.png").convert("L")).copy()
        mask = np.asarray(Image.open(geometry / "mask.png").convert("L")).copy()
        width, height = full_base.shape[1], full_base.shape[0]
        require((width, height) == (photo["width"], photo["height"]), "Canonical base is not full resolution")
        selected = lighting._sample_rows(mask.astype(np.float32) / 255., width, height, 0, height)
        subject_pixels, background_pixels = int((selected == 1).sum()), int((selected == 0).sum())
        require(subject_pixels > 0 and background_pixels > 0,
                "Choose a portrait with a clear person and background for preservation checks")
        result["full_dimensions"] = [width, height]
        result["preview_dimensions"] = [base.shape[1], base.shape[0]]
        result["solid_subject_pixels"], result["solid_background_pixels"] = subject_pixels, background_pixels
        previous, previews = {}, {}
        for scope in ("subject", "background", "all"):
            previous[scope] = copy.deepcopy(photo)
            config = {**copy.deepcopy(status["defaults"]), "enabled": True, "scope": scope,
                      "strength": .75, "ambient": .6,
                      "lights": [{**copy.deepcopy(status["light_defaults"]), "type": "directional",
                                  "azimuth": -140, "elevation": 35, "temperature": 3800, "intensity": .9}]}
            photo = service.request(f"/api/photos/{photo['id']}/lighting",
                                    {"lighting": config, "expected_version": photo["version"],
                                     "geometry_key": prepared["geometry_key"]})["photo"]
            preview, content_type = image_bytes(photo["preview_url"])
            require(content_type.startswith("image/png"), "Saved lighting preview is not served as lossless PNG")
            expected = lighting.render_lighting(base, normal, depth, mask, config)
            require(np.array_equal(preview, expected), "Saved preview differs from the prepared canonical base")
            small = export(photo, max(base.shape[:2]), scope)
            require(np.array_equal(small, preview), "Same-size PNG export differs from the saved preview")
            full = export(photo, 0, scope)
            require(full.shape == full_base.shape, "Full export dimensions changed")
            untouched = selected == 0 if scope == "subject" else selected == 1 if scope == "background" else None
            preserved = None if untouched is None else int(untouched.sum())
            if untouched is not None:
                require(np.array_equal(full[untouched], full_base[untouched]), "Unselected pixels changed in " + scope)
            changed = int(np.any(full != full_base, axis=2).sum())
            require(changed > 0, "Lighting made no pixel changes in " + scope)
            previews[scope] = preview
            result["scopes"].append({"scope": scope, "saved_preview_exact": True,
                                     "same_size_png_exact": True, "preserved_pixels": preserved,
                                     "changed_pixels": changed, "full_dimensions": [full.shape[1], full.shape[0]]})
        prior = previous["all"]
        photo = service.request(f"/api/photos/{photo['id']}/preview",
                                {key: prior[key] for key in ("params", "strokes", "lighting", "lighting_geometry")})["photo"]
        undo_preview, _ = image_bytes(photo["preview_url"])
        require(np.array_equal(undo_preview, previews["background"]), "Undo did not restore the previous lighting result")
        result["undo_exact"] = True
        before_geometry = {path.name: (path.stat().st_mtime_ns, sha(path)) for path in geometry.iterdir() if path.is_file()}
        service.stop()
        service.start()
        reopened = next(item for item in service.request("/api/photos")["photos"] if item["id"] == photo["id"])
        require(reopened["lighting"] == photo["lighting"] and reopened["lighting_geometry"] == photo["lighting_geometry"],
                "Restart did not preserve lighting and geometry")
        reopened_pixels, _ = image_bytes(reopened["preview_url"])
        require(np.array_equal(reopened_pixels, undo_preview), "Restart changed the saved preview")
        again = service.request("/api/lighting/prepare", {"photo_id": photo["id"], "device": device})["job"]
        again = service.wait_job("/api/lighting/jobs/", again["id"])["result"]
        after_geometry = {path.name: (path.stat().st_mtime_ns, sha(path)) for path in geometry.iterdir() if path.is_file()}
        require(again["geometry_key"] == photo["lighting_geometry"] and before_geometry == after_geometry,
                "Reopening unexpectedly recomputed saved geometry")
        result["restart_exact"] = result["cached_geometry_reused"] = True
        service.stop()
        result["shutdown"] = service.shutdown_checks
        result["ok"] = True
        return result
    finally:
        service.stop(strict=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path, help="New, empty verification directory outside the package")
    parser.add_argument("--photo", required=True, type=Path, help="Local portrait with a visible person and background")
    parser.add_argument("--models", required=True, type=Path, help="Existing model root; all files are verified before copying")
    parser.add_argument("--port", default=18876, type=int)
    parser.add_argument("--devices", nargs="+", choices=("cpu", "cuda"), default=["cpu"])
    args = parser.parse_args()
    for key in ("package", "work", "photo", "models"):
        setattr(args, key, getattr(args, key).resolve())
    require(Path(sys.executable).resolve() == args.package / "runtime/python.exe",
            "Run this script with the tested package's runtime/python.exe")
    require(not args.work.is_relative_to(args.package), "Verification work must be outside the distributable package")
    require(not args.work.exists(), "Verification directory must not already exist")
    require(1024 <= args.port <= 65515, "Invalid verification port")
    require(args.photo.is_file(), "Missing local verification portrait")
    require((args.package / "AI/python.exe").is_file(), "The complete AI runtime is required")
    boundary = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if boundary:
        require(args.work.is_relative_to(Path(boundary).resolve()), "Work must stay inside PORTRAITLAB_STORAGE_ROOT")
    args.work.mkdir(parents=True)
    temp = args.work / "temp"
    temp.mkdir()
    os.environ["TEMP"] = os.environ["TMP"] = str(temp)
    sys.path.insert(0, str(args.package / "app"))
    report = {"package": str(args.package), "verification": "actual portable launcher and isolated lighting API",
              "browser_visual_validation": False, "devices": [], "ok": False}
    try:
        for device in dict.fromkeys(args.devices):
            print("Verifying interactive lighting on " + device, flush=True)
            report["devices"].append(verify_device(args, device))
            write_json(args.work / "verification.json", report)
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_json(args.work / "verification.json", report)
    print(json.dumps({"ok": True, "report": str(args.work / "verification.json")}), flush=True)


if __name__ == "__main__":
    main()
