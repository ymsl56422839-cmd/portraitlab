"""Pinned neural skin-equalization curve field, never a face-generation model.

SPDX-License-Identifier: AGPL-3.0-only
Network definition: Alibaba / ModelScope, Apache-2.0, fixed source commit below.
The existing reviewed model card identifies these weights as Apache-2.0.
No wrapper imports, downloads, whitening branch, or replacement-face synthesis.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import types
import warnings

import numpy as np

if __package__:
    from . import neural_candidate as core
else:
    _directory=str(Path(__file__).resolve().parent)
    if _directory not in sys.path:sys.path.insert(0,_directory)
    import neural_candidate as core

MODEL_FILE='pytorch_model.pt'
MODEL_SHA256='760d0c0b95c4e0a692165677fe2b82dbdf8fbbd4a3780ca966599c627b082993'
SOURCE_HASHES={
    'weights_init.py':'5feb5da62e7a97cca5e60488c96d76c48074ca7521526dca0a91498880381e5b',
    'unet_deploy.py':'5fa3b9a4ba599908025057d64fe3b44ad71f6bb8614c8d13618a4cd5bbe7936b',
}


class ModelScopeSkinPredictor:
    def __init__(self,vendor,models,device='auto'):
        if device not in ('auto','cpu','cuda'):raise ValueError('处理设备须为 auto、cpu 或 cuda。')
        started=time.perf_counter()
        vendor,models=Path(vendor).resolve(),Path(models).resolve()
        network_root=vendor/'modelscope/models/cv/skin_retouching'
        for relative,digest in SOURCE_HASHES.items():
            if core.sha256(network_root/relative)!=digest:raise ValueError('均肤网络代码校验失败：'+relative)
        if core.sha256(vendor/'LICENSE')!=core.LICENSE_SHA256:raise ValueError('均肤网络许可文件校验失败。')
        if core.sha256(models/MODEL_FILE)!=MODEL_SHA256:raise ValueError('均肤模型权重校验失败。')
        import torch
        self.torch=torch
        if device=='auto':device='cuda' if torch.cuda.is_available() else 'cpu'
        if device=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDA 当前不可用，请选择 CPU。')
        self.device=torch.device(device)
        torch.set_num_threads(min(8,max(1,os.cpu_count() or 1)))
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        for name,folder in [('modelscope',vendor/'modelscope'),('modelscope.models',vendor/'modelscope/models'),
                            ('modelscope.models.cv',vendor/'modelscope/models/cv'),
                            ('modelscope.models.cv.skin_retouching',network_root)]:
            existing=sys.modules.get(name)
            if existing is not None:
                if list(getattr(existing,'__path__',[]))!=[str(folder)]:
                    raise RuntimeError('已有其他 ModelScope 模块，请重启模型进程。')
            else:
                module=types.ModuleType(name);module.__path__=[str(folder)];sys.modules[name]=module
        loaded={}
        for relative in SOURCE_HASHES:
            name='modelscope.models.cv.skin_retouching.'+relative[:-3]
            spec=importlib.util.spec_from_file_location(name,network_root/relative)
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module
            # Upstream unet_deploy modifies warning filters at import time.
            with warnings.catch_warnings():spec.loader.exec_module(module)
            loaded[relative]=module
        self.network=loaded['unet_deploy.py'].UNet(3,3,init_weights=False)
        checkpoint=torch.load(models/MODEL_FILE,map_location='cpu',weights_only=True)
        self.network.load_state_dict(checkpoint['generator'],strict=True)
        del checkpoint
        self.network.to(self.device).eval()
        if device=='cuda':torch.cuda.synchronize(self.device)
        self.load_seconds=time.perf_counter()-started
        self.device_name=torch.cuda.get_device_name(self.device) if device=='cuda' else 'CPU'

    def predict_curve(self,rgb):
        torch=self.torch
        import torch.nn.functional as functional
        with torch.inference_mode():
            tensor=torch.from_numpy(np.ascontiguousarray(rgb.transpose(2,0,1))).unsqueeze(0).float()/127.5-1
            small=functional.interpolate(tensor,(512,512),mode='bilinear',align_corners=False).to(self.device)
            curve=self.network(small)[0].permute(1,2,0).cpu().numpy().copy()
        if self.device.type=='cuda':torch.cuda.synchronize(self.device)
        return curve


def _resize_channels(array,width,height):
    from PIL import Image
    return np.stack([np.asarray(Image.fromarray(array[...,c]).resize((width,height),Image.Resampling.BILINEAR),dtype=np.float32)
                     for c in range(array.shape[2])],axis=2)


def run_skin_request(request,predictor=None):
    required={'input','roi','allowed_mask','vendor','models','output'}
    if not isinstance(request,dict) or not required.issubset(request):
        raise ValueError('均肤请求缺少必要字段：'+', '.join(sorted(required)))
    out=Path(request['output']).resolve()
    boundary=os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not out.is_relative_to(Path(boundary).resolve()):raise ValueError('均肤输出超出已配置的数据目录。')
    if out.exists() and any(out.iterdir()):raise FileExistsError('均肤输出目录须为新目录或空目录。')
    for name in ('input','allowed_mask','vendor','models'):
        if Path(request[name]).resolve().is_relative_to(out):raise ValueError('均肤输入和模型文件不能放在输出目录内。')
    out.mkdir(parents=True,exist_ok=True)
    core.configure_worker_cache(out,request.get('cache_root'))
    started=time.perf_counter()
    rgb=core._read_array(request['input'])
    roi,allowed=core._validate(rgb,request['roi'],core._read_array(request['allowed_mask'],True))
    if not np.any(allowed>0):raise ValueError('当前面部没有可用皮肤选区，无法准备均肤层。')
    x0,y0,x1,y1=roi
    source=rgb[y0:y1,x0:x1]
    predictor=predictor or ModelScopeSkinPredictor(request['vendor'],request['models'],request.get('device','auto'))
    inference_started=time.perf_counter()
    curve=np.asarray(predictor.predict_curve(source),dtype=np.float32)
    if curve.shape!=(512,512,3) or not np.isfinite(curve).all() or np.any(curve<0) or np.any(curve>1):
        raise ValueError('均肤模型返回了无效曲线场。')
    inference_seconds=time.perf_counter()-inference_started
    width,height=x1-x0,y1-y0
    # Same neutral-centered quadratic operation as the reviewed upstream branch.
    # Border ramps stay inside ROI; semantic protection is applied exactly once.
    position=np.arange(500,dtype=np.float32)
    edge=np.minimum(np.minimum(position/20,(500-position)/20),1)
    border=_resize_channels(np.minimum(edge[:,None],edge[None,:])[...,None],width,height)[...,0]
    expanded=_resize_channels(curve,width,height)
    values=source.astype(np.float32)/255
    target=(1-2*expanded)*values*values+2*expanded*values
    mask=np.clip(allowed*border,0,1).astype(np.float32)
    delta=(np.clip(target,0,1)-values)*255*mask[...,None]
    delta[mask==0]=0
    patch=out/'skin-field.npz'
    np.savez_compressed(patch,delta=delta.astype(np.float32),mask=mask,skin_feature=curve)
    field={'bounds':list(roi),'patch':patch.name,'patch_sha256':core.sha256(patch),
           'semantic_mask_applied':True,'kind':'skin_curve','default_strength':0.0}
    metadata={'schema_version':1,'kind':'skin','model':core.MODEL_ID,'branch':'equalization_curve',
        'model_revision':core.MODEL_REVISION,'source_commit':core.SOURCE_COMMIT,'model_sha256':MODEL_SHA256,
        'license':'Apache-2.0','image_size':[rgb.shape[1],rgb.shape[0]],'roi':list(roi),
        'source_rgb_sha256':core.rgb_fingerprint(rgb),'input_sha256':core.sha256(request['input']),
        'allowed_mask_sha256':core.sha256(request['allowed_mask']),'fields':[field],
        'candidates':[],'device':str(predictor.device),'device_name':predictor.device_name,
        'timings':{'model_load_seconds':predictor.load_seconds,'inference_seconds':inference_seconds,
                   'request_seconds':time.perf_counter()-started},
        'warnings':['均肤只提供可调曲线层，不替代局部点修；效果需结合不同肤质原片统一验收。'],
        'delta_contract':'float32 RGB 0..255 difference, already multiplied by semantic and ROI-border masks'}
    (out/'SOURCE-LICENSE.txt').write_bytes((Path(request['vendor'])/'LICENSE').read_bytes())
    (out/'skin.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return metadata


def load_skin_fields(output_dir):
    root=Path(output_dir).resolve()
    metadata=json.loads((root/'skin.json').read_text(encoding='utf-8'))
    if metadata.get('kind')!='skin' or metadata.get('model_sha256')!=MODEL_SHA256:
        raise ValueError('均肤层清单与当前已审核模型不符。')
    result={key:metadata[key] for key in ('image_size','source_rgb_sha256','model_sha256','timings','warnings')}
    fields=[]
    for field in metadata['fields']:
        path=(root/field['patch']).resolve()
        if not path.is_relative_to(root) or core.sha256(path)!=field['patch_sha256']:
            raise ValueError('均肤层文件路径或校验值无效。')
        x0,y0,x1,y1=field['bounds']
        if not 0<=x0<x1<=metadata['image_size'][0] or not 0<=y0<y1<=metadata['image_size'][1]:
            raise ValueError('均肤层范围无效。')
        with np.load(path,allow_pickle=False) as archive:
            arrays={key:archive[key].astype(np.float32,copy=True) for key in ('delta','mask','skin_feature')}
        if arrays['delta'].shape!=(y1-y0,x1-x0,3) or arrays['mask'].shape!=(y1-y0,x1-x0):
            raise ValueError('均肤层尺寸无效。')
        if arrays['skin_feature'].shape!=(512,512,3) or any(not np.isfinite(a).all() for a in arrays.values()):
            raise ValueError('均肤层数据无效。')
        if np.any(arrays['mask']<0) or np.any(arrays['mask']>1) or np.any(arrays['delta'][arrays['mask']==0]!=0):
            raise ValueError('均肤层保护范围无效。')
        fields.append({**field,**arrays})
    result['fields']=fields
    return result
