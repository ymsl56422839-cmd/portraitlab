/* Rectangular local retouching in image coordinates. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 const G=window.PortraitGeometry,ui={photoId:null,editing:false,armed:false,drag:null};
 const panes=new Map();
 const isEditing=(p=photo())=>!!p&&p.id===ui.photoId&&ui.editing&&!!p.local_adjustment&&state.module==='retouch'&&state.retouchTool==='basic';
 const neutral=()=>state.boot?.local_defaults?clone(state.boot.local_defaults):Object.fromEntries(Object.entries(state.boot?.defaults||{}).map(([key,value])=>[key,typeof value==='number'?(key==='opacity'||key==='saturation'?1:0):value]));
 const clamp01=value=>Math.max(0,Math.min(1,value));
 const scope=()=>isEditing()?'矩形选区内':'全图调整';
 function refresh(){
  const p=photo(),local=p?.local_adjustment,enabled=!!p&&!state.locked&&state.module==='retouch'&&state.retouchTool==='basic';
  $('selectionToolbar').hidden=state.module!=='retouch'||state.retouchTool!=='basic';
  const editing=isEditing();
  $('globalEditBtn').classList.toggle('active',!editing&&!ui.armed);
  $('globalEditBtn').setAttribute('aria-pressed',String(!editing&&!ui.armed));
  $('rectangleSelectBtn').classList.toggle('active',editing||ui.armed);
  $('rectangleSelectBtn').setAttribute('aria-pressed',String(editing||ui.armed));
  $('rectangleSelectBtn').textContent=ui.armed?'取消框选':local?'编辑矩形选区':'矩形选区';
  for(const id of ['globalEditBtn','rectangleSelectBtn'])$(id).disabled=!enabled;
  $('redrawSelectionBtn').hidden=!local||!editing;
  $('finishSelectionBtn').hidden=!editing;
  $('deleteSelectionBtn').hidden=!local;
  for(const id of ['redrawSelectionBtn','finishSelectionBtn','deleteSelectionBtn'])$(id).disabled=!enabled;
  $('selectionHint').textContent=(editing||ui.armed)&&!G.ready(p)?'正在同步塑形与画布；完成后可继续框选':ui.drag?'松开后更新局部效果 · Esc 取消本次拖动':ui.armed?'在处理结果画布内拖出选区 · Esc 取消':editing?'拖动内部移动，拖动边角缩放 · 选区跟随塑形':local?'全图调整 · 已保留一处局部修片':'全图调整 · 可框选局部套用预设';
  $('adjustmentScope').textContent=ui.armed?'等待框选 · 先在照片内拖出矩形':scope();
  $('adjustmentScope').classList.toggle('local-active',editing);
  $('retouchPanel').classList.toggle('is-local-editing',editing);
  $('toneScopeNote').textContent=editing?'当前参数仅作用于矩形内部；面部修饰仍受皮肤识别和保护笔刷限制。':'面部提亮作用于皮肤选区；整体曝光、冷暖和饱和度作用于整张照片。';
  $('brushScopeNote').textContent=editing?'局部修片沿用已有皮肤保护。需修改保护笔刷时，先切换“全图调整”。':'保护痣、雀斑或妆面。仅影响修肤和面部提亮；布光模块使用独立的主体选区。';
  if(editing||ui.armed){for(const id of ['protectBrush','restoreBrush','clearMask','brushSize'])$(id).disabled=true;$('syncBtn').disabled=true;$('batchHint').textContent='局部范围不参与批量套用，请先切换全图调整。';}
  else{$('brushSize').disabled=!p||state.locked;if(!state.batchUndo)$('batchHint').textContent='只复制全局参数，保留各自的保护笔刷和局部调整';}
  if(ui.armed){for(const def of definitions)$('control-'+def.key).disabled=true;for(const id of ['resetBtn','originalPresetBtn','savePresetBtn'])$(id).disabled=true;for(const button of document.querySelectorAll('[data-region],#presets .preset-apply'))button.disabled=true;}
  const rect=ui.drag?.currentRect||local?.rect;
  for(const [name,item] of panes){
   const visible=enabled&&(editing||ui.armed)&&G.ready(p,name)&&state[previewPanes[name].view]==='result'&&!previewHolding&&!state.brush&&(name==='primary'||state.layout==='dual');
   item.layer.hidden=!visible;item.layer.classList.toggle('drawing-selection',ui.armed||ui.drag?.mode==='draw');
   item.box.hidden=!visible||!rect||(ui.armed&&!ui.drag);
   if(rect){item.polygon.setAttribute('points',G.rectPoints(rect).map(q=>`${q.x*1000},${q.y*1000}`).join(' '));const anchor=G.toView({x:rect.x,y:rect.y});Object.assign(item.label.style,{left:anchor.x*100+'%',top:anchor.y*100+'%'});for(const handle of item.box.querySelectorAll('[data-handle]')){const dir=handle.dataset.handle,q=G.toView({x:rect.x+rect.width*(dir.includes('w')?0:dir.includes('e')?1:.5),y:rect.y+rect.height*(dir.includes('n')?0:dir.includes('s')?1:.5)});Object.assign(handle.style,{left:q.x*100+'%',top:q.y*100+'%'});}item.box.setAttribute('aria-label',`局部调整，原始选区宽 ${Math.round(rect.width*100)}%，高 ${Math.round(rect.height*100)}%`);}
  }
 }
 function setEditing(on){
  cancelDrag();ui.armed=false;ui.editing=on;ui.photoId=photo()?.id||null;
  if(on){activate();}
  renderControls();refresh();
 }
 function arm(){
  if(!photo()||state.locked)return;
  cancelDrag();ui.photoId=state.current;ui.editing=!!photo().local_adjustment;ui.armed=true;
  activate();renderControls();refresh();
  toast('在处理结果画布中拖出选区，随后预设和参数仅调整选区内。');
 }
 function activate(){document.dispatchEvent(new CustomEvent('portraitlab-tool-activated',{detail:{name:'selection'}}));if(drawing)finishStroke();state.brush=null;updateBrush();if(!Object.entries(previewPanes).some(([name,pane])=>(name==='primary'||state.layout==='dual')&&state[pane.view]==='result'))setView('result','primary');}
 function point(event,item){return G.eventPoint(event,item.stage);}
 function computeRect(drag,p){
  if(drag.mode==='draw')return {x:Math.min(p.x,drag.start.x),y:Math.min(p.y,drag.start.y),width:Math.abs(p.x-drag.start.x),height:Math.abs(p.y-drag.start.y)};
  const r=drag.before.rect,dx=p.x-drag.start.x,dy=p.y-drag.start.y;
  if(drag.mode==='move')return {...r,x:Math.max(0,Math.min(1-r.width,r.x+dx)),y:Math.max(0,Math.min(1-r.height,r.y+dy))};
  let x0=r.x,y0=r.y,x1=r.x+r.width,y1=r.y+r.height;
  if(drag.mode.includes('w'))x0=p.x;if(drag.mode.includes('e'))x1=p.x;
  if(drag.mode.includes('n'))y0=p.y;if(drag.mode.includes('s'))y1=p.y;
  return {x:Math.min(x0,x1),y:Math.min(y0,y1),width:Math.abs(x1-x0),height:Math.abs(y1-y0)};
 }
 function release(drag){if(drag.item.layer.hasPointerCapture?.(drag.pointerId))drag.item.layer.releasePointerCapture(drag.pointerId);}
 function cancelDrag(){
  const drag=ui.drag;if(!drag)return;
  ui.drag=null;release(drag);refresh();
 }
 function finishDrag(event){
  const drag=ui.drag;if(!drag||drag.pointerId!==event.pointerId)return;
  const p=photo(drag.photoId),r=drag.currentRect;ui.drag=null;release(drag);
  const valid=p&&r&&r.width*p.width>=4&&r.height*p.height>=4;
  if(!valid){refresh();toast('选区过小，请在照片内拖出至少 4 × 4 像素的矩形。',true);return;}
  const next={rect:r,params:clone(drag.before?.params||neutral())};
  if(JSON.stringify(next)!==JSON.stringify(p.local_adjustment)){
   saveHistory();p.local_adjustment=next;queuePreview(p.id,0);
  }
  ui.armed=false;ui.editing=true;ui.photoId=p.id;renderControls();refresh();
  toast('矩形选区已就绪：预设与参数仅调整框内，框外修片保留。');
 }
 for(const [name,pane] of Object.entries(previewPanes)){
  const stage=$(pane.stage),layer=document.createElement('div');layer.className='region-selection-layer';layer.hidden=true;
  const box=document.createElement('div');box.className='region-selection-box';box.setAttribute('role','group');box.tabIndex=0;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('region-selection-outline');svg.setAttribute('viewBox','0 0 1000 1000');svg.setAttribute('preserveAspectRatio','none');const polygon=document.createElementNS(svg.namespaceURI,'polygon');svg.append(polygon);box.append(svg);
  const label=document.createElement('span');label.className='region-selection-label';label.textContent='局部修片';box.append(label);
  for(const direction of ['nw','n','ne','e','se','s','sw','w']){const handle=document.createElement('span');handle.className='region-selection-handle '+direction;handle.dataset.handle=direction;handle.setAttribute('aria-hidden','true');box.append(handle);}
  layer.append(box);stage.append(layer);const item={stage,layer,box,polygon,label};panes.set(name,item);
  layer.addEventListener('pointerdown',event=>{
   if(event.button!==0||!photo()||state.locked||previewHolding||state.module!=='retouch'||!G.ready(photo(),name)||state[pane.view]!=='result')return;
   const target=event.target,mode=ui.armed?'draw':target.dataset.handle||'move';
   if(!ui.armed&&!target.closest('.region-selection-box'))return;
   event.preventDefault();event.stopPropagation();box.focus({preventScroll:true});
   ui.drag={item,pointerId:event.pointerId,photoId:state.current,mode,start:point(event,item),before:photo().local_adjustment?clone(photo().local_adjustment):null,currentRect:null};
   if(mode!=='draw')ui.drag.currentRect=clone(ui.drag.before.rect);
   layer.setPointerCapture?.(event.pointerId);refresh();
  });
  layer.addEventListener('pointermove',event=>{const d=ui.drag;if(!d||d.pointerId!==event.pointerId||d.item!==item)return;event.preventDefault();event.stopPropagation();d.currentRect=computeRect(d,point(event,item));refresh();});
  layer.addEventListener('pointerup',event=>{event.stopPropagation();finishDrag(event);});
  layer.addEventListener('pointercancel',cancelDrag);
  layer.addEventListener('lostpointercapture',event=>{if(ui.drag?.pointerId===event.pointerId)cancelDrag();});
  box.addEventListener('keydown',event=>{
   if(!isEditing()||state.locked||!G.ready(photo(),name)||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
   event.preventDefault();event.stopPropagation();saveHistory();const p=photo(),r=p.local_adjustment.rect,step=event.shiftKey?10:1;
   r.x=Math.max(0,Math.min(1-r.width,r.x+({ArrowLeft:-step,ArrowRight:step}[event.key]||0)/p.width));
   r.y=Math.max(0,Math.min(1-r.height,r.y+({ArrowUp:-step,ArrowDown:step}[event.key]||0)/p.height));
   refresh();queuePreview(p.id);
  });
 }
 $('rectangleSelectBtn').onclick=()=>{if(ui.armed){ui.armed=false;cancelDrag();renderControls();refresh();}else if(photo()?.local_adjustment){setEditing(true);toast('正在编辑已有局部调整，拖动选区可移动或改变范围。');}else arm();};
 $('redrawSelectionBtn').onclick=arm;
 $('globalEditBtn').onclick=()=>{setEditing(false);toast('已切换全图调整，现有局部效果保留。');};
 $('finishSelectionBtn').onclick=async()=>{const p=photo();if(!p)return;try{await flushPhotos([p.id]);if(p.id!==state.current)return;setEditing(false);toast('局部调整已保留，可重新点击“编辑矩形选区”继续修改。');}catch(error){toast('局部调整尚未保存：'+errorMessage(error),true);}};
 $('deleteSelectionBtn').onclick=()=>{if(!photo()?.local_adjustment||state.locked)return;cancelDrag();saveHistory();photo().local_adjustment=null;ui.editing=ui.armed=false;renderControls();refresh();queuePreview(state.current,0);toast('已删除局部调整，全图修片和布光保留；可撤销。');};
 document.addEventListener('keydown',event=>{if(event.key!=='Escape'||document.querySelector('dialog[open]')||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||document.activeElement?.isContentEditable)return;if(ui.drag){cancelDrag();event.preventDefault();}else if(ui.armed){ui.armed=false;renderControls();refresh();event.preventDefault();}else if(isEditing()){setEditing(false);toast('已退出选区，局部效果保留。');event.preventDefault();}});
 document.addEventListener('portraitlab-photo-selected',()=>{cancelDrag();ui.photoId=state.current;ui.editing=ui.armed=false;renderControls();refresh();});
 document.addEventListener('portraitlab-module-change',()=>{cancelDrag();ui.editing=ui.armed=false;refresh();});
 document.addEventListener('portraitlab-preview-geometry',()=>{cancelDrag();refresh();});
 for(const event of ['portraitlab-preview-view','portraitlab-edit-state','portraitlab-photo-updated','portraitlab-selection-change','portraitlab-coordinate-state'])document.addEventListener(event,refresh);
 document.addEventListener('portraitlab-tool-activated',event=>{if(event.detail?.name!=='selection'&&(ui.armed||ui.editing))setEditing(false);});
 window.addEventListener('resize',()=>{cancelDrag();refresh();});
 window.addEventListener('blur',()=>{cancelDrag();ui.armed=false;renderControls();refresh();});
 window.PortraitSelection={isEditing,isSelecting:()=>ui.armed||isEditing(),isArmed:()=>ui.armed,parameters:p=>isEditing(p)?p.local_adjustment.params:null,neutral,refresh,cancelDrag,exit:()=>setEditing(false),isDragging:()=>!!ui.drag};
 refresh();
})();
