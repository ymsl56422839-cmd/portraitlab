/* PortraitLab local interface. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const $ = id => document.getElementById(id);
const state = { token:'', boot:null, photos:[], selected:new Set(), current:null, view:'result', module:'retouch', split:50, zoom:1, brush:null, brushRadius:.04, busy:0, locked:false, exportIds:null, jobs:null, jobTimer:null, histories:new Map(), seq:new Map(), dirty:new Set(), pending:new Map(), timers:new Map(), importing:false, importTarget:null, batchUndo:null, hardware:null };
const definitions = [
 {key:'smooth',name:'柔化肤质',min:0,max:1,step:.01,section:'skinControls'},
 {key:'blemishes',name:'淡化瑕疵',min:0,max:1,step:.01,section:'skinControls'},
 {key:'even',name:'均匀肤色',min:0,max:1,step:.01,section:'skinControls'},
 {key:'texture',name:'纹理柔化',min:0,max:1,step:.01,section:'skinControls'},
 {key:'exposure',name:'整体曝光',min:-2,max:2,step:.05,section:'toneControls',unit:'EV'},
 {key:'relight',name:'面部曝光',min:-1,max:1,step:.01,section:'toneControls',signed:true},
 {key:'warmth',name:'冷暖',min:-1,max:1,step:.01,section:'toneControls',signed:true},
 {key:'saturation',name:'饱和度',min:0,max:2,step:.01,section:'toneControls'},
 {key:'opacity',name:'整体效果强度',min:0,max:1,step:.01,section:'opacityControl'},
];
const photo = id => state.photos.find(p => p.id === (id || state.current));
const clone = value => JSON.parse(JSON.stringify(value));
function toast(message,error=false){const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;if(error)el.setAttribute('role','alert');$('toasts').replaceChildren(el);return el;}
function updateConnectionStatus(){
 if(!state.token||state.importing)return;
 const tasks=[];if(state.lightingTask)tasks.push(state.lightingTask);if(state.jobs&&['queued','running','cancelling'].includes(state.jobs.status))tasks.push('照片导出中');if(state.pending.size)tasks.push('预览更新中');
 $('connectionStatus').textContent=tasks.join(' · ')||(state.locked?'正在提交…':'已就绪');
}
function setModule(module){
 if(state.locked)return;if(drawing)finishStroke();state.module=module;state.brush=null;updateBrush();$('retouchPanel').hidden=module!=='retouch';$('lightingPanel').hidden=module!=='lighting';
 for(const button of document.querySelectorAll('[data-module]')){const selected=button.dataset.module===module;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));}
 setView('result');renderControls();toast(module==='lighting'?'光影重塑：确认本次输入，选择光线，再生成新照片。':'修片：预设和参数只应用到当前照片，批量套用需单独操作。');document.dispatchEvent(new Event('portraitlab-module-change'));
}
for(const button of document.querySelectorAll('[data-module]'))button.onclick=()=>{if(button.dataset.module==='lighting'){if(typeof openAI==='function')openAI();}else{setModule('retouch');toast('修片：预设和参数只应用到当前照片，批量套用需单独操作。');}};
function errorMessage(error){ return error?.message || String(error); }
async function api(path,options={}){
 const headers=new Headers(options.headers||{}); if(state.token)headers.set('X-PortraitLab-Token',state.token);
 if(options.body && !(options.body instanceof FormData) && typeof options.body!=='string'){headers.set('Content-Type','application/json');options.body=JSON.stringify(options.body);}
 const response=await fetch(path,{credentials:'same-origin',...options,headers});
 let data;try{data=await response.json();}catch{throw new Error('服务没有返回有效结果，请检查软件服务是否仍在运行。');}
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
  card.addEventListener('click',()=>selectPhoto(p.id));card.addEventListener('keydown',e=>{if(e.target===card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();selectPhoto(p.id);}});list.append(card);
 }
 updateCounts();
}
function updateCounts(){
 $('photoCount').textContent=state.photos.length;$('selectedCount').textContent=`${state.selected.size} 张已选`;$('exportCount').textContent=choices().length;$('exportBtn').disabled=state.locked||!choices().length;$('syncCount').textContent=state.selected.size;$('syncBtn').disabled=state.locked||!state.current||state.selected.size<1;document.dispatchEvent(new Event('portraitlab-selection-change'));
 $('selectAll').checked=!!state.photos.length&&state.selected.size===state.photos.length;$('selectAll').indeterminate=state.selected.size>0&&state.selected.size<state.photos.length;
}
function formatted(def,value){if(def.unit)return `${value>0?'+':''}${value.toFixed(2)} ${def.unit}`;if(def.signed)return `${value>0?'+':''}${Math.round(value*100)}`;return String(Math.round(value*100));}
function updateSlider(def,value){const input=$('control-'+def.key);input.value=value;input.style.setProperty('--range-pct',`${(value-def.min)/(def.max-def.min)*100}%`);$('value-'+def.key).textContent=formatted(def,value);}
for(const def of definitions){
 const row=document.createElement('div');row.className='slider-row';const label=document.createElement('label');label.className='slider-label';label.htmlFor='control-'+def.key;const name=document.createElement('span');name.textContent=def.name;const out=document.createElement('output');out.id='value-'+def.key;label.append(name,out);
 const input=document.createElement('input');Object.assign(input,{type:'range',id:'control-'+def.key,min:def.min,max:def.max,step:def.step,disabled:true});input.setAttribute('aria-label',def.name);row.append(label,input);$(def.section).append(row);
 input.addEventListener('pointerdown',saveHistory);input.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','PageUp','PageDown','Home','End'].includes(e.key))saveHistory();});
 input.addEventListener('input',()=>{const p=photo();if(!p||state.locked)return;const value=Number(input.value);p.params[def.key]=value;updateSlider(def,value);clearPresetActive();queuePreview(p.id);});
 input.addEventListener('change',()=>{if(photo()&&!state.locked)toast(`《${photo().name}》已微调${def.name}，仅当前照片。`);});
 input.addEventListener('dblclick',()=>{if(!photo()||state.locked)return;saveHistory();photo().params[def.key]=state.boot.defaults[def.key];updateSlider(def,photo().params[def.key]);queuePreview(state.current,0);});
}
function clearPresetActive(){window.PortraitPresets?.refresh();}
function renderControls(){const p=photo();const params=p?.params||state.boot?.defaults||{};for(const def of definitions){updateSlider(def,Number(params[def.key]??(def.key==='saturation'?1:0)));$('control-'+def.key).disabled=!p||state.locked;}
 for(const id of ['resetBtn','holdOriginal','protectBrush','restoreBrush','clearMask'])$(id).disabled=!p||state.locked;
 document.querySelectorAll('[data-region]').forEach(b=>{b.disabled=!p||state.locked;b.classList.toggle('active',b.dataset.region===params.region);});
 document.querySelectorAll('#presets .preset-apply').forEach(b=>b.disabled=!p||state.locked);$('undoBtn').disabled=state.locked||!(p&&state.histories.get(p.id)?.length);$('brushCanvas').style.pointerEvents=state.locked?'none':'';clearPresetActive();updateCounts();for(const id of ['importBtn','importSmall','importEmpty','importIcon','canvasImport','importFolder','exportImportFiles','exportImportFolder','aiImportFiles','aiImportFolder']){const button=$(id);if(button)button.disabled=state.locked||state.importing;}$('startExport').disabled=state.locked||state.importing;document.querySelectorAll('[data-module]').forEach(button=>button.disabled=state.locked||!state.token);document.dispatchEvent(new Event('portraitlab-edit-state'));
}
function saveHistory(){const p=photo();if(!p||state.locked)return;const history=state.histories.get(p.id)||[];const current=clone({params:p.params,strokes:p.strokes||[]});if(JSON.stringify(history.at(-1))!==JSON.stringify(current))history.push(current);if(history.length>30)history.shift();state.histories.set(p.id,history);$('undoBtn').disabled=!history.length;}
function undo(){if(state.locked)return;const p=photo();const prior=p&&state.histories.get(p.id)?.pop();if(!prior)return;p.params=prior.params;p.strokes=prior.strokes;renderControls();queuePreview(p.id,0);}
async function selectPhoto(id){if(state.locked||!photo(id)||id===state.current)return;if(drawing)finishStroke();state.current=id;state.zoom=1;state.brush=null;$('emptyState').hidden=true;$('stageHolder').hidden=false;renderLibrary();renderControls();updateBrush();displayPhoto();const p=photo();$('renderBadge').hidden=!state.pending.has(id);if(p.status!=='ready'&&!state.locked)queuePreview(id,0);document.dispatchEvent(new CustomEvent('portraitlab-photo-selected',{detail:{id}}));}
function setImage(img,url){if(url&&img.getAttribute('src')!==url)img.src=url;}
function displayPhoto(){const p=photo();if(!p)return;setImage($('originalImage'),p.original_url);setImage($('resultImage'),p.preview_url||p.original_url);if(p.mask_url)setImage($('maskImage'),p.mask_url);$('currentName').textContent=p.name;$('currentMeta').textContent=`${p.width} × ${p.height} · ${p.face_count==null?'等待分析':p.face_count+' 张人脸'} · ${p.generation?'AI 生成尺寸':'原尺寸导出'}`;
 document.querySelector('.left-label').textContent=p.comparison_kind==='generation_input'?'生成前（对齐裁切）':p.generation?'生成底图':'原图';document.querySelector('.right-label').textContent=p.generation?'AI 生成 / 调整后':'调整后';const warnings=p.warnings||[];$('notice').hidden=!warnings.length;$('notice').textContent=warnings.join(' · ');updateComparison();setView(state.view);fitStage();
}
function updateComparison(){const p=photo();if(!p)return;const before=$('compareBeforeThumb'),after=$('compareAfterThumb');before.hidden=false;after.hidden=false;setImage(before,p.original_url);setImage(after,p.preview_url||p.original_url);$('compareBeforeLabel').textContent=p.comparison_kind==='generation_input'?'生成前（对齐裁切）':p.generation?'生成底图':'原图';$('compareAfterLabel').textContent=p.generation?'生成后 / 调整后':'处理后';$('comparisonHint').textContent=p.name;$('comparisonHint').title=p.name;}
function fitStage(){const p=photo();if(!p)return;const v=$('viewport');const ratio=p.width/p.height;const w=Math.max(30,Math.min(v.clientWidth-64,(v.clientHeight-64)*ratio))*state.zoom;const h=w/ratio;$('stage').style.width=w+'px';$('stage').style.height=h+'px';$('stageHolder').style.width=Math.max(v.clientWidth,w+48)+'px';$('stageHolder').style.height=Math.max(v.clientHeight,h+48)+'px';const canvas=$('brushCanvas');canvas.width=Math.round(w);canvas.height=Math.round(h);$('zoomFit').textContent=state.zoom===1?'适合画布':`${Math.round(w/p.width*100)}%`;}
new ResizeObserver(fitStage).observe($('viewport'));
$('originalImage').addEventListener('load',fitStage);
function setView(view){state.view=view;$('moduleCaption').textContent=(state.module==='lighting'?'光影重塑':'修片')+' · '+({compare:'原图与结果对照',result:'处理后大图',original:'原图大图',mask:'皮肤选区'}[view]||view);document.querySelectorAll('[data-view]').forEach(b=>(b.classList.toggle('active',b.dataset.view===view),b.setAttribute('aria-pressed',String(b.dataset.view===view))));const original=$('originalImage');const p=photo();if(p)setImage(original,view==='mask'?(p.preview_url||p.original_url):p.original_url);original.hidden=view==='result';original.style.clipPath=view==='compare'?`inset(0 ${100-state.split}% 0 0)`:'none';$('maskImage').hidden=view!=='mask'||!photo()?.mask_url;$('compareHandle').hidden=view!=='compare'||!!state.brush;document.querySelectorAll('.image-label').forEach(el=>el.hidden=view!=='compare'||!!state.brush);}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('zoomIn').onclick=()=>{state.zoom=Math.min(4,state.zoom*1.25);fitStage();};$('zoomOut').onclick=()=>{state.zoom=Math.max(.5,state.zoom/1.25);fitStage();};$('zoomFit').onclick=()=>{state.zoom=1;fitStage();};
$('compareHandle').addEventListener('pointerdown',e=>{e.preventDefault();e.target.setPointerCapture(e.pointerId);const move=event=>{const rect=$('stage').getBoundingClientRect();state.split=Math.max(1,Math.min(99,(event.clientX-rect.left)/rect.width*100));$('compareHandle').style.left=state.split+'%';$('originalImage').style.clipPath=`inset(0 ${100-state.split}% 0 0)`;};const end=()=>{e.target.removeEventListener('pointermove',move);e.target.removeEventListener('pointerup',end);};e.target.addEventListener('pointermove',move);e.target.addEventListener('pointerup',end);move(e);});
let savedView=null;function holdBefore(on){if(!photo())return;if(on){if(savedView===null)savedView=state.view;setView('original');}else if(savedView!==null){const old=savedView;savedView=null;setView(old);}}
$('holdOriginal').addEventListener('pointerdown',()=>holdBefore(true));window.addEventListener('pointerup',()=>holdBefore(false));$('holdOriginal').addEventListener('pointerleave',()=>holdBefore(false));
function clearPreviewTimer(id){if(state.timers.has(id)){clearTimeout(state.timers.get(id));state.timers.delete(id);}}
function queuePreview(id,delay=420){state.seq.set(id,(state.seq.get(id)||0)+1);state.dirty.add(id);clearPreviewTimer(id);const p=photo(id);if(p)p.status='settings_changed';if(state.pending.has(id))return;const feedback=$('toasts').firstElementChild;state.timers.set(id,setTimeout(()=>{state.timers.delete(id);runPreview(id).catch(error=>{if(state.current===id&&$('toasts').firstElementChild===feedback)toast('预览更新失败：'+errorMessage(error),true);});},delay));}
function runPreview(id){
 if(state.pending.has(id))return state.pending.get(id);if(!photo(id))return Promise.resolve();clearPreviewTimer(id);
 // One request in flight per photo. New edits replace the next draft, never the current request.
 const promise=(async()=>{let result;while(true){const p=photo(id);if(!p)return;const seq=state.seq.get(id)||0;const draft=clone({params:p.params,strokes:p.strokes||[]});if(id===state.current)$('renderBadge').hidden=false;
  try{result=await post(`/api/photos/${id}/preview`,draft);}catch(error){if((state.seq.get(id)||0)!==seq)continue;throw error;}
  if((state.seq.get(id)||0)===seq){state.dirty.delete(id);if(result.meta?.processing_device){state.lastProcessingDevice=result.meta.processing_device;document.dispatchEvent(new Event('portraitlab-render-device'));}mergePhoto(result.photo);if(state.current===id){displayPhoto();renderControls();}renderLibrary();return result;}
  // An older result must not overwrite unsent sliders, brush marks or undo state.
  clearPreviewTimer(id);
 }})().finally(()=>{if(state.pending.get(id)===promise)state.pending.delete(id);if(state.current===id)$('renderBadge').hidden=true;updateConnectionStatus();});state.pending.set(id,promise);updateConnectionStatus();return promise;
}
async function flushPhotos(ids){await Promise.all([...new Set(ids)].map(async id=>{while(state.dirty.has(id)||state.timers.has(id)||state.pending.has(id)){clearPreviewTimer(id);await runPreview(id);}}));}
async function flushCurrent(){if(state.current)await flushPhotos([state.current]);}
function lockEditing(locked){state.locked=locked;renderControls();updateConnectionStatus();}
function currentImportTarget(){if($('exportDialog').open)return 'export';return state.module==='lighting'?'ai':'main';}
function requestImport(folder=false,target=currentImportTarget()){if(state.locked||state.importing)return;state.importTarget=target;$(folder?'folderInput':'fileInput').click();}
async function importFiles(files,target='main'){
 if(state.locked||state.importing){toast('正在保存或导入，请稍后再添加照片。',true);return [];}if(target==='ai'&&typeof aiActive==='function'&&aiActive()&&ai.job.action==='generate'){toast('光影重塑任务正在运行；可停止后添加输入，或切换到修片继续编辑。',true);return [];}
 const accepted=[...files].filter(file=>/\.(jpe?g|png|tiff?|webp)$/i.test(file.name));
 if(!accepted.length){toast('没有可导入的照片。支持 JPEG、PNG、TIFF 和 WEBP。',true);return [];}
 if(accepted.length>50){toast('一次最多导入 50 张照片，请分批添加。',true);return [];}
 const form=new FormData();accepted.forEach(file=>form.append('files',file));let imported=[];
 state.importing=true;lockEditing(true);$('connectionStatus').textContent=`正在导入 ${accepted.length} 张…`;
 try{
  const result=await api('/api/import',{method:'POST',body:form});imported=result.photos||[];for(const item of imported)mergePhoto(item);
  if(result.errors?.length)toast(result.errors.map(error=>typeof error==='string'?error:`${error.name||'照片'}：${error.error||error.detail}`).join('；'),true);
 }catch(error){toast(errorMessage(error),true);}finally{state.importing=false;lockEditing(false);updateConnectionStatus();$('fileInput').value='';$('folderInput').value='';}
 if(imported.length){
  const ids=imported.map(item=>item.id);
  if(target==='main'){state.selected=new Set([ids[0]]);await selectPhoto(ids[0]);}
  else if(target==='export'){state.exportIds=[...new Set([...(state.exportIds||[]),...ids])];$('exportDialogCount').textContent=state.exportIds.length;}
  else if(!state.current){await selectPhoto(ids[0]);}
  renderLibrary();renderControls();document.dispatchEvent(new CustomEvent('portraitlab-imported',{detail:{target,ids}}));toast(`已导入 ${ids.length} 张${target==='ai'?'到光影重塑输入列表':''}。`);
 }
 return imported;
}
for(const id of ['importBtn','importSmall','importEmpty','importIcon','canvasImport'])$(id).onclick=()=>requestImport(false,currentImportTarget());
$('importFolder').onclick=()=>requestImport(true,currentImportTarget());
$('exportImportFiles').onclick=()=>requestImport(false,'export');$('exportImportFolder').onclick=()=>requestImport(true,'export');
$('fileInput').onchange=event=>{const target=state.importTarget||currentImportTarget();state.importTarget=null;importFiles(event.target.files,target);};
$('folderInput').onchange=event=>{const target=state.importTarget||currentImportTarget();state.importTarget=null;importFiles(event.target.files,target);};
async function collectDroppedFiles(entries,fallback){
 if(!entries.length)return fallback;const files=[];
 async function visit(entry){
  if(files.length>50)return;
  if(entry.isFile){const file=await new Promise((resolve,reject)=>entry.file(resolve,reject));if(/\.(jpe?g|png|tiff?|webp)$/i.test(file.name))files.push(file);return;}
  if(entry.isDirectory){const reader=entry.createReader();while(files.length<=50){const children=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));if(!children.length)break;for(const child of children)await visit(child);}}
 }
 for(const entry of entries)await visit(entry);return files;
}
let dragDepth=0;
function showImportDrop(show){const dialog=document.querySelector('dialog[open]');$('dropOverlay').hidden=!show||!!dialog;for(const item of document.querySelectorAll('dialog'))item.classList.toggle('is-dragging',show&&item===dialog);}
function hasFiles(event){return Array.from(event.dataTransfer?.types||[]).includes('Files');}
 document.addEventListener('dragenter',event=>{if(hasFiles(event)){event.preventDefault();dragDepth++;showImportDrop(true);}});
 document.addEventListener('dragover',event=>{if(hasFiles(event)){event.preventDefault();event.dataTransfer.dropEffect=state.locked?'none':'copy';}});
 document.addEventListener('dragleave',event=>{if(!hasFiles(event))return;dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)showImportDrop(false);});
 document.addEventListener('drop',async event=>{
  if(!hasFiles(event))return;event.preventDefault();dragDepth=0;showImportDrop(false);const target=currentImportTarget();
  const entries=Array.from(event.dataTransfer.items||[]).filter(item=>item.kind==='file').map(item=>item.webkitGetAsEntry?.()).filter(Boolean);const fallback=Array.from(event.dataTransfer.files||[]);
  try{await importFiles(await collectDroppedFiles(entries,fallback),target);}catch(error){toast('无法读取拖入的文件夹：'+errorMessage(error),true);}
 });
$('selectAll').onchange=e=>{state.selected=e.target.checked?new Set(state.photos.map(p=>p.id)):new Set();renderLibrary();};
document.querySelectorAll('[data-region]').forEach(b=>b.onclick=()=>{if(!photo()||state.locked)return;saveHistory();photo().params.region=b.dataset.region;renderControls();queuePreview(state.current,0);});
function buildPresets(){window.PortraitPresets?.initialize();}
$('resetBtn').onclick=()=>{if(!photo()||state.locked)return;saveHistory();photo().params=clone(state.boot.defaults);renderControls();queuePreview(state.current,0);toast(`已为《${photo().name}》恢复默认参数，仅当前照片。`);};$('undoBtn').onclick=undo;
function updateBrush(){$('stage').classList.toggle('brush-on',!!state.brush);$('protectBrush').classList.toggle('active',state.brush==='protect');$('restoreBrush').classList.toggle('active',state.brush==='restore');$('brushCursor').hidden=true;setView(state.view);}
function toggleBrush(mode){if(!photo()||state.locked)return;state.brush=state.brush===mode?null:mode;if(state.brush)setView('mask');updateBrush();}
$('protectBrush').onclick=()=>toggleBrush('protect');$('restoreBrush').onclick=()=>toggleBrush('restore');$('brushSize').oninput=e=>{state.brushRadius=Number(e.target.value)/100;$('brushSizeValue').textContent=e.target.value+'%';};
let drawing=null;const canvas=$('brushCanvas');function pointerPoint(e){const rect=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))];}
canvas.addEventListener('pointermove',e=>{if(!state.brush||state.locked)return;const pt=pointerPoint(e);const diameter=state.brushRadius*Math.min(canvas.clientWidth,canvas.clientHeight)*2;const cursor=$('brushCursor');cursor.hidden=false;Object.assign(cursor.style,{width:diameter+'px',height:diameter+'px',left:pt[0]*canvas.clientWidth+'px',top:pt[1]*canvas.clientHeight+'px'});if(drawing){drawing.points.push(pt);const ctx=canvas.getContext('2d');ctx.lineWidth=diameter;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=drawing.mode==='protect'?'#f18da58c':'#abd7b6a6';const last=drawing.points.at(-2);ctx.beginPath();ctx.moveTo(last[0]*canvas.width,last[1]*canvas.height);ctx.lineTo(pt[0]*canvas.width,pt[1]*canvas.height);ctx.stroke();}});
canvas.addEventListener('pointerleave',()=>{$('brushCursor').hidden=true;});canvas.addEventListener('pointerdown',e=>{if(!state.brush||!photo()||state.locked)return;e.preventDefault();saveHistory();canvas.setPointerCapture(e.pointerId);drawing={photoId:state.current,mode:state.brush,radius:state.brushRadius,points:[pointerPoint(e)]};});
function finishStroke(){if(!drawing)return;const p=photo(drawing.photoId);const {mode,radius,points}=drawing;const stroke={mode,radius,points};drawing=null;if(!p)return;if(stroke.points.length>1000)stroke.points=stroke.points.filter((_,i)=>i%Math.ceil(stroke.points.length/1000)===0);p.strokes=[...(p.strokes||[]),stroke];queuePreview(p.id,0);canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
canvas.addEventListener('pointerup',finishStroke);canvas.addEventListener('pointercancel',finishStroke);
$('clearMask').onclick=()=>{const p=photo();if(!p||state.locked)return;saveHistory();p.strokes=[];queuePreview(p.id,0);toast(`已清除《${p.name}》的手工保护区域，仅当前照片。`);};
$('exportBtn').onclick=async()=>{if(state.locked)return;const ids=choices();lockEditing(true);try{await flushPhotos(ids);state.exportIds=ids;$('exportDialogCount').textContent=ids.length;$('exportPath').placeholder=state.boot.storage.export_dir;$('exportDialog').showModal();}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);}};
$('startExport').onclick=async()=>{if(state.locked||state.importing)return;const button=$('startExport');button.disabled=true;const ids=state.exportIds||choices();lockEditing(true);try{await flushPhotos(ids);const result=await post('/api/jobs',{ids,format:$('exportFormat').value,max_side:Number($('exportSize').value),output_dir:$('exportPath').value.trim()||null});state.jobs=result.job;$('exportDialog').close();showJob(result.job);pollJob();}catch(error){toast(errorMessage(error),true);}finally{button.disabled=false;lockEditing(false);}};
$('taskDock').append($('jobPanel'));
const activeStatuses=['queued','running','cancelling'];
function showJob(job){state.jobs=job;$('jobPanel').hidden=false;const active=activeStatuses.includes(job.status);$('jobIcon').className=active?'spinner':'';$('jobIcon').textContent=active?'':job.status==='completed'?'✓':'!';$('jobTitle').textContent=({queued:'已加入导出队列',running:'正在按原始尺寸处理',cancelling:'完成当前照片后停止',completed:'照片已导出',completed_with_errors:'导出完成，部分照片需检查',cancelled:'已停止后续导出',failed:'导出失败',interrupted:'上次导出已中断'})[job.status]||job.status;$('jobProgress').style.width=`${job.total?Math.min(100,job.done/job.total*100):0}%`;
 const details=$('jobDetails');details.replaceChildren();const count=document.createElement('div');count.textContent=`${job.done} / ${job.total} 张${active?' · 后台处理中，可继续查看照片':''}`;details.append(count);for(const item of job.items||[]){if(item.status==='running'||item.error){const line=document.createElement('div');line.textContent=(item.name||item.photo_id||'照片')+(item.error?'：'+item.error:' · 处理中');details.append(line);}}if(!active){const path=document.createElement('div');path.textContent=job.output_dir||job.error||'';details.append(path);}$('cancelJob').hidden=!active;$('cancelJob').disabled=job.status==='cancelling';$('openExport').hidden=active||!job.output_dir;updateConnectionStatus();
}
async function pollJob(){clearTimeout(state.jobTimer);if(!state.jobs||!activeStatuses.includes(state.jobs.status))return;try{const result=await api('/api/jobs/'+state.jobs.id);showJob(result.job);}catch(error){toast(errorMessage(error),true);}if(state.jobs&&activeStatuses.includes(state.jobs.status))state.jobTimer=setTimeout(pollJob,1000);}
$('cancelJob').onclick=async()=>{try{const result=await post(`/api/jobs/${state.jobs.id}/cancel`,{});showJob(result.job);}catch(error){toast(errorMessage(error),true);}};$('openExport').onclick=async()=>{try{await post(`/api/jobs/${state.jobs.id}/open`,{});}catch(error){toast(errorMessage(error),true);}};$('closeJob').onclick=()=>{$('jobPanel').hidden=true;};
$('aboutBtn').onclick=()=>$('aboutDialog').showModal();
$('modelsBtn').onclick=()=>{$('modelsDialog').showModal();document.dispatchEvent(new Event('portraitlab-settings-open'));};
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
function modelInfo(){
 const data=state.boot?.models||{};const container=$('modelsDetails');container.replaceChildren();
 for(const [id,model] of Object.entries(data.models||{})){const card=document.createElement('div');card.className='source-card';const title=document.createElement('b');title.textContent=id==='face_landmarker'?'人脸与五官关键点':'皮肤、头发与衣物分区';const status=document.createElement('span');status.textContent=model.verified?'已下载并校验':model.exists?'文件已下载，首次处理时校验':'未下载';const hash=document.createElement('span');hash.textContent='SHA-256: '+model.sha256;hash.style.fontSize='9px';card.append(title,status,hash);container.append(card);}
 document.dispatchEvent(new Event('portraitlab-model-state'));
}
$('openLogsBtn').onclick=()=>{if(window.chrome?.webview){window.chrome.webview.postMessage({type:'openLogs'});}else{toast('当前通过浏览器连接本地处理服务。运行日志可从启动服务的终端或软件目录查看。');}};
window.portraitlabPrepareToQuit=async()=>{if(drawing)finishStroke();await flushPhotos([...new Set([...state.dirty,...state.pending.keys()])]);};
$('quitAppBtn').onclick=async()=>{if(window.chrome?.webview){window.chrome.webview.postMessage({type:'quit'});return;}if(!confirm('退出程序？正在运行的处理任务将停止。'))return;const button=$('quitAppBtn');button.disabled=true;try{await window.portraitlabPrepareToQuit();await post('/api/shutdown',{});$('modelsDialog').close();$('connectionStatus').textContent='服务已关闭';toast('程序已停止，可关闭此页面。');}catch(error){toast(errorMessage(error),true);button.disabled=false;}};
// Ctrl+O imports into the currently open editing feature; typing never triggers edit shortcuts.
document.addEventListener('keydown',event=>{
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='o'){event.preventDefault();requestImport(false,currentImportTarget());return;}
 if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)||document.querySelector('dialog[open]'))return;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();undo();}
 if(event.code==='Space'&&document.activeElement===document.body){event.preventDefault();holdBefore(true);}
 if(event.key.toLowerCase()==='b'&&!event.ctrlKey&&!event.metaKey)toggleBrush('protect');
 if(event.key==='Escape'&&state.brush){state.brush=null;updateBrush();}
});
document.addEventListener('keyup',event=>{if(event.code==='Space')holdBefore(false);});window.addEventListener('blur',()=>holdBefore(false));
$('holdOriginal').addEventListener('keydown',event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();holdBefore(true);}});$('holdOriginal').addEventListener('keyup',()=>holdBefore(false));$('holdOriginal').addEventListener('blur',()=>holdBefore(false));
const compareSlider=$('compareHandle');compareSlider.tabIndex=0;compareSlider.setAttribute('role','slider');compareSlider.setAttribute('aria-label','原图对比位置');compareSlider.setAttribute('aria-valuemin','1');compareSlider.setAttribute('aria-valuemax','99');compareSlider.setAttribute('aria-valuenow',String(state.split));compareSlider.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();state.split=event.key==='Home'?1:event.key==='End'?99:Math.max(1,Math.min(99,state.split+(event.key==='ArrowLeft'?-5:5)));compareSlider.style.left=state.split+'%';compareSlider.setAttribute('aria-valuenow',String(state.split));setView('compare');});
document.querySelector('.brand').addEventListener('click',event=>event.preventDefault());
window.addEventListener('beforeunload',event=>{if(state.dirty.size||state.pending.size||state.importing){event.preventDefault();event.returnValue='';}});
async function boot(){try{state.boot=await api('/api/bootstrap');state.token=state.boot.token;buildPresets();modelInfo();const result=await api('/api/photos');state.photos=result.photos;renderLibrary();renderControls();$('connectionStatus').textContent='已就绪';if(state.photos.length){state.selected.add(state.photos[0].id);await selectPhoto(state.photos[0].id);}const jobs=await api('/api/jobs');const active=jobs.jobs.find(j=>activeStatuses.includes(j.status));if(active){showJob(active);pollJob();}document.dispatchEvent(new Event('portraitlab-ready'));}catch(error){$('connectionStatus').textContent='无法连接处理服务';toast(errorMessage(error),true);}}
boot();
