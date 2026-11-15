"""Explicit verification/downloads and portable model overlays for V2.

SPDX-License-Identifier: AGPL-3.0-only
File checks, runtime dependencies and device probing are distinct states.
Reading the catalog never downloads weights or imports Torch.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import threading
import urllib.request
import uuid

from portraitlab.engine import MODEL_SPECS
from portraitlab.lighting_geometry import manifest as depth_manifest
from . import neural_candidate, skin_model


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def _signature(path):
    stat=Path(path).stat()
    return stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns


def _package_exists(name):
    try:return importlib.util.find_spec(name) is not None
    except (ImportError,ValueError,AttributeError):return False


class ModelCatalog:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock();self.verified={};self.vendor_verified={}
        self.state='unchecked';self.message='模型文件和运行组件尚未校验。'
        self.devices={'status':'unchecked','cuda_available':None,'gpu_name':None,'torch_version':None,'message':'尚未检测模型推理设备。'}
        self.device_runtime=None
        self.analysis_runtime={'state':'unchecked','message':'人物分析运行组件尚未加载检查。'}
        self.download={'status':'idle','id':None,'done':0,'total':0,'message':''}
        self.paths=Path(store.data)/'model-paths.json'
        self.configuration_messages=[]
        self._restore_overlays()

    def _overlay_root(self,group):
        data=Path(self.store.data).resolve()
        root=(data/'models'/('neural' if group=='neural' else 'analysis')).resolve()
        if not root.is_relative_to(data):raise ValueError('模型覆盖目录超出当前数据目录。')
        return root

    def _restore_overlays(self):
        if not self.paths.exists():return
        try:
            if not self.paths.resolve().is_relative_to(Path(self.store.data).resolve()):
                raise ValueError('模型路径配置位于数据目录之外。')
            configured=json.loads(self.paths.read_text(encoding='utf-8'))
            if not isinstance(configured,dict):raise ValueError('模型路径配置格式无效。')
            if configured.get('schema_version',1) not in (1,2):raise ValueError('模型路径配置版本不受支持。')
            pending={}
            for key,group in (('analysis_models','analysis'),('neural_models','neural')):
                value=configured.get(key)
                if value in (None,''):continue
                if not isinstance(value,str):raise ValueError('模型路径配置须为字符串。')
                expected=self._overlay_root(group);candidate=Path(value)
                if candidate.is_absolute():
                    if candidate.resolve()!=expected:
                        suffix=tuple(part.lower() for part in candidate.parts[-2:])
                        if suffix==('models',group) and expected.is_dir():candidate=expected
                        else:
                            self.configuration_messages.append('旧配置中的外部模型路径未覆盖本次启动设置。')
                            continue
                else:candidate=(Path(self.store.data)/candidate).resolve()
                if candidate.resolve()!=expected:raise ValueError('模型覆盖路径须位于本数据目录的 models/'+group+'。')
                pending[key]=expected
            for key,value in pending.items():setattr(self.store,key,value)
        except (OSError,ValueError,TypeError) as error:
            self.configuration_messages.append('模型路径配置未加载：'+str(error))
        if self.configuration_messages:self.message=' '.join(dict.fromkeys(self.configuration_messages))+' 请检查模型状态。'

    def _runtime_identity(self):
        configured=self.store.ai_python
        if not configured:return None
        path=Path(configured).resolve()
        try:
            site=path.parent/'Lib/site-packages'
            markers=[site/'torch/__init__.py',site/'torch/lib/torch_cpu.dll',site/'numpy/__init__.py',site/'PIL/__init__.py']
            return str(path),_signature(path) if path.is_file() else None,tuple(
                (str(marker),_signature(marker) if marker.is_file() else None) for marker in markers)
        except OSError:return str(path),None

    def _vendor_files(self,kind):
        if not self.store.vendor:return []
        root=Path(self.store.vendor).resolve();module=skin_model if kind=='skin_vendor' else neural_candidate
        return [(root/'LICENSE',neural_candidate.LICENSE_SHA256)]+[
            (root/'modelscope/models/cv/skin_retouching'/name,sha) for name,sha in module.SOURCE_HASHES.items()]

    def dependencies(self):
        portrait_present=all(_package_exists(name) for name in ('mediapipe','cv2','numpy','PIL'))
        portrait_state=self.analysis_runtime['state'] if portrait_present else 'missing'
        if portrait_state=='unchecked':portrait_state='unverified'
        result={'analysis_runtime':{'id':'analysis_runtime','name':'人物分析组件','state':portrait_state,
            'message':self.analysis_runtime['message'] if portrait_present else '缺少 MediaPipe 或图像组件；下载权重不能补齐运行组件。'}}
        identity=self._runtime_identity();runtime_path=Path(identity[0]) if identity else None
        missing=[]
        if runtime_path is None or not runtime_path.is_file():missing.append('AI Python 解释器')
        elif list(runtime_path.parent.glob('python*._pth')):
            site=runtime_path.parent/'Lib/site-packages'
            for name in ('torch','numpy','PIL'):
                if not (site/name/'__init__.py').is_file():missing.append(name)
            if not (site/'torch/lib/torch_cpu.dll').is_file():missing.append('PyTorch DLL')
        if missing:state='missing';message='缺少 '+ '、'.join(missing)+'；请补齐 AI 运行组件。'
        elif self.devices['status']=='ready' and self.device_runtime==identity:
            state='ready';message='模型运行时已成功加载；具体照片处理仍需验收。'
        elif self.devices['status']=='error' and self.device_runtime==identity:
            state='error';message=self.devices['message']
        else:state='unverified';message='已找到解释器，等待显式检查模型运行时和设备。'
        result['ai_runtime']={'id':'ai_runtime','name':'模型推理运行时','state':state,'message':message,
                              'path':str(runtime_path) if runtime_path else None,'missing':missing}
        for kind,name in (('blemish_vendor','点修网络代码'),('skin_vendor','均肤网络代码')):
            files=self._vendor_files(kind);absent=[path.name for path,_ in files if not path.is_file()]
            if not files or absent:
                state='missing';message='网络代码或许可文件缺失；下载权重不能补齐代码。'
            else:
                try:signature=tuple((str(path),_signature(path)) for path,_ in files)
                except OSError:signature=None
                checked=self.vendor_verified.get(kind)
                if checked and checked[0]==signature:
                    state='ready' if checked[1] else 'error'
                    message='固定版本代码及许可校验通过。' if checked[1] else '网络代码或许可与固定版本不符，请恢复随附组件。'
                else:state='unverified';message='已找到网络代码，等待固定版本校验。'
            result[kind]={'id':kind,'name':name,'state':state,'message':message,'missing':absent}
        code=Path(__file__).resolve().parents[2]/'portraitlab/vendor/depth_anything_v2'
        present=all((code/name).is_file() for name in ('dpt.py','dinov2.py','dinov2_layers/__init__.py'))
        result['depth_code']={'id':'depth_code','name':'空间分析代码','state':'ready' if present else 'missing',
                             'message':'随附深度网络代码文件存在。' if present else '缺少深度网络代码，下载权重不能补齐代码。'}
        return result

    def records(self):
        base=Path(self.store.analysis_models or self.store.data/'models'/'analysis').resolve()
        neural=Path(self.store.neural_models or self.store.data/'models'/'neural').resolve()
        records=[];labels={'face_landmarker':('人物定位','脸型与五官保护'),'selfie_multiclass':('人物分区','皮肤和主体范围')}
        for identifier,spec in MODEL_SPECS.items():
            name,role=labels.get(identifier,(identifier,'人物分析'))
            records.append({'id':identifier,'group':'analysis','name':name,'role':role,'path':str(base/identifier/spec['filename']),
                'relative_path':identifier+'/'+spec['filename'],'sha256':spec['sha256'],'bytes':None,'license':spec['upstream_license_claim'],
                'license_note':'权重许可按上游声明列出，代码许可与权重来源分别记录。','source_url':spec['license_source'],'url':spec['url'],
                'requires':['analysis_runtime']})
        depth=depth_manifest();item=depth['files'][0]
        records.append({'id':'lighting','group':'analysis','name':'空间光影分析','role':'深度和法线','path':str(base/item['relative_path']),
            'relative_path':item['relative_path'],'sha256':item['sha256'],'bytes':item['bytes'],'license':depth['weights_license'],
            'source_url':depth['model_repository'],'url':item['url'],'requires':['ai_runtime','depth_code']})
        for identifier,name,module,size in [('blemish','局部瑕疵修补',neural_candidate,227618450),('skin','神经均肤',skin_model,53646276)]:
            filename=module.MODEL_FILE
            records.append({'id':identifier,'group':'neural','name':name,'role':'点状修补' if identifier=='blemish' else '肤色曲线场',
                'path':str(neural/filename),'relative_path':filename,'sha256':module.MODEL_SHA256,'bytes':size,'license':'Apache-2.0',
                'source_url':'https://modelscope.cn/models/'+neural_candidate.MODEL_ID,
                'url':'https://modelscope.cn/api/v1/models/'+neural_candidate.MODEL_ID+'/repo?Revision='+neural_candidate.MODEL_REVISION+'&FilePath='+filename,
                'requires':['ai_runtime','skin_vendor' if identifier=='skin' else 'blemish_vendor']})
        dependencies=self.dependencies()
        for record in records:
            record['expected_bytes']=record['bytes'];path=Path(record['path'])
            try:
                if not path.is_file():file_state='missing';message='尚未下载或未找到文件。'
                else:
                    signature=_signature(path);verified=self.verified.get(record['id'])
                    if verified and verified[:2]==(str(path),signature):
                        file_state='ready' if verified[2] else 'error'
                        message='模型文件已校验。' if verified[2] else '文件与固定模型不符，请重新下载。'
                    else:file_state='unverified';message='已找到模型文件，等待校验。'
                    record['bytes']=record['bytes'] or signature[0]
            except OSError:file_state='error';message='无法读取模型文件，请检查文件权限。'
            unavailable=[dependencies[key] for key in record['requires'] if dependencies[key]['state']!='ready'];state=file_state
            if file_state=='ready' and unavailable:
                state='blocked' if any(d['state'] in ('missing','error') for d in unavailable) else 'unverified'
                message+=' '+ ' '.join(d['name']+'：'+d['message'] for d in unavailable)
            record.update(state=state,file_state=file_state,message=message,
                          dependencies=[deepcopy(dependencies[key]) for key in record['requires']],
                          downloadable=file_state in ('missing','error'))
        return records

    def public(self):
        with self.lock:
            records=self.records()
            if self.state=='ready' and any(record['state']!='ready' for record in records):
                self.state='unchecked';self.message='模型文件或运行组件已变化，请重新检查。'
            for record in records:
                if self.download['status']=='running' and self.download['id']==record['id']:record['state']='downloading'
            return {'status':self.state,'message':self.message,'models':records,'dependencies':self.dependencies(),
                'devices':deepcopy(self.devices),'download':deepcopy(self.download),
                'configuration_messages':list(dict.fromkeys(self.configuration_messages)),
                'capabilities':{'gpu_scope':'神经修补、均肤和深度模型推理可用 NVIDIA CUDA；确定性图像合成与几何采样使用 CPU。'}}

    def check(self):
        with self.lock:
            if self.state=='checking':return self.public()
            if self.store.closing:raise ValueError('程序正在退出，无法开始检查。')
            self.state='checking';self.message='正在校验模型、网络代码与模型推理设备。';self.devices['status']='checking'
        try:self.store.pool.submit(self._check)
        except Exception:
            with self.lock:self.state='error';self.message='检查任务未能启动。';self.devices['status']='unchecked'
            raise
        return self.public()

    def _check(self):
        try:
            try:
                mediapipe=importlib.import_module('mediapipe')
                if not hasattr(mediapipe,'tasks'):raise ImportError('MediaPipe Tasks 接口缺失')
                for name in ('cv2','numpy','PIL'):importlib.import_module(name)
                with self.lock:self.analysis_runtime={'state':'ready','message':'人物分析运行组件已加载；实际照片推理仍需验收。'}
            except (ImportError,OSError,AttributeError) as error:
                with self.lock:self.analysis_runtime={'state':'error','message':'人物分析组件无法加载：'+str(error)}
            for record in self.records():
                path=Path(record['path'])
                if not path.is_file():continue
                before=_signature(path);good=digest(path)==record['sha256'];after=_signature(path)
                with self.lock:self.verified[record['id']]=(str(path),after,good and before==after)
            for kind in ('blemish_vendor','skin_vendor'):
                files=self._vendor_files(kind)
                if not files or any(not path.is_file() for path,_ in files):continue
                before=tuple((str(path),_signature(path)) for path,_ in files)
                good=all(digest(path)==sha for path,sha in files)
                after=tuple((str(path),_signature(path)) for path,_ in files)
                with self.lock:self.vendor_verified[kind]=(after,good and before==after)
            identity=self._runtime_identity()
            try:
                runtime=self.dependencies()['ai_runtime']
                if runtime['state']=='missing':raise ValueError(runtime['message'])
                response=self.store._request_worker({},operation='devices')
                device=response.get('devices',response)
                if response.get('ok') is not True or not isinstance(device,dict) or type(device.get('cuda_available')) is not bool:
                    raise ValueError('设备检查响应缺少有效的可用性字段。')
                if not isinstance(device.get('torch_version'),str) or not device['torch_version']:
                    raise ValueError('设备检查未返回有效的 PyTorch 版本。')
                if device['cuda_available'] and (not isinstance(device.get('gpu_name'),str) or not device['gpu_name']):
                    raise ValueError('设备检查未返回有效的 GPU 名称。')
                with self.lock:
                    self.device_runtime=identity
                    self.devices={'status':'ready','cuda_available':device['cuda_available'],'gpu_name':device.get('gpu_name'),
                        'torch_version':device['torch_version'],'message':'NVIDIA GPU 和 CPU 均可选择用于模型推理。' if device['cuda_available'] else '模型运行时已加载，可选择 CPU；未检测到可用的 NVIDIA CUDA。'}
            except Exception as error:
                with self.lock:
                    self.device_runtime=identity
                    self.devices={'status':'error','cuda_available':None,'gpu_name':None,'torch_version':None,'message':'模型推理设备检查失败：'+str(error)}
            with self.lock:
                complete=all(record['state']=='ready' for record in self.records())
                self.state='ready' if complete else 'error'
                self.message='模型文件及运行组件已检查；实际效果仍需照片验收。' if complete else '部分模型或运行组件尚未就绪，请查看具体状态。'
        except Exception as error:
            with self.lock:
                self.state='error';self.message='模型检查未完成：'+str(error)
                if self.devices['status']=='checking':self.devices.update(status='unchecked',message='本轮设备检查尚未完成。')

    def start_download(self,identifier):
        record=next((item for item in self.records() if item['id']==identifier),None)
        if record is None:raise ValueError('未知模型。')
        with self.lock:
            if self.store.closing:raise ValueError('程序正在退出，无法开始下载。')
            if self.download['status']=='running':raise ValueError('已有模型正在下载。')
            if record['file_state']=='ready':raise ValueError('该模型文件已经校验，无需重复下载；请检查尚未就绪的运行组件。')
            self.download={'status':'running','id':identifier,'done':0,'total':record['expected_bytes'] or 0,'message':'准备从固定来源下载。'}
        try:self.store.pool.submit(self._download,record)
        except Exception:
            with self.lock:self.download.update(status='error',message='下载任务未能启动。')
            raise
        return self.public()

    def _owned_target(self,root,relative):
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_relative_to(Path(self.store.data).resolve()):
            raise ValueError('模型下载路径超出当前数据目录。')
        return path

    def _persist_overlay(self,key,root):
        values={'schema_version':2}
        for candidate,group in (('analysis_models','analysis'),('neural_models','neural')):
            current=root if candidate==key else getattr(self.store,candidate);owned=self._overlay_root(group)
            values[candidate]=str(owned.relative_to(Path(self.store.data).resolve())).replace('\\','/') if current and Path(current).resolve()==owned else None
        temporary=self.paths.with_name(self.paths.name+'.'+uuid.uuid4().hex+'.part')
        try:
            temporary.write_text(json.dumps(values,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temporary.replace(self.paths)
        finally:temporary.unlink(missing_ok=True)

    def _download(self,record):
        temporary=None
        try:
            root=self._overlay_root(record['group'])
            target=self._owned_target(root,record['relative_path']);target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_suffix(target.suffix+'.'+uuid.uuid4().hex+'.part');expected=record['expected_bytes']
            with urllib.request.urlopen(urllib.request.Request(record['url'],headers={'User-Agent':'PortraitLab-V2'}),timeout=45) as response,temporary.open('xb') as out:
                total=expected or int(response.headers.get('Content-Length') or 0);done=0;sha=hashlib.sha256()
                while chunk:=response.read(512*1024):
                    if self.store.closing:raise ValueError('原型退出，下载已停止。')
                    done+=len(chunk)
                    if done>(expected or 500_000_000):raise ValueError('下载长度超过固定清单限制。')
                    sha.update(chunk);out.write(chunk)
                    with self.lock:self.download.update(done=done,total=total,message='正在下载 '+record['name'])
            if (expected is not None and done!=expected) or sha.hexdigest()!=record['sha256']:
                raise ValueError('下载校验失败，未启用该文件。')
            if self.store.closing:raise ValueError('原型退出，下载未启用。')
            temporary.replace(target)
            for sibling in self.records():
                if sibling['group']!=record['group'] or sibling['id']==record['id']:continue
                source=Path(sibling['path']);dest=self._owned_target(root,sibling['relative_path'])
                if not source.is_file() or source.resolve()==dest or digest(source)!=sibling['sha256']:continue
                dest.parent.mkdir(parents=True,exist_ok=True);staged=dest.with_name(dest.name+'.'+uuid.uuid4().hex+'.part')
                try:
                    shutil.copyfile(source,staged)
                    if digest(staged)!=sibling['sha256']:raise ValueError('模型复制后校验失败，未切换目录。')
                    staged.replace(dest)
                finally:staged.unlink(missing_ok=True)
            key='neural_models' if record['group']=='neural' else 'analysis_models'
            with self.store.lock:
                self._persist_overlay(key,root)
                if key=='analysis_models' and self.store.engine:self.store.engine.close();self.store.engine=None
                setattr(self.store,key,root)
            with self.lock:
                self.download.update(status='complete',message='下载并校验完成，正在检查运行组件。')
                self.state='checking';self.devices['status']='checking'
                self.message='模型下载完成，正在检查完整功能依赖。'
            self._check()
        except Exception as error:
            with self.lock:self.download.update(status='error',message=str(error))
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
