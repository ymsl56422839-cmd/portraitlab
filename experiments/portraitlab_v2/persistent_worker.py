"""Single-model JSON-lines worker for the isolated V2 candidate prototype.

SPDX-License-Identifier: AGPL-3.0-only

Run: python -B persistent_worker.py --serve
Input: {"id":"job-1","op":"analyse","request":{...neural_candidate request...}}
Output: {"id":"job-1","ok":true,"manifest":"...","candidates":3,...}
Also accepts a bare neural_candidate request. `ping` never loads a model;
`shutdown` acknowledges and exits. Errors return JSON and preserve the worker.
Only one predictor is kept, keyed by vendor/models paths, device and model family.
Request handlers establish a short configured cache before lazy model loading.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import gc
import json
import os
from pathlib import Path
import sys
import time
import traceback

MAX_LINE_BYTES = 8 * 1024 * 1024


def _trace(stage):
    """Optional phase-only diagnostics to the caller's stderr log; no image data."""
    if os.environ.get('PORTRAITLAB_WORKER_TRACE') == '1':
        print(json.dumps({'worker_stage': stage, 'seconds': time.perf_counter(),
                          'pid': os.getpid()}), file=sys.stderr, flush=True)


def _backend():
    _trace('backend_import_start')
    if __package__:
        from . import neural_candidate
    else:
        # Windows embedded Python ._pth omits the script directory, even when
        # this worker itself is started with an absolute filename.
        module_dir = str(Path(__file__).resolve().parent)
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)
        import neural_candidate
    _trace('backend_import_ready')
    return neural_candidate


def _default_factory(vendor, models, device, family='local'):
    if family=='skin':
        _backend()
        if __package__:
            from .skin_model import ModelScopeSkinPredictor
        else:
            from skin_model import ModelScopeSkinPredictor
        return ModelScopeSkinPredictor(vendor,models,device)
    return _backend().ModelScopeLocalPredictor(vendor, models, device)


def _default_runner(request, predictor):
    if request.get('kind')=='skin':
        _backend()
        if __package__:
            from .skin_model import run_skin_request
        else:
            from skin_model import run_skin_request
        return run_skin_request(request,predictor)
    if request.get('kind') == 'manual':
        _trace('manual_module_import_start')
        _backend()  # Establish the local module path for embedded Python.
        if __package__:
            from .manual_repair import run_manual_request
        else:
            from manual_repair import run_manual_request
        _trace('manual_request_start')
        result = run_manual_request(request, predictor)
        _trace('manual_request_ready')
        return result
    return _backend().run_request(request, predictor)


def _cache_key(request):
    if not isinstance(request, dict):
        raise ValueError('request must be a JSON object')
    device = request.get('device', 'auto')
    if device not in ('auto', 'cpu', 'cuda'):
        raise ValueError('device must be auto, cpu or cuda')
    paths = []
    for field in ('vendor', 'models'):
        if not isinstance(request.get(field), str) or not request[field]:
            raise ValueError(field + ' must be a nonempty path')
        paths.append(os.path.normcase(str(Path(request[field]).resolve())))
    return (*paths, device, 'skin' if request.get('kind')=='skin' else 'local')


def _release_vendor_modules(vendor):
    """Release only the old worker's local ModelScope namespace imports."""
    root = Path(vendor).resolve()
    parents = {'modelscope', 'modelscope.models', 'modelscope.models.cv'}
    for name, module in list(sys.modules.items()):
        if name not in parents and not name.startswith('modelscope.models.cv.skin_retouching'):
            continue
        filename = getattr(module, '__file__', None)
        locations = [filename] if filename else list(getattr(module, '__path__', ()))
        if locations and all(Path(location).resolve().is_relative_to(root) for location in locations):
            sys.modules.pop(name, None)


class PredictorCache:
    """Sequential ownership; old resources are released before a new load."""
    def __init__(self, factory):
        self.factory = factory
        self.key = None
        self.predictor = None

    def get(self, key):
        if self.predictor is None or self.key != key:
            _trace('predictor_release_start')
            self.close()
            # Publish only a fully constructed model. A failed load is retryable.
            _trace('predictor_load_start')
            predictor = self.factory(*key)
            self.predictor, self.key = predictor, key
            _trace('predictor_load_ready')
        return self.predictor

    def close(self):
        old = self.predictor
        old_key = self.key
        self.predictor, self.key = None, None
        if old is None:
            return
        torch = getattr(old, 'torch', None)
        del old
        gc.collect()
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
        if old_key is not None:
            _release_vendor_modules(old_key[0])


