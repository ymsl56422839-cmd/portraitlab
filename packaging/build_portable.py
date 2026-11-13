"""Build self-contained Windows runtimes and the native PortraitLab launcher.

The build never treats a virtual environment itself as a distributable runtime:
only its site-packages are copied into a clean official embedded CPython.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.10"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# SHA-256 published in python.org's Sigstore messageSignature.messageDigest.
PYTHON_SHA256 = base64.b64decode("SsvtbdHHRLA3bjsc9XzpBvncnpXmiCRYTICZpjAlo8M=").hex()


def checked(path):
    path = Path(path).expanduser().resolve()
    boundary = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if boundary and not path.is_relative_to(Path(boundary).expanduser().resolve()):
        raise ValueError(f"Destination outside PORTRAITLAB_STORAGE_ROOT: {path}")
    return path


def mkdir(path):
    path = checked(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path, value):
    checked(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def prepare_generated(directory, destination):
    directory, destination = checked(directory), checked(destination)
    if directory.parent != destination or directory.name not in ("runtime", "AI", "app"):
        raise ValueError("Refusing to reset any directory other than generated runtime/AI/app")
    marker = directory / ".portraitlab-generated"
    if directory.exists():
        if not marker.exists():
            raise ValueError(f"Existing directory is not marked as build-generated: {directory}")
        shutil.rmtree(checked(directory))
    mkdir(directory)
    checked(marker).write_text("PortraitLab generated distribution component\n", encoding="utf-8")


def embedded_archive(work):
    path = checked(mkdir(work / "downloads") / f"python-{PYTHON_VERSION}-embed-amd64.zip")
    if not path.exists() or sha(path) != PYTHON_SHA256:
        temporary = checked(path.with_suffix(".part"))
        with urllib.request.urlopen(PYTHON_URL, timeout=60) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target, 1024 * 1024)
        if sha(temporary) != PYTHON_SHA256:
            temporary.unlink()
            raise ValueError("Embedded Python checksum does not match the official release digest")
        os.replace(temporary, checked(path))
    return path


def extract_verified(archive, destination):
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = checked(destination / member.filename)
            if not target.is_relative_to(destination):
                raise ValueError("Archive path traversal")
            if member.is_dir():
                mkdir(target)
            else:
                mkdir(target.parent)
                with zipped.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def copy_tree(source, target, ignore=None):
    target = checked(target)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def runtime(source_env, target, destination, archive):
    source_env = Path(source_env).resolve()
    packages = source_env / "Lib/site-packages"
    if not packages.is_dir():
        raise ValueError(f"No site-packages found: {packages}")
    prepare_generated(target, destination)
    extract_verified(archive, target)
    installed = mkdir(target / "Lib/site-packages")
    copy_tree(packages, installed, shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    # ._pth isolates this runtime from global Python installations and the old venv.
    checked(target / "python312._pth").write_text(
        "python312.zip\n.\nLib/site-packages\n../app\nimport site\n", encoding="utf-8")
    write_json(target / "runtime-manifest.json", {
        "python": PYTHON_VERSION, "architecture": "win-amd64", "source_url": PYTHON_URL,
        "sha256": PYTHON_SHA256, "isolation": "python312._pth; no pyvenv.cfg or external base interpreter",
    })
    licenses = collect_licenses(installed, destination / "Licenses" / target.name)
    py_license = target / "LICENSE.txt"
    if py_license.exists():
        shutil.copy2(py_license, checked(mkdir(destination / "Licenses/Python") / "LICENSE.txt"))
    print(json.dumps({"component": target.name, "distributions": len(licenses),
                      "missing_recorded_license_text": [p["name"] for p in licenses if not p["license_files"] and not p.get("supplemental_license_files")]}), flush=True)


def collect_licenses(packages, output):
    mkdir(output)
    inventory = []
    for dist in sorted(importlib.metadata.distributions(path=[str(packages)]), key=lambda d: d.metadata.get("Name", "")):
        name, version = dist.metadata.get("Name", "unknown"), dist.version
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name + "-" + version)
        record = {"name": name, "version": version, "license_expression": dist.metadata.get("License-Expression"),
                  "license_field": dist.metadata.get("License"), "license_files": []}
        for item in dist.files or []:
            if not re.search(r"license|licence|copying|notice|copyright|authors", Path(str(item)).name, re.I):
                continue
            original = Path(dist.locate_file(item)).resolve()
            if not original.is_file() or not original.is_relative_to(packages):
                continue
            relative = original.relative_to(packages)
            target = checked(output / safe / relative)
            mkdir(target.parent)
            shutil.copy2(original, target)
            record["license_files"].append(str(relative).replace("\\", "/"))
        supplement = PROJECT / "licenses/runtime-extra/provenance.json"
        if supplement.is_file():
            details = json.loads(supplement.read_text(encoding="utf-8-sig"))
            record["supplemental_license_files"] = []
            for item in details.get("files", []):
                if item.get("package", "").lower() == name.lower() and item.get("version") == version:
                    original = supplement.parent / item["file"]
                    if sha(original) != item["sha256"]:
                        raise ValueError("Supplemental license digest mismatch: " + str(original))
                    supplement_output = mkdir(output.parent / "runtime-extra")
                    shutil.copy2(original, checked(supplement_output / original.name))
                    shutil.copy2(supplement, checked(supplement_output / supplement.name))
                    record["supplemental_license_files"].append("../runtime-extra/" + original.name)
        inventory.append(record)
    write_json(output / "inventory.json", inventory)
    return inventory


def app_source(destination):
    target = destination / "app"
    prepare_generated(target, destination)
    ignored = shutil.ignore_patterns(".git", ".local", ".venv", "venv", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".ruff_cache", ".vscode", "work", "runtime", "cache", "logs", "state.json", "*.zip", "*.log", "*.tmp", "*.part", "*.safetensors", "*.ckpt", "*.pt", "*.onnx", "*.tflite", "*.task")
    # Deliberate source-only allowlist: never sweep a developer's arbitrary root files.
    source_directories = ("portraitlab", "web", "tools", "packaging", "licenses", "docs", "tests")
    source_files = (".gitattributes", ".gitignore", "Launch.cmd", "Launch.ps1", "setup.ps1", "LICENSE", "NOTICE", "README.md", "THIRD_PARTY_NOTICES.md", "MODEL_LICENSES.md", "REPRODUCING.md", "pytest.ini", "requirements.txt", "requirements.lock.txt", "requirements-dev.txt", "requirements-relight.txt", "requirements-relight.lock.txt")
    for name in source_directories:
        if (PROJECT / name).is_dir():
            copy_tree(PROJECT / name, target / name, ignored)
    for name in source_files:
        if (PROJECT / name).is_file():
            shutil.copy2(PROJECT / name, checked(target / name))
    mkdir(target / "models")
    for name in ("manifest.json", "relight-manifest.json"):
        if (PROJECT / "models" / name).is_file():
            shutil.copy2(PROJECT / "models" / name, checked(target / "models" / name))
    write_json(destination / "source-manifest.json", {str(p.relative_to(target)).replace("\\", "/"): sha(p) for p in sorted(target.rglob("*")) if p.is_file()})
    for filename in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "MODEL_LICENSES.md", "REPRODUCING.md"):
        source = PROJECT / filename
        if source.is_file():
            shutil.copy2(source, checked(destination / filename))
    portable_readme = PROJECT / "packaging/PORTABLE-README.txt"
    if portable_readme.is_file():
        shutil.copy2(portable_readme, checked(destination / "PORTABLE-README.txt"))
    if (PROJECT / "licenses").is_dir():
        copy_tree(PROJECT / "licenses", mkdir(destination / "Licenses/Models"), shutil.ignore_patterns("runtime-extra"))
    if (PROJECT / "licenses/runtime-extra").is_dir():
        copy_tree(PROJECT / "licenses/runtime-extra", mkdir(destination / "Licenses/runtime-extra"))
    for component in ("runtime", "AI"):
        packages = destination / component / "Lib/site-packages"
        if packages.is_dir():
            collect_licenses(packages, destination / "Licenses" / component)


def launcher(destination, work):
    compiler = None
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    for folder in ("Framework64", "Framework"):
        candidate = windir / f"Microsoft.NET/{folder}/v4.0.30319/csc.exe"
        if candidate.is_file():
            compiler = candidate
            break
    if compiler is None:
        raise RuntimeError("Windows .NET Framework C# compiler is required to build the launcher")
    manifest = checked(work / "PortraitLab.manifest")
    manifest.write_text('''<?xml version="1.0" encoding="utf-8"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
 <assemblyIdentity version="0.2.0.0" name="PortraitLab.Launcher"/>
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings><longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware><dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware></windowsSettings></application>
</assembly>''', encoding="utf-8")
    executable = checked(destination / "PortraitLab.exe")
    args = [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001",
            "/out:" + str(executable), "/win32manifest:" + str(manifest),
            "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll", str(PROJECT / "packaging/Launcher.cs")]
    subprocess.run(args, cwd=PROJECT, check=True, env=os.environ.copy())
    print(json.dumps({"launcher": str(executable), "bytes": executable.stat().st_size}), flush=True)


def copy_models(source, destination):
    source = Path(source).resolve()
    target = mkdir(destination / "Data/models")
    for folder in ("face_landmarker", "selfie_multiclass", "ic-light"):
        if (source / folder).is_dir():
            copy_tree(source / folder, target / folder, shutil.ignore_patterns(".cache", "*.lock", "*.part"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--core-env", type=Path)
    parser.add_argument("--ai-env", type=Path)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--stage", choices=("all", "core", "ai", "app", "launcher", "models"), default="all")
    args = parser.parse_args()
    destination, work = mkdir(args.destination), mkdir(args.work)
    os.environ["TEMP"] = os.environ["TMP"] = str(mkdir(work / "temp"))
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.stage in ("all", "core"):
        if not args.core_env:
            parser.error("--core-env is required for core/all builds")
        runtime(args.core_env, destination / "runtime", destination, embedded_archive(work))
    if args.stage in ("all", "ai") and args.ai_env:
        runtime(args.ai_env, destination / "AI", destination, embedded_archive(work))
    if args.stage in ("all", "app"):
        app_source(destination)
    if args.stage in ("all", "launcher"):
        launcher(destination, work)
    if args.models and args.stage in ("all", "models"):
        copy_models(args.models, destination)
    write_json(destination / "portable-build.json", {"application": "PortraitLab", "version": "0.2.0", "platform": "Windows x64",
        "entry": "PortraitLab.exe", "python": PYTHON_VERSION, "python_source": PYTHON_URL, "python_sha256": PYTHON_SHA256,
        "ai_runtime_present": (destination / "AI/python.exe").is_file(), "core_runtime_present": (destination / "runtime/python.exe").is_file(),
        "license_policy": "Application source is AGPL-3.0; runtime dependencies and model weights retain their separate licenses."})
    print(json.dumps({"destination": str(destination), "stage": args.stage}), flush=True)


if __name__ == "__main__":
    main()
