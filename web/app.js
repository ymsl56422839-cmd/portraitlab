/* PortraitLab local interface. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const $ = id => document.getElementById(id);
const state = { token:'', boot:null, photos:[], selected:new Set(), current:null, view:'compare', split:50, zoom:1, brush:null, brushRadius:.04, busy:0, locked:false, exportIds:null, jobs:null, jobTimer:null, histories:new Map(), seq:new Map(), dirty:new Set(), pending:new Map(), timers:new Map() };
const definitions = [
 {key:'smooth',name:'柔化肤质',min:0,max:1,step:.01,section:'skinControls'},
 {key:'blemishes',name:'淡化瑕疵',min:0,max:1,step:.01,section:'skinControls'},
 {key:'even',name:'均匀肤色',min:0,max:1,step:.01,section:'skinControls'},
 {key:'texture',name:'纹理柔化',min:0,max:1,step:.01,section:'skinControls'},
 {key:'exposure',name:'整体曝光',min:-2,max:2,step:.05,section:'toneControls',unit:'EV'},
 {key:'relight',name:'面部提亮',min:-1,max:1,step:.01,section:'toneControls',signed:true},
 {key:'warmth',name:'冷暖',min:-1,max:1,step:.01,section:'toneControls',signed:true},
 {key:'saturation',name:'饱和度',min:0,max:2,step:.01,section:'toneControls'},
 {key:'opacity',name:'整体效果强度',min:0,max:1,step:.01,section:'opacityControl'},
];
const photo = id => state.photos.find(p => p.id === (id || state.current));
const clone = value => JSON.parse(JSON.stringify(value));
function toast(message,error=false){ const el=document.createElement('div'); el.className='toast'+(error?' error':'');el.textContent=message;$('toasts').append(el);setTimeout(()=>el.remove(),error?8000:4200); }
function errorMessage(error){ return error?.message || String(error); }
async function api(path,options={}){
 const headers=new Headers(options.headers||{}); if(state.token)headers.set('X-PortraitLab-Token',state.token);
 if(options.body && !(options.body instanceof FormData) && typeof options.body!=='string'){headers.set('Content-Type','application/json');options.body=JSON.stringify(options.body);}
 const response=await fetch(path,{credentials:'same-origin',...options,headers});
 let data;try{data=await response.json();}catch{throw new Error('服务没有返回有效结果，请检查工作台是否仍在运行。');}
 if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));return data;
}
const post=(path,body)=>api(path,{method:'POST',body});
function mergePhoto(updated){ const index=state.photos.findIndex(p=>p.id===updated.id);if(index>=0)state.photos[index]={...state.photos[index],...updated};else state.photos.push(updated); }
function choices(){ return state.selected.size?[...state.selected]:(state.current?[state.current]:[]); }
function statusLabel(p){if(p.status==='error')return '处理需要检查';if(p.generation)return `AI 生成 · ${p.width} × ${p.height}`;if(p.face_count===0)return '未检测到人脸';if(p.status==='settings_changed')return '参数已更新';if(p.face_count!=null)return `${p.face_count} 张人脸 · 已调整`;return `${p.width} × ${p.height}`;}
function renderLibrary(){
 const list=$('photoList');list.replaceChildren();
 if(!state.photos.length){const e=document.createElement('div');e.className='library-empty';e.innerHTML='照片与编辑参数<br>会保留在当前工作区';list.append(e);}
 for(const p of state.photos){
  const card=document.createElement('div');card.className='photo-card'+(p.id===state.current?' active':'');card.dataset.id=p.id;card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','编辑 '+p.name);
  const thumb=document.createElement('img');thumb.src=p.preview_url||p.original_url;thumb.alt='';thumb.loading='lazy';
  const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=state.selected.has(p.id);checkbox.setAttribute('aria-label','选择 '+p.name);checkbox.addEventListener('click',e=>e.stopPropagation());checkbox.addEventListener('change',()=>{checkbox.checked?state.selected.add(p.id):state.selected.delete(p.id);updateCounts();});
  const copy=document.createElement('div');copy.className='photo-card-copy';const title=document.createElement('strong');title.textContent=p.name;title.title=p.name;const subtitle=document.createElement('small');subtitle.textContent=statusLabel(p);copy.append(title,subtitle);card.append(thumb,checkbox,copy);
  card.addEventListener('click',()=>selectPhoto(p.id));card.addEventListener('keydown',e=>{if(e.key==='Enter')selectPhoto(p.id);});list.append(card);
 }
 updateCounts();
}
function updateCounts(){
 $('photoCount').textContent=state.photos.length;$('selectedCount').textContent=`${state.selected.size} 张已选`;$('exportCount').textContent=choices().length;$('exportBtn').disabled=state.locked||!choices().length;$('syncCount').textContent=state.selected.size;$('syncBtn').disabled=state.locked||!state.current||state.selected.size<2;
 $('selectAll').checked=!!state.photos.length&&state.selected.size===state.photos.length;$('selectAll').indeterminate=state.selected.size>0&&state.selected.size<state.photos.length;
}
function formatted(def,value){if(def.unit)return `${value>0?'+':''}${value.toFixed(2)} ${def.unit}`;if(def.signed)return `${value>0?'+':''}${Math.round(value*100)}`;return String(Math.round(value*100));}
function updateSlider(def,value){const input=$('control-'+def.key);input.value=value;input.style.setProperty('--range-pct',`${(value-def.min)/(def.max-def.min)*100}%`);$('value-'+def.key).textContent=formatted(def,value);}
for(const def of definitions){
 const row=document.createElement('div');row.className='slider-row';const label=document.createElement('label');label.className='slider-label';label.htmlFor='control-'+def.key;const name=document.createElement('span');name.textContent=def.name;const out=document.createElement('output');out.id='value-'+def.key;label.append(name,out);
 const input=document.createElement('input');Object.assign(input,{type:'range',id:'control-'+def.key,min:def.min,max:def.max,step:def.step,disabled:true});input.setAttribute('aria-label',def.name);row.append(label,input);$(def.section).append(row);
 input.addEventListener('pointerdown',saveHistory);input.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key))saveHistory();});
 input.addEventListener('input',()=>{const p=photo();if(!p||state.locked)return;const value=Number(input.value);p.params[def.key]=value;updateSlider(def,value);clearPresetActive();queuePreview(p.id);});
 input.addEventListener('dblclick',()=>{if(!photo()||state.locked)return;saveHistory();photo().params[def.key]=state.boot.defaults[def.key];updateSlider(def,photo().params[def.key]);queuePreview(state.current,0);});
}
function clearPresetActive(){document.querySelectorAll('#presets button').forEach(b=>b.classList.remove('active'));}
function renderControls(){const p=photo();const params=p?.params||state.boot?.defaults||{};for(const def of definitions){updateSlider(def,Number(params[def.key]??(def.key==='saturation'?1:0)));$('control-'+def.key).disabled=!p||state.locked;}
 for(const id of ['resetBtn','holdOriginal','protectBrush','restoreBrush','clearMask'])$(id).disabled=!p||state.locked;
 document.querySelectorAll('[data-region]').forEach(b=>{b.disabled=!p||state.locked;b.classList.toggle('active',b.dataset.region===params.region);});
 document.querySelectorAll('#presets button').forEach(b=>b.disabled=!p||state.locked);$('undoBtn').disabled=state.locked||!(p&&state.histories.get(p.id)?.length);$('brushCanvas').style.pointerEvents=state.locked?'none':'';clearPresetActive();updateCounts();
}
function saveHistory(){const p=photo();if(!p||state.locked)return;const history=state.histories.get(p.id)||[];const current=clone({params:p.params,strokes:p.strokes||[]});if(JSON.stringify(history.at(-1))!==JSON.stringify(current))history.push(current);if(history.length>30)history.shift();state.histories.set(p.id,history);$('undoBtn').disabled=!history.length;}
function undo(){if(state.locked)return;const p=photo();const prior=p&&state.histories.get(p.id)?.pop();if(!prior)return;p.params=prior.params;p.strokes=prior.strokes;renderControls();queuePreview(p.id,0);}
async function selectPhoto(id){if(id===state.current)return;if(drawing)finishStroke();state.current=id;state.zoom=1;state.brush=null;$('emptyState').hidden=true;$('stageHolder').hidden=false;renderLibrary();renderControls();updateBrush();displayPhoto();const p=photo();$('renderBadge').hidden=!state.pending.has(id);if(p.status!=='ready'&&!state.locked)queuePreview(id,0);}
function setImage(img,url){if(url&&img.getAttribute('src')!==url)img.src=url;}
function displayPhoto(){const p=photo();if(!p)return;setImage($('originalImage'),p.original_url);setImage($('resultImage'),p.preview_url||p.original_url);if(p.mask_url)setImage($('maskImage'),p.mask_url);$('currentName').textContent=p.name;$('currentMeta').textContent=`${p.width} × ${p.height} · ${p.face_count==null?'等待分析':p.face_count+' 张人脸'} · ${p.generation?'AI 生成尺寸':'原尺寸导出'}`;
 document.querySelector('.left-label').textContent=p.comparison_kind==='generation_input'?'生成前（对齐裁切）':p.generation?'生成底图':'原图';document.querySelector('.right-label').textContent=p.generation?'AI 生成 / 调整后':'调整后';const warnings=p.warnings||[];$('notice').hidden=!warnings.length;$('notice').textContent=warnings.join(' · ');setView(state.view);fitStage();
}
function fitStage(){const p=photo();if(!p)return;const v=$('viewport');const ratio=p.width/p.height;const w=Math.max(30,Math.min(v.clientWidth-64,(v.clientHeight-64)*ratio))*state.zoom;const h=w/ratio;$('stage').style.width=w+'px';$('stage').style.height=h+'px';$('stageHolder').style.width=Math.max(v.clientWidth,w+48)+'px';$('stageHolder').style.height=Math.max(v.clientHeight,h+48)+'px';const canvas=$('brushCanvas');canvas.width=Math.round(w);canvas.height=Math.round(h);$('zoomFit').textContent=state.zoom===1?'适合画布':`${Math.round(w/p.width*100)}%`;}
new ResizeObserver(fitStage).observe($('viewport'));
$('originalImage').addEventListener('load',fitStage);
function setView(view){state.view=view;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));const original=$('originalImage');const p=photo();if(p)setImage(original,view==='mask'?(p.preview_url||p.original_url):p.original_url);original.hidden=view==='result';original.style.clipPath=view==='compare'?`inset(0 ${100-state.split}% 0 0)`:'none';$('maskImage').hidden=view!=='mask'||!photo()?.mask_url;$('compareHandle').hidden=view!=='compare'||!!state.brush;document.querySelectorAll('.image-label').forEach(el=>el.hidden=view!=='compare'||!!state.brush);}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('zoomIn').onclick=()=>{state.zoom=Math.min(4,state.zoom*1.25);fitStage();};$('zoomOut').onclick=()=>{state.zoom=Math.max(.5,state.zoom/1.25);fitStage();};$('zoomFit').onclick=()=>{state.zoom=1;fitStage();};
$('compareHandle').addEventListener('pointerdown',e=>{e.preventDefault();e.target.setPointerCapture(e.pointerId);const move=event=>{const rect=$('stage').getBoundingClientRect();state.split=Math.max(1,Math.min(99,(event.clientX-rect.left)/rect.width*100));$('compareHandle').style.left=state.split+'%';$('originalImage').style.clipPath=`inset(0 ${100-state.split}% 0 0)`;};const end=()=>{e.target.removeEventListener('pointermove',move);e.target.removeEventListener('pointerup',end);};e.target.addEventListener('pointermove',move);e.target.addEventListener('pointerup',end);move(e);});
let savedView=null;function holdBefore(on){if(!photo())return;if(on){if(savedView===null)savedView=state.view;setView('original');}else if(savedView!==null){const old=savedView;savedView=null;setView(old);}}
$('holdOriginal').addEventListener('pointerdown',()=>holdBefore(true));window.addEventListener('pointerup',()=>holdBefore(false));$('holdOriginal').addEventListener('pointerleave',()=>holdBefore(false));
function clearPreviewTimer(id){if(state.timers.has(id)){clearTimeout(state.timers.get(id));state.timers.delete(id);}}
function queuePreview(id,delay=420){state.seq.set(id,(state.seq.get(id)||0)+1);state.dirty.add(id);clearPreviewTimer(id);const p=photo(id);if(p)p.status='settings_changed';if(state.pending.has(id))return;state.timers.set(id,setTimeout(()=>{state.timers.delete(id);runPreview(id).catch(error=>toast(errorMessage(error),true));},delay));}
function runPreview(id){
 if(state.pending.has(id))return state.pending.get(id);if(!photo(id))return Promise.resolve();clearPreviewTimer(id);
 // One request in flight per photo. New edits replace the next draft, never the current request.
 const promise=(async()=>{let result;while(true){const p=photo(id);if(!p)return;const seq=state.seq.get(id)||0;const draft=clone({params:p.params,strokes:p.strokes||[]});if(id===state.current)$('renderBadge').hidden=false;
  try{result=await post(`/api/photos/${id}/preview`,draft);}catch(error){if((state.seq.get(id)||0)!==seq)continue;throw error;}
  if((state.seq.get(id)||0)===seq){state.dirty.delete(id);mergePhoto(result.photo);if(state.current===id){displayPhoto();renderControls();}renderLibrary();return result;}
  // An older result must not overwrite unsent sliders, brush marks or undo state.
  clearPreviewTimer(id);
 }})().finally(()=>{if(state.pending.get(id)===promise)state.pending.delete(id);if(state.current===id)$('renderBadge').hidden=true;});state.pending.set(id,promise);return promise;
}
async function flushPhotos(ids){await Promise.all([...new Set(ids)].map(async id=>{while(state.dirty.has(id)||state.timers.has(id)||state.pending.has(id)){clearPreviewTimer(id);await runPreview(id);}}));}
async function flushCurrent(){if(state.current)await flushPhotos([state.current]);}
function lockEditing(locked){state.locked=locked;renderControls();}
async function importFiles(files){const all=[...files];const accepted=all.filter(f=>/\.(jpe?g|png|tiff?|webp)$/i.test(f.name));if(!accepted.length){toast('请选择 JPEG、PNG、TIFF 或 WEBP 照片。',true);return;}if(accepted.length>50){toast('一次最多导入 50 张，请分批导入。',true);return;}const form=new FormData();accepted.forEach(f=>form.append('files',f));$('importBtn').disabled=true;$('connectionStatus').textContent=`正在导入 ${accepted.length} 张照片…`;
 try{const result=await api('/api/import',{method:'POST',body:form});for(const p of result.photos){mergePhoto(p);state.selected.add(p.id);}renderLibrary();if(result.photos.length){await selectPhoto(result.photos[0].id);toast(`已导入 ${result.photos.length} 张，原文件保持不变。`);}if(result.errors?.length)toast(result.errors.map(e=>typeof e==='string'?e:`${e.name||e.filename||'文件'}：${e.error||e.detail}`).join('；'),true);}catch(error){toast(errorMessage(error),true);}finally{$('importBtn').disabled=false;$('connectionStatus').textContent='本地工作台 · 已连接';$('fileInput').value='';$('folderInput').value='';}
}
for(const id of ['importBtn','importSmall','importEmpty'])$(id).onclick=()=>$('fileInput').click();$('fileInput').onchange=e=>importFiles(e.target.files);$('importFolder').onclick=()=>$('folderInput').click();$('folderInput').onchange=e=>importFiles(e.target.files);
let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();dragDepth++;$('dropOverlay').hidden=false;}});document.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});document.addEventListener('dragleave',()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('dropOverlay').hidden=true;});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;if(e.dataTransfer.files.length)importFiles(e.dataTransfer.files);});
$('selectAll').onchange=e=>{state.selected=e.target.checked?new Set(state.photos.map(p=>p.id)):new Set();renderLibrary();};
document.querySelectorAll('[data-region]').forEach(b=>b.onclick=()=>{if(!photo()||state.locked)return;saveHistory();photo().params.region=b.dataset.region;renderControls();queuePreview(state.current,0);});
function buildPresets(){const container=$('presets');container.replaceChildren();for(const preset of state.boot.presets){const b=document.createElement('button');b.textContent=preset.name;b.dataset.preset=preset.id;b.onclick=()=>{if(!photo()||state.locked)return;saveHistory();photo().params=clone(preset.params);renderControls();b.classList.add('active');queuePreview(state.current,0);};container.append(b);}}
$('resetBtn').onclick=()=>{if(!photo()||state.locked)return;saveHistory();photo().params=clone(state.boot.defaults);renderControls();queuePreview(state.current,0);};$('undoBtn').onclick=undo;
$('syncBtn').onclick=async()=>{if(state.locked||!photo())return;const sourceId=state.current;const ids=[...state.selected];lockEditing(true);try{await flushPhotos([...ids,sourceId]);const params=clone(photo(sourceId).params);const result=await post('/api/settings/apply',{ids,params});result.photos.forEach(mergePhoto);renderLibrary();if(ids.includes(state.current))displayPhoto();toast(`参数已同步到 ${ids.length} 张照片，各自的保护笔刷保留。`);}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);if(photo()?.status==='settings_changed'&&!state.dirty.has(state.current))queuePreview(state.current,0);}};
function updateBrush(){$('stage').classList.toggle('brush-on',!!state.brush);$('protectBrush').classList.toggle('active',state.brush==='protect');$('restoreBrush').classList.toggle('active',state.brush==='restore');$('brushCursor').hidden=true;setView(state.view);}
function toggleBrush(mode){if(!photo()||state.locked)return;state.brush=state.brush===mode?null:mode;if(state.brush)setView('mask');updateBrush();}
$('protectBrush').onclick=()=>toggleBrush('protect');$('restoreBrush').onclick=()=>toggleBrush('restore');$('brushSize').oninput=e=>{state.brushRadius=Number(e.target.value)/100;$('brushSizeValue').textContent=e.target.value+'%';};
let drawing=null;const canvas=$('brushCanvas');function pointerPoint(e){const rect=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))];}
canvas.addEventListener('pointermove',e=>{if(!state.brush||state.locked)return;const pt=pointerPoint(e);const diameter=state.brushRadius*Math.min(canvas.clientWidth,canvas.clientHeight)*2;const cursor=$('brushCursor');cursor.hidden=false;Object.assign(cursor.style,{width:diameter+'px',height:diameter+'px',left:pt[0]*canvas.clientWidth+'px',top:pt[1]*canvas.clientHeight+'px'});if(drawing){drawing.points.push(pt);const ctx=canvas.getContext('2d');ctx.lineWidth=diameter;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=drawing.mode==='protect'?'#f18da58c':'#abd7b6a6';const last=drawing.points.at(-2);ctx.beginPath();ctx.moveTo(last[0]*canvas.width,last[1]*canvas.height);ctx.lineTo(pt[0]*canvas.width,pt[1]*canvas.height);ctx.stroke();}});
canvas.addEventListener('pointerleave',()=>{$('brushCursor').hidden=true;});canvas.addEventListener('pointerdown',e=>{if(!state.brush||!photo()||state.locked)return;e.preventDefault();saveHistory();canvas.setPointerCapture(e.pointerId);drawing={photoId:state.current,mode:state.brush,radius:state.brushRadius,points:[pointerPoint(e)]};});
function finishStroke(){if(!drawing)return;const p=photo(drawing.photoId);const {mode,radius,points}=drawing;const stroke={mode,radius,points};drawing=null;if(!p)return;if(stroke.points.length>1000)stroke.points=stroke.points.filter((_,i)=>i%Math.ceil(stroke.points.length/1000)===0);p.strokes=[...(p.strokes||[]),stroke];queuePreview(p.id,0);canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
canvas.addEventListener('pointerup',finishStroke);canvas.addEventListener('pointercancel',finishStroke);
$('clearMask').onclick=()=>{const p=photo();if(!p||state.locked)return;saveHistory();p.strokes=[];queuePreview(p.id,0);toast('已清除手工保护区域。');};
$('exportBtn').onclick=async()=>{if(state.locked)return;const ids=choices();lockEditing(true);try{await flushPhotos(ids);state.exportIds=ids;$('exportDialogCount').textContent=ids.length;$('exportPath').placeholder=state.boot.storage.export_dir;$('exportDialog').showModal();}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);}};
$('startExport').onclick=async()=>{const button=$('startExport');button.disabled=true;const ids=state.exportIds||choices();lockEditing(true);try{await flushPhotos(ids);const result=await post('/api/jobs',{ids,format:$('exportFormat').value,max_side:Number($('exportSize').value),output_dir:$('exportPath').value.trim()||null});state.jobs=result.job;$('exportDialog').close();showJob(result.job);pollJob();}catch(error){toast(errorMessage(error),true);}finally{button.disabled=false;lockEditing(false);}};
const activeStatuses=['queued','running','cancelling'];
function showJob(job){state.jobs=job;$('jobPanel').hidden=false;const active=activeStatuses.includes(job.status);$('jobIcon').className=active?'spinner':'';$('jobIcon').textContent=active?'':job.status==='completed'?'✓':'!';$('jobTitle').textContent=({queued:'已加入导出队列',running:'正在按原始尺寸处理',cancelling:'完成当前照片后停止',completed:'照片已导出',completed_with_errors:'导出完成，部分照片需检查',cancelled:'已停止后续导出',failed:'导出失败',interrupted:'上次导出已中断'})[job.status]||job.status;$('jobProgress').style.width=`${job.total?Math.min(100,job.done/job.total*100):0}%`;
 const details=$('jobDetails');details.replaceChildren();const count=document.createElement('div');count.textContent=`${job.done} / ${job.total} 张${active?' · 后台处理中，可继续查看照片':''}`;details.append(count);for(const item of job.items||[]){if(item.status==='running'||item.error){const line=document.createElement('div');line.textContent=(item.name||item.photo_id||'照片')+(item.error?'：'+item.error:' · 处理中');details.append(line);}}if(!active){const path=document.createElement('div');path.textContent=job.output_dir||job.error||'';details.append(path);}$('cancelJob').hidden=!active;$('cancelJob').disabled=job.status==='cancelling';$('openExport').hidden=active||!job.output_dir;
}
async function pollJob(){clearTimeout(state.jobTimer);if(!state.jobs||!activeStatuses.includes(state.jobs.status))return;try{const result=await api('/api/jobs/'+state.jobs.id);showJob(result.job);}catch(error){toast(errorMessage(error),true);}if(state.jobs&&activeStatuses.includes(state.jobs.status))state.jobTimer=setTimeout(pollJob,1000);}
$('cancelJob').onclick=async()=>{try{const result=await post(`/api/jobs/${state.jobs.id}/cancel`,{});showJob(result.job);}catch(error){toast(errorMessage(error),true);}};$('openExport').onclick=async()=>{try{await post(`/api/jobs/${state.jobs.id}/open`,{});}catch(error){toast(errorMessage(error),true);}};$('closeJob').onclick=()=>{$('jobPanel').hidden=true;};
$('aboutBtn').onclick=()=>$('aboutDialog').showModal();$('modelsBtn').onclick=()=>$('modelsDialog').showModal();document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
function modelInfo(){const data=state.boot.models;$('modelStatusText').textContent=data.ready?'处理模型已准备 · CPU':'处理模型需要准备';const container=$('modelsDetails');container.replaceChildren();for(const [id,model] of Object.entries(data.models||{})){const card=document.createElement('div');card.className='source-card';const title=document.createElement('b');title.textContent=id==='face_landmarker'?'人脸与五官关键点':'皮肤、头发与衣物分区';const status=document.createElement('span');status.textContent=model.exists?'文件已准备；首次处理会校验完整性。':'尚未下载。';const hash=document.createElement('span');hash.textContent='SHA-256: '+model.sha256;hash.style.wordBreak='break-all';hash.style.fontSize='9px';card.append(title,status,hash);container.append(card);}}
document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)||document.querySelector('dialog[open]'))return;if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='o'){e.preventDefault();$('fileInput').click();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();undo();}if(e.code==='Space'){e.preventDefault();holdBefore(true);}if(e.key.toLowerCase()==='b')toggleBrush('protect');if(e.key==='Escape'&&state.brush){state.brush=null;updateBrush();}});document.addEventListener('keyup',e=>{if(e.code==='Space')holdBefore(false);});window.addEventListener('blur',()=>holdBefore(false));
async function boot(){try{state.boot=await api('/api/bootstrap');state.token=state.boot.token;buildPresets();modelInfo();const result=await api('/api/photos');state.photos=result.photos;renderLibrary();renderControls();$('connectionStatus').textContent='本地工作台 · 已连接';if(state.photos.length){state.selected.add(state.photos[0].id);await selectPhoto(state.photos[0].id);}const jobs=await api('/api/jobs');const active=jobs.jobs.find(j=>activeStatuses.includes(j.status));if(active){showJob(active);pollJob();}document.dispatchEvent(new Event('portraitlab-ready'));}catch(error){$('connectionStatus').textContent='无法连接处理服务';toast(errorMessage(error),true);}}
boot();
