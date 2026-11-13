"""Create ordinary model-free Core/AI ZIPs suitable for GitHub release assets.

Each AI archive contains a disjoint set of files under the same top-level
directory. Extract every AI archive next to Core; no multipart tool is needed.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import zipfile
from build_portable import checked, mkdir, sha, write_json

MAX_ASSET_BYTES = int(1.8 * 1024 ** 3)
# DEFLATE's worst-case expansion and ZIP metadata fit comfortably in this margin.
UNCOMPRESSED_BUDGET = MAX_ASSET_BYTES - 16 * 1024 * 1024


def files_under(path):
    return sorted(p for p in path.rglob("*") if p.is_file())


def groups_for(files):
    bins = []
    for path in sorted(files, key=lambda p: (-p.stat().st_size, str(p))):
        size = path.stat().st_size
        if size > UNCOMPRESSED_BUDGET:
            raise ValueError("A single file is too large for an independent ZIP: " + str(path))
        for group in bins:
            if group["bytes"] + size <= UNCOMPRESSED_BUDGET:
                group["files"].append(path)
                group["bytes"] += size
                break
        else:
            bins.append({"bytes": size, "files": [path]})
    return bins


def archive_one(package, output, name, files, category):
    target = checked(output / name)
    temporary = checked(target.with_suffix(".zip.part"))
    if target.exists() or temporary.exists():
        raise FileExistsError("Release archives are never overwritten: " + str(target))
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(files):
            member = Path(package.name) / path.relative_to(package)
            archive.write(path, str(member).replace("\\", "/"))
    if temporary.stat().st_size >= MAX_ASSET_BYTES:
        raise ValueError("Archive exceeds the release asset ceiling: " + str(temporary))
    with zipfile.ZipFile(temporary) as archive:
        damaged = archive.testzip()
        if damaged:
            raise ValueError("ZIP CRC validation failed: " + damaged)
        if any("/Data/" in n or "/Cache/" in n for n in archive.namelist()):
            raise ValueError("Runtime data or cache unexpectedly entered an archive")
    os.replace(temporary, target)
    record = {"file": name, "component": category, "bytes": target.stat().st_size,
              "sha256": sha(target), "files": len(files),
              "uncompressed_bytes": sum(p.stat().st_size for p in files), "zip_crc_verified": True}
    print(json.dumps(record), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--component", choices=("all", "core", "ai"), default="all")
    args = parser.parse_args()
    package, output = Path(args.package).resolve(), mkdir(args.output)
    if package == output or output.is_relative_to(package):
        raise ValueError("Release output must be outside the portable folder")
    if not (package / "runtime/python.exe").is_file() or not (package / "AI/python.exe").is_file():
        raise ValueError("Build both runtimes before creating the full release")
    core_files = []
    for folder in ("app", "runtime", "Licenses"):
        core_files.extend(files_under(package / folder))
    for name in ("PortraitLab.exe", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "MODEL_LICENSES.md", "REPRODUCING.md", "portable-build.json", "source-manifest.json", "PORTABLE-README.txt"):
        if (package / name).is_file():
            core_files.append(package / name)
    if sum(p.stat().st_size for p in core_files) > UNCOMPRESSED_BUDGET:
        raise ValueError("Core grew beyond a single release archive")
    ai_groups = groups_for(files_under(package / "AI"))
    tasks = [(package.name + "-Core.zip", core_files, "core")]
    tasks.extend((package.name + f"-AI-{i:02d}-of-{len(ai_groups):02d}.zip", group["files"], "ai")
                 for i, group in enumerate(ai_groups, 1))
    tasks = [task for task in tasks if args.component == "all" or task[2] == args.component]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 4))) as executor:
        futures = [executor.submit(archive_one, package, output, name, files, category) for name, files, category in tasks]
        records = [future.result() for future in futures]
    manifest = output / "release-packages.json"
    if manifest.is_file():
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        names = {record["file"] for record in records}
        for record in previous["archives"]:
            if record["file"] in names:
                continue
            path = output / record["file"]
            if not path.is_file() or path.stat().st_size != record["bytes"] or sha(path) != record["sha256"]:
                raise ValueError("Previous release asset changed: " + str(path))
            records.append(record)
    records.sort(key=lambda record: (record["component"] != "core", record["file"]))
    write_json(output / "release-packages.json", {"version": "0.2.0", "platform": "Windows x64",
        "top_level_directory": package.name, "model_weights_included": False,
        "maximum_asset_bytes": MAX_ASSET_BYTES, "archives": records,
        "installation": "Extract Core and every AI ZIP into the same parent folder. All ZIPs are ordinary independent archives; no concatenation is needed. Launch PortraitLab.exe, then download models from the app."})
    checked(output / "SHA256SUMS.txt").write_text("".join(item["sha256"] + "  " + item["file"] + "\n" for item in records), encoding="utf-8")
    print(json.dumps({"release_directory": str(output), "archives": len(records)}), flush=True)


if __name__ == "__main__":
    main()
