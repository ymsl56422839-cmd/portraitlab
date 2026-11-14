/* PortraitLab local interface. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const $ = id => document.getElementById(id);
const state = { token:'', boot:null, photos:[], selected:new Set(), current:null, view:'result', secondaryView:'original', layout:'single', module:'retouch', split:50, secondarySplit:50, zoom:1, fitZoom:true, centerX:.5, centerY:.5, brush:null, brushRadius:.04, busy:0, locked:false, exportIds:null, jobs:null, jobTimer:null, histories:new Map(), seq:new Map(), dirty:new Set(), pending:new Map(), timers:new Map(), importing:false, importTarget:null, batchUndo:null, hardware:null };
const definitions = [
 {key:'acne',name:'去痘强度',min:0,max:1,step:.01,section:'skinControls'},
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
function editingParams(p=photo()){return window.PortraitSelection?.parameters(p)||p?.params;}
function setEditingParams(p,params){if(window.PortraitSelection?.isEditing(p))p.local_adjustment.params=clone(params);else p.params=clone(params);}
function editScopeLabel(p=photo()){return window.PortraitSelection?.isEditing(p)?'仅矩形选区内':'当前照片全局修片';}
function toast(message,error=false){const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;if(error)el.setAttribute('role','alert');$('toasts').replaceChildren(el);return el;}
function updateConnectionStatus(){
 if(!state.token||state.importing)return;
 const tasks=[];if(state.lightingTask)tasks.push(state.lightingTask);if(state.jobs&&['queued','running','cancelling'].includes(state.jobs.status))tasks.push('照片导出中');if(state.pending.size)tasks.push('预览更新中');
 $('connectionStatus').textContent=tasks.join(' · ')||(state.locked?'正在提交…':'已就绪');
}
async function settleLighting(action){return await (window.PortraitLighting?.beforeLeave?.(action)??true);}
async function setModule(module){
 if(state.locked)return false;if(module!==state.module&&state.module==='lighting'&&!await settleLighting('切换模块'))return false;if(drawing)finishStroke();state.module=module;state.brush=null;updateBrush();$('retouchPanel').hidden=module!=='retouch';$('lightingPanel').hidden=module!=='lighting';
 for(const button of document.querySelectorAll('[data-module]')){const selected=button.dataset.module===module;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));}
 setView('result');renderControls();toast(module==='lighting'?'光影重塑：选择布光方案，在画布上调整光源，再保留调整。':'修片：预设和参数只应用到当前照片，批量套用需单独操作。');document.dispatchEvent(new Event('portraitlab-module-change'));return true;
}
for(const button of document.querySelectorAll('[data-module]'))button.onclick=async()=>{if(await setModule(button.dataset.module)&&button.dataset.module==='lighting')await window.PortraitLighting?.open?.();};
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
function updateRemovalNotice(){const ids=state.removalUndo||[];$('libraryRemoval').hidden=!ids.length;$('libraryRemovalText').textContent=`已移除 ${ids.length} 张`;$('undoPhotoRemoval').disabled=state.locked||state.importing||!ids.length;}
function clearCurrentPhoto(){
 state.current=null;state.brush=null;state.fitZoom=true;state.centerX=state.centerY=.5;previewHolding=false;
 $('emptyState').hidden=false;for(const id of ['stageHolder','secondaryStageHolder','renderBadge'])$(id).hidden=true;
 for(const id of ['resultImage','originalImage','secondaryResultImage','secondaryOriginalImage','maskImage'])$(id).removeAttribute('src');
 $('currentName').textContent='尚未选择照片';$('currentMeta').textContent='导入后开始编辑';$('notice').hidden=true;updateBrush();renderControls();
}
async function removePhotos(ids){
 if(state.locked||state.importing||!ids.length)return;if(ids.includes(state.current)&&!await settleLighting('移除照片'))return;if(drawing)finishStroke();
 const current=state.current,next=state.photos.find(p=>!ids.includes(p.id))?.id;let removed=[];lockEditing(true);
 try{
  await flushPhotos(ids);const result=await post('/api/photos/remove',{ids});removed=result.removed_ids;
  const removedSet=new Set(removed);state.photos=state.photos.filter(p=>!removedSet.has(p.id));state.selected=new Set([...state.selected].filter(id=>!removedSet.has(id)));state.removalUndo=result.last_removed_ids||removed;
  if(removedSet.has(current))clearCurrentPhoto();renderLibrary();document.dispatchEvent(new CustomEvent('portraitlab-photos-removed',{detail:{ids:removed}}));
  toast(`已从照片库移除 ${removed.length} 张照片，可在左侧撤销。原文件和已导出照片均保留。`);
 }catch(error){toast('移除失败：'+errorMessage(error),true);}finally{lockEditing(false);updateRemovalNotice();}
 if(removed.includes(current)&&next)await selectPhoto(next);
}
async function undoPhotoRemoval(){
 if(state.locked||state.importing||!state.removalUndo?.length)return;let restored=[];lockEditing(true);
 try{const result=await post('/api/photos/restore',{ids:state.removalUndo});restored=result.photos;for(const p of restored){mergePhoto(p);state.selected.add(p.id);}state.removalUndo=result.last_removed_ids||[];renderLibrary();toast(`已恢复 ${restored.length} 张照片及其编辑记录。`);}
 catch(error){toast('恢复失败：'+errorMessage(error),true);}finally{lockEditing(false);updateRemovalNotice();}
 if(!state.current&&restored.length)await selectPhoto(restored[0].id);
}
$('removeSelectedPhotos').onclick=()=>removePhotos([...state.selected]);$('undoPhotoRemoval').onclick=undoPhotoRemoval;
function statusLabel(p){if(p.status==='error')return '处理需要检查';if(p.generation)return `AI 生成 · ${p.width} × ${p.height}`;if(p.face_count===0)return '未检测到人脸';if(p.status==='settings_changed')return '参数已更新';if(p.face_count!=null)return `${p.face_count} 张人脸 · 已调整`;return `${p.width} × ${p.height}`;}
function renderLibrary(){
 const list=$('photoList');list.replaceChildren();
 if(!state.photos.length){const e=document.createElement('div');e.className='library-empty';e.innerHTML='照片与编辑参数<br>会保留在当前工作区';list.append(e);}
 for(const p of state.photos){
  const card=document.createElement('div');card.className='photo-card'+(p.id===state.current?' active':'');card.dataset.id=p.id;card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','编辑 '+p.name);
  const thumb=document.createElement('img');thumb.src=p.preview_url||p.original_url;thumb.alt='';thumb.loading='lazy';
  const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=state.selected.has(p.id);checkbox.setAttribute('aria-label','选择 '+p.name);checkbox.addEventListener('click',e=>e.stopPropagation());checkbox.addEventListener('change',()=>{checkbox.checked?state.selected.add(p.id):state.selected.delete(p.id);updateCounts();});
  const copy=document.createElement('div');copy.className='photo-card-copy';const title=document.createElement('strong');title.textContent=p.name;title.title=p.name;const subtitle=document.createElement('small');subtitle.textContent=statusLabel(p);copy.append(title,subtitle);card.append(thumb,checkbox,copy);
  const remove=document.createElement('button');remove.type='button';remove.className='photo-remove';remove.setAttribute('aria-label','从照片库移除 '+p.name);remove.title='从照片库移除（可撤销）';remove.innerHTML='<svg aria-hidden="true"><use href="#i-trash"/></svg>';remove.disabled=state.locked||state.importing;remove.onclick=e=>{e.stopPropagation();removePhotos([p.id]);};card.append(remove);
  card.addEventListener('click',()=>selectPhoto(p.id));card.addEventListener('keydown',e=>{if(e.target===card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();selectPhoto(p.id);}});list.append(card);
 }
 updateCounts();
}
function updateCounts(){
 $('photoCount').textContent=state.photos.length;$('selectedCount').textContent=`${state.selected.size} 张已选`;$('exportCount').textContent=choices().length;$('exportBtn').disabled=state.locked||!choices().length;$('syncCount').textContent=state.selected.size;$('syncBtn').disabled=state.locked||!state.current||state.selected.size<1;document.dispatchEvent(new Event('portraitlab-selection-change'));
 $('selectAll').checked=!!state.photos.length&&state.selected.size===state.photos.length;$('selectAll').indeterminate=state.selected.size>0&&state.selected.size<state.photos.length;
 $('removeSelectedPhotos').disabled=state.locked||state.importing||!state.selected.size;document.querySelectorAll('.photo-remove').forEach(b=>b.disabled=state.locked||state.importing);updateRemovalNotice();
}
function formatted(def,value){if(def.unit)return `${value>0?'+':''}${value.toFixed(2)} ${def.unit}`;if(def.signed)return `${value>0?'+':''}${Math.round(value*100)}`;return String(Math.round(value*100));}
function updateSlider(def,value){const input=$('control-'+def.key);input.value=value;input.style.setProperty('--range-pct',`${(value-def.min)/(def.max-def.min)*100}%`);$('value-'+def.key).textContent=formatted(def,value);}
for(const def of definitions){
 const row=document.createElement('div');row.className='slider-row';const label=document.createElement('label');label.className='slider-label';label.htmlFor='control-'+def.key;const name=document.createElement('span');name.textContent=def.name;const out=document.createElement('output');out.id='value-'+def.key;label.append(name,out);
 const input=document.createElement('input');Object.assign(input,{type:'range',id:'control-'+def.key,min:def.min,max:def.max,step:def.step,disabled:true});input.setAttribute('aria-label',def.name);row.append(label,input);$(def.section).append(row);
 input.addEventListener('pointerdown',saveHistory);input.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','PageUp','PageDown','Home','End'].includes(e.key))saveHistory();});
 input.addEventListener('input',()=>{const p=photo();if(!p||state.locked)return;const value=Number(input.value);editingParams(p)[def.key]=value;updateSlider(def,value);clearPresetActive();queuePreview(p.id);});
 input.addEventListener('change',()=>{if(photo()&&!state.locked)toast(`《${photo().name}》已微调${def.name}，${editScopeLabel()}。`);});
 input.addEventListener('dblclick',()=>{if(!photo()||state.locked)return;saveHistory();const baseline=window.PortraitSelection?.isEditing()?window.PortraitSelection.neutral():state.boot.defaults;editingParams()[def.key]=baseline[def.key];updateSlider(def,editingParams()[def.key]);queuePreview(state.current,0);});
}
function clearPresetActive(){window.PortraitPresets?.refresh();}
function renderControls(){const p=photo();const params=editingParams(p)||state.boot?.defaults||{};for(const def of definitions){updateSlider(def,Number(params[def.key]??(def.key==='saturation'?1:0)));$('control-'+def.key).disabled=!p||state.locked;}
 for(const id of ['resetBtn','holdOriginal','protectBrush','restoreBrush','clearMask'])$(id).disabled=!p||state.locked;
 document.querySelectorAll('[data-region]').forEach(b=>{b.disabled=!p||state.locked;b.classList.toggle('active',b.dataset.region===params.region);});
 document.querySelectorAll('#presets .preset-apply').forEach(b=>b.disabled=!p||state.locked);$('undoBtn').disabled=state.locked||!(p&&state.histories.get(p.id)?.length);$('brushCanvas').style.pointerEvents=state.locked?'none':'';clearPresetActive();updateCounts();for(const id of ['importBtn','importSmall','importEmpty','importIcon','canvasImport','importFolder','exportImportFiles','exportImportFolder','aiImportFiles','aiImportFolder']){const button=$(id);if(button)button.disabled=state.locked||state.importing;}$('startExport').disabled=state.locked||state.importing;document.querySelectorAll('[data-module]').forEach(button=>button.disabled=state.locked||!state.token);document.dispatchEvent(new Event('portraitlab-edit-state'));
}
function recordHistory(id,snapshot){const history=state.histories.get(id)||[],current=clone({local_adjustment:photo(id)?.local_adjustment??null,...snapshot});if(JSON.stringify(history.at(-1))!==JSON.stringify(current))history.push(current);if(history.length>30)history.shift();state.histories.set(id,history);if(id===state.current)$('undoBtn').disabled=state.locked||!history.length;}
function saveHistory(){const p=photo();if(!p||state.locked)return;recordHistory(p.id,{params:p.params,strokes:p.strokes||[],local_adjustment:p.local_adjustment??null,...(p.lighting?{lighting:p.lighting,lighting_geometry:p.lighting_geometry??null}:{})});}
async function undo(){if(state.locked||!await settleLighting('撤销已保存调整'))return;window.PortraitSelection?.cancelDrag();const p=photo();const prior=p&&state.histories.get(p.id)?.pop();if(!prior)return;p.params=prior.params;p.strokes=prior.strokes;p.local_adjustment=prior.local_adjustment??null;if(prior.lighting)p.lighting=prior.lighting;if(Object.hasOwn(prior,'lighting_geometry'))p.lighting_geometry=prior.lighting_geometry;renderControls();queuePreview(p.id,0);}
async function selectPhoto(id){if(state.locked||!photo(id)||id===state.current)return false;if(!await settleLighting('切换照片'))return false;if(drawing)finishStroke();state.current=id;state.fitZoom=true;state.centerX=state.centerY=.5;state.brush=null;$('emptyState').hidden=true;$('stageHolder').hidden=false;$('secondaryStageHolder').hidden=false;renderLibrary();renderControls();updateBrush();displayPhoto();const p=photo();$('renderBadge').hidden=!state.pending.has(id);if(p.status!=='ready'&&!state.locked)queuePreview(id,0);document.dispatchEvent(new CustomEvent('portraitlab-photo-selected',{detail:{id}}));return true;}
function setImage(img,url){if(url&&img.getAttribute('src')!==url)img.src=url;}
function displayPhoto(){const p=photo();if(!p)return;
 for(const id of ['resultImage','secondaryResultImage'])setImage($(id),p.preview_url||p.original_url);
 if(p.mask_url)setImage($('maskImage'),p.mask_url);
 $('currentName').textContent=p.name;$('currentMeta').textContent=`${p.width} × ${p.height} · ${p.face_count==null?'等待分析':p.face_count+' 张人脸'} · ${p.generation?'生成图像':'原尺寸导出'}`;
 const warnings=p.warnings||[];$('notice').hidden=!warnings.length;$('notice').textContent=warnings.join(' · ');
 renderViews();fitStage();document.dispatchEvent(new CustomEvent('portraitlab-photo-updated',{detail:{id:p.id,version:p.version}}));
}

// View content is independent; geometry uses one scale and one image-space center.
const previewPanes={
 primary:{viewport:'viewport',stage:'stage',original:'originalImage',result:'resultImage',handle:'compareHandle',view:'view',split:'split',attribute:'data-view'},
 secondary:{viewport:'secondaryViewport',stage:'secondaryStage',original:'secondaryOriginalImage',result:'secondaryResultImage',handle:'secondaryCompareHandle',view:'secondaryView',split:'secondarySplit',attribute:'data-secondary-view'}
};
let previewHolding=false;
const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
function visiblePanes(){return state.layout==='dual'?Object.values(previewPanes):[previewPanes.primary];}
function comparisonBounds(pane){
 const stage=$(pane.stage),viewport=$(pane.viewport),w=parseFloat(stage.style.width)||1,h=parseFloat(stage.style.height)||1,left=parseFloat(stage.style.left)||0,top=parseFloat(stage.style.top)||0;
 return {w,h,left,top,start:clamp(-left,0,w),end:clamp(viewport.clientWidth-left,0,w),viewport};
}
function updateSplitGeometry(pane){
 const bounds=comparisonBounds(pane),{w,h,start,end,top,viewport}=bounds;
 const x=start+(end-start)*state[pane.split]/100,percent=x/w*100,stage=$(pane.stage);
 $(pane.handle).style.left=percent+'%';$(pane.handle).firstElementChild.style.top=clamp(viewport.clientHeight/2-top,18,Math.max(18,h-18))+'px';
 if(!previewHolding&&state[pane.view]==='compare')$(pane.original).style.clipPath=`inset(0 ${100-percent}% 0 0)`;
 const before=stage.querySelector('.left-label'),after=stage.querySelector('.right-label');
 before.style.left=(start+9)+'px';after.style.right=(w-end+9)+'px';before.style.top=after.style.top=(Math.max(0,-top)+9)+'px';
}
function renderViews(){
 const p=photo();$('moduleCaption').textContent=state.module==='lighting'?'光影重塑':'修片';
 for(const [name,pane] of Object.entries(previewPanes)){
  const view=previewHolding?'original':state[pane.view],split=state[pane.split];
  const original=$(pane.original),result=$(pane.result),stage=$(pane.stage),handle=$(pane.handle);
  if(p)setImage(original,view==='mask'?(p.preview_url||p.original_url):p.original_url);
  original.hidden=view==='result';result.hidden=view==='original';original.style.clipPath=view==='compare'?`inset(0 ${100-split}% 0 0)`:'none';
  handle.hidden=!p||view!=='compare';handle.style.left=split+'%';handle.setAttribute('aria-valuenow',String(Math.round(split)));handle.setAttribute('aria-valuetext',`原图 ${Math.round(split)}%，处理结果 ${Math.round(100-split)}%`);
  for(const b of document.querySelectorAll(`[${pane.attribute}]`)){const active=b.getAttribute(pane.attribute)===view;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));b.disabled=!p;}
  const before=stage.querySelector('.left-label'),after=stage.querySelector('.right-label');
  before.textContent=p?.comparison_kind==='generation_input'?'生成前 · 对齐裁切':p?.generation?'生成底图':'原图';after.textContent='处理结果';before.hidden=after.hidden=!p||view!=='compare';
  if(name==='primary'){const maskUrl=window.PortraitSelection?.isEditing(p)?p?.local_mask_url:p?.mask_url;setImage($('maskImage'),maskUrl);$('maskImage').hidden=view!=='mask'||!maskUrl;stage.classList.toggle('brush-on',!!state.brush&&view==='mask'&&!previewHolding);}
  updateSplitGeometry(pane);
 }
 $('holdOriginal').setAttribute('aria-pressed',String(previewHolding));
 for(const id of ['zoomIn','zoomOut','zoomSlider','zoomFit'])$(id).disabled=!p;
 if(!p){$('zoomPercent').textContent='—';$('zoomSlider').setAttribute('aria-valuetext','尚未选择照片');}
 document.dispatchEvent(new Event('portraitlab-preview-view'));
}
function fitStage(){
 const p=photo();if(!p)return;const panes=visiblePanes();
 if(state.fitZoom)state.zoom=Math.max(.001,Math.min(4,...panes.map(pane=>{const v=$(pane.viewport);return Math.min(Math.max(1,v.clientWidth-32)/p.width,Math.max(1,v.clientHeight-32)/p.height);})));
 const w=p.width*state.zoom,h=p.height*state.zoom;
 // Clamp the center, rather than independent scroll positions, to keep views aligned.
 const areaW=Math.max(...panes.map(pane=>$(pane.viewport).clientWidth)),areaH=Math.max(...panes.map(pane=>$(pane.viewport).clientHeight));
 state.centerX=w<=areaW?.5:clamp(state.centerX,areaW/(2*w),1-areaW/(2*w));
 state.centerY=h<=areaH?.5:clamp(state.centerY,areaH/(2*h),1-areaH/(2*h));
 for(const pane of panes){const v=$(pane.viewport),stage=$(pane.stage),top=v.clientHeight/2-state.centerY*h;Object.assign(stage.style,{width:w+'px',height:h+'px',left:(v.clientWidth/2-state.centerX*w)+'px',top:top+'px'});updateSplitGeometry(pane);v.classList.toggle('can-pan',w>v.clientWidth||h>v.clientHeight);}
 const c=$('brushCanvas'),backingScale=Math.min(1,2048/Math.max(w,h)),cw=Math.max(1,Math.round(w*backingScale)),ch=Math.max(1,Math.round(h*backingScale));
 if(c.width!==cw||c.height!==ch){c.width=cw;c.height=ch;}
 const percentage=Number((state.zoom*100).toFixed(1));$('zoomPercent').textContent=percentage+'%';$('zoomSlider').value=clamp(Math.log(state.zoom/.01)/Math.log(400)*1000,0,1000);$('zoomSlider').setAttribute('aria-valuetext',percentage+'%');$('zoomFit').classList.toggle('active',state.fitZoom);$('zoomFit').setAttribute('aria-pressed',String(state.fitZoom));
 $('zoomHint').textContent=state.layout==='dual'?'同步缩放与平移 · 滚轮缩放 · 拖动画面':'滚轮缩放 · 拖动画面';
 document.dispatchEvent(new Event('portraitlab-preview-geometry'));
}
function setLayout(layout){
 if(!['single','dual'].includes(layout))return;if(drawing)finishStroke();state.layout=layout;
 $('previewGrid').dataset.layout=layout;$('secondaryPane').hidden=layout!=='dual';$('primaryViewLabel').textContent=layout==='dual'?'左视图':'单视图';
 for(const b of document.querySelectorAll('button[data-layout]')){const active=b.dataset.layout===layout;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));}
 fitStage();
}
function setView(view,paneName='primary'){
 const pane=previewPanes[paneName];if(!pane||!['original','compare','result',...(paneName==='primary'?['mask']:[])].includes(view))return;
 if(drawing)finishStroke();previewHolding=false;state[pane.view]=view;
 if(paneName==='primary'&&view!=='mask'){state.brush=null;$('protectBrush').classList.remove('active');$('restoreBrush').classList.remove('active');$('brushCursor').hidden=true;}
 renderViews();
}
function setZoom(zoom,paneName='primary',clientX,clientY){
 const p=photo();if(!p||!Number.isFinite(zoom))return;if(drawing)finishStroke();
 const v=$(previewPanes[paneName].viewport),rect=v.getBoundingClientRect();
 const dx=clientX==null?0:clientX-rect.left-v.clientWidth/2,dy=clientY==null?0:clientY-rect.top-v.clientHeight/2;
 const anchorX=state.centerX+dx/(p.width*state.zoom),anchorY=state.centerY+dy/(p.height*state.zoom);
 state.zoom=clamp(zoom,.01,4);state.fitZoom=false;state.centerX=anchorX-dx/(p.width*state.zoom);state.centerY=anchorY-dy/(p.height*state.zoom);fitStage();
}
function setSplit(value,paneName='primary'){const pane=previewPanes[paneName];state[pane.split]=clamp(value,0,100);renderViews();}
for(const b of document.querySelectorAll('button[data-layout]'))b.onclick=()=>setLayout(b.dataset.layout);
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
document.querySelectorAll('[data-secondary-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.secondaryView,'secondary')));
$('zoomIn').onclick=()=>setZoom(state.zoom*1.25);$('zoomOut').onclick=()=>setZoom(state.zoom/1.25);
$('zoomSlider').oninput=e=>setZoom(.01*Math.pow(400,Number(e.target.value)/1000));
$('zoomFit').onclick=()=>{if(drawing)finishStroke();state.fitZoom=true;state.centerX=state.centerY=.5;fitStage();};
for(const [name,pane] of Object.entries(previewPanes)){
 const viewport=$(pane.viewport),handle=$(pane.handle);let pan=null,dragPointer=null;
 new ResizeObserver(()=>{if(drawing)finishStroke();fitStage();}).observe(viewport);
 handle.addEventListener('pointerdown',e=>{if(e.button!==0||!photo())return;e.preventDefault();e.stopPropagation();dragPointer=e.pointerId;handle.setPointerCapture(e.pointerId);handle.focus({preventScroll:true});});
 handle.addEventListener('pointermove',e=>{if(dragPointer!==e.pointerId)return;const bounds=comparisonBounds(pane),rect=bounds.viewport.getBoundingClientRect();if(bounds.end>bounds.start)setSplit((e.clientX-rect.left-bounds.left-bounds.start)/(bounds.end-bounds.start)*100,name);});
 const endSplit=e=>{if(dragPointer===null||e.pointerId!==dragPointer)return;dragPointer=null;if(handle.hasPointerCapture(e.pointerId))handle.releasePointerCapture(e.pointerId);};
 for(const event of ['pointerup','pointercancel','lostpointercapture'])handle.addEventListener(event,endSplit);
 handle.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();setSplit(e.key==='Home'?0:e.key==='End'?100:state[pane.split]+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?10:1),name);});
 viewport.addEventListener('wheel',e=>{if(!photo())return;e.preventDefault();if(window.PortraitSelection?.isDragging())return;const pixels=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?viewport.clientHeight:1);setZoom(state.zoom*Math.exp(-clamp(pixels,-500,500)*.0015),name,e.clientX,e.clientY);},{passive:false});
 viewport.addEventListener('pointerdown',e=>{
  if(!photo()||![0,1].includes(e.button)||e.target.closest('.compare-handle,.light-widget,.light-widget-handle,.lighting-overlay-control'))return;
  if(name==='primary'&&state.brush&&e.button===0&&e.target===$('brushCanvas'))return;
  if(drawing)finishStroke();e.preventDefault();viewport.setPointerCapture(e.pointerId);viewport.classList.add('is-panning');pan={id:e.pointerId,x:e.clientX,y:e.clientY,cx:state.centerX,cy:state.centerY};
 });
 viewport.addEventListener('pointermove',e=>{if(!pan||pan.id!==e.pointerId)return;const p=photo();state.centerX=pan.cx-(e.clientX-pan.x)/(p.width*state.zoom);state.centerY=pan.cy-(e.clientY-pan.y)/(p.height*state.zoom);fitStage();});
 const endPan=e=>{if(!pan||pan.id!==e.pointerId)return;pan=null;viewport.classList.remove('is-panning');if(viewport.hasPointerCapture(e.pointerId))viewport.releasePointerCapture(e.pointerId);};
 for(const event of ['pointerup','pointercancel','lostpointercapture'])viewport.addEventListener(event,endPan);
 window.addEventListener('blur',()=>{if(pan)endPan({pointerId:pan.id});if(dragPointer!==null)endSplit({pointerId:dragPointer});});
 viewport.addEventListener('keydown',e=>{if(e.target!==viewport||!photo()||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;e.preventDefault();const p=photo();state.centerX+=({ArrowLeft:-40,ArrowRight:40}[e.key]||0)/(p.width*state.zoom);state.centerY+=({ArrowUp:-40,ArrowDown:40}[e.key]||0)/(p.height*state.zoom);fitStage();});
}
function holdBefore(on){if(!photo())return;if(drawing)finishStroke();previewHolding=on;renderViews();}
$('holdOriginal').addEventListener('pointerdown',e=>{e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);holdBefore(true);});window.addEventListener('pointerup',()=>holdBefore(false));window.addEventListener('pointercancel',()=>holdBefore(false));$('holdOriginal').addEventListener('lostpointercapture',()=>holdBefore(false));
function clearPreviewTimer(id){if(state.timers.has(id)){clearTimeout(state.timers.get(id));state.timers.delete(id);}}
function queuePreview(id,delay=420){state.seq.set(id,(state.seq.get(id)||0)+1);state.dirty.add(id);clearPreviewTimer(id);const p=photo(id);if(p)p.status='settings_changed';if(state.pending.has(id))return;const feedback=$('toasts').firstElementChild;state.timers.set(id,setTimeout(()=>{state.timers.delete(id);runPreview(id).catch(error=>{if(state.current===id&&$('toasts').firstElementChild===feedback)toast('预览更新失败：'+errorMessage(error),true);});},delay));}
function runPreview(id){
 if(state.pending.has(id))return state.pending.get(id);if(!photo(id))return Promise.resolve();clearPreviewTimer(id);
 // One request in flight per photo. New edits replace the next draft, never the current request.
 const promise=(async()=>{let result;while(true){const p=photo(id);if(!p)return;const seq=state.seq.get(id)||0;const draft=clone({params:p.params,strokes:p.strokes||[],local_adjustment:p.local_adjustment??null,...(p.lighting?{lighting:p.lighting,lighting_geometry:p.lighting_geometry??null}:{})});if(id===state.current)$('renderBadge').hidden=false;
  try{result=await post(`/api/photos/${id}/preview`,draft);}catch(error){if((state.seq.get(id)||0)!==seq)continue;throw error;}
  if((state.seq.get(id)||0)===seq){if(result.meta?.stale)throw new Error('照片状态已被其他操作更新，本次预览尚未完成；请重新应用调整。');state.dirty.delete(id);if(result.meta?.processing_device){state.lastProcessingDevice=result.meta.processing_device;document.dispatchEvent(new Event('portraitlab-render-device'));}mergePhoto(result.photo);if(state.current===id){displayPhoto();renderControls();}renderLibrary();return result;}
  // An older result must not overwrite unsent sliders, brush marks or undo state.
  clearPreviewTimer(id);
 }})().finally(()=>{if(state.pending.get(id)===promise)state.pending.delete(id);if(state.current===id)$('renderBadge').hidden=true;updateConnectionStatus();});state.pending.set(id,promise);updateConnectionStatus();return promise;
}
async function flushPhotos(ids){await Promise.all([...new Set(ids)].map(async id=>{while(state.dirty.has(id)||state.timers.has(id)||state.pending.has(id)){clearPreviewTimer(id);await runPreview(id);}}));}
async function flushCurrent(){if(state.current)await flushPhotos([state.current]);}
function lockEditing(locked){state.locked=locked;renderControls();updateConnectionStatus();}
function currentImportTarget(){if($('exportDialog').open)return 'export';return state.module==='lighting'?'lighting':'main';}
function requestImport(folder=false,target=currentImportTarget()){if(state.locked||state.importing)return;state.importTarget=target;$(folder?'folderInput':'fileInput').click();}
async function importFiles(files,target='main'){
 if(state.locked||state.importing){toast('正在保存或导入，请稍后再添加照片。',true);return [];}if(target!=='export'&&!await settleLighting('导入照片'))return [];
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
  if(target==='main'||target==='lighting'){state.selected=new Set([ids[0]]);await selectPhoto(ids[0]);}
  else if(target==='export'){state.exportIds=[...new Set([...(state.exportIds||[]),...ids])];$('exportDialogCount').textContent=state.exportIds.length;}
  else if(!state.current){await selectPhoto(ids[0]);}
  renderLibrary();renderControls();document.dispatchEvent(new CustomEvent('portraitlab-imported',{detail:{target,ids}}));toast(`已导入 ${ids.length} 张照片${target==='lighting'?'，可为当前照片布光':''}。`);
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
document.querySelectorAll('[data-region]').forEach(b=>b.onclick=()=>{if(!photo()||state.locked)return;saveHistory();editingParams().region=b.dataset.region;renderControls();queuePreview(state.current,0);});
function buildPresets(){window.PortraitPresets?.initialize();}
$('resetBtn').onclick=()=>{if(!photo()||state.locked)return;saveHistory();setEditingParams(photo(),window.PortraitSelection?.isEditing()?window.PortraitSelection.neutral():state.boot.defaults);renderControls();queuePreview(state.current,0);toast(`已为《${photo().name}》恢复参数，${editScopeLabel()}。`);};$('undoBtn').onclick=undo;
function updateBrush(){$('protectBrush').classList.toggle('active',state.brush==='protect');$('restoreBrush').classList.toggle('active',state.brush==='restore');$('brushCursor').hidden=true;renderViews();}
function toggleBrush(mode){if(!photo()||state.locked||window.PortraitSelection?.isSelecting())return;state.brush=state.brush===mode?null:mode;if(state.brush)setView('mask');updateBrush();}
$('protectBrush').onclick=()=>toggleBrush('protect');$('restoreBrush').onclick=()=>toggleBrush('restore');$('brushSize').oninput=e=>{state.brushRadius=Number(e.target.value)/100;$('brushSizeValue').textContent=e.target.value+'%';};
let drawing=null;const canvas=$('brushCanvas');function pointerPoint(e){const rect=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))];}
canvas.addEventListener('pointermove',e=>{if(!state.brush||state.locked||previewHolding)return;const pt=pointerPoint(e);const diameter=state.brushRadius*Math.min(canvas.clientWidth,canvas.clientHeight)*2;const cursor=$('brushCursor');cursor.hidden=false;Object.assign(cursor.style,{width:diameter+'px',height:diameter+'px',left:pt[0]*canvas.clientWidth+'px',top:pt[1]*canvas.clientHeight+'px'});if(drawing){drawing.points.push(pt);const ctx=canvas.getContext('2d');ctx.lineWidth=state.brushRadius*Math.min(canvas.width,canvas.height)*2;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=drawing.mode==='protect'?'#f18da58c':'#abd7b6a6';const last=drawing.points.at(-2);ctx.beginPath();ctx.moveTo(last[0]*canvas.width,last[1]*canvas.height);ctx.lineTo(pt[0]*canvas.width,pt[1]*canvas.height);ctx.stroke();}});
canvas.addEventListener('pointerleave',()=>{$('brushCursor').hidden=true;});canvas.addEventListener('pointerdown',e=>{if(e.button!==0||!state.brush||!photo()||state.locked||previewHolding)return;e.preventDefault();saveHistory();canvas.setPointerCapture(e.pointerId);drawing={photoId:state.current,mode:state.brush,radius:state.brushRadius,points:[pointerPoint(e)]};});
function finishStroke(){if(!drawing)return;const p=photo(drawing.photoId);const {mode,radius,points}=drawing;const stroke={mode,radius,points};drawing=null;if(!p)return;if(stroke.points.length>1000)stroke.points=stroke.points.filter((_,i)=>i%Math.ceil(stroke.points.length/1000)===0);p.strokes=[...(p.strokes||[]),stroke];queuePreview(p.id,0);canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
canvas.addEventListener('pointerup',finishStroke);canvas.addEventListener('pointercancel',finishStroke);
$('clearMask').onclick=()=>{const p=photo();if(!p||state.locked)return;saveHistory();p.strokes=[];queuePreview(p.id,0);toast(`已清除《${p.name}》的手工保护区域，仅当前照片。`);};
$('exportBtn').onclick=async()=>{if(state.locked||!await settleLighting('导出照片'))return;const ids=choices();lockEditing(true);try{await flushPhotos(ids);state.exportIds=ids;$('exportDialogCount').textContent=ids.length;$('exportPath').placeholder=state.boot.storage.export_dir;$('exportDialog').showModal();}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);}};
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
window.portraitlabPrepareToQuit=async()=>{if(window.PortraitLighting?.isDirty?.())throw new Error('布光调整尚未保留，请先选择“保留调整”或放弃调整，再退出。');if(window.PortraitLighting?.hasActiveJob?.())throw new Error('布光准备仍在进行，请完成或停止准备后再退出。');if(drawing)finishStroke();await flushPhotos([...new Set([...state.dirty,...state.pending.keys()])]);};
$('quitAppBtn').onclick=async()=>{if(!await settleLighting('退出程序'))return;if(window.chrome?.webview){window.chrome.webview.postMessage({type:'quit'});return;}if(!confirm('退出程序？正在运行的处理任务将停止。'))return;const button=$('quitAppBtn');button.disabled=true;try{await window.portraitlabPrepareToQuit();await post('/api/shutdown',{});$('modelsDialog').close();$('connectionStatus').textContent='服务已关闭';toast('程序已停止，可关闭此页面。');}catch(error){toast(errorMessage(error),true);button.disabled=false;}};
// Ctrl+O imports into the currently open editing feature; typing never triggers edit shortcuts.
document.addEventListener('keydown',event=>{
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='o'){event.preventDefault();requestImport(false,currentImportTarget());return;}
 if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)||document.querySelector('dialog[open]'))return;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();undo();}
 if(event.code==='Space'&&[document.body,$('viewport'),$('secondaryViewport')].includes(document.activeElement)){event.preventDefault();holdBefore(true);}
 if(event.key.toLowerCase()==='b'&&!event.ctrlKey&&!event.metaKey&&state.module==='retouch')toggleBrush('protect');
 if(event.key==='Escape'&&state.brush){state.brush=null;updateBrush();}
});
document.addEventListener('keyup',event=>{if(event.code==='Space')holdBefore(false);});window.addEventListener('blur',()=>holdBefore(false));
$('holdOriginal').addEventListener('keydown',event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();holdBefore(true);}});$('holdOriginal').addEventListener('keyup',()=>holdBefore(false));$('holdOriginal').addEventListener('blur',()=>holdBefore(false));
document.querySelector('.brand').addEventListener('click',event=>event.preventDefault());
window.addEventListener('beforeunload',event=>{if(state.dirty.size||state.pending.size||state.importing||window.PortraitLighting?.isDirty?.()){event.preventDefault();event.returnValue='';}});
function requirePatchBackend(data){
 if(data.capabilities?.local_adjustments===true)return;
 state.locked=true;const error=new Error('软件更新尚未生效：请关闭全部照影窗口后重新打开，再使用矩形选区和新预设。');error.name='UpdateRequired';throw error;
}
async function boot(){try{state.boot=await api('/api/bootstrap');requirePatchBackend(state.boot);state.token=state.boot.token;buildPresets();modelInfo();const result=await api('/api/photos');state.photos=result.photos;state.removalUndo=result.last_removed_ids||[];renderLibrary();renderControls();renderViews();$('connectionStatus').textContent='已就绪';if(state.photos.length){state.selected.add(state.photos[0].id);await selectPhoto(state.photos[0].id);}const jobs=await api('/api/jobs');const active=jobs.jobs.find(j=>activeStatuses.includes(j.status));if(active){showJob(active);pollJob();}document.dispatchEvent(new Event('portraitlab-ready'));}catch(error){$('connectionStatus').textContent=error.name==='UpdateRequired'?'请关闭全部窗口后重启':'无法连接处理服务';renderControls();toast(errorMessage(error),true);}}
boot();
