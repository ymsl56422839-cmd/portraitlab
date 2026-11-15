"""Build the offline V2 Windows share folder from reviewed local components.

No downloads, user data, environment cloning, or destination replacement.
Use `build --plan` before `build`; `verify` checks every immutable payload file.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = "distribution-manifest.json"
VERSION = "0.7.0"
CORE_MODULES = ("__init__", "engine", "face_shape", "lighting", "lighting_geometry")
V2_MODULES = ("__init__", "geometry_v2", "lighting_v2", "manual_repair", "model_catalog",
              "neural_candidate", "persistent_worker", "render_proxy", "retouch_engine",
              "server", "skin_model", "workspace")
RUNTIME_PACKAGES = frozenset("""absl-py annotated-doc annotated-types anyio attrs certifi cffi
click colorama contourpy cycler fastapi flatbuffers fonttools h11 httpcore httpx idna jax
jaxlib kiwisolver matplotlib mediapipe ml-dtypes numpy opencv-contrib-python opentelemetry-api
opt-einsum packaging pillow protobuf pycparser pydantic pydantic-core pyparsing python-dateutil
python-multipart scipy sentencepiece sounddevice starlette typing-extensions typing-inspection
tzdata uvicorn""".split())
AI_PACKAGES = frozenset("""accelerate certifi charset-normalizer colorama diffusers filelock
fsspec huggingface-hub idna importlib-metadata jinja2 lazy-importer markupsafe mpmath networkx
numpy packaging pillow psutil pyyaml regex requests safetensors setuptools sympy tokenizers
torch tqdm transformers typing-extensions urllib3 zipp""".split())
EMBEDDED_FILES = frozenset("""_asyncio.pyd _bz2.pyd _ctypes.pyd _decimal.pyd _elementtree.pyd
_hashlib.pyd _lzma.pyd _msi.pyd _multiprocessing.pyd _overlapped.pyd _queue.pyd _socket.pyd
_sqlite3.pyd _ssl.pyd _uuid.pyd _wmi.pyd _zoneinfo.pyd libcrypto-3.dll libffi-8.dll libssl-3.dll
LICENSE.txt pyexpat.pyd python.cat python.exe python3.dll python312.dll python312.zip pythonw.exe
select.pyd sqlite3.dll unicodedata.pyd vcruntime140_1.dll vcruntime140.dll winsound.pyd""".split())
LAUNCHER_FILES = ("PortraitLab.exe", "Microsoft.Web.WebView2.Core.dll",
                  "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll")
REQUIRED_TERMS = ("WebView2Runtime/LICENSE.txt", "MicrosoftVC/LICENSE.txt",
                  "NVIDIA/CUDA-12.8-EULA.txt", "NVIDIA/cuDNN-9.7-License.txt", "THIRD_PARTY_NOTICES.md")
MODEL_FILES = (
    ("analysis", "face_landmarker/face_landmarker.task", "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
    ("analysis", "selfie_multiclass/selfie_multiclass_256x256.tflite", "c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0"),
    ("analysis", "lighting/depth_anything_v2_vits.pth", "715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378"),
    ("neural", "joint_20210926.pth", "c9e1388b19d9334c90db08308c86de208f535920ee381ae8a9cb63484a78351a"),
    ("neural", "pytorch_model.pt", "760d0c0b95c4e0a692165677fe2b82dbdf8fbbd4a3780ca966599c627b082993"),
)
VENDOR_HASHES = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "weights_init.py": "5feb5da62e7a97cca5e60488c96d76c48074ca7521526dca0a91498880381e5b",
    "detection_model/detection_module.py": "23fce16544465ef41232ce652fecc95a71db3bb81af575d4aefc472a4e45a2ec",
    "detection_model/detection_unet_in.py": "e3b02ff03941aabc967c0fa66c39a00a56e84fbbe98f15c52de6bdf5555a9168",
    "inpainting_model/gconv.py": "fca8ece60d31dabf3d6eda186657efd72328f94aafe7b11fe130259ee873bd20",
    "inpainting_model/inpainting_unet.py": "bc151c88dabac96ef2a17f31c6dc979688d65c4f0f0f8dde73e1c2e845f462dd",
    "unet_deploy.py": "5fa3b9a4ba599908025057d64fe3b44ad71f6bb8614c8d13618a4cd5bbe7936b",
}
PTH = "python312.zip\n.\nLib/site-packages\n../app\nimport site\n"
PRIVATE_PARTS = frozenset(("__pycache__", ".git", ".pytest_cache", "browser-profile", "uploads",
                           "imports", "exports", "logs", "photos", "sessions"))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_output(path):
    path = Path(path).expanduser().resolve()
    boundary = os.environ.get("PORTRAITLAB_STORAGE_ROOT")
    if boundary and not path.is_relative_to(Path(boundary).expanduser().resolve()):
        raise ValueError("Output is outside PORTRAITLAB_STORAGE_ROOT")
    return path


def relative_name(value):
    value = str(value).replace("\\", "/")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or any(x in ("", ".", "..") for x in value.split("/"))
            or any(":" in x for x in path.parts)):
        raise ValueError("Unsafe relative path: " + value)
    return value


def safe_source(root, relative):
    root = Path(root).resolve()
    path = root / relative_name(relative)
    for node in (path, *path.parents):
        if node == root:
            break
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise ValueError("Linked source entries are not distributable")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError("Missing or external source file: " + str(relative))
    return path


def excluded(relative):
    p = PurePosixPath(str(relative).replace("\\", "/"))
    return (any(x.lower() in PRIVATE_PARTS for x in p.parts)
            or p.name.lower() in ("direct_url.json", "state.json", "model-receipts.json")
            or p.suffix.lower() in (".pyc", ".pyo", ".log", ".tmp", ".part", ".whl", ".db"))


@dataclass(frozen=True)
class Entry:
    source: Path | None
    target: str
    component: str
    expected: str | None = None
    content: bytes | None = None

    @property
    def size(self):
        return self.source.stat().st_size if self.source else len(self.content or b"")


class Plan:
    def __init__(self):
        self.entries = {}

    def add(self, root, relative, target, component, expected=None):
        entry = Entry(safe_source(root, relative), relative_name(target), component, expected)
        self._insert(entry)

    def text(self, target, text, component):
        self._insert(Entry(None, relative_name(target), component, content=text.encode("utf-8")))

    def _insert(self, entry):
        key = entry.target.casefold()
        previous = self.entries.get(key)
        if previous and previous != entry:
            if (previous.component == entry.component == "licenses" and previous.source and entry.source
                    and sha256(previous.source) == sha256(entry.source)):
                return
            raise ValueError("Duplicate output path: " + entry.target)
        self.entries[key] = entry

    def tree(self, root, target, component, allowed_suffixes=None):
        root = Path(root).resolve()
        if not root.is_dir():
            raise ValueError("Missing component directory: " + str(root))
        for source in sorted(root.rglob("*")):
            if source.is_file():
                relative = source.relative_to(root).as_posix()
                if not excluded(relative) and (allowed_suffixes is None or source.suffix.lower() in allowed_suffixes
                                               or source.name in ("LICENSE", "COPYING", "NOTICE")):
                    self.add(root, relative, target + "/" + relative, component)

    def summary(self):
        components = {}
        for entry in self.entries.values():
            value = components.setdefault(entry.component, {"files": 0, "bytes": 0})
            value["files"] += 1
            value["bytes"] += entry.size
        return {"version": VERSION, "files": len(self.entries), "bytes": sum(e.size for e in self.entries.values()),
                "components": components}


def vc_runtime_files(root):
    if root is None:
        return {}
    root = Path(root).resolve()
    names = {p.name: p for p in root.iterdir() if p.is_file() and re.fullmatch(
        r"(?:msvcp140[a-z0-9_]*|vcruntime140[a-z0-9_]*|concrt140|vcomp140|vccorlib140|vcamp140)\.dll", p.name, re.I)}
    if not {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}.issubset(names):
        raise ValueError("App-local VC runtime must include msvcp140 and vcruntime140/_1")
    return names


def runtime_entries(plan, donor, component, allowed, vc_runtime=None):
    donor = Path(donor).resolve()
    vc_files = vc_runtime_files(vc_runtime)
    for name in sorted(EMBEDDED_FILES):
        if name in vc_files:
            continue
        plan.add(donor, name, component + "/" + name, component)
    for name, source in sorted(vc_files.items()):
        plan.add(source.parent, name, component + "/" + name, component)
    # Rebuild, never inherit an embedded interpreter's search paths or executable .pth files.
    plan.text(component + "/python312._pth", PTH, component)
    site = donor / "Lib/site-packages"
    found = set()
    for dist in importlib.metadata.distributions(path=[str(site)]):
        name = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
        if name not in allowed:
            continue
        if name in found:
            raise ValueError("Duplicate installed distribution: " + name)
        found.add(name)
        if not dist.files:
            raise ValueError("Distribution has no RECORD: " + name)
        for file in dist.files:
            raw = str(file).replace("\\", "/")
            if raw.startswith("../") or excluded(raw) or raw.endswith(".pth"):
                continue  # Console installers and executable search-path hooks are not runtime assets.
            relative = relative_name(raw)
            plan.add(site, relative, f"{component}/Lib/site-packages/{relative}", component)
    missing = allowed - found
    if missing:
        raise ValueError("Missing runtime distributions: " + ", ".join(sorted(missing)))


def source_entries(plan, repo):
    repo = Path(repo)
    for module in CORE_MODULES:
        relative = f"portraitlab/{module}.py"
        plan.add(repo, relative, "app/" + relative, "source")
    plan.tree(repo / "portraitlab/vendor", "app/portraitlab/vendor", "source", {".py", ".yaml", ".txt"})
    for module in V2_MODULES:
        relative = f"experiments/portraitlab_v2/{module}.py"
        plan.add(repo, relative, "app/" + relative, "source")
    for name in ("app.js", "index.html", "style.css", "workspace.js"):
        relative = "experiments/portraitlab_v2/web/" + name
        plan.add(repo, relative, "app/" + relative, "source")
    for relative in ("experiments/portraitlab_v2/presets_v2.json", "models/lighting-manifest.json"):
        plan.add(repo, relative, "app/" + relative, "source")
    for name in ("build_v2_share.py", "V2Launcher.cs", "v2_build_launcher.py", "webview2.py", "webview2.lock.json"):
        plan.add(repo, "packaging/" + name, "app/packaging/" + name, "source")
    plan.add(repo, "LICENSE", "LICENSE", "licenses")
    plan.add(repo, "LICENSE", "app/LICENSE", "licenses")
    for name in ("LICENSE.txt", "NOTICE.txt"):
        plan.add(repo, "licenses/webview2/" + name, "app/licenses/webview2/" + name, "licenses")


def make_plan(args):
    plan = Plan()
    version_source = (args.repo / "portraitlab/__init__.py").read_text(encoding="utf-8")
    if not re.search(r'^__version__\s*=\s*"' + re.escape(VERSION) + r'"\s*$', version_source, re.M):
        raise ValueError("Application and distribution versions differ")
    source_entries(plan, args.repo)
    if args.vc_runtime_root is None:
        raise ValueError("Offline share releases require --vc-runtime-root")
    runtime_entries(plan, args.donor / "runtime", "runtime", RUNTIME_PACKAGES, args.vc_runtime_root)
    runtime_entries(plan, args.donor / "AI", "AI", AI_PACKAGES, args.vc_runtime_root)
    for group, relative, digest in MODEL_FILES:
        root = args.analysis_models if group == "analysis" else args.neural_models
        plan.add(root, relative, f"Data/models/{group}/{relative}", "models", digest)
    for relative, digest in VENDOR_HASHES.items():
        path = relative if relative == "LICENSE" else "modelscope/models/cv/skin_retouching/" + relative
        plan.add(args.vendor, path, "vendor/ModelScope/" + path, "models", digest)
    for name in LAUNCHER_FILES:
        lock = json.loads((args.repo / "packaging/webview2.lock.json").read_text(encoding="utf-8"))
        pinned = next((x["sha256"] for x in lock["files"] if x["destination"] == name), None)
        plan.add(args.launcher_dir, name, name, "launcher", pinned)
    plan.add(args.launcher_dir, "webview2-manifest.json", "webview2-manifest.json", "launcher")
    plan.tree(args.launcher_dir / "Licenses/WebView2", "Licenses/WebView2", "licenses", {".txt", ".json"})
    if not (args.webview2 / "msedgewebview2.exe").is_file():
        raise ValueError("Fixed WebView2 directory must contain msedgewebview2.exe")
    plan.tree(args.webview2, "WebView2", "browser")
    # License staging is a separately audited input, never the user's app data directory.
    for relative in REQUIRED_TERMS:
        safe_source(args.licenses, relative)
    # Preserve uncommon license names too (LICENSE.BSD, LICENCE, AUTHORS, etc.).
    # This directory is curated by the redistribution audit, not discovered from app state.
    plan.tree(args.licenses, "Licenses", "licenses")
    if not any(e.component == "licenses" and e.target.startswith("Licenses/") for e in plan.entries.values()):
        raise ValueError("License staging directory is empty")
    plan.add(args.readme.parent, args.readme.name, "使用说明.txt", "documentation")
    plan.text("VERSION.txt", VERSION + "\n", "metadata")
    return plan


def write_build(plan, destination, progress=None):
    destination = checked_output(destination)
    if destination.exists():
        raise ValueError("Destination must be new; existing folders are never overwritten")
    for entry in plan.entries.values():
        if entry.source and (entry.source.is_relative_to(destination) or destination.is_relative_to(entry.source.parent)):
            raise ValueError("Destination overlaps a source component")
        if entry.expected and sha256(entry.source) != entry.expected:
            raise ValueError("Pinned component checksum mismatch: " + entry.target)
    destination.mkdir(parents=True)
    records = []
    for index, entry in enumerate(sorted(plan.entries.values(), key=lambda e: e.target), 1):
        target = checked_output(destination / entry.target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.source:
            shutil.copy2(entry.source, target)
        else:
            target.write_bytes(entry.content or b"")
        digest = sha256(target)
        if entry.expected and digest != entry.expected:
            raise ValueError("Copied component checksum mismatch: " + entry.target)
        records.append({"path": entry.target, "bytes": target.stat().st_size, "sha256": digest,
                        "component": entry.component})
        if progress and (index % 1000 == 0 or index == len(plan.entries)):
            progress({"copied_files": index, "total_files": len(plan.entries), "component": entry.component})
    for name in ("Cache",):
        checked_output(destination / name).mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "version": VERSION, "platform": "windows-x86_64", "offline_models": True,
                "mutable_directories": ["Cache", "Data/photos", "Data/cache", "Data/exports", "Data/uploads", "Data/logs", "Exports"],
                "mutable_files": ["Data/state.json"], "files": records}
    checked_output(destination / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**plan.summary(), "manifest_sha256": sha256(destination / MANIFEST)}


def verify(directory, *, pristine=True):
    directory = Path(directory).resolve()
    manifest = json.loads(safe_source(directory, MANIFEST).read_text(encoding="utf-8"))
    expected = {MANIFEST.casefold()}
    for item in manifest["files"]:
        relative = relative_name(item["path"])
        if relative.casefold() in expected:
            raise ValueError("Duplicate manifest entry: " + relative)
        expected.add(relative.casefold())
        path = safe_source(directory, relative)
        if path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise ValueError("Payload checksum mismatch: " + relative)
    for path in directory.rglob("*"):
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if relative.casefold() not in expected:
                if not pristine and (relative in manifest.get("mutable_files", []) or
                                     any(relative.startswith(x + "/") for x in manifest["mutable_directories"])):
                    continue
                raise ValueError("Unexpected file in share folder: " + relative)
    return {"files": len(expected) - 1, "version": manifest["version"], "pristine": pristine}


def archive(directory, output, max_part_bytes=None):
    """Create ordinary ZIPs: one complete archive, or disjoint GitHub-safe ZIP parts.

    Each part includes the same top-level folder. Extract every part beside each
    other to merge them. This is not a .001 binary split requiring special tools.
    """
    directory, output = Path(directory).resolve(), checked_output(output)
    if output.is_relative_to(directory):
        raise ValueError("Archive output must be outside the distribution")
    verify(directory)
    paths = sorted(p for p in directory.rglob("*") if p.is_file())
    budget = max_part_bytes - 16 * 1024 * 1024 if max_part_bytes else None
    groups, current, size = [], [], 0
    for path in paths:
        length = path.stat().st_size
        if budget and length > budget:
            raise ValueError("Single file exceeds archive part limit: " + path.name)
        if budget and current and size + length > budget:
            groups.append(current)
            current, size = [], 0
        current.append(path)
        size += length
    if current:
        groups.append(current)
    output.mkdir(parents=True, exist_ok=True)
    names = [directory.name + (f"-part{i+1:02d}-of-{len(groups):02d}" if max_part_bytes else "") + ".zip"
             for i in range(len(groups))]
    if any((output / name).exists() or (output / (name + ".part")).exists() for name in names):
        raise ValueError("Archive output already exists")
    result = []
    for name, group in zip(names, groups):
        temporary = checked_output(output / (name + ".part"))
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zipped:
            for path in group:
                zipped.write(path, directory.name + "/" + path.relative_to(directory).as_posix())
        if max_part_bytes and temporary.stat().st_size > max_part_bytes:
            raise ValueError("Compressed ZIP exceeds part size limit")
        with zipfile.ZipFile(temporary) as zipped:
            bad = zipped.testzip()
            if bad:
                raise ValueError("ZIP CRC failed: " + bad)
        target = checked_output(output / name)
        temporary.rename(target)
        result.append({"file": name, "bytes": target.stat().st_size, "sha256": sha256(target)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--repo", type=Path, default=PROJECT)
    for name in ("donor", "analysis-models", "neural-models", "vendor", "launcher-dir", "licenses", "readme", "destination"):
        build.add_argument("--" + name, type=Path, required=True)
    build.add_argument("--webview2-root", "--webview2", dest="webview2", type=Path, required=True)
    build.add_argument("--vc-runtime-root", type=Path)
    build.add_argument("--plan", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("directory", type=Path)
    check.add_argument("--allow-user-data", action="store_true")
    pack = commands.add_parser("archive")
    pack.add_argument("directory", type=Path)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--github-parts", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        plan = make_plan(args)
        result = plan.summary() if args.plan else write_build(plan, args.destination,
            progress=lambda value: print(json.dumps(value), file=sys.stderr, flush=True))
    elif args.command == "verify":
        result = verify(args.directory, pristine=not args.allow_user_data)
    else:
        result = archive(args.directory, args.output, int(1.8 * 1024**3) if args.github_parts else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
