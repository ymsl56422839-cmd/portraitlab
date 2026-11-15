"""Explicit, disposable interaction previews. Never used for native tiles/export.

The UI requests these only while adjusting, labels them, then replaces them
with native render tiles before allowing retention. Pixel-sized settings and
model fields are rescaled; the actual editing functions remain shared.
SPDX-License-Identifier: AGPL-3.0-only
"""
from collections import OrderedDict
from copy import deepcopy
import json
import math
import threading

import cv2
import numpy as np


def resize(array,width,height):
    return cv2.resize(array,(max(1,width),max(1,height)),interpolation=cv2.INTER_AREA)


def _area_axis(array,edges,axis,step):
    """Integrate piecewise-constant pixels without a full native prefix image."""
    length=array.shape[axis]
    edges=np.clip(np.asarray(edges,np.float64),0,length)
    indices=np.floor(edges).astype(np.intp)
    fractions=edges-indices
    left,right=indices[:-1],indices[1:]
    shape=[1]*array.ndim;shape[axis]=len(left)
    # The last edge is the patch boundary. reduceat therefore sums exactly the
    # integer interiors; repeated indices represent an empty integer interior.
    result=np.add.reduceat(array,left,axis=axis,dtype=np.float64)
    result*=((right>left).reshape(shape))
    result+=np.take(array,np.minimum(right,length-1),axis=axis)*fractions[1:].reshape(shape)
    result-=np.take(array,left,axis=axis)*fractions[:-1].reshape(shape)
    return (result/step).astype(np.float32)