class LazyPredictor:
    """Construct after run_request has validated inputs and set F-safe caches."""
    def __init__(self, cache, key):
        self._cache, self._key = cache, key

    def __getattr__(self, name):
        return getattr(self._cache.get(self._key), name)


def _emit(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    stream.flush()


def serve(input_stream, output_stream, error_stream, *, predictor_factory=None, request_runner=None):
    """Process one request at a time. Injectable streams/backends support tests."""
    cache = PredictorCache(predictor_factory or _default_factory)
    runner = request_runner or _default_runner
    _trace('serve_ready')
    try:
        while True:
            line = input_stream.readline(MAX_LINE_BYTES + 1)
            _trace('line_received')
            if not line:
                break
            identifier = None
            try:
                if len(line) > MAX_LINE_BYTES:
                    while line and not line.endswith('\n'):
                        line = input_stream.readline(MAX_LINE_BYTES + 1)
                    raise ValueError('Request exceeds the 8 MiB line limit')
                if not line.strip():
                    continue
                message = json.loads(line)
                _trace('json_parsed')
                if not isinstance(message, dict):
                    raise ValueError('Each request must be a JSON object')
                identifier = message.get('id')
                if identifier is not None and (type(identifier) not in (str, int)):
                    identifier = None
                    raise ValueError('id must be a string, integer, or null')
                operation = message.get('op', 'analyse')
                if operation == 'shutdown':
                    with redirect_stdout(error_stream):
                        cache.close()
                    _emit(output_stream, {'id': identifier, 'ok': True, 'shutdown': True})
                    break
                if operation == 'ping':
                    _emit(output_stream, {'id': identifier, 'ok': True, 'status': 'ready',
                                          'model_cached': cache.predictor is not None})
                    continue
                if operation == 'devices':
                    with redirect_stdout(error_stream):
                        configuration=message.get('request',{})
                        cache_root=configuration.get('cache_root') or os.environ.get('PORTRAITLAB_WORKER_CACHE_ROOT')
                        if not cache_root:raise ValueError('请先配置较短的工作缓存目录，再检查处理设备。')
                        _backend().configure_worker_cache(Path(cache_root)/'devices',cache_root)
                        import torch
                        available=bool(torch.cuda.is_available())
                        device_response={'id':identifier,'ok':True,'cuda_available':available,
                            'gpu_name':torch.cuda.get_device_name(0) if available else None,'torch_version':torch.__version__}
                    _emit(output_stream,device_response)
                    continue
                if operation != 'analyse':
                    raise ValueError('Unknown operation; use analyse, devices, ping, or shutdown')
                request = message.get('request', message)
                key = _cache_key(request)
                _trace('request_key_ready')
                reused = cache.predictor is not None and cache.key == key
                started = time.perf_counter()
                # In CLI mode FD 1 also points at stderr, including native output.
                with redirect_stdout(error_stream):
                    metadata = runner(request, LazyPredictor(cache, key))
                response = {'id': identifier, 'ok': True,
                            'manifest': str(Path(request['output']).resolve() / ('skin.json' if request.get('kind')=='skin' else 'candidates.json')),
                            'candidates': len(metadata['candidates']), 'timings': metadata['timings'],
                            'model_reused': reused, 'worker_request_seconds': time.perf_counter() - started}
                _emit(output_stream, response)
            except Exception as error:
                traceback.print_exc(file=error_stream)
                error_stream.flush()
                _emit(output_stream, {'id': identifier, 'ok': False,
                                      'error': str(error), 'error_type': type(error).__name__})
    finally:
        with redirect_stdout(error_stream):
            cache.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', required=True)
    parser.parse_args()
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    # Keep a private protocol descriptor. Libraries writing directly to FD 1
    # then go to stderr as well as ordinary Python prints. No files are opened.
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    tracing = os.environ.get('PORTRAITLAB_WORKER_TRACE') == '1'
    if tracing:
        import faulthandler
        faulthandler.enable(file=sys.stderr)
        faulthandler.dump_traceback_later(30, repeat=True, file=sys.stderr)
    _trace('stdio_ready')
    try:
        serve(sys.stdin, protocol, sys.stderr)
    finally:
        if tracing:
            faulthandler.cancel_dump_traceback_later()
        protocol.close()


if __name__ == '__main__':
    main()
