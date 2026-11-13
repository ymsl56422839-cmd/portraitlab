"""Prepare a local, reproducible environment in configurable writable folders."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import concurrent.futures

PROJECT = Path(__file__).resolve().parents[1]

def checked(path):
    resolved = Path(path).expanduser().resolve()
    boundary = os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not resolved.is_relative_to(Path(boundary).expanduser().resolve()):
        raise ValueError(f'Writable path is outside PORTRAITLAB_STORAGE_ROOT: {resolved}')
    return resolved

def layout():
    checked(PROJECT)
    base = PROJECT.parents[1] / 'work/portraitlab' if PROJECT.parent.name == 'outputs' else PROJECT / '.local'
    base = checked(os.environ.get('PORTRAITLAB_WORK', base))
    data = checked(os.environ.get('PORTRAITLAB_DATA', base / 'runtime'))
    paths = {'base': base, 'data': data, 'venv': checked(base / 'venv'),
             'temp': checked(base / 'temp'), 'cache': checked(os.environ.get('PORTRAITLAB_CACHE', base / 'cache'))}
    for path in paths.values():
        checked(path).mkdir(parents=True, exist_ok=True)
    return paths

def environment(paths):
    result = os.environ.copy()
    for key, value in {
        'TEMP': paths['temp'], 'TMP': paths['temp'], 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONNOUSERSITE': '1', 'PORTRAITLAB_DATA': paths['data'], 'PORTRAITLAB_CACHE': paths['cache'],
        'PIP_CACHE_DIR': paths['cache'] / 'pip', 'MPLCONFIGDIR': paths['cache'] / 'matplotlib',
        'HF_HOME': paths['cache'] / 'huggingface', 'TORCH_HOME': paths['cache'] / 'torch',
        'CUDA_CACHE_PATH': paths['cache'] / 'cuda', 'TORCH_EXTENSIONS_DIR': paths['cache'] / 'torch-extensions',
        'TORCHINDUCTOR_CACHE_DIR': paths['cache'] / 'torch-inductor',
        'TRITON_CACHE_DIR': paths['cache'] / 'triton',
        'XDG_CACHE_HOME': paths['cache'], 'UV_CACHE_DIR': paths['cache'] / 'uv',
        'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PYTHONUTF8': '1',
    }.items():
        result[key] = str(value)
    return result

MODELS = (
    ('face_landmarker', 'face_landmarker.task',
     'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
     '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff'),
    ('selfie_multiclass', 'selfie_multiclass_256x256.tflite',
     'https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/1/selfie_multiclass_256x256.tflite',
     'c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0'),
)

def download_models(paths):
    def fetch(model):
        key, filename, url, expected = model
        destination = checked(paths['data'] / 'models' / key / filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
            return {'id': key, 'sha256': expected, 'bytes': destination.stat().st_size, 'verified': True}
        temporary = checked(destination.with_suffix(destination.suffix + '.part'))
        h = hashlib.sha256()
        request = urllib.request.Request(url, headers={'User-Agent': 'PortraitLab/0.1 research'})
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                h.update(chunk); output.write(chunk)
        if h.hexdigest() != expected:
            temporary.unlink()
            raise ValueError(f'Model checksum mismatch: {key}')
        checked(temporary).replace(checked(destination))
        return {'id': key, 'sha256': expected, 'bytes': destination.stat().st_size, 'verified': True}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(fetch, MODELS))
    checked(paths['data'] / 'model-receipts.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    print(json.dumps({'models': records}, ensure_ascii=True))

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--download-models', action='store_true')
    p.add_argument('--models-only', action='store_true')
    args = p.parse_args()
    paths = layout(); env = environment(paths)
    os.environ.update(env)
    python = checked(paths['venv'] / 'Scripts/python.exe')
    if not args.models_only:
        if not python.exists():
            subprocess.run([sys.executable, '-B', '-m', 'venv', '--copies', str(paths['venv'])],
                           cwd=PROJECT, env=env, check=True)
        requirements = PROJECT / 'requirements.lock.txt'
        if not requirements.exists(): requirements = PROJECT / 'requirements.txt'
        subprocess.run([str(python), '-B', '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                        '--no-compile', '--disable-pip-version-check', '-r', str(requirements)],
                       cwd=PROJECT, env=env, check=True)
    if args.download_models or args.models_only:
        download_models(paths)
    print(json.dumps({'ready': True, 'python': str(python), 'data': str(paths['data'])}))

if __name__ == '__main__': main()