def resample_patch(array,bounds,image_size,target_size):
    """Area-resample a zero-outside sparse patch on the full image's grid.

    A native patch boundary is generally between proxy pixels. Independent
    cv2.resize calls shift that boundary; integrating against global output
    pixel footprints preserves phase and allows adjacent sparse deltas to add.
    """
    a,b,d,e=bounds;width,height=image_size;w,h=target_size
    array=np.asarray(array)
    if array.ndim not in (2,3) or array.shape[:2]!=(e-b,d-a):
        raise ValueError('交互预览字段尺寸与原图范围不一致。')
    if not 0<=a<d<=width or not 0<=b<e<=height or not 1<=w<=width or not 1<=h<=height:
        raise ValueError('交互预览采样范围无效。')
    box=[a*w//width,b*h//height,(d*w+width-1)//width,(e*h+height-1)//height]
    xs=np.arange(box[0],box[2]+1,dtype=np.float64)*(width/w)-a
    ys=np.arange(box[1],box[3]+1,dtype=np.float64)*(height/h)-b
    # Those outside edges enclose the patch exactly; remove round-off at a
    # coincident boundary before integer interior sums are selected.
    xs[0],xs[-1]=0,d-a;ys[0],ys[-1]=0,e-b
    result=_area_axis(array,xs,1,width/w)
    return box,_area_axis(result,ys,0,height/h)


class ProxyRenderer:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock();self.sources=OrderedDict();self.frames=OrderedDict()

    def source_key(self,p):
        selected=set(p['selected_ids'])
        patches=tuple((c['id'],tuple(c['bounds']),c.get('patch'),c.get('kind')) for c in p['candidates'] if c['id'] in selected)
        return (p['id'],p['width'],p['height'],p['analysis_generation'],tuple(sorted(selected)),p['strength'],patches,
                json.dumps(p.get('_assets',{}),sort_keys=True))

    def prepare(self,p):
        key=self.source_key(p)
        if key in self.sources:
            self.sources.move_to_end(key);return key,self.sources[key]
        identifier=p['id'];width,height=p['width'],p['height']
        factor=2**max(0,math.ceil(math.log2(max(width,height)/1280)))
        w,h=math.ceil(width/factor),math.ceil(height/factor);sx,sy=w/width,h/height
        original=self.store.original(identifier)
        source=resize(original,w,h);correction=np.zeros((h,w,3),np.float32)
        selected=[c for c in p['candidates'] if c['id'] in p['selected_ids']]
        # Re-evaluate only affected aligned native chunks; no full float image.
        chunks=set()
        for c in selected:
            a,b,d,e=c['bounds']
            for y in range(b//512,(e-1)//512+1):
                for x in range(a//512,(d-1)//512+1):chunks.add((x,y))
        for x,y in sorted(chunks):
            a,b=x*512,y*512;d,e=min(width,a+512),min(height,b+512)
            changed=self.store.repair_region(identifier,(a,b,d,e),snapshot=p)
            delta=changed.astype(np.float32)-original[b:e,a:d]
            box,reduced=resample_patch(delta,[a,b,d,e],[width,height],[w,h])
            pa,pb,pd,pe=box
            correction[pb:pe,pa:pd]+=reduced
        # Never replace unedited source pixels with a separately resized chunk.
        repaired=np.clip(np.rint(source.astype(np.float32)+correction),0,255).astype(np.uint8)
        assets=dict(self.store.workspace.read_assets(p))
        if assets.get('analysis'):
            analysis=dict(assets['analysis']);analysis['image_size']=[w,h];analysis['face_width']*=min(sx,sy)
            assets['analysis']=analysis
        if assets.get('raw'):
            assets['raw']={**assets['raw'],'source_shape':[h,w]}
        if assets.get('skin'):
            skin={**assets['skin'],'image_size':[w,h],'fields':[]}
            for field in assets['skin']['fields']:
                box,delta=resample_patch(field['delta'],field['bounds'],[width,height],[w,h])
                _,mask=resample_patch(field['mask'],field['bounds'],[width,height],[w,h])
                skin['fields'].append({**field,'bounds':box,'delta':delta,'mask':np.clip(mask,0,1)})
            assets['skin']=skin
        marks=[]
        for c in selected:
            patch=self.store._patch_data(identifier,c,p['analysis_generation'])
            if 'mask' not in patch:continue
            box,mask=resample_patch(patch['mask'],c['bounds'],[width,height],[w,h])
            marks.append((box,np.clip(mask,0,1)))
        def read_marks(box):
            x0,y0,x1,y1=box;result=np.zeros((y1-y0,x1-x0),np.float32)
            for (a,b,d,e),mask in marks:
                l,t,r,z=max(x0,a),max(y0,b),min(x1,d),min(y1,e)
                if l<r and t<z:result[t-y0:z-y0,l-x0:r-x0]=np.maximum(result[t-y0:z-y0,l-x0:r-x0],mask[t-b:z-b,l-a:r-a])
            return result
        grades=OrderedDict()
        def grade(params,box,compute):
            signature=json.dumps(params,sort_keys=True)
            if signature not in grades:
                grades[signature]=compute([0,0,w,h])
                while len(grades)>2:grades.popitem(last=False)
            else:grades.move_to_end(signature)
            a,b,d,e=box;return grades[signature][b:e,a:d].copy()
        context={'source':source,'assets':assets,'repaired':lambda box:repaired[box[1]:box[3],box[0]:box[2]].copy(),
            'marks':read_marks,'marks_available':any(np.any(mask>0) for _,mask in marks),'grade':grade}
        value={'context':context,'p':{**p,'width':w,'height':h},'sx':sx,'sy':sy}
        self.sources[key]=value
        while len(self.sources)>2:self.sources.popitem(last=False)
        return key,value

    def frame(self,p,settings):
        with self.lock:
            key,prepared=self.prepare(p);frame_key=(key,json.dumps(settings,sort_keys=True))
            if frame_key in self.frames:
                self.frames.move_to_end(frame_key);return self.frames[frame_key]
            s=deepcopy(settings);sx,sy=prepared['sx'],prepared['sy'];scale=min(sx,sy)
            if s['local']['rect'] is not None:s['local']['rect']=[v*(sx if i%2==0 else sy) for i,v in enumerate(s['local']['rect'])]
            s['local']['feather']*=scale
            for stroke in s['protection']:
                stroke['radius']*=scale;stroke['points']=[[x*sx,y*sy] for x,y in stroke['points']]
            proxy=prepared['p']
            value=self.store.workspace.render(p['id'],[0,0,proxy['width'],proxy['height']],proxy,s,prepared['context'])
            self.frames[frame_key]=value
            while len(self.frames)>3:self.frames.popitem(last=False)
            return value

    def tile(self,p,settings,bounds,level):
        if type(level) is not int or not 0<=level<=30 or len(bounds)!=4 or any(type(v) is not int for v in bounds):
            raise ValueError('交互预览图块坐标无效。')
        x0,y0,x1,y1=bounds
        if not 0<=x0<x1<=p['width'] or not 0<=y0<y1<=p['height']:
            raise ValueError('交互预览图块超出照片。')
        frame=self.frame(p,settings);x0,y0,x1,y1=bounds;scale=2**level
        out_width,out_height=math.ceil((x1-x0)/scale),math.ceil((y1-y0)/scale)
        # Global pixel-center mapping aligns every adjacent interaction tile.
        xs=(x0+(np.arange(out_width,dtype=np.float32)+.5)*scale)*frame.shape[1]/p['width']-.5
        ys=(y0+(np.arange(out_height,dtype=np.float32)+.5)*scale)*frame.shape[0]/p['height']-.5
        mx,my=np.meshgrid(xs,ys)
        return cv2.remap(frame,mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
