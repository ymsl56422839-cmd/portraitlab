/* Continuous, reversible healing brush. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 const host=$('retouchToolHost');if(!host)return;
 const G=window.PortraitGeometry,ui={active:false,mode:'auto',sampling:false,source:null,lastSource:null,radius:.004,strength:1,hardness:.65,drag:null},panes=[];let pendingFeedback=null;
 host.innerHTML=`<details class="spot-tool" open><summary>修补笔刷 <span id="spotCount">0 笔</span></summary><p class="control-note">点按或涂抹局部瑕疵，每次拖动作为一笔，可撤销。此工具独立于基础调整，不受矩形选区和整体效果强度限制。</p><div class="spot-modes" role="group" aria-label="修补方式"><button data-spot-mode="auto" type="button" aria-pressed="true">自动修补</button><button data-spot-mode="sampled" type="button" aria-pressed="false">取样修补</button><button data-spot-mode="restore" type="button" aria-pressed="false">恢复修补前</button></div><div class="spot-actions"><button id="spotToggle" type="button" aria-pressed="false">开始修补</button><button id="spotSample" type="button">选取样本</button><button id="spotRemove" type="button">撤销上一笔</button><button id="spotClear" type="button">清除全部修补</button></div><label class="spot-slider">笔刷半径 <output id="spotRadiusValue"></output><input id="spotRadius" aria-label="修补半径" type="range" min="0.1" max="5" step="0.1" value="0.4"></label><label class="spot-slider">柔边 <output id="spotSoftnessValue">35%</output><input id="spotSoftness" aria-label="修补柔边" type="range" min="0" max="100" step="5" value="35"></label><label class="spot-slider">修补强度 <output id="spotStrengthValue">100%</output><input id="spotStrength" aria-label="修补强度" type="range" min="0" max="100" step="5" value="100"></label><p id="spotHint" class="control-note" role="status">先放大画布检查瑕疵。自动取样找不到合适纹理时会保留原貌并提示。</p></details>`;
 const eligible=()=>!!photo()&&!state.locked&&state.module==='retouch'&&state.retouchTool==='repair';
 const visible=item=>eligible()&&G.ready(photo(),item.name)&&ui.active&&!previewHolding&&(item.name!=='secondary'||state.layout==='dual')&&state[previewPanes[item.name].view]==='result';
 function requestPreview(p,action,message){pendingFeedback={id:p.id,action,repairs:JSON.stringify(p.spot_repairs||[]),count:p.spot_repairs?.length||0};toast(message);queuePreview(p.id,0);}
 function previewCompleted(id,result){
  const f=pendingFeedback;if(!f||f.id!==id||JSON.stringify(result?.photo?.spot_repairs||[])!==f.repairs)return;pendingFeedback=null;if(state.current!==id)return;
  const meta=result?.meta?.spot_repairs,warnings=meta?.warnings||[];let message;
  if(f.action==='remove')message='已撤销上一笔修补，预览已更新。';else if(f.action==='clear')message='已清除全部修补，其他修片与塑形保留。';else{
   const repair=meta?.repairs?.[f.count-1];if(Number.isFinite(repair?.source_x)&&Number.isFinite(repair?.source_y))ui.lastSource={x:repair.source_x,y:repair.source_y};
   if(repair?.changed_pixels>0)message=repair.mode==='restore'?'局部已恢复至修补前，预览已更新。':`第 ${f.count} 笔修补已完成，预览已更新。`;
   else{const reasons={same_source_and_target:'样本与目标重合，请另选纹理取样。',zero_strength:'修补强度为零。',no_context:'周边像素不足，请缩小笔刷。',no_valid_pixels:'目标与样本没有有效重叠区域。',unchanged_pixels:'请调整样本或笔刷范围。',no_suitable_source:'未找到相近纹理，请缩小范围或改用取样修补。'};message='本笔未产生变化。'+(reasons[repair?.no_op_reason]||'请检查样本与修补范围。');}
  }toast([message,...new Set(warnings)].join('；'));refresh();
 }
 function previewFailed(id,error){if(pendingFeedback?.id!==id)return;pendingFeedback=null;toast('修补预览未完成：'+errorMessage(error)+'。请重试。',true);}
 function refresh(){
  const p=photo(),enabled=eligible(),ready=G.ready(p),count=p?.spot_repairs?.length||0;
  if(state.brush||window.PortraitSelection?.isSelecting()||window.PortraitShape?.isActive())ui.active=false;
  $('spotCount').textContent=count+' 笔';$('spotToggle').disabled=!enabled||!ready;$('spotToggle').textContent=ui.active?'结束修补':'开始修补';$('spotToggle').setAttribute('aria-pressed',String(ui.active));
  $('spotSample').disabled=!enabled||!ready||!ui.active||ui.mode==='restore';$('spotRemove').disabled=$('spotClear').disabled=!enabled||!count;
  for(const id of ['spotRadius','spotSoftness','spotStrength'])$(id).disabled=!enabled;
  for(const b of document.querySelectorAll('[data-spot-mode]')){b.disabled=!enabled;b.setAttribute('aria-pressed',String(b.dataset.spotMode===ui.mode));}
  $('spotRadiusValue').textContent=p?Math.round(ui.radius*Math.min(p.width,p.height))+' px':(ui.radius*100).toFixed(1)+'%';$('spotSoftnessValue').textContent=Math.round((1-ui.hardness)*100)+'%';$('spotStrengthValue').textContent=Math.round(ui.strength*100)+'%';
  if(enabled&&!ready)$('spotHint').textContent='正在同步塑形与画布；完成后可继续修补。';
  else if(ui.drag)$('spotHint').textContent='松开后处理这一笔 · Esc 取消本次涂抹';
  else if(ui.active)$('spotHint').textContent=ui.mode==='restore'?'涂抹恢复修补前的局部纹理，保留上游修肤与色彩效果。':ui.mode==='auto'?'点按或涂抹瑕疵，自动寻找附近样本；Alt 点击切换手动取样。':ui.sampling||!ui.source?'点击相近的干净纹理取样；Alt 点击可随时更换样本。':'点按或涂抹瑕疵 · Alt 点击更换样本 · Esc 结束修补';
  else $('spotHint').textContent=count?'修补记录已保留。点击“开始修补”继续，可逐笔撤销。':'先放大画布检查瑕疵，点击“开始修补”后点按或涂抹。';
  for(const item of panes){item.layer.hidden=!visible(item);if(item.layer.hidden)item.cursor.setAttribute('points','');const src=ui.mode==='sampled'?ui.source:ui.mode==='auto'?ui.lastSource:null;item.source.hidden=!src;if(src){const q=G.toView(src);item.source.style.left=q.x*100+'%';item.source.style.top=q.y*100+'%';item.source.title=ui.mode==='auto'?'上一笔自动选择的纹理样本':'手动纹理样本';}}
 }
 function ensureResult(){if(!Object.entries(previewPanes).some(([name,p])=>(name==='primary'||state.layout==='dual')&&state[p.view]==='result'))setView('result','primary');}
 function activate(){if(!eligible()||!G.ready())return;cancelDrag();const next=!ui.active;if(next){document.dispatchEvent(new CustomEvent('portraitlab-tool-activated',{detail:{name:'repair'}}));if(drawing)finishStroke();state.brush=null;updateBrush();window.PortraitSelection?.exit();ui.active=true;ui.sampling=ui.mode==='sampled'&&!ui.source;ensureResult();}else{ui.active=false;$('spotHint').textContent='修补记录已保留，可逐笔撤销。';}refresh();}
 function setMode(mode){cancelDrag();ui.mode=mode;ui.lastSource=null;ui.sampling=mode==='sampled'&&!ui.source;refresh();}
 $('spotToggle').onclick=activate;for(const b of document.querySelectorAll('[data-spot-mode]'))b.onclick=()=>setMode(b.dataset.spotMode);
 $('spotSample').onclick=()=>{setMode('sampled');ui.sampling=true;refresh()};
 $('spotRadius').oninput=e=>{ui.radius=Number(e.target.value)/100;refresh()};$('spotSoftness').oninput=e=>{ui.hardness=1-Number(e.target.value)/100;refresh()};$('spotStrength').oninput=e=>{ui.strength=Number(e.target.value)/100;refresh()};
 $('spotRemove').onclick=()=>{const p=photo();if(!eligible()||!p.spot_repairs?.length)return;cancelDrag();saveHistory();p.spot_repairs=p.spot_repairs.slice(0,-1);requestPreview(p,'remove','已撤销上一笔，正在更新预览。');refresh()};
 $('spotClear').onclick=()=>{const p=photo();if(!eligible()||!p.spot_repairs?.length)return;cancelDrag();saveHistory();p.spot_repairs=[];requestPreview(p,'clear','已清除修补记录，正在更新预览。');refresh()};
 function totalPoints(p){return (p.spot_repairs||[]).reduce((n,r)=>n+(r.points?.length||1),0)}
 function appendPoint(d,p,force=false){const previous=d.points.at(-1),current=photo(d.photoId),distance=Math.hypot((p.x-previous[0])*current.width,(p.y-previous[1])*current.height);if(!force&&distance<Math.max(1,d.radius*Math.min(current.width,current.height)*.3))return;if(distance<.01)return;if(d.points.length>=d.limit){d.limited=true;return;}d.points.push([p.x,p.y]);}
 function draw(item){const d=ui.drag;if(!d||d.item!==item){item.trail.setAttribute('points','');return;}item.trail.setAttribute('points',d.points.map(p=>G.toView({x:p[0],y:p[1]})).map(p=>`${p.x*1000},${p.y*1000}`).join(' '));const bounds=item.stage.getBoundingClientRect();item.trail.style.strokeWidth=String(2*d.radius*Math.min(bounds.width,bounds.height));}
 function cancelDrag(){const d=ui.drag;ui.drag=null;if(d&&d.item.layer.hasPointerCapture?.(d.pointerId))d.item.layer.releasePointerCapture(d.pointerId);for(const item of panes)item.trail.setAttribute('points','');refresh();}
 for(const [name,pane] of Object.entries(previewPanes)){
  const stage=$(pane.stage),layer=document.createElement('div');layer.className='spot-layer';layer.hidden=true;layer.setAttribute('aria-label','连续修补画布');
  const source=document.createElement('span');source.className='spot-source';source.textContent='＋';source.hidden=true;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('spot-overlay');svg.setAttribute('viewBox','0 0 1000 1000');svg.setAttribute('preserveAspectRatio','none');const trail=document.createElementNS(svg.namespaceURI,'polyline'),cursor=document.createElementNS(svg.namespaceURI,'polygon');trail.classList.add('spot-trail');cursor.classList.add('spot-footprint');svg.append(trail,cursor);layer.append(svg,source);stage.append(layer);
  const item={name,stage,layer,source,trail,cursor};panes.push(item);
  const point=e=>G.eventPoint(e,stage);
  layer.addEventListener('pointermove',e=>{if(!visible(item))return;const p=point(e);cursor.setAttribute('points',G.circle(p,ui.radius).map(p=>`${p.x*1000},${p.y*1000}`).join(' '));cursor.classList.toggle('sampling',e.altKey||ui.sampling);const d=ui.drag;if(d?.item===item&&d.pointerId===e.pointerId){appendPoint(d,p);draw(item);e.preventDefault();e.stopPropagation();}});
  layer.addEventListener('pointerleave',()=>cursor.setAttribute('points',''));
  layer.addEventListener('pointerdown',e=>{
   if(e.button!==0||!visible(item))return;e.preventDefault();e.stopPropagation();const p=point(e);
   if(e.altKey||(ui.mode==='sampled'&&(ui.sampling||!ui.source))){ui.mode='sampled';ui.source=p;ui.sampling=false;refresh();toast('已选取样本。点按或涂抹需要修复的位置。');return;}
   const current=photo(),repairs=current.spot_repairs||[],remaining=16384-totalPoints(current);if(repairs.length>=100||remaining<1){toast('修补记录已达上限，请撤销或清除不需要的记录。',true);return;}
   ui.drag={item,pointerId:e.pointerId,photoId:current.id,mode:ui.mode,source:ui.source?{...ui.source}:null,radius:ui.radius,strength:ui.strength,hardness:ui.hardness,points:[[p.x,p.y]],limit:Math.min(1000,remaining)};layer.setPointerCapture?.(e.pointerId);draw(item);refresh();
  });
  layer.addEventListener('pointerup',e=>{const d=ui.drag;if(!d||d.item!==item||d.pointerId!==e.pointerId)return;e.preventDefault();e.stopPropagation();appendPoint(d,point(e),true);const current=photo(d.photoId);cancelDrag();if(!current||current.id!==state.current)return;saveHistory();const [x,y]=d.points[0],stroke={x,y,mode:d.mode,radius:d.radius,strength:d.strength,hardness:d.hardness,points:d.points};if(d.mode==='sampled'){stroke.source_x=d.source.x;stroke.source_y=d.source.y;}current.spot_repairs=[...(current.spot_repairs||[]),stroke];requestPreview(current,'add',`已记录第 ${current.spot_repairs.length} 笔，正在更新预览。${d.limited?'本笔已达到采样上限，请下一笔继续。':''}`);refresh();});
  layer.addEventListener('pointercancel',cancelDrag);layer.addEventListener('lostpointercapture',e=>{if(ui.drag?.pointerId===e.pointerId)cancelDrag()});
 }
 function stop(){cancelDrag();ui.active=false;refresh()}
 function resetPhoto(){stop();ui.source=ui.lastSource=null;ui.sampling=ui.mode==='sampled';pendingFeedback=null;refresh()}
 document.addEventListener('portraitlab-photo-selected',resetPhoto);document.addEventListener('portraitlab-module-change',stop);document.addEventListener('portraitlab-tool-activated',e=>{if(e.detail?.name!=='repair')stop()});
 for(const event of ['portraitlab-edit-state','portraitlab-preview-view','portraitlab-preview-geometry','portraitlab-photo-updated','portraitlab-coordinate-state'])document.addEventListener(event,refresh);
 window.addEventListener('blur',cancelDrag);document.addEventListener('keydown',e=>{if(e.key==='Escape'&&(ui.drag||ui.active)&&!document.querySelector('dialog[open]')){if(ui.drag)cancelDrag();else stop();e.preventDefault()}});
 window.PortraitSpotRepair={refresh,stop,clearFeedback(){pendingFeedback=null;},isActive:()=>ui.active,isDragging:()=>!!ui.drag,previewCompleted,previewFailed};refresh();
})();
