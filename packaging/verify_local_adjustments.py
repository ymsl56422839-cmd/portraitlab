"""Exercise persistent local retouching through the real Windows executable.

Run with the tested package's runtime/python.exe -B. Supply an existing local
workspace as --fixtures-data; its portrait and two verified foundation models
are copied into a new --work directory on F:. Original Data is hashed before
and after, never opened by the service. CPU is mandatory; CUDA runs if present.
This verifies the packaged API, image pixels and persistence, not mouse gestures.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import time
import uuid


# Use the existing actual-EXE lifecycle helper even with embedded Python's
# isolated ._pth configuration. No source server is imported or launched.
_helper_path = Path(__file__).resolve().with_name("verify_lighting.py")
_spec = importlib.util.spec_from_file_location("portraitlab_package_verification", _helper_path)
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
Service, require, sha, contained = _helper.Service, _helper.require, _helper.sha, _helper.contained

PREVIEW_CONTRACT = "canonical-full-resolution-area-png-v1"


def checked_output(path, root):
    path = contained(path, root)
    require(path.drive.upper() == "F:", "All verification output must stay on F: " + str(path))
    return path


def write_json(path, value, root):
    checked_output(path, root).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def tree_hashes(root):
    """Include every existing file, not only state.json or imported originals."""
    return {str(path.relative_to(root)).replace("\\", "/"): {"size": path.stat().st_size, "sha256": sha(path)}
            for path in sorted(root.rglob("*")) if path.is_file()}


def prepare_fixtures(args, report):
    state = json.loads((args.fixtures_data / "state.json").read_text(encoding="utf-8"))
    if args.photo is None:
        records = [record for key, record in state["photos"].items()
                   if key not in state.get("photo_trash", {}) and record.get("face_count", 0)]
        require(records, "Fixture workspace needs an analyzed portrait with a detected face, or supply --photo")
        source_photo = contained(records[0]["original_path"], args.fixtures_data)
    else:
        source_photo = args.photo
    require(source_photo.is_file(), "Portrait fixture is missing")
    fixtures = checked_output(args.work / "fixtures", args.work)
    fixtures.mkdir()
    photo = checked_output(fixtures / ("portrait" + source_photo.suffix.lower()), args.work)
    shutil.copy2(source_photo, photo)
    require(sha(photo) == sha(source_photo), "Copied portrait hash differs")
    registry = json.loads((args.package / "app/models/manifest.json").read_text(encoding="utf-8"))
    selected = [record for record in registry["models"] if record["id"] in ("face_landmarker", "selfie_multiclass")]
    require(len(selected) == 2, "Foundation model manifest changed; review verification fixture selection")
    models = []
    for record in selected:
        relative = Path(record["id"]) / record["filename"]
        source = contained(args.fixtures_data / "models" / relative, args.fixtures_data)
        require(source.is_file() and sha(source) == record["sha256"], "Fixture model hash mismatch: " + str(relative))
        target = checked_output(args.work / "Data/models" / relative, args.work)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        require(sha(target) == record["sha256"], "Copied model hash mismatch")
        models.append({"file": str(relative), "sha256": record["sha256"]})
    report["fixtures"] = {"source_photo": str(source_photo), "copied_photo": str(photo),
                          "photo_sha256": sha(photo), "models": models}
    return photo


def verify(args, report, save_report):
    import numpy as np
    from PIL import Image

    fixture = prepare_fixtures(args, report)
    service = Service(args.package, args.work, args.port)
    report["steps"] = []
    last_response = None

    def step(name, **details):
        report["steps"].append({"name": name, **details})
        save_report()
        print(name, flush=True)

    def image(route, mode="RGB"):
        raw, content_type = service.request(route, raw=True)
        with Image.open(io.BytesIO(raw)) as opened:
            pixels = np.asarray(opened.convert(mode)).copy()
        return pixels, content_type

    def snapshot(photo):
        return {key: copy.deepcopy(photo.get(key)) for key in
                ("params", "strokes", "local_adjustment", "lighting", "lighting_geometry")}

    def preview(photo, label, edit):
        nonlocal last_response
        started = time.monotonic()
        last_response = service.request(f"/api/photos/{photo['id']}/preview", edit)
        saved = last_response["photo"]
        require(saved["status"] == "ready", label + ": preview did not finish")
        require(saved["preview_contract"] == PREVIEW_CONTRACT and not saved["preview_needs_refresh"],
                label + ": unexpected canonical preview contract")
        pixels, media = image(saved["preview_url"])
        require(media.startswith("image/png"), label + ": preview is not lossless PNG")
        step(label, seconds=round(time.monotonic() - started, 3), version=saved["version"],
             retouch_base=last_response["meta"].get("retouch_base"),
             processing_device=last_response["meta"].get("processing_device"),
             local_device=last_response["meta"].get("local_adjustment", {}).get("engine_meta", {}).get("processing_device"),
             acne=last_response["meta"].get("local_adjustment", {}).get("engine_meta", {}).get("acne"))
        return saved, pixels

    def export(photo, label, edge):
        target = checked_output(args.work / "exports" / label, args.work)
        job = service.request("/api/jobs", {"ids": [photo["id"]], "format": "png", "max_side": edge,
                                          "output_dir": str(target)})["job"]
        job = service.wait_job("/api/jobs/", job["id"], timeout=args.timeout)
        item = job["items"][0]
        require(item["status"] == "completed", label + ": export item failed")
        path = checked_output(item["path"], args.work)
        sidecar = json.loads(checked_output(item["sidecar"], args.work).read_text(encoding="utf-8"))
        require(sidecar["params"] == photo["params"] and sidecar["local_adjustment"] == photo["local_adjustment"],
                label + ": export lost global/local edits")
        with Image.open(path) as opened:
            require(opened.format == "PNG", label + ": export format differs")
            pixels = np.asarray(opened.convert("RGB")).copy()
        return pixels, sidecar

    def outside(shape, rect, source_shape=None):
        h, w = shape[:2]
        yy, xx = np.ogrid[:h, :w]
        if source_shape is None or tuple(source_shape[:2]) == (h, w):
            # Source-sized output uses exactly the engine's pixel-centre rule.
            return ((xx + .5 < rect["x"] * w) | (xx + .5 >= (rect["x"] + rect["width"]) * w)
                    | (yy + .5 < rect["y"] * h) | (yy + .5 >= (rect["y"] + rect["height"]) * h))
        # At smaller sizes a sample can average both sides of the boundary.
        # Compare only samples whose entire source footprint is outside it.
        return ((xx + 1 <= rect["x"] * w) | (xx >= (rect["x"] + rect["width"]) * w)
                | (yy + 1 <= rect["y"] * h) | (yy >= (rect["y"] + rect["height"]) * h))

    def check_local(photo, pixels, global_preview, global_full, label, expect_change=True):
        local = photo["local_adjustment"]
        require(local is not None, label + ": local adjustment disappeared")
        exterior = outside(pixels.shape, local["rect"], global_full.shape)
        require(np.array_equal(pixels[exterior], global_preview[exterior]), label + ": preview exterior changed")
        same, _ = export(photo, label + "-preview-size", max(pixels.shape[:2]))
        require(np.array_equal(same, pixels), label + ": same-size PNG differs from preview")
        full, sidecar = export(photo, label + "-full-size", 0)
        require(full.shape == global_full.shape, label + ": full dimensions changed")
        exterior_full = outside(full.shape, local["rect"])
        require(np.array_equal(full[exterior_full], global_full[exterior_full]), label + ": full-size exterior changed")
        changes = int(np.any(full != global_full, axis=2).sum())
        if expect_change:
            require(changes > 0, label + ": local edit changed no pixels")
        require(photo.get("local_mask_url"), label + ": local skin mask URL missing")
        mask, media = image(photo["local_mask_url"], "L")
        require(media.startswith("image/png") and mask.shape == pixels.shape[:2], label + ": local mask format/size differs")
        require(not np.any(mask[outside(mask.shape, local["rect"])]), label + ": local mask spills outside the rectangle")
        require(np.any(mask), label + ": rectangle has no eligible skin; choose a centred portrait fixture")
        step(label + "-verified", same_size_png_exact=True, full_size_exterior_exact=True,
             preview_exterior_exact=True, changed_full_pixels=changes, local_mask_pixels=int(np.count_nonzero(mask)),
             dimensions=[full.shape[1], full.shape[0]], sidecar_local_preserved=True)
        return full, sidecar

    try:
        boot = service.start()
        require(boot.get("capabilities", {}).get("local_adjustments") is True,
                "Executable is serving an old app; stage the patch and close old windows before this verification")
        require(boot["capabilities"].get("canonical_preview_contract") == PREVIEW_CONTRACT,
                "Packaged preview contract does not match the patch")
        require("acne" in boot["defaults"] and boot["local_defaults"]["acne"] == 0,
                "Independent acne parameter or its neutral local default is missing")
        report["application_version"] = boot["version"]
        report["capabilities"] = boot["capabilities"]
        require(service.request("/api/hardware", {"device": "cpu"})["preference"] == "cpu", "CPU selection failed")
        boundary = "portraitlab" + uuid.uuid4().hex
        raw = (("--" + boundary + '\r\nContent-Disposition: form-data; name="files"; filename="portrait'
                + fixture.suffix + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
               + fixture.read_bytes() + ("\r\n--" + boundary + "--\r\n").encode())
        imported = service.request("/api/import", raw, content_type="multipart/form-data; boundary=" + boundary)
        require(not imported.get("errors") and len(imported["photos"]) == 1, "Fixture portrait import failed")
        photo = imported["photos"][0]
        params = {**boot["defaults"], "smooth": .12, "blemishes": .08, "even": .15, "texture": .04,
                  "acne": 0, "exposure": .18, "warmth": .08, "opacity": .8}
        photo, global_preview = preview(photo, "cpu-global", {"params": params, "strokes": [], "local_adjustment": None})
        require(photo["face_count"] > 0, "Fixture does not contain a detected face")
        require(last_response["meta"]["processing_device"]["actual_device"] == "cpu", "CPU baseline metadata differs")
        global_full, _ = export(photo, "cpu-global-full", 0)
        local = {"rect": {"x": .20, "y": .12, "width": .58, "height": .68},
                 "params": {**boot["local_defaults"], "exposure": .45, "acne": .6, "region": "skin"}}
        photo, edited_preview = preview(photo, "cpu-local", {"local_adjustment": local})
        expected_snapshot = snapshot(photo)
        cpu_full, _ = check_local(photo, edited_preview, global_preview, global_full, "cpu-local")
        nested_device = last_response["meta"]["local_adjustment"]["engine_meta"]["processing_device"]
        require(nested_device["actual_device"] == "cpu", "CPU local metadata differs")
        require(last_response["meta"]["processing_device"] == nested_device, "Outer metadata does not report the local processing pass")

        acne_only = copy.deepcopy(local)
        acne_only["params"]["exposure"] = 0
        photo, acne_preview = preview(photo, "cpu-acne-only", {"local_adjustment": acne_only})
        _, acne_sidecar = check_local(photo, acne_preview, global_preview, global_full, "cpu-acne-only", expect_change=False)
        acne_meta = acne_sidecar["engine_meta"]["local_adjustment"]["engine_meta"]["acne"]
        require(acne_meta["strength"] == .6, "Acne-only strength was not saved/rendered")
        step("cpu-acne-only-note", spots_found=acne_meta["spots_found"],
             note="A real portrait may contain no detected spots; synthetic acne-detection fixtures are covered by unit tests.")

        for label, rect in (("cpu-move", {"x": .30, "y": .08, "width": .58, "height": .68}),
                            ("cpu-resize", {"x": .34, "y": .22, "width": .30, "height": .42})):
            changed_local = {"rect": rect, "params": local["params"]}
            photo, pixels = preview(photo, label, {"local_adjustment": changed_local})
            check_local(photo, pixels, global_preview, global_full, label)
        photo, deleted = preview(photo, "cpu-delete", {"local_adjustment": None})
        require(photo["local_mask_url"] is None and np.array_equal(deleted, global_preview), "Deleting local edits did not restore the global preview")
        photo, restored = preview(photo, "cpu-restore", expected_snapshot)
        require(np.array_equal(restored, edited_preview), "Undo/restore did not reproduce saved pixels")
        photo, retained = preview(photo, "cpu-finish-selection", {})
        require(snapshot(photo) == expected_snapshot and np.array_equal(retained, edited_preview),
                "Leaving local editing erased or changed the persistent adjustment")
        service.stop()
        boot_after = service.start()
        require(boot_after["version"] == boot["version"], "Restart connected to a different application")
        reopened = next(item for item in service.request("/api/photos")["photos"] if item["id"] == photo["id"])
        require(snapshot(reopened) == expected_snapshot, "Restart lost global/local settings")
        pixels, _ = image(reopened["preview_url"])
        require(np.array_equal(pixels, edited_preview), "Restart changed saved preview pixels")
        reopened, rerendered = preview(reopened, "cpu-restart-render", {})
        require(np.array_equal(rerendered, edited_preview), "Restart rerender changed pixels")
        full, _ = export(reopened, "cpu-restart-full", 0)
        require(np.array_equal(full, cpu_full), "Restart full-size export changed pixels")
        step("cpu-restart-verified", preview_exact=True, full_export_exact=True)

        if args.gpu == "off":
            report["gpu"] = {"tested": False, "reason": "explicitly disabled"}
        else:
            hardware = service.request("/api/hardware", {"device": "auto"})
            if not hardware.get("classic_gpu_available"):
                report["gpu"] = {"tested": False, "reason": hardware.get("message"), "hardware": hardware}
                require(args.gpu != "require", "CUDA was required but the packaged runtime reports it unavailable")
            else:
                service.request("/api/hardware", {"device": "cuda"})
                reopened, gpu_global = preview(reopened, "cuda-global", {"local_adjustment": None})
                gpu_base, _ = export(reopened, "cuda-global-full", 0)
                reopened, gpu_local = preview(reopened, "cuda-local", {"local_adjustment": local})
                gpu_full, _ = check_local(reopened, gpu_local, gpu_global, gpu_base, "cuda-local")
                local_device = last_response["meta"]["local_adjustment"]["engine_meta"]["processing_device"]
                require(local_device["actual_device"] == "cuda", "CUDA local composition did not use CUDA")
                require(last_response["meta"]["processing_device"] == local_device, "Outer device metadata differs from the actual local pass")
                report["gpu"] = {"tested": True, "hardware": hardware, "local_device": local_device,
                                 "cpu_max_abs_difference": int(np.max(np.abs(gpu_full.astype(np.int16) - cpu_full.astype(np.int16))))}
        service.stop()
        report["shutdown_checks"] = service.shutdown_checks
        require(len(service.shutdown_checks) == 2 and all(item["launcher_exit_code"] == 0 and item["service_stopped"]
                                                        for item in service.shutdown_checks), "Both real-EXE shutdowns must be clean")
        report["cpu_passed"] = True
    finally:
        service.stop(strict=False)
        report["shutdown_checks"] = service.shutdown_checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path, help="New isolated directory on F:, outside all source Data and the package")
    parser.add_argument("--fixtures-data", required=True, type=Path, help="Existing workspace containing portrait records and verified models")
    parser.add_argument("--photo", type=Path, help="Optional explicit local portrait instead of the first detected-face fixture")
    parser.add_argument("--protect-data", action="append", type=Path, default=[], help="Additional existing Data directory to hash before and after")
    parser.add_argument("--port", type=int, default=18881)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--gpu", choices=("auto", "off", "require"), default="auto")
    args = parser.parse_args()
    for name in ("package", "work", "fixtures_data", "photo"):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).resolve())
    require(Path(sys.executable).resolve() == args.package / "runtime/python.exe", "Run with the tested package's runtime/python.exe -B")
    require((args.package / "PortraitLab.exe").is_file(), "Portable executable is missing")
    require(args.work.drive.upper() == "F:" and not args.work.exists(), "Work must be a new directory on F:")
    require(not args.work.is_relative_to(args.package) and not args.work.is_relative_to(args.fixtures_data), "Work must be outside package and source Data")
    require(1024 <= args.port <= 65515, "Invalid test port")
    protected = {args.fixtures_data, *(path.resolve() for path in args.protect_data)}
    if (args.package / "Data").is_dir():
        protected.add(args.package / "Data")
    require(all(path.is_dir() and not path.is_relative_to(args.work) and not args.work.is_relative_to(path) for path in protected),
            "Protected Data must exist and must not overlap test work")
    args.work.mkdir(parents=True)
    temp = checked_output(args.work / "temp", args.work)
    temp.mkdir()
    os.environ.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    before = {str(path): tree_hashes(path) for path in sorted(protected)}
    write_json(args.work / "original-data-before.json", before, args.work)
    report = {"ok": False, "package": str(args.package), "executable_sha256": sha(args.package / "PortraitLab.exe"),
              "verification": "actual portable EXE; isolated Data/Cache; no source-server replacement",
              "browser_visual_validation": False, "work": str(args.work), "gpu_mode": args.gpu}
    def save_report():
        write_json(args.work / "verification.json", report, args.work)
    try:
        verify(args, report, save_report)
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        after = {str(path): tree_hashes(path) for path in sorted(protected)}
        write_json(args.work / "original-data-after.json", after, args.work)
        report["original_data_unchanged"] = before == after
        if before != after:
            report["ok"] = False
            report["original_data_error"] = "Protected original Data hash inventory changed during verification"
        save_report()
    require(report["original_data_unchanged"], report.get("original_data_error", "Original Data changed"))
    print(json.dumps({"ok": True, "report": str(args.work / "verification.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
