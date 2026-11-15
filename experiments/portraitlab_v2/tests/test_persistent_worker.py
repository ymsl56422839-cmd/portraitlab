"""Protocol, reuse and failure recovery without Torch or model downloads."""
import gc
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import weakref

SPEC = importlib.util.spec_from_file_location('persistent_worker_test_subject',
                                             Path(__file__).resolve().parents[1] / 'persistent_worker.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def request(tmp_path, device='auto', job='job'):
    return {'input': str(tmp_path / 'input.npy'), 'allowed_mask': str(tmp_path / 'mask.npy'),
            'roi': [0, 0, 10, 10], 'vendor': str(tmp_path / 'vendor'),
            'models': str(tmp_path / 'models'), 'output': str(tmp_path / job), 'device': device}


def run_lines(messages, factory, runner):
    incoming = io.StringIO('\n'.join(json.dumps(m) if isinstance(m, dict) else m for m in messages) + '\n')
    outgoing, errors = io.StringIO(), io.StringIO()
    worker.serve(incoming, outgoing, errors, predictor_factory=factory, request_runner=runner)
    return [json.loads(line) for line in outgoing.getvalue().splitlines()], errors.getvalue()


def test_standalone_backend_import_with_embedded_style_path(monkeypatch):
    module_dir = Path(worker.__file__).resolve().parent
    monkeypatch.setattr(sys, 'path', [p for p in sys.path if Path(p).resolve() != module_dir])
    monkeypatch.delitem(sys.modules, 'neural_candidate', raising=False)
    backend = worker._backend()
    assert Path(backend.__file__).resolve() == module_dir / 'neural_candidate.py'
    assert str(module_dir) in sys.path


def test_reuse_and_shutdown_with_clean_protocol(tmp_path):
    created, calls = [], []

    class Fake:
        def detect(self):
            print('third-party stdout during inference')

    def factory(*key):
        created.append(key)
        print('third-party stdout during model construction')
        return Fake()

    def runner(req, predictor):
        predictor.detect()
        calls.append(req['output'])
        return {'candidates': [1, 2], 'timings': {'analysis_seconds': .01}}

    responses, errors = run_lines([
        {'id': 0, 'op': 'ping'},
        {'id': 1, 'op': 'analyse', 'request': request(tmp_path, job='one')},
        {'id': 2, 'request': request(tmp_path, job='two')},
        {'id': 3, 'op': 'shutdown'},
        {'id': 4, 'request': request(tmp_path, job='must-not-run')},
    ], factory, runner)
    assert len(created) == 1 and len(calls) == 2
    assert [r['id'] for r in responses] == [0, 1, 2, 3]
    assert responses[0]['model_cached'] is False
    assert responses[1]['model_reused'] is False and responses[2]['model_reused'] is True
    assert responses[1]['manifest'].endswith('candidates.json')
    assert responses[-1]['shutdown'] is True
    assert 'third-party stdout during model construction' in errors
    assert 'third-party stdout during inference' in errors


def test_replaces_old_model_before_loading_new_key(tmp_path, monkeypatch):
    created, references = [], []
    marker_name = 'modelscope.models.cv.skin_retouching.worker_test_marker'
    unrelated_name = 'modelscope.models.cv.skin_retouching.worker_unrelated_test'
    unrelated = types.ModuleType(unrelated_name)
    unrelated.__file__ = str(tmp_path.parent / 'external.py')
    monkeypatch.setitem(sys.modules, unrelated_name, unrelated)

    class Fake:
        def detect(self):
            pass

    def factory(*key):
        gc.collect()
        assert not any(ref() is not None for ref in references)
        assert marker_name not in sys.modules
        marker = types.ModuleType(marker_name)
        marker.__file__ = str(Path(key[0]) / 'network.py')
        monkeypatch.setitem(sys.modules, marker_name, marker)
        created.append(key)
        instance = Fake()
        references.append(weakref.ref(instance))
        return instance

    def runner(req, predictor):
        predictor.detect()
        return {'candidates': [], 'timings': {}}

    a = request(tmp_path, 'cpu')
    equivalent = {**a, 'vendor': str(tmp_path / 'vendor' / '..' / 'vendor')}
    changed = {**a, 'models': str(tmp_path / 'other-models')}
    moved_vendor = {**changed, 'vendor': str(tmp_path / 'other-vendor')}
    responses, _ = run_lines([a, equivalent, request(tmp_path, 'cuda'), changed, moved_vendor], factory, runner)
    assert all(r['ok'] for r in responses)
    assert len(created) == 4
    assert [r['model_reused'] for r in responses] == [False, True, False, False, False]
    gc.collect()
    assert not any(ref() is not None for ref in references)
    assert marker_name not in sys.modules
    assert sys.modules[unrelated_name] is unrelated


def test_invalid_input_failed_load_and_failed_inference_do_not_exit(tmp_path):
    loads = []

    class Fake:
        def detect(self):
            pass

    def factory(*key):
        loads.append(key)
        if len(loads) == 1:
            raise RuntimeError('simulated model load failure')
        return Fake()

    def runner(req, predictor):
        predictor.detect()
        if req.get('fail_inference'):
            raise ValueError('simulated inference failure')
        return {'candidates': [], 'timings': {}}

    responses, errors = run_lines([
        '{bad-json', {'id': 'bad-op', 'op': 'unsupported'},
        {'id': 'bad-model', 'request': request(tmp_path)},
        {'id': 'bad-inference', 'request': {**request(tmp_path), 'fail_inference': True}},
        {'id': 'recovered', 'request': request(tmp_path)}, {'id': 'done', 'op': 'shutdown'},
    ], factory, runner)
    assert [r['ok'] for r in responses] == [False, False, False, False, True, True]
    assert responses[4]['id'] == 'recovered' and responses[4]['model_reused'] is True
    assert len(loads) == 2
    assert 'simulated model load failure' in errors and 'simulated inference failure' in errors
