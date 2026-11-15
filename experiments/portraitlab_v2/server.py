"""Native-resolution tiles and opt-in neural blemish candidates.

SPDX-License-Identifier: AGPL-3.0-only
This experimental server uses explicit storage/model paths. No downloads occur.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
from time import perf_counter
import uuid

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageOps
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
TILE = 512
MAX_PIXELS = 80_000_000
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def decode(path):
    with Image.open(path) as image:
        if image.format not in ('JPEG', 'PNG', 'TIFF', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('仅支持单帧 JPEG、PNG、TIFF 和 WebP。')
        if min(image.size) < 16 or image.width * image.height > MAX_PIXELS:
            raise ValueError('图像须至少 16×16，最多 8000 万像素。')
        icc = image.info.get('icc_profile')
        transformed = ImageOps.exif_transpose(image)
        if icc:
            transformed = ImageCms.profileToProfile(transformed, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                    ImageCms.createProfile('sRGB'), outputMode='RGB')
        return np.asarray(transformed.convert('RGB')).copy()


def png(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, format='PNG', icc_profile=SRGB, compress_level=1)
    return stream.getvalue()


def downsample(array,scale):
    if scale==1:return array
    h,w=array.shape[:2];bottom,right=(-h)%scale,(-w)%scale
    if bottom or right:array=cv2.copyMakeBorder(array,0,bottom,0,right,cv2.BORDER_REPLICATE)
    return cv2.resize(array,((w+scale-1)//scale,(h+scale-1)//scale),interpolation=cv2.INTER_AREA)


class Store:
    def __init__(self, data, *, analysis_models=None, ai_python=None, neural_models=None, vendor=None):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self.render_slots = threading.Semaphore(2)
        self.state_path = self.data / 'state.json'
        self.photos = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
        self.analysis_models, self.ai_python = analysis_models, ai_python
        self.neural_models, self.vendor = neural_models, vendor
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='candidate-analysis')
        self.engine = None
        self.arrays, self.patches = {}, {}
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.tile_metrics = []
        self.process = None
        self.worker_queue = None
        self.worker_log = None
        self.closing = False
        for p in self.photos.values():
            p.setdefault('manual_requests',{})
            if p['analysis']['status'] == 'running':
                p['analysis'] = {'status':'error', 'message':'上次分析已中断，可以重新分析。'}
            for job in p['manual_requests'].values():
                if job['status']=='running':job['status']='error'
        from experiments.portraitlab_v2.workspace import Workspace
        from experiments.portraitlab_v2.model_catalog import ModelCatalog
        self.workspace = Workspace(self)
        self.models = ModelCatalog(self)
        from experiments.portraitlab_v2.render_proxy import ProxyRenderer
        self.proxy = ProxyRenderer(self)

    @staticmethod
    def snapshot(p):
        # Keep immutable asset generations with the edit: undo must never attach
        # previous parameters to a newly analysed person or candidate set.
        return deepcopy({key:p[key] for key in ('selected_ids','strength','settings','candidates',
            'analysis_generation','faces','face_index','_assets','preparation') if key in p})

    def persist(self):
        write_json(self.state_path, self.photos)

    def folder(self, identifier):
        if identifier not in self.photos:
            raise ValueError('照片不存在。')
        return self.data / 'photos' / identifier

    def public(self, identifier):
        with self.lock:
            p = json.loads(json.dumps(self.photos[identifier]))
        p.pop('manual_requests',None)
        p['thumbnail_url'] = f'/api/photos/{identifier}/thumbnail'
        for c in p['candidates']:
            c.pop('patch', None)
            c['thumbnail_url'] = f'/api/photos/{identifier}/candidates/{c["id"]}/thumbnail'
        for face in p.get('faces', []):
            face['thumbnail_url'] = f'/api/photos/{identifier}/faces/{face["face_index"]}/thumbnail'
        return self.workspace.decorate(p)

    def original(self, identifier):
        with self.lock:
            if identifier not in self.arrays:
                self.arrays[identifier] = np.load(self.folder(identifier)/'original.npy', mmap_mode='r', allow_pickle=False)
            return self.arrays[identifier]

    def import_path(self, path, name=None):
        rgb = decode(path)
        identifier = uuid.uuid4().hex
        directory = self.data/'photos'/identifier
        directory.mkdir(parents=True)
        np.save(directory/'original.npy', rgb, allow_pickle=False)
        h,w = rgb.shape[:2]
        thumb = Image.fromarray(rgb); thumb.thumbnail((240,240))
        thumb.save(directory/'thumbnail.png', icc_profile=SRGB)
        p = {'id':identifier,'name':Path(name or path.name).name,'width':w,'height':h,'version':0,
             'tile_size':TILE,'levels':max(1,math.ceil(math.log2(max(w,h)/TILE))+1),
             'selected_ids':[],'strength':.7,'candidates':[],'faces':[], 'history':[], 'redo':[],
             'analysis':{'status':'idle','message':'原片已载入。分析只提出候选，默认不修改照片。'},
             'analysis_generation':'none','working_format':'8-bit sRGB input; float32 linear editing; 8-bit sRGB PNG output'}
        from experiments.portraitlab_v2.workspace import defaults
        p.update(settings=defaults(),_assets={},preparation={'status':'idle','analysis_ready':False,'skin_ready':False,'geometry_ready':False,
            'message':'可先调整曝光与色彩；人像专项需准备照片。'})
        with self.lock:
            self.photos[identifier] = p
            self.persist()
        return self.public(identifier)

    def _patch_data(self, identifier, candidate, generation):
        key = (identifier,generation,candidate['id'])
        with self.lock:
            if key not in self.patches:
                directory = (self.folder(identifier)/'analysis'/generation).resolve()
                path = (directory/candidate['patch']).resolve()
                if not path.is_relative_to(directory):
                    raise ValueError('候选文件路径无效。')
                with np.load(path, allow_pickle=False) as archive:
                    value={'delta':archive['delta'].astype(np.float32)}
                    if 'mask' in archive:
                        value['mask']=archive['mask'].astype(np.float32)
                        if value['mask'].shape!=value['delta'].shape[:2] or not np.isfinite(value['mask']).all() or np.any((value['mask']<0)|(value['mask']>1)):
                            raise ValueError('修补蒙版无效。')
                    elif candidate.get('kind')=='manual':raise ValueError('手动修补缺少范围蒙版。')
                    self.patches[key] = value
            return self.patches[key]

    def repair_region(self, identifier, bounds, selected_ids=None, strength=None, snapshot=None):
        with self.lock:
            p = snapshot or json.loads(json.dumps(self.photos[identifier]))
        x0,y0,x1,y1 = bounds
        source = self.original(identifier)[y0:y1,x0:x1]
        selected = set(p['selected_ids'] if selected_ids is None else selected_ids)
        amount = p['strength'] if strength is None else strength
        changes = [c for c in p['candidates'] if c['id'] in selected and
                   c['bounds'][0]<x1 and c['bounds'][2]>x0 and c['bounds'][1]<y1 and c['bounds'][3]>y0]
        if not changes or not amount:
            return source.copy()
        result = source.astype(np.float32)
        for c in changes:
            a,b,d,e = c['bounds']; ix0,iy0,ix1,iy1 = max(a,x0),max(b,y0),min(d,x1),min(e,y1)
            patch = self._patch_data(identifier,c,p['analysis_generation'])
            delta=patch['delta'][iy0-b:iy1-b,ix0-a:ix1-a]
            target=result[iy0-y0:iy1-y0,ix0-x0:ix1-x0]
            if c.get('kind')=='manual':
                alpha=patch['mask'][iy0-b:iy1-b,ix0-a:ix1-a,None]*amount
                base=source[iy0-y0:iy1-y0,ix0-x0:ix1-x0]
                target[:]=base+(target-base)*(1-alpha)+delta*amount
            else:target+=delta*amount
        return np.rint(np.clip(result,0,255)).astype(np.uint8)

    def region(self, identifier, bounds, selected_ids=None, strength=None, snapshot=None, settings=None):
        with self.lock:
            p=deepcopy(snapshot or self.photos[identifier])
        if selected_ids is not None:p['selected_ids']=selected_ids
        if strength is not None:p['strength']=strength
        return self.workspace.render(identifier,list(bounds),p,settings=settings)

    def tile(self, identifier, kind, level, x, y, expected_version=None, draft=None, quality='native'):
        started = perf_counter()
        with self.lock:
            p = json.loads(json.dumps(self.photos[identifier]))
            edit=deepcopy(self.workspace.drafts.get(identifier)) if kind=='preview' else None
        if kind in ('result','preview') and expected_version is not None and expected_version != p['version']:
            raise HTTPException(409, '画布版本已更新。')
        if kind=='preview' and (not edit or edit['id']!=draft or edit['base_version']!=p['version']):
            raise HTTPException(409,'草稿已变化，请查看当前预览。')
        if kind not in ('original','result','preview') or not 0<=level<p['levels'] or min(x,y)<0:
            raise ValueError('图块坐标无效。')
        if quality not in ('native','interactive'):raise ValueError('预览质量无效。')
        if level==0 or kind=='original':quality='native'
        scale=2**level
        x0,y0 = x*TILE*scale,y*TILE*scale
        x1,y1 = min(p['width'],x0+TILE*scale),min(p['height'],y0+TILE*scale)
        if x0>=x1 or y0>=y1:
            raise ValueError('图块超出照片。')
        from experiments.portraitlab_v2.workspace import defaults
        settings=edit['settings'] if edit else p.get('settings',defaults())
        if kind=='original':identity=()
        elif settings==defaults():
            affected=tuple(c['id'] for c in p['candidates'] if c['id'] in p['selected_ids'] and
                c['bounds'][0]<x1 and c['bounds'][2]>x0 and c['bounds'][1]<y1 and c['bounds'][3]>y0)
            identity=(p['analysis_generation'] if affected else '',affected,p['strength'] if affected else 0)
        else:identity=(p['version'],draft if kind=='preview' else '')
        key = (identifier,kind,level,x,y,identity,quality)
        with self.lock:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                self.tile_metrics.append({'level':level,'cached':True,'ms':(perf_counter()-started)*1000})
                self.tile_metrics = self.tile_metrics[-2000:]
                return cached
        with self.render_slots:
            if kind=='original':
                array=self.original(identifier)[y0:y1,x0:x1]
                if scale>1:array=downsample(array,scale)
            elif quality=='interactive':
                self.workspace.validate_ready(p,settings)
                array=self.proxy.tile(p,settings,(x0,y0,x1,y1),level)
            else:
                # Bound temporary float buffers even at fit-to-window zoom.
                # Aligned chunks preserve the exact native renderer at every mip.
                array=np.zeros((math.ceil((y1-y0)/scale),math.ceil((x1-x0)/scale),3),np.uint8)
                edge=max(TILE,scale)
                for top in range(y0,y1,edge):
                    for left in range(x0,x1,edge):
                        with self.lock:
                            if self.closing:raise ValueError('原型正在退出。')
                            if kind=='preview' and self.workspace.drafts.get(identifier,{}).get('id')!=draft:raise HTTPException(409,'草稿已变化。')
                            if self.photos[identifier]['version']!=p['version']:raise HTTPException(409,'画布版本已更新。')
                        right,bottom=min(x1,left+edge),min(y1,top+edge)
                        chunk=self.region(identifier,(left,top,right,bottom),snapshot=p,settings=edit['settings'] if edit else None)
                        if scale>1:chunk=downsample(chunk,scale)
                        dy,dx=(top-y0)//scale,(left-x0)//scale
                        array[dy:dy+chunk.shape[0],dx:dx+chunk.shape[1]]=chunk
        with self.lock:
            if kind!='original' and self.photos[identifier]['version']!=p['version']:raise HTTPException(409,'画布版本已更新。')
            if kind=='preview' and self.workspace.drafts.get(identifier,{}).get('id')!=draft:raise HTTPException(409,'草稿已变化。')
        encoded = png(array)
        with self.lock:
            if key not in self.cache:
                self.cache[key] = encoded; self.cache_bytes += len(encoded)
            while self.cache_bytes>128*1024*1024:
                _,previous = self.cache.popitem(last=False); self.cache_bytes -= len(previous)
            self.tile_metrics.append({'level':level,'cached':False,'ms':(perf_counter()-started)*1000})
            self.tile_metrics = self.tile_metrics[-2000:]
        return encoded

    def select(self, identifier, value):
        if not isinstance(value,dict) or set(value)-{'selected_ids','expected_version','strength'}:
            raise ValueError('选择参数无效。')
        ids = value.get('selected_ids')
        if not isinstance(ids,list) or len(ids)>1000 or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):
            raise ValueError('候选选择列表无效。')
        with self.lock:
            p=self.photos[identifier]
            self.workspace.ensure_editable(p,value.get('expected_version',p['version']))
            if value.get('expected_version',p['version'])!=p['version']:
                raise HTTPException(409,'照片状态已更新，请刷新后重试。')
            if p['analysis']['status']=='running':
                raise HTTPException(409,'分析进行中，完成后再选择候选。')
            if set(ids)-{c['id'] for c in p['candidates']}:
                raise ValueError('候选不属于当前照片。')
            strength=value.get('strength',p['strength'])
            if isinstance(strength,bool) or not isinstance(strength,(int,float)) or not math.isfinite(strength) or not 0<=strength<=1:
                raise ValueError('修复强度须在 0 到 1 之间。')
            from experiments.portraitlab_v2.workspace import defaults
            proposed={**p,'selected_ids':ids,'strength':strength}
            self.workspace.validate_ready(proposed,p.get('settings',defaults()))
            if ids!=p['selected_ids'] or strength!=p['strength']:
                before=json.loads(json.dumps(p))
                p['history'].append(self.snapshot(p))
                p['history']=p['history'][-100:];p['redo']=[]
                p['selected_ids'],p['strength']=ids,strength;p['version']+=1
                try:self.persist()
                except OSError:
                    self.photos[identifier]=before
                    raise
        return self.public(identifier)

    def history(self,identifier,direction,expected_version=None):
        with self.lock:
            p=self.photos[identifier]
            self.workspace.ensure_editable(p,p['version'] if expected_version is None else expected_version)
            if expected_version is not None and expected_version!=p['version']:
                raise HTTPException(409,'照片状态已更新，请查看当前结果后再操作。')
            if p['analysis']['status']=='running':
                raise HTTPException(409,'分析进行中。')
            source,target=(p['history'],p['redo']) if direction=='undo' else (p['redo'],p['history'])
            if source:
                before=json.loads(json.dumps(p))
                target.append(self.snapshot(p))
                previous=source.pop();p.update(previous);p['version']+=1
                try:self.persist()
                except OSError:
                    self.photos[identifier]=before
                    raise
        return self.public(identifier)

    def manual(self,identifier,value):
        from experiments.portraitlab_v2.manual_repair import validate_strokes
        if not isinstance(value,dict) or set(value)-{'expected_version','strokes','device','request_id'}:
            raise ValueError('手动标记参数无效。')
        device=value.get('device','auto')
        if device not in ('auto','cpu','cuda'):raise ValueError('计算设备无效。')
        expected=value.get('expected_version')
        if isinstance(expected,bool) or not isinstance(expected,int):raise ValueError('手动修补须携带当前照片版本。')
        request_id=value.get('request_id',uuid.uuid4().hex)
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:raise ValueError('请求编号无效。')
        if not all((self.ai_python,self.neural_models,self.vendor)):raise ValueError('手动修补尚未配置模型路径。')
        with self.lock:
            p=self.photos[identifier]
            strokes=validate_strokes(value.get('strokes'),p['width'],p['height'])
            fingerprint=hashlib.sha256(json.dumps([device,strokes],sort_keys=True).encode()).hexdigest()
            requests=p.setdefault('manual_requests',{})
            if request_id in requests:
                previous=requests[request_id]
                if previous['fingerprint']!=fingerprint:raise HTTPException(409,'此请求编号已用于另一组标记。')
                if previous['status'] in ('running','complete'):return self.public(identifier)
            if self.closing:raise ValueError('原型正在退出。')
            if p['version']!=expected:raise HTTPException(409,'照片状态已更新，请检查后重新标记。')
            self.workspace.ensure_editable(p,expected)
            if p['analysis']['status']=='running':raise HTTPException(409,'当前照片正在处理，请稍候。')
            job_id=uuid.uuid4().hex
            generation=p['analysis_generation']
            before=json.loads(json.dumps(p))
            requests[request_id]={'status':'running','fingerprint':fingerprint,'job_id':job_id}
            while len(requests)>100:requests.pop(next(iter(requests)))
            p['analysis']={'status':'running','kind':'manual','job_id':job_id,'message':'正在修补手动标记区域；首次使用需加载模型。完成前仍显示已保留的结果。'}
            try:self.persist()
            except OSError:
                self.photos[identifier]=before
                raise
            self.pool.submit(self._manual,identifier,strokes,device,expected,generation,request_id,job_id)
        return self.public(identifier)

    def _manual(self,identifier,strokes,device,version,previous_generation,request_id,job_id):
        started=perf_counter()
        try:
            generation=previous_generation if previous_generation!='none' else uuid.uuid4().hex
            directory=self.folder(identifier)/'analysis'/generation/'manual'/job_id
            directory.mkdir(parents=True)
            request={'kind':'manual','input':str(self.folder(identifier)/'original.npy'),'strokes':strokes,
                     'vendor':str(Path(self.vendor).resolve()),'models':str(Path(self.neural_models).resolve()),
                     'output':str(directory/'result'),'device':device,'cache_root':str(self.data/'cache'/'worker')}
            write_json(directory/'request.json',request)
            response=self._request_worker(request)
            write_json(directory/'worker-response.json',response)
            result=json.loads((directory/'result'/'candidates.json').read_text(encoding='utf-8'))
            added=result['candidates']
            for index,candidate in enumerate(added):
                candidate.update(id=f'm-{job_id}-{index+1}',kind='manual',source='manual',group_id=job_id,default_enabled=True)
                candidate['patch']=f'manual/{job_id}/result/'+candidate['patch']
            with self.lock:
                p=self.photos[identifier]
                if p['version']!=version or p['analysis_generation']!=previous_generation or p['analysis'].get('job_id')!=job_id:
                    raise ValueError('照片在修补期间已变化，本次修补未应用。')
                before=json.loads(json.dumps(p))
                if added:
                    p['history'].append(self.snapshot(p))
                    p['history']=p['history'][-100:];p['redo']=[]
                    p['candidates'].extend(added)
                    p['selected_ids']=p['selected_ids']+[c['id'] for c in added]
                    p['analysis_generation']=generation;p['version']+=1
                p['manual_requests'][request_id]['status']='complete'
                p['analysis']={'status':'ready','kind':'manual','job_id':job_id,
                    'message':f'已修补 {len(strokes)} 笔手动标记；可取消对应修补或撤销本次操作。' if added else '标记区域没有产生可见变化；照片保持原样。',
                    'device':result.get('device'),'timings':result.get('timings'),
                    'model_reused':response.get('model_reused',False),'total_seconds':perf_counter()-started}
                try:self.persist()
                except OSError:
                    self.photos[identifier]=before
                    raise
        except Exception as error:
            with self.lock:
                p=self.photos[identifier]
                if p['analysis'].get('job_id')==job_id:
                    p['manual_requests'][request_id]['status']='error'
                    p['analysis']={'status':'error','kind':'manual','job_id':job_id,'message':'手动修补未应用：'+str(error)}
                    try:self.persist()
                    except OSError:pass  # Keep the error visible; prior persisted edits remain valid.

    def analyze(self,identifier,face_index=None,device='auto'):
        if device not in ('auto','cpu','cuda') or (face_index is not None and (isinstance(face_index,bool) or not isinstance(face_index,int))):
            raise ValueError('分析设备或人物索引无效。')
        if not all((self.analysis_models,self.ai_python,self.neural_models,self.vendor)):
            raise ValueError('此原型尚未配置模型路径，原尺寸查看仍可使用。')
        with self.lock:
            if self.closing:raise ValueError('原型正在退出。')
            p=self.photos[identifier]
            if p['analysis']['status']=='running':
                return self.public(identifier)
            self.workspace.ensure_editable(p,p['version'])
            if p['selected_ids']:
                raise ValueError('重新分析前请先清除当前候选选择。')
            if p.get('preparation',{}).get('analysis_ready'):
                if face_index is None:face_index=p.get('face_index')
                elif face_index!=p.get('face_index'):
                    raise ValueError('请先在照片准备中选择该人物，避免候选与当前修片人物不一致。')
            p['analysis']={'status':'running','kind':'automatic','message':'正在分析人物与候选瑕疵；首次初始化模型需要等待，画布仍可查看。'};self.persist()
        self.pool.submit(self._analyze,identifier,face_index,device)
        return self.public(identifier)

    def _analyze(self,identifier,face_index,device):
        started=perf_counter()
        with self.lock:expected_version=self.photos[identifier]['version']
        try:
            from portraitlab.engine import Engine
            if self.engine is None:
                self.engine=Engine(Path(self.analysis_models))
            rgb=self.original(identifier)
            analysis=self.engine.analyze(rgb)
            if self.closing:raise ValueError('分析已随原型退出而取消。')
            h,w=rgb.shape[:2]
            faces=[]
            for index,points in enumerate(analysis['faces']):
                pixels=np.asarray(points)[:,:2]*[w,h]
                lo,hi=np.maximum(pixels.min(0),0),np.minimum(pixels.max(0),[w,h])
                faces.append({'face_index':index,'bounds':[int(lo[0]),int(lo[1]),int(hi[0]),int(hi[1])]})
            if not faces:
                raise ValueError('未识别到人脸。此阶段可继续原尺寸查看，未自动修改照片。')
            face = max(faces,key=lambda f:(f['bounds'][2]-f['bounds'][0])*(f['bounds'][3]-f['bounds'][1])) if face_index is None else next((f for f in faces if f['face_index']==face_index),None)
            if face is None:
                raise ValueError('所选人物不存在。')
            x0,y0,x1,y1=face['bounds'];radius=max(x1-x0,y1-y0)*.75;cx,cy=(x0+x1)/2,(y0+y1)/2
            roi=[max(0,round(cx-radius)),max(0,round(cy-radius)),min(w,round(cx+radius)),min(h,round(cy+radius))]
            generation=uuid.uuid4().hex
            directory=self.folder(identifier)/'analysis'/generation;directory.mkdir(parents=True)
            allowed=cv2.resize(analysis['face_mask'],(w,h),interpolation=cv2.INTER_LINEAR)[roi[1]:roi[3],roi[0]:roi[2]]
            np.save(directory/'allowed-mask.npy',allowed,allow_pickle=False)
            request={'input':str(self.folder(identifier)/'original.npy'),'roi':roi,'allowed_mask':str(directory/'allowed-mask.npy'),
                     'vendor':str(Path(self.vendor).resolve()),'models':str(Path(self.neural_models).resolve()),
                     'output':str(directory/'result'),'device':device,'threshold':.35,'cache_root':str(self.data/'cache'/'worker')}
            write_json(directory/'request.json',request)
            response=self._request_worker(request)
            write_json(directory/'worker-response.json',response)
            result=json.loads((directory/'result'/'candidates.json').read_text(encoding='utf-8'))
            for candidate in result['candidates']:
                candidate['patch']='result/'+candidate['patch']
            with self.lock:
                p=self.photos[identifier]
                if p['version']!=expected_version:raise ValueError('照片在分析期间已变化，本次候选未应用。')
                before=deepcopy(p)
                p['history'].append(self.snapshot(p));p['history']=p['history'][-100:];p['redo']=[]
                p.update(candidates=result['candidates'],selected_ids=[],faces=faces,face_index=face['face_index'],
                         analysis_generation=generation,version=p['version']+1,
                         analysis={'status':'ready','kind':'automatic','message':f"发现 {len(result['candidates'])} 处候选，尚未修改照片。请逐处检查，痣与雀斑也可能被误识别。",
                                   'device':result.get('device'),'timings':result.get('timings'),
                                   'model_reused':response.get('model_reused',False),'total_seconds':perf_counter()-started})
                try:self.persist()
                except OSError:
                    self.photos[identifier]=before
                    raise
        except Exception as error:
            with self.lock:
                previous='本次分析失败，仍保留上一轮候选。' if self.photos[identifier]['candidates'] else ''
                self.photos[identifier]['analysis']={'status':'error','message':previous+str(error)}
                self.persist()

    def worker_environment(self):
        env=os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        env['PORTRAITLAB_WORKER_CACHE_ROOT']=str(self.data/'cache'/'worker')
        for variable,name in [('TEMP','temp'),('TMP','temp'),('HF_HOME','huggingface'),('TORCH_HOME','torch'),('CUDA_CACHE_PATH','cuda'),('MPLCONFIGDIR','matplotlib'),('XDG_CACHE_HOME','xdg')]:
            path=self.data/'cache'/name;path.mkdir(parents=True,exist_ok=True);env[variable]=str(path)
        return env

    def _request_worker(self,request,operation='analyse'):
        with self.worker_lock:
            return self._request_worker_serial(request,operation)

    def _request_worker_serial(self,request,operation):
        """One serialized analysis queue, one reusable isolated AI process."""
        with self.lock:
            if self.closing:raise ValueError('分析已随原型退出而取消。')
            if self.process is None or self.process.poll() is not None:
                if self.worker_log:self.worker_log.close()
                env=self.worker_environment()
                self.worker_log=(self.data/'cache'/'worker.log').open('a',encoding='utf-8')
                self.process=subprocess.Popen([str(self.ai_python),'-B',str(ROOT/'persistent_worker.py'),'--serve'],
                    cwd=self.data,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.worker_log,
                    text=True,encoding='utf-8',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                responses=queue.Queue();self.worker_queue=responses
                process=self.process
                def read_responses():
                    try:
                        for line in process.stdout:responses.put(line)
                    finally:responses.put(None)
                threading.Thread(target=read_responses,daemon=True,name='candidate-worker-output').start()
            process=self.process;responses=self.worker_queue
            job_id=uuid.uuid4().hex
            try:
                process.stdin.write(json.dumps({'id':job_id,'op':operation,'request':request},ensure_ascii=False)+'\n')
                process.stdin.flush()
            except OSError as error:
                raise ValueError('模型进程已中断，请重新分析。') from error
        try:line=responses.get(timeout=600)
        except queue.Empty:
            process.kill();process.wait();raise ValueError('分析超时，已停止模型进程，可以重试。')
        if line is None:raise ValueError('模型进程已退出，请检查独立数据目录 cache/worker.log。')
        response=json.loads(line)
        if response.get('id')!=job_id:raise ValueError('模型响应与当前任务不匹配。')
        if not response.get('ok'):raise ValueError('分析未完成：'+response.get('error','未知模型错误'))
        return response

    def export(self,identifier,expected_version=None):
        with self.lock:
            p=deepcopy(self.photos[identifier])
            self.workspace.ensure_editable(p,p['version'] if expected_version is None else expected_version)
        if expected_version is not None and expected_version!=p['version']:
            raise HTTPException(409,'照片状态已更新，请查看当前结果后再导出。')
        if p['analysis']['status']=='running':
            raise ValueError('分析完成后再导出。')
        folder=self.data/'exports';folder.mkdir(exist_ok=True)
        name=identifier+'-'+uuid.uuid4().hex[:8]+'.png'
        target=folder/name
        # Disk-backed output keeps the 39MP workflow from allocating multiple
        # full float images. All processing is the same native tile renderer.
        scratch=target.with_suffix('.rgb')
        result=np.memmap(scratch,mode='w+',dtype=np.uint8,shape=(p['height'],p['width'],3))
        try:
            with self.render_slots:
                for y in range(0,p['height'],TILE):
                    for x in range(0,p['width'],TILE):
                        right,bottom=min(p['width'],x+TILE),min(p['height'],y+TILE)
                        result[y:bottom,x:right]=self.region(identifier,(x,y,right,bottom),snapshot=p)
            Image.fromarray(result).save(target,icc_profile=SRGB)
        finally:
            result.flush();result._mmap.close();del result
            scratch.unlink(missing_ok=True)
        write_json(target.with_suffix('.json'),{'prototype':'portraitlab-v2','photo_id':identifier,'name':p['name'],
                    'selected_ids':p['selected_ids'],'strength':p['strength'],'analysis_generation':p['analysis_generation'],
                    'applied_candidates':[{key:c.get(key) for key in ('id','kind','group_id','bounds','patch_sha256')}
                                          for c in p['candidates'] if c['id'] in p['selected_ids']],
                    'dimensions':[p['width'],p['height']],'working_format':p['working_format'],
                    'settings':p.get('settings',{}),'version':p['version']})
        return {'path':str(target),'url':'/api/exports/'+name}

    def close(self):
        with self.lock:
            self.closing=True
            self.workspace.close()
            if self.process and self.process.poll() is None:self.process.terminate()
        self.pool.shutdown(wait=True,cancel_futures=True)
        if self.process:
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        if self.worker_log:self.worker_log.close()
        if self.engine:self.engine.close()


def create_app(data,port=18891,**configuration):
    store=Store(data,**configuration);token=secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(app):
        yield
        await run_in_threadpool(store.close)
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store;app.state.token=token
    @app.middleware('http')
    async def local(request,call_next):
        host=request.headers.get('host','').lower()
        if host not in (f'127.0.0.1:{port}',f'localhost:{port}'):
            return JSONResponse({'detail':'主机不匹配。'},403)
        origin=request.headers.get('origin')
        if origin and origin!='http://'+host:
            return JSONResponse({'detail':'不允许跨站访问。'},403)
        if request.method not in ('GET','HEAD') and request.headers.get('X-PortraitLab-Token')!=token:
            return JSONResponse({'detail':'请刷新本地原型页面。'},403)
        try:return await call_next(request)
        except (ValueError,KeyError) as error:return JSONResponse({'detail':str(error)},400)
    @app.get('/api/bootstrap')
    def bootstrap():
        active=[i for i,p in store.photos.items() if not p.get('archived')]
        return {'token':token,'application':'portraitlab-v2',
                'version':os.environ.get('PORTRAITLAB_BUILD_VERSION','v2-research-3'),
                'storage':{'data_dir':str(store.data)},'photos':[store.public(i) for i in active],
                'current_id':active[-1] if active else None,'tile_size':TILE,
                'capabilities':{'native_tiles':True,'selectable_neural_candidates':True,'automatic_beauty':False,
                                'manual_mark_repair':True,'advanced_relighting':True,'face_shape':True,
                                'retouch':True,'local_adjustments':True,'presets':True,'drafts':True},
                'models_configured':bool(all(configuration.values()))}
    @app.get('/api/models')
    def models():return store.models.public()
    @app.post('/api/models/check')
    def check_models():return store.models.check()
    @app.post('/api/models/download')
    async def download_model(request:Request):
        body=await request.json()
        if not isinstance(body,dict) or set(body)!={'id'}:raise ValueError('请选择需要下载的模型。')
        return store.models.start_download(body['id'])
    @app.get('/api/presets')
    def presets():
        from experiments.portraitlab_v2 import retouch_engine,lighting_v2
        from experiments.portraitlab_v2.workspace import defaults
        return {'categories':[{'id':i,'name':name} for i,name in [('general','通用'),('senior','长者'),('adult','成人'),('youth','少年')]],
            'presets':retouch_engine.list_presets(),'retouch_schema':retouch_engine.PARAM_SCHEMA,'defaults':defaults(),
            'lighting_presets':lighting_v2.LIGHT_PRESETS,'light_defaults':lighting_v2.DEFAULT_LIGHT}
    @app.post('/api/presets/apply')
    async def apply_presets(request:Request):return store.workspace.apply_batch(await object_body(request))
    @app.post('/api/presets/resolve')
    async def resolve_preset(request:Request):
        from experiments.portraitlab_v2 import retouch_engine
        body=await request.json()
        if not isinstance(body,dict) or set(body)-{'preset_id','current_params','strength'}:raise ValueError('预设参数无效。')
        return retouch_engine.resolve_preset(body.get('preset_id'),body.get('current_params'),body.get('strength',1))
    @app.get('/api/batch')
    def batch():
        with store.lock:return deepcopy(store.workspace.batch)
    @app.post('/api/photos/remove')
    async def remove_many(request:Request):
        body=await request.json()
        if not isinstance(body,dict) or set(body)!={'photo_ids'}:raise ValueError('请选择需要移除的照片。')
        return store.workspace.remove(body['photo_ids'])
    @app.delete('/api/photos/{identifier}')
    async def remove_one(identifier:str,request:Request):
        version=await expected(request)
        with store.lock:
            store.workspace.ensure_editable(store.photos[identifier],version)
            return store.workspace.remove([identifier])
    @app.post('/api/photos/{identifier}/preview')
    async def preview(identifier:str,request:Request):return store.workspace.preview(identifier,await object_body(request))
    @app.post('/api/photos/{identifier}/commit')
    async def commit(identifier:str,request:Request):return store.workspace.commit(identifier,await object_body(request))
    @app.post('/api/photos/{identifier}/discard')
    async def discard(identifier:str,request:Request):return store.workspace.discard(identifier,await object_body(request))
    @app.post('/api/photos/{identifier}/reset')
    async def reset(identifier:str,request:Request):return store.workspace.reset(identifier,await object_body(request))
    @app.post('/api/photos/{identifier}/prepare')
    async def prepare(identifier:str,request:Request):return store.workspace.prepare(identifier,await object_body(request))
    async def object_body(request):
        body=await request.json()
        if not isinstance(body,dict):raise ValueError('请求参数须为对象。')
        return body
    @app.get('/api/capabilities')
    def capabilities():
        return {name:json.loads((ROOT/name).read_text(encoding='utf-8')) for name in ['capability-plan.json','acceptance-plan.json','lighting-geometry-capabilities.json'] if (ROOT/name).exists()}
    @app.post('/api/import')
    async def upload(files:list[UploadFile]=File(...)):
        if not 1<=len(files)<=10:raise ValueError('每批最多导入10张。')
        photos=[];errors=[]
        for file in files:
            directory=store.data/'uploads';directory.mkdir(exist_ok=True)
            target=directory/(uuid.uuid4().hex+Path(file.filename or '').suffix.lower())
            size=0
            try:
                with target.open('wb') as stream:
                    while chunk:=await file.read(1024*1024):
                        size+=len(chunk)
                        if size>100*1024*1024:raise ValueError('单张照片最多100MiB。')
                        stream.write(chunk)
                photos.append(await run_in_threadpool(store.import_path,target,file.filename))
            except (ValueError,OSError) as error:
                errors.append({'name':file.filename or '未命名照片','message':str(error)})
            finally:
                target.unlink(missing_ok=True);await file.close()
        return {'photos':photos,'errors':errors}
    @app.get('/api/photos/{identifier}')
    def photo(identifier:str):return store.public(identifier)
    @app.get('/api/photos/{identifier}/tiles/{kind}/{level}/{x}/{y}.png')
    def tile(identifier:str,kind:str,level:int,x:int,y:int,v:int|None=None,draft:str|None=None,quality:str='native'):
        return Response(store.tile(identifier,kind,level,x,y,v,draft,quality),media_type='image/png',headers={'Cache-Control':'no-store',
            'X-PortraitLab-Quality':'native' if level==0 or kind=='original' else quality})
    @app.get('/api/photos/{identifier}/thumbnail')
    def thumbnail(identifier:str):return FileResponse(store.folder(identifier)/'thumbnail.png',media_type='image/png')
    def crop_thumb(identifier,bounds):
        array=store.original(identifier);h,w=array.shape[:2]
        x0,y0,x1,y1=bounds
        im=Image.fromarray(np.asarray(array[max(0,y0):min(h,y1),max(0,x0):min(w,x1)]));im.thumbnail((160,160))
        return Response(png(np.asarray(im)),media_type='image/png')
    @app.get('/api/photos/{identifier}/candidates/{candidate}/thumbnail')
    def candidate_thumb(identifier:str,candidate:str):
        c=next((c for c in store.photos[identifier]['candidates'] if c['id']==candidate),None)
        if c is None:raise HTTPException(404,'候选不存在。')
        return crop_thumb(identifier,c.get('thumb_bounds',c['bounds']))
    @app.get('/api/photos/{identifier}/faces/{face_index}/thumbnail')
    def face_thumb(identifier:str,face_index:int):
        f=next((f for f in store.photos[identifier]['faces'] if f['face_index']==face_index),None)
        if f is None:raise HTTPException(404,'人物不存在。')
        return crop_thumb(identifier,f['bounds'])
    @app.post('/api/photos/{identifier}/selection')
    async def selection(identifier:str,request:Request):return store.select(identifier,await request.json())
    @app.post('/api/photos/{identifier}/manual')
    async def manual(identifier:str,request:Request):return store.manual(identifier,await request.json())
    @app.post('/api/photos/{identifier}/undo')
    async def undo(identifier:str,request:Request):return store.history(identifier,'undo',await expected(request))
    @app.post('/api/photos/{identifier}/redo')
    async def redo(identifier:str,request:Request):return store.history(identifier,'redo',await expected(request))
    async def expected(request):
        body=await request.json()
        if (not isinstance(body,dict) or set(body)!={'expected_version'} or
            isinstance(body['expected_version'],bool) or not isinstance(body['expected_version'],int)):
            raise ValueError('操作须携带当前照片版本。')
        return body['expected_version']
    @app.post('/api/photos/{identifier}/analyze')
    async def analyze(identifier:str,request:Request):
        body=await request.json()
        if not isinstance(body,dict) or set(body)-{'face_index','device'}:raise ValueError('分析参数无效。')
        return store.analyze(identifier,body.get('face_index'),body.get('device','auto'))
    @app.post('/api/photos/{identifier}/export')
    async def export(identifier:str,request:Request):return await run_in_threadpool(store.export,identifier,await expected(request))
    @app.get('/api/exports/{name}')
    def download(name:str):
        if Path(name).name!=name or not name.endswith('.png'):raise ValueError('文件名无效。')
        if not (store.data/'exports'/name).is_file():raise HTTPException(404,'导出文件不存在。')
        return FileResponse(store.data/'exports'/name,media_type='image/png',filename=name)
    @app.get('/api/metrics')
    def metrics():return {'tiles':store.tile_metrics,'encoded_cache_bytes':store.cache_bytes}
    @app.post('/api/shutdown')
    def shutdown():
        app.state.shutdown_callback();return {'ok':True}
    app.mount('/',StaticFiles(directory=ROOT/'web',html=True),name='web')
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18891)
    for name in ('analysis-models','ai-python','neural-models','vendor'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--seed-photo',type=Path)
    args=parser.parse_args()
    app=create_app(args.data,args.port,analysis_models=args.analysis_models,ai_python=args.ai_python,neural_models=args.neural_models,vendor=args.vendor)
    if args.seed_photo and not app.state.store.photos:app.state.store.import_path(args.seed_photo)
    import uvicorn
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,access_log=False))
    app.state.shutdown_callback=lambda:setattr(server,'should_exit',True)
    server.run()


if __name__=='__main__':main()
