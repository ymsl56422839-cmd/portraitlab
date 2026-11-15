"""Layered native-resolution retouching shared by preview and export.

SPDX-License-Identifier: AGPL-3.0-only
The renderer performs no inference. It combines an explicitly prepared neural
curve delta with independent texture, colour and local-luminance layers. All
working/composite values are float32 linear RGB; encoding happens at the edge.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np

if __package__:
    from . import neural_candidate as core
else:
    _directory=str(Path(__file__).resolve().parent)
    if _directory not in sys.path:sys.path.insert(0,_directory)
    import neural_candidate as core

PIPELINE_VERSION='layered-native-retouch-v2'
LUMA=np.array([.2126,.7152,.0722],np.float32)
DEFAULT_PARAMS={'amount':1.,'skin_even':0.,'texture_soften':0.,'tone_even':0.,'redness':0.,
    'mark_fade':0.,'eye_brighten':0.,'shine_reduce':0.,'fine_lines':0.,'face_exposure':0.,
    'exposure':0.,'temperature':0.,'tint':0.,'contrast':0.,'saturation':1.,'shadows':0.,'highlights':0.}
_DEFINITIONS={
    'amount':('总强度',0,1,'percent','overall'),
    'skin_even':('神经均肤',0,1,'percent','skin'),
    'texture_soften':('质感柔肤',0,1,'percent','skin'),
    'tone_even':('均匀肤色',0,1,'percent','skin'),
    'redness':('减淡泛红',0,1,'percent','skin'),
    'mark_fade':('痘印色泽',0,1,'percent','skin'),
    'eye_brighten':('眼周提亮',0,1,'percent','face'),
    'shine_reduce':('压制反光',0,1,'percent','skin'),
    'fine_lines':('细纹减淡',0,1,'percent','face'),
    'face_exposure':('面部曝光',-1,1,'EV','face'),
    'exposure':('曝光',-3,3,'EV','tone'),
    'temperature':('冷暖',-1,1,'relative','colour'),
    'tint':('色调',-1,1,'relative','colour'),
    'contrast':('对比度',-1,1,'relative','tone'),
    'saturation':('饱和度',0,2,'ratio','colour'),
    'shadows':('提亮阴影',0,1,'percent','tone'),
    'highlights':('收敛高光',0,1,'percent','tone'),
}
PARAM_SCHEMA={key:{'label':label,'min':low,'max':high,'default':DEFAULT_PARAMS[key],
                  'unit':unit,'group':group,'needs_skin_prepare':key=='skin_even',
                  'needs_analysis':group in ('skin','face')}
              for key,(label,low,high,unit,group) in _DEFINITIONS.items()}
PRESET_CATEGORIES={'general':'通用','senior':'长者','adult':'成人','youth':'少年'}
_SKIN_KEYS=('texture_soften','tone_even','redness','mark_fade','shine_reduce')
_FACE_KEYS=('skin_even','eye_brighten','fine_lines','face_exposure')
_LEFT_EYE=[33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246]
_RIGHT_EYE=[263,249,390,373,374,380,381,382,362,398,384,385,386,387,388,466]
_BROWS=[[70,63,105,66,107,55,65,52,53,46],[336,296,334,293,300,276,283,282,295,285]]
_LIPS=[61,146,91,181,84,17,314,405,321,375,291,409,270,269,267,0,37,39,40,185]
_LOWER_EYES=[[33,7,163,144,145,153,154,155,133],[263,249,390,373,374,380,381,382,362]]


def validate_params(values=None):
    if values is None:return dict(DEFAULT_PARAMS)
    if not isinstance(values,dict):raise ValueError('修片参数须为对象。')
    unknown=set(values)-set(DEFAULT_PARAMS)
    if unknown:raise ValueError('未知修片参数：'+', '.join(sorted(unknown)))
    result=dict(DEFAULT_PARAMS)
    for key,value in values.items():
        label,low,high,_,_=_DEFINITIONS[key]
        try:result[key]=core._number(value,low,high,key)
        except ValueError:raise ValueError(f'{label}须在 {low:g}～{high:g} 之间。') from None
    return result


def srgb_to_linear(values):
    values=np.asarray(values,dtype=np.float32)
    return np.where(values<=.04045,values/12.92,((np.maximum(values,0)+.055)/1.055)**2.4).astype(np.float32)


def linear_to_srgb(values):
    values=np.maximum(np.asarray(values,dtype=np.float32),0)
    return np.where(values<=.0031308,values*12.92,1.055*np.power(values,1/2.4)-.055).astype(np.float32)


def encode_srgb(values):
    return np.clip(np.rint(linear_to_srgb(values)*255),0,255).astype(np.uint8)


def _rgb(rgb):
    if not isinstance(rgb,np.ndarray) or rgb.dtype!=np.uint8 or rgb.ndim!=3 or rgb.shape[2]!=3:
        raise ValueError('修片需要原尺寸的 8 位 RGB 原图。')
    h,w=rgb.shape[:2]
    if min(h,w)<2 or h*w>80_000_000:raise ValueError('图片须至少为 2×2 像素，最多 8000 万像素。')
    return h,w


def _mask(array,shape):
    if array is None:return np.zeros(shape,np.float32)
    result=np.asarray(array,dtype=np.float32)
    if result.ndim!=2 or not result.size or not np.isfinite(result).all():raise ValueError('皮肤分区蒙版无效。')
    if np.any(result<0) or np.any(result>1):raise ValueError('皮肤分区蒙版须在 0～1 之间。')
    if result.shape!=shape:result=cv2.resize(result,(shape[1],shape[0]),interpolation=cv2.INTER_LINEAR)
    return result.astype(np.float32,copy=True)


def _blur(values,sigma):
    radius=max(1,int(math.ceil(3*float(sigma))))
    return cv2.GaussianBlur(values,(radius*2+1,radius*2+1),float(sigma),borderType=cv2.BORDER_REFLECT_101).astype(np.float32)


def _smooth(low,high,values):
    t=np.clip((values-low)/max(high-low,1e-6),0,1)
    return t*t*(3-2*t)


def prepare_analysis(rgb,raw_analysis=None,face_index=None):
    """Return only top-level ndarrays and JSON metadata, ready for npz + JSON.

    raw_analysis: existing faces (478x2 normalized), face_mask, skin_mask,
    optional parts.hair, and optional mark_mask identifying user-approved marks.
    No new detector or downloaded model is invoked here.
    """
    height,width=_rgb(rgb)
    raw=raw_analysis or {}
    if not isinstance(raw,dict):raise ValueError('修片分析数据须为对象。')
    scale=min(1.,1600/max(height,width));shape=(max(2,round(height*scale)),max(2,round(width*scale)))
    faces=[]
    for face in raw.get('faces',[]):
        value=np.asarray(face,np.float32)
        if value.shape!=(478,2) or not np.isfinite(value).all():raise ValueError('面部关键点须为 478×2 有限坐标。')
        faces.append(np.clip(value,0,1))
    if len(faces)>32:raise ValueError('单张照片最多准备 32 张面部的修片分区。')
    if face_index is not None:
        if type(face_index) is not int or not 0<=face_index<len(faces):raise ValueError('选择的人脸不存在。')
        faces=[faces[face_index]]
    facial=_mask(raw.get('face_mask',raw.get('mask')),shape)
    skin=_mask(raw.get('skin_mask',raw.get('face_mask',raw.get('mask'))),shape)
    raw_mark=raw.get('mark_mask')
    marked=_mask(raw_mark,np.asarray(raw_mark).shape) if raw_mark is not None else np.zeros(shape,np.float32)
    exclusions=np.zeros(shape,np.float32);eyes=np.zeros(shape,np.float32)
    selected=np.zeros(shape,np.float32);face_widths=[]
    for face in faces:
        points=face*np.array([shape[1],shape[0]],np.float32)
        hull=cv2.convexHull(np.rint(points).astype(np.int32))
        cv2.fillConvexPoly(selected,hull,1.)
        span=max(1,float(points[:,0].max()-points[:,0].min()))
        face_widths.append(span/scale)
        feature=np.zeros(shape,np.uint8)
        for indices in [_LEFT_EYE,_RIGHT_EYE,_LIPS,*_BROWS,[98,97,2,326,327]]:
            polygon=cv2.convexHull(np.rint(points[indices]).astype(np.int32))
            cv2.fillConvexPoly(feature,polygon,1)
        radius=max(1,round(span*.008))
        feature=cv2.dilate(feature,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(radius*2+1,radius*2+1)))
        exclusions=np.maximum(exclusions,feature.astype(np.float32))
        for indices in _LOWER_EYES:
            lid=points[indices]
            direction=lid[-1]-lid[0]
            normal=np.array([-direction[1],direction[0]],np.float32)
            normal/=max(float(np.linalg.norm(normal)),1e-6)
            if normal[1]<0:normal=-normal
            polygon=np.concatenate([lid,lid[::-1]+normal*(span*.055)],axis=0)
            eye=np.zeros(shape,np.uint8)
            cv2.fillPoly(eye,[np.rint(polygon).astype(np.int32)],1)
            inside=cv2.distanceTransform(eye,cv2.DIST_L2,3)
            eyes=np.maximum(eyes,np.clip(inside/max(span*.012,1),0,1))
    if face_index is not None:
        facial*=selected
        skin*=selected
    parts=raw.get('parts') or {}
    hair=_mask(parts.get('hair',raw.get('hair_mask')),shape)
    protect=np.maximum(exclusions,_smooth(.15,.65,hair))
    facial*=1-protect;skin*=1-protect
    eyes*=facial
    small=cv2.resize(rgb,(shape[1],shape[0]),interpolation=cv2.INTER_AREA).astype(np.float32)/255
    linear=srgb_to_linear(small);luminance=linear@LUMA
    chosen=skin>.4
    reference=np.median((linear/(luminance[...,None]+.01))[chosen],axis=0) if chosen.any() else np.ones(3,np.float32)
    skin_percentiles=np.percentile(luminance[chosen],[25,50,75,95]).tolist() if chosen.any() else [0.,0.,0.,0.]
    warnings_out=list(raw.get('warnings',[]))
    if not np.any(skin>0):warnings_out.append('未获取可用皮肤分区；曝光和色彩可直接调整，人像专项需先分析照片。')
    return {'prepared_version':PIPELINE_VERSION,'image_size':[width,height],'map_size':[shape[1],shape[0]],
        'source_rgb_sha256':core.rgb_fingerprint(rgb),'face_count':len(faces),'face_index':face_index,
        'face_width':float(np.median(face_widths)) if face_widths else float(min(height,width)*.25),
        'face_mask':facial.astype(np.float32),'skin_mask':skin.astype(np.float32),
        'eye_mask':eyes.astype(np.float32),'mark_mask':marked.astype(np.float32),
        'feature_protection':protect.astype(np.float32),'skin_reference':reference.astype(float).tolist(),
        'skin_luminance_quartiles':skin_percentiles,'scene_luminance_median':float(np.median(luminance)),
        'clipped_fraction':float(np.mean(np.max(small,axis=2)>=.995)),
        'warnings':list(dict.fromkeys(warnings_out))}


def _sample_map(array,bounds,image_size):
    x0,y0,x1,y1=bounds;width,height=image_size
    value=np.asarray(array,dtype=np.float32)
    xx=((np.arange(x0,x1,dtype=np.float32)+.5)*value.shape[1]/width-.5)
    yy=((np.arange(y0,y1,dtype=np.float32)+.5)*value.shape[0]/height-.5)
    map_x,map_y=np.meshgrid(xx,yy)
    return cv2.remap(value,map_x,map_y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)


def _weighted_blur(values,mask,sigma):
    weight=_blur(mask,sigma)
    numerator=_blur(values*(mask[...,None] if values.ndim==3 else mask),sigma)
    denominator=np.maximum(weight,1e-5)
    blurred=numerator/(denominator[...,None] if values.ndim==3 else denominator)
    return np.where((weight>1e-5)[...,None],blurred,values) if values.ndim==3 else np.where(weight>1e-5,blurred,values)


def _luminance_target(rgb,target):
    old=rgb@LUMA
    scale=np.clip(np.maximum(target,0)/np.maximum(old,1e-6),0,8)
    return rgb*scale[...,None]


def _neural_delta(original,bounds,prepared,skin_fields):
    if not isinstance(skin_fields,dict) or not skin_fields.get('fields'):
        raise ValueError('请先准备神经均肤层，再调整神经均肤强度。')
    if skin_fields.get('image_size')!=prepared.get('image_size'):
        raise ValueError('均肤层尺寸与当前照片不一致，请重新准备。')
    if skin_fields.get('source_rgb_sha256')!=prepared.get('source_rgb_sha256'):
        raise ValueError('均肤层不属于当前原片，请重新准备。')
    x0,y0,x1,y1=bounds
    result=np.zeros((y1-y0,x1-x0,3),np.float32);count=np.zeros((y1-y0,x1-x0),np.float32)
    for field in skin_fields['fields']:
        a,b,c,d=field['bounds'];l,t,r,z=max(x0,a),max(y0,b),min(x1,c),min(y1,d)
        if l>=r or t>=z:continue
        delta=np.asarray(field['delta'],np.float32)[t-b:z-b,l-a:r-a]
        before=original[t:z,l:r].astype(np.float32)/255
        correction=srgb_to_linear(np.clip(before+delta/255,0,1))-srgb_to_linear(before)
        result[t-y0:z-y0,l-x0:r-x0]+=correction
        count[t-y0:z-y0,l-x0:r-x0]+=(np.asarray(field['mask'])[t-b:z-b,l-a:r-a]>0)
    return result/np.maximum(count,1)[...,None]


def render_region(rgb,bounds,params=None,analysis=None,*,skin_fields=None,region_reader=None,linear_output=False,reader_linear=False,
                  mark_region_reader=None,mark_region_available=False):
    """Render exact native coordinates; use this same call for preview/export.

    region_reader(bounds) supplies the original plus selected candidate/manual
    repairs, including requested halo. Normally uint8 RGB or float sRGB 0..1;
    reader_linear=True accepts float32 linear RGB directly, including HDR >1.
    mark_region_reader(bounds) supplies a sparse user-approved mark mask without
    keeping a full-resolution float mask. Its presence requires the caller's
    explicit mark_region_available=True; individual tiles may legitimately be 0.
    The caller applies its frame selection/protection after this layer stack.
    """
    height,width=_rgb(rgb);p=validate_params(params)
    if not isinstance(bounds,(tuple,list)) or len(bounds)!=4 or any(type(v) is not int for v in bounds):
        raise ValueError('渲染范围须为原图整数像素坐标。')
    x0,y0,x1,y1=bounds
    if not 0<=x0<x1<=width or not 0<=y0<y1<=height:raise ValueError('渲染范围超出图片。')
    active=p['amount']>0 and any(p[key]!=DEFAULT_PARAMS[key] for key in p if key!='amount')
    prepared=analysis or {}
    portrait=active and any(p[key]!=0 for key in (*_SKIN_KEYS,*_FACE_KEYS))
    if portrait:
        if prepared.get('prepared_version')!=PIPELINE_VERSION:
            if prepared.get('face_mask') is not None or prepared.get('skin_mask') is not None:
                prepared=prepare_analysis(rgb,prepared)
            else:raise ValueError('请先分析照片，获取皮肤和五官保护分区后再使用人像修片。')
        if prepared.get('image_size')!=[width,height]:raise ValueError('修片分析尺寸与当前照片不一致。')
        for keys,mask_name,message in [(_SKIN_KEYS,'skin_mask','当前没有可用皮肤分区，请重新分析照片。'),
                                       (_FACE_KEYS,'face_mask','当前没有可用面部皮肤分区，请重新分析照片。')]:
            if any(p[key]!=0 for key in keys) and not np.any(np.asarray(prepared.get(mask_name,[]))>0):raise ValueError(message)
        if p['eye_brighten'] and not np.any(np.asarray(prepared.get('eye_mask',[]))>0):raise ValueError('未获取眼周分区，请先完成面部分析。')
        if p['mark_fade']:
            if mark_region_reader is not None:
                if not callable(mark_region_reader) or mark_region_available is not True:
                    raise ValueError('请先确认痘印色泽的处理范围。')
            elif not np.any(np.asarray(prepared.get('mark_mask',[]))>0):
                raise ValueError('请先选定需要处理的色点候选或手动标记，再使用痘印色泽调整。')
        if p['skin_even'] and not skin_fields:raise ValueError('请先准备神经均肤层。')
        # A portrait occupies only a small fraction of a large camera original.
        # No spatial layer can affect an output pixel outside its semantic mask;
        # avoid expanding/blur-processing the surrounding empty background.
        needed=[]
        if any(p[key] for key in _SKIN_KEYS):needed.append('skin_mask')
        if any(p[key] for key in _FACE_KEYS):needed.append('face_mask')
        if not any(np.any(_sample_map(prepared[name],bounds,[width,height])>0) for name in needed):
            p={**p,**{key:0. for key in (*_SKIN_KEYS,*_FACE_KEYS)}}
            portrait=False
            active=any(p[key]!=DEFAULT_PARAMS[key] for key in p if key!='amount')
    span=float(prepared.get('face_width',min(height,width)*.25))
    small_sigma=float(np.clip(span*.0018,.7,3));medium_sigma=float(np.clip(span*.008,2,14))
    broad_sigma=float(np.clip(span*.025,5,40))
    needs_neighbours=active and any(p[key]>0 for key in ('texture_soften','tone_even','redness','mark_fade','shine_reduce','fine_lines'))
    # Sequential spatial layers expand the dependency radius cumulatively.
    halo=0
    if p['texture_soften'] or p['fine_lines']:halo+=int(math.ceil(3*medium_sigma))+1
    halo+=sum(int(math.ceil(3*broad_sigma)) for key in ('tone_even','redness','mark_fade') if p[key])
    if p['shine_reduce']:halo+=int(math.ceil(3*medium_sigma))
    if not needs_neighbours:halo=0
    padded=[max(0,x0-halo),max(0,y0-halo),min(width,x1+halo),min(height,y1+halo)]
    a,b,c,d=padded
    source=region_reader(padded) if region_reader else rgb[b:d,a:c]
    source=np.asarray(source)
    if source.shape!=(d-b,c-a,3) or not np.isfinite(source).all():raise ValueError('原尺寸分块读取器返回了无效区域。')
    if reader_linear:
        if not np.issubdtype(source.dtype,np.floating) or np.any(source<0):
            raise ValueError('线性分块读取器须返回非负的 float32 RGB。')
        base=source.astype(np.float32)
    else:
        if source.dtype==np.uint8:srgb=source.astype(np.float32)/255
        elif np.issubdtype(source.dtype,np.floating) and np.all((source>=0)&(source<=1)):srgb=source.astype(np.float32)
        else:raise ValueError('分块读取器须返回 uint8 RGB 或 0～1 浮点 sRGB。')
        base=srgb_to_linear(srgb)
    if not active:
        region=base[y0-b:y1-b,x0-a:x1-a]
        if linear_output:return region.copy()
        return source[y0-b:y1-b,x0-a:x1-a].copy() if source.dtype==np.uint8 else encode_srgb(region)
    work=base.copy()
    if portrait:
        skin=_sample_map(prepared['skin_mask'],padded,[width,height])
        face=_sample_map(prepared['face_mask'],padded,[width,height])
        if p['skin_even']:work=np.maximum(work+_neural_delta(rgb,padded,prepared,skin_fields)*p['skin_even'],0)
        if p['texture_soften'] or p['fine_lines']:
            luminance=work@LUMA
            low=_blur(luminance,small_sigma);broad=_blur(luminance,medium_sigma)
            middle=low-broad
            gx=cv2.Sobel(luminance,cv2.CV_32F,1,0,ksize=3)/8
            gy=cv2.Sobel(luminance,cv2.CV_32F,0,1,ksize=3)/8
            edge_guard=1-_smooth(.015,.075,np.sqrt(gx*gx+gy*gy))
            adjustment=-np.clip(middle,-.07,.07)*(.65*p['texture_soften'])*skin*edge_guard
            if p['fine_lines']:
                crease=np.maximum(-middle,0)*_smooth(.002,.025,-middle)
                adjustment+=crease*(.55*p['fine_lines'])*face*edge_guard
            # Fine band luminance-low is retained; no full-face blur substitute.
            work=_luminance_target(work,luminance+adjustment)
        if p['tone_even']:
            luminance=work@LUMA
            chroma=work/(luminance[...,None]+.01)
            local=_weighted_blur(chroma,skin,broad_sigma)
            reference=np.asarray(prepared['skin_reference'],np.float32)
            change=(reference-local)*(luminance[...,None]+.01)
            change-=np.sum(change*LUMA,axis=2)[...,None]
            work+=np.clip(change,-.12,.12)*(.35*p['tone_even'])*skin[...,None]
        if p['redness']:
            luminance=work@LUMA
            opponent=work[...,0]-.5*(work[...,1]+work[...,2])
            local=_weighted_blur(opponent,skin,broad_sigma)
            excess=np.maximum(opponent-local,0)+np.maximum(local,0)*.08
            red=excess*(.65*p['redness'])*skin
            correction=np.stack([-red,red*(LUMA[0]/(LUMA[1]+LUMA[2])),red*(LUMA[0]/(LUMA[1]+LUMA[2]))],axis=2)
            work+=correction
        if p['mark_fade']:
            if mark_region_reader is not None:
                marked=np.asarray(mark_region_reader(padded),dtype=np.float32)
                if marked.shape!=skin.shape or not np.isfinite(marked).all() or np.any(marked<0) or np.any(marked>1):
                    raise ValueError('痘印处理范围须为当前分块尺寸、0～1 的有效蒙版。')
            else:marked=_sample_map(prepared['mark_mask'],padded,[width,height])
            marked=marked*skin
            surrounding=_weighted_blur(work,skin,broad_sigma)
            luminance=work@LUMA;target_y=surrounding@LUMA
            chroma_change=(surrounding-target_y[...,None])-(work-luminance[...,None])
            light=np.maximum(target_y-luminance,0)*.3
            work+=(chroma_change*.55+light[...,None])*marked[...,None]*p['mark_fade']
        if p['shine_reduce']:
            luminance=work@LUMA;local=_weighted_blur(luminance,skin,medium_sigma)
            levels=prepared.get('skin_luminance_quartiles',[.1,.2,.3,.6])
            gate=_smooth(max(.12,float(levels[2])),max(.35,float(levels[3])+.04),luminance)
            correction=np.maximum(luminance-local*.9,0)*gate*skin*(.7*p['shine_reduce'])
            work=_luminance_target(work,luminance-correction)
        if p['eye_brighten']:
            eye=_sample_map(prepared['eye_mask'],padded,[width,height])
            gate=1-_smooth(.45,.85,work@LUMA)
            work*=np.exp2(eye*gate*(.45*p['eye_brighten']))[...,None]
        if p['face_exposure']:
            work*=1+face[...,None]*(2**p['face_exposure']-1)
    if p['exposure']:work*=2**p['exposure']
    if p['temperature'] or p['tint']:
        gain=np.exp2(np.array([.18*p['temperature']+.07*p['tint'],-.1*p['tint'],-.18*p['temperature']+.07*p['tint']],np.float32))
        gain/=float(gain@LUMA)
        work*=gain
    luminance=work@LUMA
    if p['shadows'] or p['highlights']:
        ev=.8*p['shadows']*(1-_smooth(.02,.28,luminance))-.75*p['highlights']*_smooth(.35,.95,luminance)
        work*=np.exp2(ev)[...,None]
    if p['contrast']:
        luminance=work@LUMA
        target=.18*np.power(np.maximum(luminance,1e-8)/.18,1+.65*p['contrast'])
        target[luminance==0]=0
        work=_luminance_target(work,target)
    if p['saturation']!=1:
        luminance=work@LUMA
        work=luminance[...,None]+(work-luminance[...,None])*p['saturation']
    result=np.maximum(base+(work-base)*p['amount'],0).astype(np.float32)
    result=result[y0-b:y1-b,x0-a:x1-a]
    return result.copy() if linear_output else encode_srgb(result)


def iter_render_tiles(rgb,params=None,analysis=None,*,skin_fields=None,region_reader=None,tile_size=512,linear_output=False,reader_linear=False,
                      mark_region_reader=None,mark_region_available=False):
    height,width=_rgb(rgb)
    if type(tile_size) is not int or not 64<=tile_size<=2048:raise ValueError('输出块大小须在 64～2048 像素之间。')
    for y in range(0,height,tile_size):
        for x in range(0,width,tile_size):
            bounds=[x,y,min(width,x+tile_size),min(height,y+tile_size)]
            yield bounds,render_region(rgb,bounds,params,analysis,skin_fields=skin_fields,region_reader=region_reader,linear_output=linear_output,
                                       reader_linear=reader_linear,mark_region_reader=mark_region_reader,mark_region_available=mark_region_available)


def list_presets():
    document=json.loads((Path(__file__).with_name('presets_v2.json')).read_text(encoding='utf-8'))
    seen=set()
    for preset in document['presets']:
        if preset['id'] in seen or preset['category'] not in PRESET_CATEGORIES:
            raise ValueError('预设目录含重复标识或无效分类。')
        seen.add(preset['id'])
        validate_params(preset['params'])
        if any(action not in ('prepare_skin','review_blemishes') for action in preset.get('actions',[])):
            raise ValueError('预设目录含未知准备步骤。')
    return deepcopy(document['presets'])


def resolve_preset(preset_id,current_params=None,strength=1.0):
    intensity=core._number(strength,0,1,'strength')
    preset=next((item for item in list_presets() if item['id']==preset_id),None)
    if preset is None:raise ValueError('该修片预设不存在。')
    result=validate_params(current_params)
    for key,value in preset['params'].items():
        result[key]=DEFAULT_PARAMS[key]+(float(value)-DEFAULT_PARAMS[key])*intensity
    result=validate_params(result)
    needs=list(preset.get('actions',[]))
    if result['skin_even']==0:needs=[step for step in needs if step!='prepare_skin']
    if result['skin_even']>0 and 'prepare_skin' not in needs:needs.append('prepare_skin')
    warnings_out=[]
    if 'review_blemishes' in needs:warnings_out.append('点修只生成待审核候选，不自动去除痣、雀斑或其他色点。')
    if result['amount']==0:warnings_out.append('当前修片总强度为 0；参数已保存，调高总强度后显示效果。')
    operations='、'.join(PARAM_SCHEMA[key]['label'] for key in preset['params'])
    summary=f"已应用《{preset['name']}》：{operations}；预设强度 {intensity:.0%}。"
    exposure_notes=[f"{PARAM_SCHEMA[key]['label']} {result[key]:+.2f} EV" for key in ('exposure','face_exposure') if key in preset['params']]
    if exposure_notes:summary+=' '+ '，'.join(exposure_notes)+'。'
    return {'params':result,'needs':needs,'actions':needs,'summary':summary,
            'warnings':warnings_out,'preset_id':preset['id'],'category':preset['category'],
            'changed_keys':list(preset['params']),'pipeline':PIPELINE_VERSION}
