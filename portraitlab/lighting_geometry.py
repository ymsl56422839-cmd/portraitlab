"""Pinned geometry-model preparation for deterministic interactive lighting.

No torch import or filesystem write occurs during module import.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = APP_ROOT / 'models/lighting-manifest.json'
GEOMETRY_VERSION = 'dav2-small-depth-normal-rgb8-v1'


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def geometry_signature() -> str:
    return hashlib.sha256(MANIFEST.read_bytes()+GEOMETRY_VERSION.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=16)
def _verified_hash(path: str, size: int, modified: int, changed: int) -> str:
    return file_sha256(Path(path))


def model_status(models_dir: Path | str) -> dict:
    spec = manifest()
    files = []
    for item in spec['files']:
        path = Path(models_dir)/item['relative_path']
        exists, verified = path.is_file(), False
        if exists:
            stat = path.stat()
            verified = stat.st_size == item['bytes'] and _verified_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == item['sha256']
        files.append({**item, 'exists': exists, 'verified': verified})
    return {'ready': all(item['verified'] for item in files), 'files': files,
            'name': spec['name'], 'license': spec['weights_license'],
            'license_source': spec['license_source'], 'bytes': sum(item['bytes'] for item in files),
            'signature': geometry_signature(), 'geometry_version': GEOMETRY_VERSION}


status = model_status


def download_models(models_dir: Path | str, progress=None, cancel=None, cancel_event=None) -> dict:
    cancel = cancel_event if cancel_event is not None else cancel
    report = progress or (lambda value: None)
    root = Path(models_dir).resolve()
    for item in manifest()['files']:
        if cancel is not None and cancel.is_set():
            raise RuntimeError('模型准备已取消。')
        path = (root/item['relative_path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('模型文件路径越界。')
        ready = model_status(root)
        if next(record for record in ready['files'] if record['filename'] == item['filename'])['verified']:
            report({'message': '布光模型已校验，无需重复下载。', 'done': item['bytes'], 'total': item['bytes'], 'filename': item['filename']})
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.part')
        try:
            request = urllib.request.Request(item['url'], headers={'User-Agent': 'PortraitLab/0.3 local-lighting'})
            done, digest = 0, hashlib.sha256()
            with urllib.request.urlopen(request, timeout=45) as response, temporary.open('xb') as output:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError('模型准备已取消。')
                    chunk = response.read(512*1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > item['bytes']:
                        raise RuntimeError('布光模型长度与固定清单不符。')
                    output.write(chunk)
                    digest.update(chunk)
                    report({'message': '正在下载布光分析模型…', 'done': done, 'total': item['bytes'], 'filename': item['filename']})
            if done != item['bytes'] or digest.hexdigest() != item['sha256']:
                raise RuntimeError('布光模型校验失败，请重新下载。')
            if cancel is not None and cancel.is_set():
                raise RuntimeError('模型准备已取消。')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    result = model_status(root)
    if not result['ready']:
        raise RuntimeError('布光模型没有通过校验。')
    report({'message': '布光分析模型已就绪。', 'done': result['bytes'], 'total': result['bytes']})
    return result


download = download_models
