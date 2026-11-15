"""Non-destructive editing transactions and one canonical full-resolution renderer.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
from time import perf_counter
import uuid

import cv2
import numpy as np
from fastapi import HTTPException

from . import retouch_engine as retouch
from . import lighting_v2 as lighting
from . import geometry_v2 as geometry


def defaults():
    return {'retouch':deepcopy(retouch.DEFAULT_PARAMS),'lighting':deepcopy(lighting.DEFAULT_LIGHTING),
            'geometry':geometry.validate_geometry(None),
            'local':{'enabled':False,'rect':None,'feather':20.,'params':deepcopy(retouch.DEFAULT_PARAMS)},
            'protection':[]}


def number(value,lo,hi,label):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
        raise ValueError(f'{label}须在 {lo} 至 {hi} 之间。')
    return float(value)


def validate_settings(raw,width,height):
    if not isinstance(raw,dict) or set(raw)-set(defaults()):raise ValueError('调整设置含未知字段。')
    settings={**defaults(),**deepcopy(raw)}
    settings['retouch']=retouch.validate_params(settings['retouch'])
    settings['lighting']=lighting.validate_lighting(settings['lighting'])
    settings['geometry']=geometry.validate_geometry(settings['geometry'])
    local=settings['local']
    if not isinstance(local,dict) or set(local)-{'enabled','rect','feather','params'}:raise ValueError('局部调整设置无效。')
    local={**defaults()['local'],**local}
    if type(local['enabled']) is not bool:raise ValueError('局部调整开关无效。')
    local['feather']=number(local['feather'],0,100,'选框羽化')
    local['params']=retouch.validate_params(local['params'])
    if local['rect'] is not None:
        r=local['rect']
        if not isinstance(r,list) or len(r)!=4:raise ValueError('选框须包含四个原图坐标。')
        r=[number(v,0,width if i%2==0 else height,'选框坐标') for i,v in enumerate(r)]
        if r[0]>=r[2] or r[1]>=r[3]:raise ValueError('选框宽高必须大于零。')
        local['rect']=r
    if local['enabled'] and local['rect'] is None:raise ValueError('请先框选局部调整范围。')
    settings['local']=local
    strokes=settings['protection']
    if not isinstance(strokes,list) or len(strokes)>200:raise ValueError('保护笔画最多 200 笔。')
    clean=[];total=0
    for stroke in strokes:
        if not isinstance(stroke,dict) or set(stroke)!={'mode','radius','points'} or stroke['mode'] not in ('protect','restore'):
            raise ValueError('保护笔画须包含模式、半径和坐标。')
        radius=number(stroke['radius'],2,500,'保护笔刷半径')
        points=stroke['points']
        if not isinstance(points,list) or not points:raise ValueError('保护笔画不能为空。')
        total+=len(points)
        if total>10000:raise ValueError('保护笔画采样点过多，请分次操作。')
        normalized=[]
        for point in points:
            if not isinstance(point,list) or len(point)!=2:raise ValueError('保护笔画坐标无效。')
            normalized.append([number(point[0],0,width,'横坐标'),number(point[1],0,height,'纵坐标')])
        clean.append({'mode':stroke['mode'],'radius':radius,'points':normalized})
    settings['protection']=clean
    return settings


def to_linear(rgb):
    value=np.asarray(rgb,dtype=np.float32)/255.
    return np.where(value<=.04045,value/12.92,((value+.055)/1.055)**2.4).astype(np.float32)


def to_srgb(rgb):
    value=np.clip(rgb,0,1)
    encoded=np.where(value<=.0031308,value*12.92,1.055*np.power(value,1/2.4)-.055)
    return np.rint(np.clip(encoded,0,1)*255).astype(np.uint8)


def rectangle_mask(bounds,rect,feather):
    x0,y0,x1,y1=bounds
    yy,xx=np.ogrid[y0:y1,x0:x1]
    distance=np.minimum(np.minimum(xx+.5-rect[0],rect[2]-xx-.5),np.minimum(yy+.5-rect[1],rect[3]-yy-.5))
    amount=(distance>=0).astype(np.float32) if feather<=0 else np.clip(distance/feather,0,1).astype(np.float32)
    return amount*amount*(3-2*amount)


def protection_mask(bounds,strokes):
    x0,y0,x1,y1=bounds
    mask=np.zeros((y1-y0,x1-x0),np.float32)
    yy,xx=np.ogrid[y0:y1,x0:x1]
    xx,yy=xx+.5,yy+.5
    for stroke in strokes:
        r=stroke['radius'];points=stroke['points']
        if (max(p[0] for p in points)+r<x0 or min(p[0] for p in points)-r>x1 or
            max(p[1] for p in points)+r<y0 or min(p[1] for p in points)-r>y1):continue
        one=np.zeros_like(mask)
        for a,b in (zip(points,points[1:]) if len(points)>1 else [(points[0],points[0])]):
            dx,dy=b[0]-a[0],b[1]-a[1]
            t=np.clip(((xx-a[0])*dx+(yy-a[1])*dy)/max(dx*dx+dy*dy,1e-10),0,1)
            distance=np.sqrt((xx-a[0]-t*dx)**2+(yy-a[1]-t*dy)**2)
            alpha=np.clip((r-distance)/max(1.,r*.2),0,1)
            one=np.maximum(one,alpha*alpha*(3-2*alpha))
        mask=np.maximum(mask,one) if stroke['mode']=='protect' else mask*(1-one)
    return mask


def save_analysis(directory,analysis):
    directory.mkdir(parents=True,exist_ok=True)
    arrays={k:v for k,v in analysis.items() if isinstance(v,np.ndarray)}
    scalars={k:v for k,v in analysis.items() if not isinstance(v,np.ndarray)}
    np.savez_compressed(directory/'analysis.npz',**arrays)
    (directory/'analysis.json').write_text(json.dumps(scalars,ensure_ascii=False),encoding='utf-8')


def load_analysis(directory):
    value=json.loads((directory/'analysis.json').read_text(encoding='utf-8'))
    with np.load(directory/'analysis.npz',allow_pickle=False) as arrays:value.update({key:arrays[key].copy() for key in arrays.files})
    return value


class SourceReader:
    def __init__(self,reader,size):self.reader=reader;self.image_size=size
    def __call__(self,bounds):return self.reader(bounds)


class Workspace:
    def __init__(self,store):
        self.store=store
        self.assets={}
        self.drafts={}
        self.batch={'status':'idle','items':[],'message':'尚未批量套用预设。'}
        self.process=None
        for p in store.photos.values():
            p.pop('_batch_busy',None)
            p['working_format']='8-bit sRGB input; float32 linear editing; 8-bit sRGB PNG output'
            p.setdefault('settings',defaults())
            p.setdefault('_assets',{})
            p.setdefault('preparation',{'status':'idle','analysis_ready':False,'skin_ready':False,'geometry_ready':False,'message':'可先调整曝光与色彩；人像专项需准备照片。'})
            if p['preparation']['status']=='running':p['preparation'].update(status='error',message='上次照片准备已中断，可以重新准备。')

    def decorate(self,p):
        p.setdefault('settings',defaults())
        p.setdefault('preparation',{'status':'idle','analysis_ready':False,'skin_ready':False,'geometry_ready':False})
        p['draft']=deepcopy(self.drafts.get(p['id']))
        p['edit_feedback']=deepcopy(p['draft'].get('feedback',{})) if p['draft'] else self.geometry_feedback(p,p['settings'])
        p['batch_busy']=bool(p.pop('_batch_busy',False))
        p.pop('_assets',None)
        return p

    def busy(self,p):
        return p['analysis']['status']=='running' or p.get('preparation',{}).get('status')=='running' or p.get('_batch_busy',False)

    def ensure_editable(self,p,expected,allow_draft=False):
        if self.store.closing:raise HTTPException(409,'原型正在退出。')
        if p.get('archived'):raise HTTPException(409,'照片已从照片库移除。')
        if isinstance(expected,bool) or not isinstance(expected,int) or expected!=p['version']:
            raise HTTPException(409,'照片状态已变化，请重新查看当前画面。')
        if self.busy(p):raise HTTPException(409,'照片正在处理，请等待完成。')
        if p['id'] in self.drafts and not allow_draft:raise HTTPException(409,'请先保留或取消当前草稿。')

    def requirements(self,settings):
        params=[settings['retouch']]+([settings['local']['params']] if settings['local']['enabled'] else [])
        params=[p for p in params if p.get('amount',1)>0]
        facial=('skin_even','texture_soften','tone_even','redness','mark_fade','eye_brighten','shine_reduce','fine_lines','face_exposure')
        needs=set()
        if any(any(p.get(k,0)!=0 for k in facial) for p in params):needs.add('analysis')
        if any(p.get('skin_even',0)>0 for p in params):needs.add('skin')
        if settings['geometry'].get('enabled') and any(any(abs(face.get(k,0))>0 for k in face if k!='face_index') for face in settings['geometry'].get('faces',[])):needs.add('analysis')
        if lighting.lighting_active(settings['lighting']):needs.add('lighting')
        return needs

    def validate_ready(self,p,settings):
        if settings['retouch'].get('amount',1)>0 and settings['retouch'].get('mark_fade',0)>0 and not self.approved_marks(p):
            raise HTTPException(409,{'message':'请先确认色点候选或完成手动标记，再使用全图痘印色泽调整；也可以框选范围后进行局部调整。',
                'needs':['confirmed_marks']})
        ready=p.get('preparation',{})
        for feature in self.requirements(settings):
            key={'analysis':'analysis_ready','skin':'skin_ready','lighting':'geometry_ready'}[feature]
            if not ready.get(key):raise HTTPException(409,{'message':'请先准备此功能需要的照片分析。','needs':sorted(self.requirements(settings))})
        params=[settings['retouch']]+([settings['local']['params']] if settings['local']['enabled'] else [])
        params=[value for value in params if value.get('amount',1)>0]
        if any(any(value.get(key,0)!=0 for key in (*retouch._SKIN_KEYS,*retouch._FACE_KEYS)) for value in params):
            assets=self.read_assets(p)
            analysis=assets.get('analysis',{})
            if analysis.get('prepared_version')!=retouch.PIPELINE_VERSION or analysis.get('image_size')!=[p['width'],p['height']]:
                raise HTTPException(409,'照片分区分析与当前照片不一致，请重新准备。')
            presence=assets.get('mask_presence',{})
            for keys,mask,message in ((retouch._SKIN_KEYS,'skin_mask','当前没有可用皮肤分区，请重新分析照片。'),
                                      (retouch._FACE_KEYS,'face_mask','当前没有可用面部皮肤分区，请重新分析照片。'),
                                      (('eye_brighten',),'eye_mask','未获取眼周分区，请先完成面部分析。')):
                if any(any(value.get(key,0)!=0 for key in keys) for value in params) and not presence.get(mask):
                    raise HTTPException(409,message)
            if any(value.get('skin_even',0)>0 for value in params) and not assets.get('skin'):
                raise HTTPException(409,'请先准备神经均肤层。')

    @staticmethod
    def approved_marks(p):
        selected=set(p.get('selected_ids',[]))
        return [candidate for candidate in p.get('candidates',[]) if candidate['id'] in selected]

    def mark_reader(self,identifier,p):
        """Read only user-approved sparse masks, at original image coordinates."""
        approved=self.approved_marks(p)
        def read(bounds):
            x0,y0,x1,y1=bounds
            mask=np.zeros((y1-y0,x1-x0),np.float32)
            for candidate in approved:
                a,b,d,e=candidate['bounds']
                ix0,iy0,ix1,iy1=max(a,x0),max(b,y0),min(d,x1),min(e,y1)
                if ix0>=ix1 or iy0>=iy1:continue
                patch=self.store._patch_data(identifier,candidate,p['analysis_generation'])
                if 'mask' not in patch:raise ValueError('已确认候选缺少原始蒙版，请重新分析后确认。')
                target=mask[iy0-y0:iy1-y0,ix0-x0:ix1-x0]
                np.maximum(target,patch['mask'][iy0-b:iy1-b,ix0-a:ix1-a],out=target)
            return mask
        return read,bool(approved)

    def geometry_feedback(self,p,settings):
        if not geometry.geometry_active(settings['geometry']):return {'warnings':[],'stroke_clamps':[]}
        assets=self.read_assets(p)
        _,metadata=geometry.build_operations(settings['geometry'],assets.get('raw',{}),p['width'],p['height'])
        return {key:deepcopy(metadata[key]) for key in ('warnings','stroke_clamps','limitations')}

    def preview(self,identifier,body):
        with self.store.lock:
            p=self.store.photos[identifier]
            self.ensure_editable(p,body.get('expected_version'),allow_draft=True)
            settings=validate_settings(body.get('settings'),p['width'],p['height'])
            self.validate_ready(p,settings)
            feedback=self.geometry_feedback(p,settings)
            self.drafts[identifier]={'id':uuid.uuid4().hex,'base_version':p['version'],'settings':settings,'feedback':feedback}
        return self.store.public(identifier)

    def commit(self,identifier,body):
        with self.store.lock:
            p=self.store.photos[identifier]
            self.ensure_editable(p,body.get('expected_version'),allow_draft=True)
            draft=self.drafts.get(identifier)
            if not draft or body.get('preview_id')!=draft['id']:raise HTTPException(409,'草稿已变化，请重新查看后保留。')
            if draft['base_version']!=p['version']:raise HTTPException(409,'草稿依赖的照片分析已变化，请重新预览。')
            self.validate_ready(p,draft['settings'])
            before=deepcopy(p)
            if draft['settings']!=p.get('settings',defaults()):
                p['history'].append(self.store.snapshot(p));p['history']=p['history'][-100:];p['redo']=[]
                p['settings']=deepcopy(draft['settings']);p['version']+=1
                try:self.store.persist()
                except Exception:self.store.photos[identifier]=before;raise
            self.drafts.pop(identifier,None)
        return self.store.public(identifier)

    def discard(self,identifier,body):
        with self.store.lock:
            p=self.store.photos[identifier]
            self.ensure_editable(p,body.get('expected_version'),allow_draft=True)
            draft=self.drafts.get(identifier)
            if draft and body.get('preview_id')!=draft['id']:raise HTTPException(409,'草稿已变化，请刷新。')
            self.drafts.pop(identifier,None)
        return self.store.public(identifier)

    def reset(self,identifier,body):
        with self.store.lock:
            p=self.store.photos[identifier]
            self.ensure_editable(p,body.get('expected_version'),allow_draft=True)
            before=deepcopy(p)
            if p['selected_ids'] or p.get('settings',defaults())!=defaults():
                p['history'].append(self.store.snapshot(p));p['history']=p['history'][-100:];p['redo']=[]
                p['selected_ids']=[];p['settings']=defaults();p['version']+=1
                try:self.store.persist()
                except Exception:self.store.photos[identifier]=before;raise
            self.drafts.pop(identifier,None)
        return self.store.public(identifier)

    def read_assets(self,p):
        spec=p.get('_assets',{})
        key=json.dumps(spec,sort_keys=True)
        with self.store.lock:
            if key in self.assets:return self.assets[key]
        result={}
        if spec.get('analysis'):
            result['analysis']=load_analysis(Path(spec['analysis']))
            result['raw']=load_analysis(Path(spec['raw_analysis']))
            result['mask_presence']={name:bool(np.any(np.asarray(result['analysis'].get(name,[]))>0))
                for name in ('face_mask','skin_mask','eye_mask')}
        if spec.get('skin'):
            from .skin_model import load_skin_fields
            result['skin']=load_skin_fields(Path(spec['skin']))
        if spec.get('lighting'):
            with np.load(Path(spec['lighting'])/'geometry.npz',allow_pickle=False) as arrays:
                result['lighting']=lighting.prepare_fields({name:arrays[name].copy() for name in arrays.files})
        with self.store.lock:
            if len(self.assets)>=8:self.assets.pop(next(iter(self.assets)))
            self.assets[key]=result
        return result

    def render(self,identifier,bounds,snapshot,settings=None,context=None):
        p=snapshot;settings=settings or p.get('settings',defaults())
        if settings==defaults():return context['repaired'](bounds) if context else self.store.repair_region(identifier,bounds,snapshot=p)
        if context is None:self.validate_ready(p,settings)
        source=context['source'] if context else self.store.original(identifier)
        size=(p['width'],p['height'])
        assets=context['assets'] if context else self.read_assets(p)
        analysis=assets.get('analysis',{})
        read_marks,marks_available=(context['marks'],context['marks_available']) if context else self.mark_reader(identifier,p)
        # The canonical source includes opt-in repair candidates, never another preview.
        def repaired(box):return context['repaired'](box) if context else self.store.repair_region(identifier,box,snapshot=p)
        def compute_grade(box):
            return retouch.render_region(source,box,settings['retouch'],analysis,
                skin_fields=assets.get('skin'),region_reader=repaired,linear_output=True,
                mark_region_reader=read_marks,mark_region_available=marks_available)
        def graded(box):
            return context['grade'](settings['retouch'],box,compute_grade) if context else compute_grade(box)
        def unwarped(box):
            result=graded(box)
            local=settings['local']
            if local['enabled']:
                alpha=rectangle_mask(box,local['rect'],local['feather'])
                if np.any(alpha):
                    changed=retouch.render_region(source,box,local['params'],analysis,
                        skin_fields=assets.get('skin'),region_reader=graded,reader_linear=True,linear_output=True,
                        mark_region_reader=lambda region:rectangle_mask(region,local['rect'],0),mark_region_available=True)
                    result=result+(changed-result)*alpha[...,None]
            if settings['lighting'].get('enabled'):
                result=lighting.render_region(result,assets.get('lighting',{}),settings['lighting'],box,size,linear_input=True)
            if settings['protection']:
                alpha=protection_mask(box,settings['protection'])
                x0,y0,x1,y1=box
                result=result+(to_linear(source[y0:y1,x0:x1])-result)*alpha[...,None]
            return result
        result=geometry.render_region(SourceReader(unwarped,size),bounds,settings['geometry'],assets.get('raw',{}),image_size=size)
        return to_srgb(result)

    def remove(self,identifiers):
        if not isinstance(identifiers,list) or not 1<=len(identifiers)<=100 or len(set(identifiers))!=len(identifiers):
            raise ValueError('请选择 1 至 100 张照片。')
        with self.store.lock:
            for identifier in identifiers:
                p=self.store.photos[identifier]
                self.ensure_editable(p,p['version'])
            before={i:deepcopy(self.store.photos[i]) for i in identifiers}
            for identifier in identifiers:self.store.photos[identifier]['archived']=True
            try:self.store.persist()
            except Exception:self.store.photos.update(before);raise
        return {'removed':identifiers,'message':'已从照片库移除，原文件未删除。'}

    def prepare(self,identifier,body):
        features=body.get('features',['analysis','skin','lighting'])
        if not isinstance(features,list) or not features or set(features)-{'analysis','skin','lighting'}:raise ValueError('照片准备项目无效。')
        device=body.get('device','auto')
        if device not in ('auto','cpu','cuda'):raise ValueError('计算设备无效。')
        face_index=body.get('face_index')
        if face_index is not None and (isinstance(face_index,bool) or not isinstance(face_index,int) or face_index<0):raise ValueError('人物索引无效。')
        with self.store.lock:
            p=self.store.photos[identifier]
            self.ensure_editable(p,body.get('expected_version'))
            if face_index is not None and p.get('face_index') not in (None,face_index) and p.get('settings',defaults())!=defaults():
                raise ValueError('更换修片人物前请先恢复原图，避免已有调整被套用到另一人。')
            previous=deepcopy(p)
            p['preparation'].update(status='running',message='正在准备照片分析；原有结果保持不变。')
            try:
                self.store.persist()
                self.store.pool.submit(self._prepare,identifier,features,device,face_index)
            except Exception:
                self.store.photos[identifier]=previous
                self.store.persist()
                raise
        return self.store.public(identifier)

    def _prepare(self,identifier,features,device,face_index=None):
        try:self._prepare_sync(identifier,features,device,face_index)
        except Exception as error:
            with self.store.lock:
                self.store.photos[identifier]['preparation'].update(status='error',message=str(error))
                self.store.persist()

    def _prepare_sync(self,identifier,features,device,face_index=None):
        started=perf_counter()
        with self.store.lock:
            p=deepcopy(self.store.photos[identifier])
        assets=deepcopy(p.get('_assets',{}))
        ready=deepcopy(p['preparation'])
        original=self.store.original(identifier);height,width=original.shape[:2]
        directory=self.store.folder(identifier)/'prepared'/uuid.uuid4().hex
        directory.mkdir(parents=True)
        if self.store.closing:raise ValueError('原型正在退出。')
        if not ready.get('analysis_ready') or (face_index is not None and face_index!=p.get('face_index')):
            from portraitlab.engine import Engine
            if not self.store.analysis_models:raise ValueError('请先在模型管理中准备人物定位与分区模型。')
            if self.store.engine is None:self.store.engine=Engine(Path(self.store.analysis_models))
            raw=self.store.engine.analyze(original)
            if not raw.get('parts'):
                scale=min(1.,1600/max(height,width))
                rgb=cv2.resize(original,(max(16,round(width*scale)),max(16,round(height*scale)))) if scale<1 else original
                with self.store.engine._lock:raw['parts']=self.store.engine._parts_adapter.predict(rgb.astype(np.float32)/255.)
            faces=raw.get('faces',[])
            if face_index is None and faces:
                face_index=max(range(len(faces)),key=lambda i:float(np.prod(np.ptp(np.asarray(faces[i]),axis=0))))
            if face_index is not None and face_index>=len(faces):raise ValueError('所选人物未被识别，请重新选择。')
            analysis=retouch.prepare_analysis(original,raw,face_index=face_index)
            flat_raw={'faces':np.asarray(faces,dtype=np.float32),'face_mask':raw['face_mask'],'skin_mask':raw['skin_mask'],
                'source_shape':[height,width],'warnings':raw.get('warnings',[]),'face_index':face_index}
            for name,value in raw.get('parts',{}).items():
                if isinstance(value,np.ndarray):flat_raw['parts__'+name]=value
            save_analysis(directory/'analysis',analysis);save_analysis(directory/'raw',flat_raw)
            assets.update(analysis=str(directory/'analysis'),raw_analysis=str(directory/'raw'))
            ready.update(analysis_ready=True,skin_ready=False,geometry_ready=False)
            assets.pop('skin',None)
            assets.pop('lighting',None)
            subject=lighting.subject_mask_from_analysis(raw)
            np.save(directory/'subject-mask.npy',subject,allow_pickle=False)
            assets['subject_mask']=str(directory/'subject-mask.npy')
        else:
            analysis=load_analysis(Path(assets['analysis']));flat_raw=load_analysis(Path(assets['raw_analysis']))
            faces=flat_raw['faces'];face_index=flat_raw.get('face_index',p.get('face_index'))
        if 'skin' in features and not ready.get('skin_ready'):
            if face_index is None or not len(faces):raise ValueError('未识别到可均肤的人物；可继续全图色彩或手动修补。')
            points=np.asarray(faces[face_index],np.float32)*[width,height]
            lo,hi=points.min(0),points.max(0);center=(lo+hi)/2;radius=max(hi-lo)*.75
            roi=[max(0,int(center[0]-radius)),max(0,int(center[1]-radius)),min(width,int(center[0]+radius)+1),min(height,int(center[1]+radius)+1)]
            x0,y0,x1,y1=roi
            field=np.asarray(analysis.get('face_mask',flat_raw['face_mask']),np.float32)
            yy,xx=np.mgrid[y0:y1,x0:x1].astype(np.float32)
            allowed=cv2.remap(field,(xx+.5)*field.shape[1]/width-.5,(yy+.5)*field.shape[0]/height-.5,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
            mask=directory/'skin-allowed.npy';np.save(mask,np.clip(allowed,0,1),allow_pickle=False)
            self._progress(identifier,'正在生成可调的神经均肤层。')
            request={'kind':'skin','input':str(self.store.folder(identifier)/'original.npy'),'roi':roi,'allowed_mask':str(mask),
                'vendor':str(Path(self.store.vendor).resolve()),'models':str(Path(self.store.neural_models).resolve()),
                'output':str(directory/'skin'),'device':device,'cache_root':str(self.store.data/'cache'/'worker')}
            self.store._request_worker(request)
            assets['skin']=str(directory/'skin');ready['skin_ready']=True
        if 'lighting' in features and not ready.get('geometry_ready'):
            self._progress(identifier,'正在分析空间起伏与主体范围。')
            request={'input':str(self.store.folder(identifier)/'original.npy'),'subject_mask':assets['subject_mask'],
                'models':str(Path(self.store.analysis_models).resolve()),'output':str(directory/'lighting'),'device':device,'edge':1536}
            path=directory/'lighting-request.json';path.write_text(json.dumps(request),encoding='utf-8')
            env=self.store.worker_environment()
            with (directory/'lighting.log').open('w',encoding='utf-8') as log:
                with self.store.lock:
                    if self.store.closing:raise ValueError('原型正在退出。')
                    process=subprocess.Popen([str(self.store.ai_python),'-B',str(Path(__file__).parent/'lighting_v2.py'),'--request',str(path)],
                        cwd=self.store.data,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.process=process
                try:code=process.wait(timeout=600)
                except subprocess.TimeoutExpired:process.kill();process.wait();raise ValueError('光影分析超时，任务已停止。')
                finally:
                    with self.store.lock:self.process=None
            if code:raise ValueError('光影分析未完成，请查看独立准备目录中的 lighting.log。')
            assets['lighting']=str(directory/'lighting');ready['geometry_ready']=True
        public_faces=[]
        for index,points in enumerate(faces):
            points=np.asarray(points)*[width,height];lo,hi=np.maximum(points.min(0),0),np.minimum(points.max(0),[width,height])
            public_faces.append({'face_index':index,'bounds':[int(lo[0]),int(lo[1]),int(hi[0]),int(hi[1])]})
        with self.store.lock:
            current=self.store.photos[identifier]
            if self.store.closing:raise ValueError('照片准备已随原型退出而取消。')
            if current['version']!=p['version']:
                raise ValueError('照片在准备期间发生变化，本次准备结果未替换当前画面。')
            previous=deepcopy(current)
            dependencies_changed=(assets!=current.get('_assets',{}) or face_index!=current.get('face_index'))
            current.update(_assets=assets,faces=public_faces,face_index=face_index,
                preparation={**ready,'status':'ready','message':'照片准备完成，可以调整并查看草稿。','device':device,'seconds':perf_counter()-started})
            if dependencies_changed:current['version']+=1
            try:self.store.persist()
            except Exception:self.store.photos[identifier]=previous;raise
            if dependencies_changed:self.drafts.pop(identifier,None)

    def _progress(self,identifier,message):
        with self.store.lock:self.store.photos[identifier]['preparation'].update(status='running',message=message)

    def apply_batch(self,body):
        identifiers=body.get('photo_ids');preset_id=body.get('preset_id');strength=number(body.get('strength',1),0,1,'预设强度')
        device=body.get('device','auto')
        if device not in ('auto','cpu','cuda'):raise ValueError('计算设备无效。')
        if not isinstance(identifiers,list) or not 1<=len(identifiers)<=100 or len(set(identifiers))!=len(identifiers):raise ValueError('请选择 1 至 100 张照片。')
        retouch.resolve_preset(preset_id,strength=strength)
        with self.store.lock:
            if self.batch['status']=='running':raise HTTPException(409,'上一批预设仍在处理中。')
            for identifier in identifiers:self.ensure_editable(self.store.photos[identifier],self.store.photos[identifier]['version'])
            self.batch={'status':'running','done':0,'total':len(identifiers),'message':'正在逐张准备并套用预设。',
                'items':[{'photo_id':i,'name':self.store.photos[i]['name'],'status':'queued','message':''} for i in identifiers]}
            for identifier in identifiers:self.store.photos[identifier]['_batch_busy']=True
        self.store.pool.submit(self._batch,preset_id,strength,device)
        return deepcopy(self.batch)

    def _batch(self,preset_id,strength,device):
        for item in self.batch['items']:
            identifier=item['photo_id'];item.update(status='running',message='准备照片…')
            try:
                if self.store.closing:raise ValueError('原型已退出，余下项目未处理。')
                p=self.store.photos[identifier]
                resolved=retouch.resolve_preset(preset_id,current_params=p.get('settings',defaults())['retouch'],strength=strength)
                settings=deepcopy(p.get('settings',defaults()));settings['retouch']=resolved['params']
                features=self.requirements(settings)
                if features:self._prepare_sync(identifier,list(features),device,p.get('face_index'))
                needs=resolved.get('needs',{})
                review=needs.get('review_blemishes',False) if isinstance(needs,dict) else 'review_blemishes' in needs
                if review and not p['candidates'] and not p['selected_ids']:
                    p['analysis']={'status':'running','kind':'automatic','message':'正在准备需人工确认的瑕疵候选。'}
                    self.store._analyze(identifier,p.get('face_index'),device)
                    if p['analysis']['status']=='error':raise ValueError(p['analysis']['message'])
                with self.store.lock:
                    p=self.store.photos[identifier];previous=deepcopy(p)
                    self.validate_ready(p,settings)
                    self.geometry_feedback(p,settings)
                    p['history'].append(self.store.snapshot(p));p['history']=p['history'][-100:];p['redo']=[]
                    p['settings']=settings;p['version']+=1
                    try:self.store.persist()
                    except Exception:self.store.photos[identifier]=previous;raise
                item.update(status='done',message=resolved.get('summary','已套用预设。')+(' 瑕疵候选需逐处确认，未自动删除痣或雀斑。' if review else ''))
            except Exception as error:item.update(status='error',message=str(error))
            finally:
                with self.store.lock:
                    self.store.photos[identifier].pop('_batch_busy',None)
                    self.batch['done']+=1
        with self.store.lock:
            failures=sum(item['status']=='error' for item in self.batch['items'])
            self.batch.update(status='complete',message=f'批量完成，{len(self.batch["items"])-failures} 张成功，{failures} 张未完成。')

    def close(self):
        with self.store.lock:
            if self.process and self.process.poll() is None:self.process.terminate()

