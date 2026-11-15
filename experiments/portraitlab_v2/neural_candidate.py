"""Reviewable local skin-repair candidates for the isolated V2 prototype.

SPDX-License-Identifier: AGPL-3.0-only
Network/pipeline reference: ModelScope, Copyright Alibaba, Inc. and affiliates,
Apache-2.0, commit 34844f9977a33f541cb5d8959c40a57cd6155b3b. Only checksum-pinned
network definitions are imported. No upstream wrapper, whitening, download, or
whole-face generation runs. The detector does NOT distinguish moles from acne.

CLI: python -B neural_candidate.py --request request.json
Request: input, roi=[x0,y0,x1,y1], allowed_mask, vendor, models, output,
device=auto|cpu|cuda. RGB input is uint8, already oriented and normalized to sRGB.
The allowed mask is float 0..1 (.npy), or a grayscale PNG (0..255), full image
or ROI size. All candidates default to disabled. A candidate NPZ contains an
already masked float32 RGB delta, not multiplied by the user strength.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import types

import numpy as np

MODEL_ID = 'damo/cv_unet_skin_retouching_torch'
MODEL_REVISION = '30f890aab89b787aae93c3f396dbf32714bd950e'
SOURCE_COMMIT = '34844f9977a33f541cb5d8959c40a57cd6155b3b'
MODEL_FILE = 'joint_20210926.pth'
MODEL_SHA256 = 'c9e1388b19d9334c90db08308c86de208f535920ee381ae8a9cb63484a78351a'
LICENSE_SHA256 = 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30'
SOURCE_HASHES = {
    'weights_init.py': '5feb5da62e7a97cca5e60488c96d76c48074ca7521526dca0a91498880381e5b',
    'detection_model/detection_module.py': '23fce16544465ef41232ce652fecc95a71db3bb81af575d4aefc472a4e45a2ec',
    'detection_model/detection_unet_in.py': 'e3b02ff03941aabc967c0fa66c39a00a56e84fbbe98f15c52de6bdf5555a9168',
    'inpainting_model/gconv.py': 'fca8ece60d31dabf3d6eda186657efd72328f94aafe7b11fe130259ee873bd20',
    'inpainting_model/inpainting_unet.py': 'bc151c88dabac96ef2a17f31c6dc979688d65c4f0f0f8dde73e1c2e845f462dd',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def rgb_fingerprint(rgb):
    digest = hashlib.sha256()
    digest.update(json.dumps(list(rgb.shape), separators=(',', ':')).encode('ascii'))
    digest.update(np.ascontiguousarray(rgb).data)
    return digest.hexdigest()


def _number(value, low, high, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (float, int, np.number)):
        raise ValueError(f'{name} must be a finite number')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return value


def _validate(rgb, roi, allowed_mask):
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('Input must be uint8 RGB')
    h, w = rgb.shape[:2]
    if min(h, w) < 2 or h * w > 80_000_000:
        raise ValueError('Input dimensions must be 2px or larger and at most 80MP')
    if not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(
            isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in roi):
        raise ValueError('ROI must contain four integer pixel coordinates')
    x0, y0, x1, y1 = map(int, roi)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError('ROI lies outside input image')
    if (x1 - x0) * (y1 - y0) > 16_000_000:
        raise ValueError('Select a face ROI of at most 16MP')
    allowed = np.asarray(allowed_mask, dtype=np.float32)
    if allowed.shape == (h, w):
        allowed = allowed[y0:y1, x0:x1]
    if allowed.shape != (y1 - y0, x1 - x0) or not np.isfinite(allowed).all():
        raise ValueError('Allowed mask must match the full image or ROI')
    if np.any(allowed < 0) or np.any(allowed > 1):
        raise ValueError('Allowed mask must be within 0..1')
    return tuple(map(int, roi)), allowed.copy()


def _components(binary):
    """8-connected components without adding a scipy/OpenCV worker dependency."""
    h, w = binary.shape
    starts = np.flatnonzero(binary)
    pending = set(map(int, starts))
    result = []
    for start in starts:
        start = int(start)
        if start not in pending:
            continue
        pending.remove(start)
        todo, pixels = [start], []
        while todo:
            index = todo.pop()
            pixels.append(index)
            y, x = divmod(index, w)
            for ny in range(max(0, y-1), min(h, y+2)):
                for nx in range(max(0, x-1), min(w, x+2)):
                    other = ny*w+nx
                    if other in pending:
                        pending.remove(other)
                        todo.append(other)
        ids = np.asarray(pixels, dtype=np.int64)
        ys, xs = ids // w, ids % w
        result.append((int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1, ids))
    return result


def analyse_candidates(rgb, roi, allowed_mask, predictor, *, threshold=.35, min_area=4, max_candidates=64):
    """Return metadata and independent patches; no writes, no implicit application.

    predictor.detect(roi_rgb) -> float32 probability [H,W]
    predictor.repair(roi_rgb, component_soft_mask, bounds) -> float RGB [bh,bw,3]
    """
    roi, allowed = _validate(rgb, roi, allowed_mask)
    threshold = _number(threshold, .05, .95, 'threshold')
    if type(min_area) is not int or not 1 <= min_area <= 4096:
        raise ValueError('min_area must be an integer in 1..4096')
    if type(max_candidates) is not int or not 1 <= max_candidates <= 128:
        raise ValueError('max_candidates must be an integer in 1..128')
    x0, y0, x1, y1 = roi
    source = rgb[y0:y1, x0:x1]
    started = time.perf_counter()
    probability = np.asarray(predictor.detect(source), dtype=np.float32)
    detection_seconds = time.perf_counter()-started
    if probability.shape != allowed.shape or not np.isfinite(probability).all() or np.any(probability < 0) or np.any(probability > 1):
        raise ValueError('Detector returned an invalid probability map')
    # Extremely broad detector activation is not a small-blemish result. Reject
    # before allocating a Python component set for millions of active pixels.
    binary = (probability >= threshold) & (allowed > 0)
    if np.count_nonzero(binary) > 200_000:
        raise ValueError('Too many candidate pixels; use a smaller ROI or higher threshold')
    components = _components(binary)
    eligible = [c for c in components if len(c[4]) >= min_area and c[2]-c[0] <= 448 and c[3]-c[1] <= 448]
    eligible.sort(key=lambda c: (-float(probability.flat[c[4]].max()), c[1], c[0]))
    candidates, patches = [], {}
    unchanged_components = 0
    repair_started = time.perf_counter()
    soft = np.where(probability >= .5, 1., probability).astype(np.float32)
    for a, b, c, d, ids in eligible[:max_candidates]:
        candidate_started = time.perf_counter()
        candidate_mask = np.zeros_like(probability)
        candidate_mask.flat[ids] = soft.flat[ids]
        predicted = np.asarray(predictor.repair(source, candidate_mask, [a,b,c,d]), dtype=np.float32)
        original = source[b:d, a:c]
        if predicted.shape != original.shape or not np.isfinite(predicted).all():
            raise ValueError('Repair network returned an invalid patch')
        mask = candidate_mask[b:d, a:c] * allowed[b:d, a:c]
        delta = (np.clip(predicted,0,255)-original.astype(np.float32))*mask[...,None]
        delta[mask == 0] = 0
        changed = np.any(np.rint(original.astype(np.float32)+delta) != original,axis=2)
        if not changed.any():
            unchanged_components += 1
            continue
        identifier = f'c{len(candidates)+1:04d}'
        full_bounds = [a+x0,b+y0,c+x0,d+y0]
        pad = max(24, math.ceil(max(c-a,d-b)*1.5))
        bounds = [max(0,full_bounds[0]-pad),max(0,full_bounds[1]-pad),
                  min(rgb.shape[1],full_bounds[2]+pad),min(rgb.shape[0],full_bounds[3]+pad)]
        candidates.append({'id':identifier,'bounds':full_bounds,'thumb_bounds':bounds,
            'score':float(probability.flat[ids].max()),'mean_score':float(probability.flat[ids].mean()),
            'area':len(ids),'default_enabled':False,'requires_review':True,
            'classification':'unclassified_skin_mark','patch':identifier+'.npz',
            'changed_pixels_at_full_strength':int(np.count_nonzero(changed)),
            'repair_seconds':time.perf_counter()-candidate_started})
        patches[identifier]={'delta':delta.astype(np.float32),'mask':mask.astype(np.float32)}
    metadata = {'schema_version':1,'model':MODEL_ID,'model_revision':MODEL_REVISION,
        'source_commit':SOURCE_COMMIT,'source_rgb_sha256':rgb_fingerprint(rgb),
        'image_size':[rgb.shape[1],rgb.shape[0]],'roi':list(roi),'threshold':threshold,
        'default_strength':.7,'default_enabled':False,'candidates':candidates,
        'omitted_components':len(components)-len(candidates),
        'unchanged_components':unchanged_components,
        'timings':{'detection_seconds':detection_seconds,'candidate_repair_seconds':time.perf_counter()-repair_started,
                   'analysis_seconds':time.perf_counter()-started},
        'warnings':['候选不能自动区分痘、痣与雀斑；默认未应用，请逐点确认。'],
        'delta_contract':'RGB float32 (prediction-original)*detector_soft_mask*allowed_mask; unscaled by user strength'}
    if len(eligible)>max_candidates:
        metadata['warnings'].append('候选数量达到上限，其余区域未处理。')
    if any(c[2]-c[0]>448 or c[3]-c[1]>448 for c in components):
        metadata['warnings'].append('大面积连通区域已跳过，请缩小选区检查。')
    return metadata, patches


def compose_candidates(rgb, metadata, patches, enabled_ids=(), *, strength=.7, protection=None):
    """Reference full-image compositor. Tile viewers can use the same deltas locally."""
    if not isinstance(rgb,np.ndarray) or rgb.dtype!=np.uint8 or rgb.ndim!=3 or rgb.shape[2]!=3:
        raise ValueError('Input must be uint8 RGB')
    if list(rgb.shape[1::-1]) != metadata['image_size'] or rgb_fingerprint(rgb)!=metadata['source_rgb_sha256']:
        raise ValueError('Candidate patches belong to a different source image')
    strength = _number(strength,0,1,'strength')
    ids = list(enabled_ids)
    known = {c['id']:c for c in metadata['candidates']}
    if len(set(ids)) != len(ids) or any(identifier not in known for identifier in ids):
        raise ValueError('Unknown or duplicate candidate selection')
    if protection is not None:
        protection = np.asarray(protection,dtype=np.float32)
        if protection.shape!=rgb.shape[:2] or not np.isfinite(protection).all() or np.any(protection<0) or np.any(protection>1):
            raise ValueError('Protection mask must match the image and lie within 0..1')
    output = rgb.copy()
    for identifier in ids:
        candidate=known[identifier]
        bounds=candidate['bounds']
        if not isinstance(bounds,(list,tuple)) or len(bounds)!=4 or any(type(v) is not int for v in bounds):
            raise ValueError('Invalid candidate bounds')
        x0,y0,x1,y1=bounds
        if not (0<=x0<x1<=rgb.shape[1] and 0<=y0<y1<=rgb.shape[0]):
            raise ValueError('Invalid candidate bounds')
        delta=np.asarray(patches[identifier]['delta'],dtype=np.float32)
        if delta.shape!=(y1-y0,x1-x0,3) or not np.isfinite(delta).all():
            raise ValueError('Invalid candidate delta')
        if protection is not None:
            delta=delta*(1-protection[y0:y1,x0:x1,None])
        changed=np.clip(np.rint(rgb[y0:y1,x0:x1].astype(np.float32)+delta*strength),0,255).astype(np.uint8)
        active=np.any(delta!=0,axis=2)
        output[y0:y1,x0:x1][active]=changed[active]
    return output


class ModelScopeLocalPredictor:
    """Two pinned networks, lazy Torch import; call repeatedly for a warm worker."""
    def __init__(self, vendor, models, device='auto'):
        if device not in ('auto','cpu','cuda'):
            raise ValueError('device must be auto, cpu or cuda')
        started=time.perf_counter()
        vendor,models=Path(vendor).resolve(),Path(models).resolve()
        network_root=vendor/'modelscope/models/cv/skin_retouching'
        for rel,digest in SOURCE_HASHES.items():
            if sha256(network_root/rel)!=digest:
                raise ValueError('Network source checksum mismatch: '+rel)
        if sha256(vendor/'LICENSE')!=LICENSE_SHA256 or sha256(models/MODEL_FILE)!=MODEL_SHA256:
            raise ValueError('Model or license checksum mismatch')
        import torch
        self.torch=torch
        if device=='auto':
            device='cuda' if torch.cuda.is_available() else 'cpu'
        self.device=torch.device(device)
        if device=='cuda' and not torch.cuda.is_available():
            raise RuntimeError('Requested CUDA is unavailable; choose CPU explicitly')
        torch.set_num_threads(min(8,max(1,os.cpu_count() or 1)))
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        # Construct only known namespace packages. No vendor __init__ executes.
        for name,folder in [('modelscope',vendor/'modelscope'),('modelscope.models',vendor/'modelscope/models'),
            ('modelscope.models.cv',vendor/'modelscope/models/cv'),('modelscope.models.cv.skin_retouching',network_root),
            ('modelscope.models.cv.skin_retouching.detection_model',network_root/'detection_model'),
            ('modelscope.models.cv.skin_retouching.inpainting_model',network_root/'inpainting_model')]:
            existing=sys.modules.get(name)
            if existing is not None and list(getattr(existing,'__path__',[]))!=[str(folder)]:
                raise RuntimeError('Conflicting ModelScope module already loaded: '+name)
            module=types.ModuleType(name);module.__path__=[str(folder)];sys.modules[name]=module
        loaded={}
        for rel in SOURCE_HASHES:
            name='modelscope.models.cv.skin_retouching.'+rel[:-3].replace('/','.')
            spec=importlib.util.spec_from_file_location(name,network_root/rel)
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            loaded[rel]=module
        self.detector=loaded['detection_model/detection_unet_in.py'].DetectionUNet(3,1,init_weights=False)
        self.repairer=loaded['inpainting_model/inpainting_unet.py'].RetouchingNet(4,3,init_weights=False)
        checkpoint=torch.load(models/MODEL_FILE,map_location='cpu',weights_only=True)
        self.detector.load_state_dict(checkpoint['detection_net'],strict=True)
        self.repairer.load_state_dict(checkpoint['inpainting_net'],strict=True)
        del checkpoint
        self.detector.to(self.device).eval();self.repairer.to(self.device).eval()
        self._sync()
        self.load_seconds=time.perf_counter()-started
        self.device_name=torch.cuda.get_device_name(self.device) if device=='cuda' else 'CPU'

    def _sync(self):
        if self.device.type=='cuda':self.torch.cuda.synchronize(self.device)

    def detect(self, rgb):
        torch=self.torch
        import torch.nn.functional as functional
        with torch.inference_mode():
            tensor=torch.from_numpy(np.ascontiguousarray(rgb.transpose(2,0,1))).unsqueeze(0).float()/127.5-1
            # Downsample before transferring to CUDA; never upload the 39MP frame.
            small=functional.interpolate(tensor,(768,768),mode='bilinear',align_corners=True).to(self.device)
            probability=torch.sigmoid(self.detector(small)).cpu()
            result=functional.interpolate(probability,rgb.shape[:2],mode='nearest')[0,0].numpy().copy()
        self._sync()
        return result

    def repair(self, rgb, soft_mask, bounds):
        torch=self.torch
        x0,y0,x1,y1=bounds
        left=(x0+x1)//2-288;top=(y0+y1)//2-288
        tile=np.full((576,576,3),127.5,np.float32)
        keep=np.ones((576,576),np.float32)
        a,b=max(0,left),max(0,top);c,d=min(rgb.shape[1],left+576),min(rgb.shape[0],top+576)
        tile[b-top:d-top,a-left:c-left]=rgb[b:d,a:c]
        keep[b-top:d-top,a-left:c-left]=1-soft_mask[b:d,a:c]
        with torch.inference_mode():
            tensor=torch.from_numpy(tile.transpose(2,0,1).copy()).unsqueeze(0).to(self.device)/127.5-1
            mask=torch.from_numpy(keep).unsqueeze(0).unsqueeze(0).to(self.device)
            prediction=self.repairer(tensor*mask,mask)
            patch=prediction[0,:,y0-top:y1-top,x0-left:x1-left].permute(1,2,0).cpu().numpy()
            result=(patch+1)*127.5
        self._sync()
        return result


def _read_array(path, mask=False):
    path=Path(path)
    if path.suffix.lower()=='.npy':return np.load(path,allow_pickle=False)
    from PIL import Image
    with Image.open(path) as image:
        return np.asarray(image.convert('L' if mask else 'RGB')).astype(np.float32)/255 if mask else np.asarray(image.convert('RGB')).copy()


def configure_worker_cache(output, cache_root=None):
    """Use an explicit short runtime-cache root independently of job nesting."""
    configured = cache_root if cache_root is not None else os.environ.get('PORTRAITLAB_WORKER_CACHE_ROOT')
    if configured is not None:
        if not isinstance(configured,(str,Path)) or not str(configured).strip() or not Path(configured).is_absolute():
            raise ValueError('工作缓存目录须为明确的绝对路径。')
        cache=Path(configured).resolve()
    else:
        cache=Path(output).resolve()/'worker-cache'
    boundary=os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not cache.is_relative_to(Path(boundary).resolve()):
        raise ValueError('工作缓存目录超出已配置的数据目录，请检查缓存设置。')
    # Windows native DLL/subprocess components may still use MAX_PATH-limited
    # temporary paths. Reserve room for generated filenames and subdirectories.
    if os.name=='nt' and len(str(cache))>200:
        raise ValueError('工作缓存目录过长，请通过 PORTRAITLAB_WORKER_CACHE_ROOT 配置较短的绝对路径（根路径不超过 200 字符）。')
    for variable,folder in [('TEMP','temp'),('TMP','temp'),('HF_HOME','hf'),('TORCH_HOME','torch'),
        ('MPLCONFIGDIR','matplotlib'),('XDG_CACHE_HOME','xdg'),('CUDA_CACHE_PATH','cuda'),
        ('TORCH_EXTENSIONS_DIR','torch-extensions'),('TORCHINDUCTOR_CACHE_DIR','inductor'),('TRITON_CACHE_DIR','triton')]:
        target=cache/folder;target.mkdir(parents=True,exist_ok=True);os.environ[variable]=str(target)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    sys.dont_write_bytecode=True
    import tempfile
    tempfile.tempdir=None
    return cache


def run_request(request, predictor=None):
    required={'input','roi','allowed_mask','vendor','models','output'}
    if not isinstance(request,dict) or not required.issubset(request):
        raise ValueError('Request requires '+', '.join(sorted(required)))
    out=Path(request['output']).resolve()
    boundary=os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not out.is_relative_to(Path(boundary).resolve()):
        raise ValueError('Output is outside the configured writable storage root')
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Candidate output must be new or empty')
    if Path(request['input']).resolve().is_relative_to(out) or Path(request['allowed_mask']).resolve().is_relative_to(out):
        raise ValueError('Inputs cannot be inside the candidate output directory')
    out.mkdir(parents=True,exist_ok=True)
    configure_worker_cache(out,request.get('cache_root'))
    started=time.perf_counter()
    rgb=_read_array(request['input'])
    allowed=_read_array(request['allowed_mask'],True)
    _validate(rgb,request['roi'],allowed)
    predictor=predictor or ModelScopeLocalPredictor(request['vendor'],request['models'],request.get('device','auto'))
    metadata,patches=analyse_candidates(rgb,request['roi'],allowed,predictor,
        threshold=request.get('threshold',.35),min_area=request.get('min_area',4),max_candidates=request.get('max_candidates',64))
    metadata.update(device=str(predictor.device),device_name=predictor.device_name,
        model_sha256=MODEL_SHA256,input_sha256=sha256(request['input']),allowed_mask_sha256=sha256(request['allowed_mask']))
    metadata['timings']['model_load_seconds']=predictor.load_seconds
    for candidate in metadata['candidates']:
        path=out/candidate['patch']
        np.savez_compressed(path,**patches[candidate['id']])
        candidate['patch_sha256']=sha256(path)
    metadata['timings']['request_seconds']=time.perf_counter()-started
    (out/'SOURCE-LICENSE.txt').write_bytes((Path(request['vendor'])/'LICENSE').read_bytes())
    (out/'candidates.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',type=Path,required=True)
    args=parser.parse_args()
    try:
        request=json.loads(args.request.read_text(encoding='utf-8-sig'))
        result=run_request(request)
        print(json.dumps({'ok':True,'manifest':str(Path(request['output']).resolve()/'candidates.json'),
            'candidates':len(result['candidates']),'timings':result['timings']},ensure_ascii=False),flush=True)
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error),'error_type':type(error).__name__},ensure_ascii=False),flush=True)
        raise SystemExit(1)


if __name__=='__main__':main()
