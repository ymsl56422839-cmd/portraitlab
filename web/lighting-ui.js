/* Interactive, reversible lighting editor. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 const byId=id=>document.getElementById(id),copy=value=>JSON.parse(JSON.stringify(value)),limit=(n,a,b)=>Math.max(a,Math.min(b,n));
 const types={directional:'方向光',point:'点光源',spot:'聚光灯',area:'柔光箱'};
 const fallbackLight={id:'light-1',type:'directional',enabled:true,x:.25,y:.25,z:1,azimuth:-135,elevation:45,intensity:.8,temperature:6500,radius:.5,softness:.5,beam_angle:60,target_x:.5,target_y:.5};
 const fallbackConfig={schema:1,enabled:false,scope:'subject',strength:1,ambient:1,lights:[]};
 const editor={status:null,photoId:null,prepared:null,draft:null,saved:null,selected:null,job:null,pollTimer:null,loading:false,saving:false,sequence:0,frame:null,renderer:null,images:null,error:null,leavePromise:null,preset:null,lastFrame:0,previewPending:false,previewError:null,previewMessage:null,previewNotice:null,dragCancel:null,showSelection:false,selectionMap:null,panes:[]};
 const host=byId('lightingToolHost');
 host.innerHTML=`
  <section class="light-section light-intro"><div class="light-heading"><h2>交互布光</h2><span id="lightingDraftState" class="status-tag">选择照片</span></div>
   <p class="light-note">在原片上调整光线。选择预设，或在画布上拖动光源。</p>
   <div class="light-import"><button id="lightingImport" type="button">导入照片</button><button id="lightingImportFolder" type="button">导入文件夹</button></div>
  </section>
  <section class="light-section light-preparation"><div class="light-heading"><h3>照片分析</h3><strong id="lightingPrepareState">正在检查</strong></div>
   <p id="lightingPrepareMessage" class="light-note" role="status">检查布光组件…</p><p id="lightingAnalysisWarnings" class="light-note light-warning" role="status" hidden></p>
   <label class="light-field">分析设备<select id="lightingDevice"><option value="auto">自动选择</option><option value="cuda">NVIDIA GPU</option><option value="cpu">CPU</option></select></label>
   <div class="light-prepare-actions"><button id="lightingPrepare" type="button" disabled>分析当前照片</button><button id="lightingDownload" type="button" disabled>检查模型</button><button id="lightingCancelJob" type="button" hidden>停止分析</button></div>
   <div id="lightingProgress" class="light-progress" hidden><span></span></div>
  </section>
  <fieldset id="lightingControls" disabled>
   <section class="light-section"><div class="light-heading"><h3>灯光预设</h3></div><div id="lightingPresets" class="light-presets"></div><p id="lightingPresetHint" class="light-note">预设替换灯光配置，保留所选作用范围，可继续调整。</p></section>
   <section class="light-section"><div class="light-heading"><h3>作用范围</h3><label class="light-check"><input id="lightingEnabled" type="checkbox">启用布光</label></div>
    <select id="lightingScope" aria-label="灯光作用范围"><option value="subject">仅主体 · 保留背景</option><option value="background">仅背景 · 保留主体</option><option value="all">整张照片</option></select><p id="lightingScopeHint" class="light-note">头发、衣物和身体均属于主体；选区外保留原来的像素。</p>
    <label class="light-check"><input id="lightingShowSelection" type="checkbox">显示作用选区</label><p class="light-note">高亮当前作用区域，用于检查发丝和轮廓，不会写入照片。</p>
    <div id="lightingGlobalControls"></div>
   </section>
   <section class="light-section"><div class="light-heading"><h3>光源 <span id="lightingLightCount">0 / 4</span></h3><button id="lightingAdd" type="button">添加光源</button></div>
    <div id="lightingLights" class="light-list"></div><p class="light-note">拖动带编号的光源控制点。聚光灯的十字控制点用于调整照射目标。</p>
    <div id="lightingDetail" hidden><div class="light-detail-heading"><label class="light-field">光源类型<select id="lightingType"><option value="directional">方向光</option><option value="point">点光源</option><option value="spot">聚光灯</option><option value="area">柔光箱</option></select></label><button id="lightingRemove" type="button" title="移除选中的光源">移除光源</button></div><div id="lightingLightControls"></div></div>
   </section>
  </fieldset>
  <p class="light-note light-limits">原有阴影和反光仍来自照片。深度分析可能有误，建议放大检查脸部、发丝及轮廓。</p>`;
 const actions=document.createElement('div');actions.className='lighting-actions';actions.innerHTML='<p id="lightingFeedback" role="status">选择照片后开始布光。</p><div><button id="lightingDiscard" type="button" disabled>放弃调整</button><button id="lightingApply" type="button" class="primary" disabled>保留调整</button></div><small id="lightingPreviewDevice">预览未准备</small>';byId('lightingPanel').append(actions);
 const canvasStatus=document.createElement('div');canvasStatus.id='lightingCanvasStatus';canvasStatus.className='lighting-canvas-status';canvasStatus.hidden=true;canvasStatus.setAttribute('role','status');canvasStatus.innerHTML='<strong id="lightingCanvasState"></strong><span id="lightingCanvasMessage"></span>';byId('previewGrid').before(canvasStatus);
 const leave=document.createElement('dialog');leave.id='lightingLeaveDialog';leave.innerHTML='<div class="dialog-heading"><h2>布光调整尚未保留</h2></div><p id="lightingLeaveMessage" class="dialog-description"></p><p id="lightingLeaveError" class="form-error" role="alert" hidden></p><div class="dialog-actions"><button id="lightingLeaveStay" type="button">继续布光</button><button id="lightingLeaveDiscard" type="button">放弃调整</button><button id="lightingLeaveSave" type="button" class="primary">保留调整</button></div>';document.body.append(leave);

 const globalFields=[{key:'strength',label:'整体强度',min:0,max:1,step:.01,format:v=>Math.round(v*100)+'%'},{key:'ambient',label:'环境亮度',min:0,max:1.5,step:.01,format:v=>Math.round(v*100)+'%'}];
 const lightFields=[
  {key:'intensity',label:'光源强度',min:0,max:3,step:.01,format:v=>Math.round(v*100)+'%'},
  {key:'temperature',label:'色温',min:2000,max:12000,step:100,format:v=>Math.round(v)+' K'},
  {key:'azimuth',label:'方位角',min:-180,max:180,step:1,format:v=>Math.round(v)+'°',only:['directional']},
  {key:'elevation',label:'俯仰角',min:5,max:90,step:1,format:v=>Math.round(v)+'°',only:['directional']},
  {key:'x',label:'水平位置',min:-1,max:2,step:.01,format:v=>Math.round(v*100)+'%',only:['point','spot','area']},
  {key:'y',label:'垂直位置',min:-1,max:2,step:.01,format:v=>Math.round(v*100)+'%',only:['point','spot','area']},
  {key:'z',label:'相对距离',min:.1,max:3,step:.01,format:v=>v.toFixed(2),only:['point','spot','area']},
  {key:'radius',label:'柔光箱尺寸',min:.02,max:2,step:.01,format:v=>Math.round(v*100)+'%',only:['area']},
  {key:'beam_angle',label:'光束角',min:5,max:120,step:1,format:v=>Math.round(v)+'°',only:['spot']},
  {key:'softness',label:'边缘柔度',min:0,max:1,step:.01,format:v=>Math.round(v*100)+'%'},
  {key:'target_x',label:'照射目标 · 水平',min:-1,max:2,step:.01,format:v=>Math.round(v*100)+'%',only:['spot']},
  {key:'target_y',label:'照射目标 · 垂直',min:-1,max:2,step:.01,format:v=>Math.round(v*100)+'%',only:['spot']}
 ];
 function makeSliders(fields,container,scope){for(const field of fields){const row=document.createElement('div');row.className='slider-row light-slider';row.dataset.lightField=field.key;row.innerHTML=`<label class="slider-label" for="lighting-${scope}-${field.key}"><span>${field.label}</span><output></output></label><input id="lighting-${scope}-${field.key}" type="range" min="${field.min}" max="${field.max}" step="${field.step}" aria-label="${field.label}">`;const input=row.querySelector('input');input.addEventListener('input',()=>{const target=scope==='global'?editor.draft:selectedLight();if(!target)return;target[field.key]=Number(input.value);markChanged();updateValues();});container.append(row);}}
 makeSliders(globalFields,byId('lightingGlobalControls'),'global');makeSliders(lightFields,byId('lightingLightControls'),'lamp');
 const selectedLight=()=>editor.draft?.lights.find(light=>light.id===editor.selected);
 const geometryChanged=()=>!!photo()?.lighting_geometry&&!!editor.prepared&&editor.prepared.photo_id===photo()?.id&&editor.prepared.geometry_key!==photo().lighting_geometry;
 const dirty=()=>!!editor.draft&&!!editor.saved&&(JSON.stringify(editor.draft)!==JSON.stringify(editor.saved)||geometryChanged());
 const active=()=>!!editor.job&&['queued','running','cancelling'].includes(editor.job.status);
 const restartMessage='软件更新尚未生效，请关闭所有照影窗口后重新打开。';
 function compatibilityIssue(){return editor.status&&!['canonical_preview','refresh_geometry','geometry_restore'].every(key=>editor.status.capabilities?.[key]===true)?restartMessage:null;}
 function syncLightingToast(message,begin=false){const banner=byId('toasts').firstElementChild;if(!banner||banner.dataset.lightingPhoto!==state.current||banner.classList.contains('error'))return;if(begin)banner.dataset.lightingState='preview';if(banner.dataset.lightingState==='preview')banner.textContent=message;}
 function feedback(message,error=false){byId('lightingFeedback').textContent=message;byId('lightingFeedback').classList.toggle('error',error);syncLightingToast(message);renderCanvasStatus();document.dispatchEvent(new CustomEvent('portraitlab-lighting-preview-state',{detail:{status:error?'failed':editor.previewPending?'rendering':inactiveReason()?'inactive':'ready',message,dirty:dirty(),selection:editor.showSelection,scope:editor.draft?.scope,strength:editor.draft?.strength}}));}
 function renderCanvasStatus(){canvasStatus.hidden=state.module!=='lighting'||!photo();if(canvasStatus.hidden)return;const scope={subject:'仅主体',background:'仅背景',all:'整张照片'}[editor.draft?.scope]||'仅主体',ready=!!editor.images&&editor.prepared?.photo_id===state.current,shown=editor.panes.some(visible),reason=inactiveReason(),compatibility=compatibilityIssue();let label,message,status='ready';if(editor.previewError){label='预览失败';message=editor.previewError;status='failed';}else if(editor.saving){label='正在保留';message='正在保存当前布光调整…';status='rendering';}else if(!ready){label=compatibility?'等待重启':active()||editor.loading?'正在分析':'等待分析';message=compatibility||editor.error||'照片分析完成后，可在画布上拖动光源。';status=compatibility||editor.error?'failed':'rendering';}else if(editor.previewPending){label='正在预览';message='正在更新光线与画面…';status='rendering';}else if(!shown){label='原图视图';message='当前查看原图；调整灯光时将切换到处理结果。';}else if(editor.showSelection){label='选区叠加';message='非最终效果 · 关闭“显示作用选区”查看实际布光照片。';status='selection';}else if(reason){label='布光未生效';message=reason;status='inactive';}else{label=dirty()?'实时预览':'已保留';message=`${scope} · 整体强度 ${Math.round(editor.draft.strength*100)}%${dirty()?' · 尚未保留':''}`;}canvasStatus.dataset.state=status;byId('lightingCanvasState').textContent=label;byId('lightingCanvasMessage').textContent=message;}
 function inactiveReason(){const p=editor.draft;if(!p)return null;if(!p.enabled)return '布光已关闭，当前显示修片底图。勾选“启用布光”后查看灯光效果。';if(p.strength<=0)return '整体强度为 0，当前显示修片底图。提高整体强度后查看灯光效果。';if(!p.lights.length)return '尚未添加光源。请选择灯光预设，或添加光源。';if(!p.lights.some(l=>l.enabled!==false&&l.intensity>0))return '所有光源均已关闭或强度为 0，当前显示修片底图。启用光源后继续布光。';return null;}
 function previewMessage(){const reason=inactiveReason();if(reason)return reason+(dirty()?' 当前调整尚未保留。':'');if(editor.showSelection)return '选区叠加 · 非最终效果。关闭“显示作用选区”后查看实际布光照片。';const scope={subject:'仅主体',background:'仅背景',all:'整张照片'}[editor.draft?.scope];return dirty()?`预览已更新 · ${scope} · 整体强度 ${Math.round(editor.draft.strength*100)}%，尚未保留。`:'已恢复上次保留的布光。';}
 function revealEditableView(){if(previewHolding)holdBefore(false);const resultViews=['result','compare'];if(!resultViews.includes(state.view)&&!(state.layout==='dual'&&resultViews.includes(state.secondaryView))){setView('result',state.layout==='dual'?'secondary':'primary');return true;}return false;}
 function fromPhoto(p){return copy(p?.lighting||editor.status?.defaults||fallbackConfig);}
 function updateValues(){
  const lamp=selectedLight();for(const [scope,fields,target] of [['global',globalFields,editor.draft],['lamp',lightFields,lamp]])for(const field of fields){const input=byId(`lighting-${scope}-${field.key}`),row=input.closest('.light-slider');row.hidden=!!field.only&&!field.only.includes(lamp?.type);if(target){const value=Number(target[field.key]);input.value=value;row.querySelector('output').textContent=field.format(value);input.style.setProperty('--range-pct',`${(value-field.min)/(field.max-field.min)*100}%`);}}
  if(editor.draft){byId('lightingEnabled').checked=editor.draft.enabled;byId('lightingScope').value=editor.draft.scope;byId('lightingScopeHint').textContent={subject:'头发、衣物和身体均属于主体；选区外保留原来的像素。',background:'新增灯光仅作用于背景；主体选区保留原来的像素。',all:'新增灯光作用于整张照片，包括背景。'}[editor.draft.scope];}
  byId('lightingDetail').hidden=!lamp;if(lamp)byId('lightingType').value=lamp.type;
 }
 function renderLights(){const list=byId('lightingLights');list.replaceChildren();const lights=editor.draft?.lights||[];byId('lightingLightCount').textContent=lights.length+' / 4';
  if(!lights.length){const text=document.createElement('p');text.className='light-note';text.textContent='尚未添加光源。可以先选择上方预设。';list.append(text);}
  lights.forEach((light,index)=>{const item=document.createElement('div');item.className='light-list-item'+(light.id===editor.selected?' selected':'');const enable=document.createElement('input');enable.type='checkbox';enable.checked=light.enabled;enable.setAttribute('aria-label',`启用光源 ${index+1}`);enable.onchange=()=>{light.enabled=enable.checked;markChanged();};const button=document.createElement('button');button.type='button';button.textContent=`${index+1} · ${types[light.type]}`;button.setAttribute('aria-pressed',String(light.id===editor.selected));button.onclick=()=>{editor.selected=light.id;renderLights();updateWidgets();};item.append(enable,button);list.append(item);});updateValues();}
 function renderPresets(){const list=byId('lightingPresets');list.replaceChildren();for(const preset of editor.status?.presets||[]){const button=document.createElement('button');button.type='button';button.dataset.lightingPreset=preset.id;button.classList.toggle('active',preset.id===editor.preset);button.setAttribute('aria-pressed',String(preset.id===editor.preset));const title=document.createElement('strong');title.textContent=preset.name;const note=document.createElement('span');note.textContent=preset.description;button.append(title,note);button.onclick=()=>{const scope=editor.draft?.scope||'subject';editor.draft=copy(preset.lighting);editor.draft.scope=scope;editor.selected=editor.draft.lights[0]?.id||null;editor.preset=preset.id;markChanged(false,`已预览“${preset.name}”，点击“保留调整”应用到当前照片。`);renderLights();renderPresets();byId('lightingPresetHint').textContent=preset.description.replace(/[。；;.!！\s]+$/u,'')+'；保留当前作用范围，可在画布或参数区继续调整。';};list.append(button);}}
 function renderStatus(){
  const p=photo(),ready=editor.prepared?.photo_id===p?.id&&!!editor.images,working=active()||editor.loading,compatibility=compatibilityIssue(),error=editor.error||editor.statusError;
  byId('lightingControls').disabled=!ready||editor.saving||state.locked;
  byId('lightingAdd').disabled=!ready||(editor.draft?.lights.length||0)>=4||editor.saving||working||state.locked;
  byId('lightingPrepare').disabled=!!compatibility||!p||!editor.status?.ready||!editor.status?.runtime_ready||working||editor.saving||state.locked;
  byId('lightingDevice').disabled=!!compatibility||working||editor.saving||state.locked;
  const cuda=editor.status?.cuda_available??state.hardware?.cuda_available;byId('lightingDevice').querySelector('[value="cuda"]').disabled=cuda===false;
  byId('lightingDownload').disabled=!editor.status?.runtime_ready||working||editor.saving||state.locked;byId('lightingDownload').hidden=!!editor.status?.ready;
  byId('lightingPrepare').textContent=compatibility?'重新打开软件后分析':ready?'重新分析照片':'分析当前照片';byId('lightingDownload').textContent=error?'重新准备模型':'下载布光模型';
  byId('lightingCancelJob').hidden=!active();byId('lightingCancelJob').disabled=editor.job?.status==='cancelling';
  byId('lightingProgress').hidden=!active();byId('lightingProgress').querySelector('span').style.width=`${limit((editor.job?.progress?.fraction||0)*100,3,100)}%`;
  const title=compatibility?'等待重启':error?'准备失败':working?(editor.job?.action==='download'?'正在准备模型':'正在分析照片'):ready?'分析已就绪':!p?'尚未选择照片':editor.status?.ready?'可以分析':editor.status?.runtime_ready?'模型未下载':'需要运行组件';
  byId('lightingPrepareState').textContent=title;
  byId('lightingPrepareMessage').textContent=compatibility||error||(working?(editor.job?.progress?.message||'正在准备，请稍候…'):ready?'深度与主体选区已缓存。拖动光源可直接预览。':editor.status?.message||'首次分析需要准备布光模型。');
  const warnings=ready?editor.prepared.meta?.warnings||[]:[];byId('lightingAnalysisWarnings').hidden=!warnings.length;byId('lightingAnalysisWarnings').textContent=warnings.join(' ');
  byId('lightingDraftState').textContent=editor.saving?'正在保留':editor.previewError?'预览失败':editor.previewPending?'正在预览':dirty()?'尚未保留':ready?(inactiveReason()?'布光未生效':'已保留'):compatibility?'等待重启':p?'等待分析':'选择照片';
  byId('lightingApply').disabled=!ready||!dirty()||editor.previewPending||!!editor.previewError||editor.saving||working||state.locked;byId('lightingApply').textContent=editor.saving?'正在保留…':'保留调整';byId('lightingDiscard').disabled=!dirty()||editor.saving||state.locked;
  for(const id of ['lightingImport','lightingImportFolder'])byId(id).disabled=state.locked||state.importing||editor.saving;
  const status=byId('settingsAIState'),message=byId('settingsAIMessage'),download=byId('settingsAIDownload');if(status){status.textContent=compatibility?'等待重启':error?'准备失败':working?title:editor.status?.ready?'布光模型已就绪':editor.status?.runtime_ready?'布光模型未下载':'需要运行组件';status.dataset.state=compatibility||error?'failed':working?'running':editor.status?.ready?'ready':'missing';message.textContent=compatibility||error||(working?editor.job?.progress?.message:null)||editor.status?.message||'正在检查布光模型…';download.textContent=editor.status?.ready?'检查模型状态':'下载布光模型';download.disabled=working||editor.saving||state.locked||!editor.status?.runtime_ready;}
  state.lightingTask=working?title:null;updateConnectionStatus();renderCanvasStatus();
 }
 function markChanged(clearPreset=true,message=null,reveal=true,hideSelection=true){if(clearPreset){editor.preset=null;document.querySelectorAll('[data-lighting-preset]').forEach(button=>{button.classList.remove('active');button.setAttribute('aria-pressed','false');});}const changedView=reveal&&revealEditableView(),selectionHidden=hideSelection&&editor.showSelection;if(selectionHidden){editor.showSelection=false;byId('lightingShowSelection').checked=false;editor.panes.forEach(pane=>pane.selection.hidden=true);editor.previewNotice='已关闭选区叠加，显示实际布光效果。 ';}else if(changedView)editor.previewNotice='已切换到处理结果。 ';editor.previewPending=true;editor.previewError=null;editor.previewMessage=message;renderStatus();scheduleDraw();updateWidgets();syncLightingToast('正在更新布光预览…',true);feedback(selectionHidden?'已关闭选区叠加，正在更新布光预览…':changedView?'已切换到处理结果，正在更新布光预览…':'正在更新布光预览…');}
 function ensurePanes(){if(editor.panes.length)return;for(const [name,stageId,originalId] of [['primary','stage','originalImage'],['secondary','secondaryStage','secondaryOriginalImage']]){const stage=byId(stageId),canvas=document.createElement('canvas');canvas.className='lighting-preview';canvas.setAttribute('aria-label','当前布光预览');canvas.hidden=true;stage.insertBefore(canvas,byId(originalId));const selection=document.createElement('canvas');selection.className='lighting-selection';selection.setAttribute('aria-label','灯光作用选区高亮');selection.hidden=true;stage.insertBefore(selection,byId(originalId));const overlay=document.createElement('div');overlay.className='lighting-widget-layer';overlay.hidden=true;stage.append(overlay);editor.panes.push({name,stage,canvas,selection,overlay});}}
 function visible(pane){return state.module==='lighting'&&editor.prepared?.photo_id===state.current&&!!editor.images&&!previewHolding&&(pane.name==='primary'?['compare','result'].includes(state.view):state.layout==='dual'&&['compare','result'].includes(state.secondaryView));}
 function refreshPreview(){ensurePanes();for(const pane of editor.panes){const show=visible(pane);pane.canvas.hidden=!show;pane.selection.hidden=!show||!editor.showSelection;pane.overlay.hidden=!show;}updateWidgets();renderCanvasStatus();if(editor.images)scheduleDraw();}
 function selectionMap(width,height){const scope=editor.draft.scope,key=`${scope}:${width}:${height}`;if(editor.selectionMap?.key===key)return editor.selectionMap.canvas;const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const context=canvas.getContext('2d',{willReadFrequently:true});context.drawImage(editor.images.mask,0,0,width,height);const pixels=context.getImageData(0,0,width,height);for(let i=0;i<pixels.data.length;i+=4){const mask=pixels.data[i]/255,amount=scope==='all'?1:scope==='background'?1-mask:mask;pixels.data[i]=76;pixels.data[i+1]=210;pixels.data[i+2]=230;pixels.data[i+3]=Math.round(amount*85);}context.putImageData(pixels,0,0);editor.selectionMap={key,canvas};return canvas;}
 function scheduleDraw(){
  if(editor.frame||!editor.images||!editor.draft)return;
  editor.frame=requestAnimationFrame(()=>{
   editor.frame=null;if(!editor.images||!editor.draft)return;
   try{
    const rendered=editor.renderer.render(editor.draft),selection=editor.showSelection?selectionMap(rendered.width,rendered.height):null;let drawn=0;
    for(const pane of editor.panes){if(pane.canvas.width!==rendered.width||pane.canvas.height!==rendered.height){pane.canvas.width=rendered.width;pane.canvas.height=rendered.height;}pane.canvas.getContext('2d').drawImage(rendered,0,0);drawn++;pane.selection.hidden=!selection||!visible(pane);if(selection){pane.selection.width=rendered.width;pane.selection.height=rendered.height;pane.selection.getContext('2d').drawImage(selection,0,0);}}
    byId('lightingPreviewDevice').textContent=editor.renderer.mode==='webgl2'?`图形加速预览 · ${rendered.width} × ${rendered.height}`:`CPU 预览 · ${rendered.width} × ${rendered.height}`;editor.lastFrame=performance.now();
    if((editor.previewPending||editor.previewError)&&drawn){editor.previewPending=false;editor.previewError=null;feedback((editor.previewNotice||'')+(inactiveReason()||editor.showSelection?previewMessage():(editor.previewMessage||previewMessage())));editor.previewMessage=null;editor.previewNotice=null;renderStatus();}
   }catch(error){editor.previewPending=false;editor.previewError=errorMessage(error);editor.previewMessage=null;editor.previewNotice=null;renderStatus();feedback('预览失败：'+editor.previewError,true);}
  });
 }
 function projected(light,aspect){if(light.type!=='directional')return [light.x,light.y];const az=light.azimuth*Math.PI/180,el=light.elevation*Math.PI/180;return [.5+Math.cos(az)*Math.cos(el)*.45/aspect,.5+Math.sin(az)*Math.cos(el)*.45];}
 function updateWidgets(){
  if(!editor.panes.length)return;const lights=editor.draft?.lights||[];
  for(const pane of editor.panes){const show=visible(pane),keys=new Set();pane.overlay.hidden=!show;if(!show)continue;
   lights.forEach((light,index)=>{for(const target of [false,...(light.type==='spot'&&light.id===editor.selected?[true]:[])]){const key=light.id+(target?'-target':'');keys.add(key);let button=Array.from(pane.overlay.children).find(item=>item.dataset.widgetKey===key);if(!button){button=document.createElement('button');button.type='button';button.className='light-widget'+(target?' light-target':'');button.dataset.widgetKey=key;pane.overlay.append(button);bindWidget(button,pane,light.id,target);}
    button.textContent=target?'＋':String(index+1);button.setAttribute('aria-label',target?`光源 ${index+1} 的照射目标`:`光源 ${index+1}，${types[light.type]}，拖动调整位置`);button.setAttribute('aria-pressed',String(light.id===editor.selected));button.classList.toggle('selected',light.id===editor.selected);button.classList.toggle('muted',!light.enabled||!editor.draft.enabled);button.disabled=editor.saving||state.locked;
    const rect=pane.stage.getBoundingClientRect(),v=pane.stage.parentElement.parentElement.getBoundingClientRect(),aspect=(photo()?.width||1)/(photo()?.height||1),xy=target?[light.target_x,light.target_y]:projected(light,aspect),x=xy[0]*rect.width,y=xy[1]*rect.height;
    const left=Math.max(17,v.left-rect.left+17),right=Math.min(rect.width-17,v.right-rect.left-17),top=Math.max(17,v.top-rect.top+17),bottom=Math.min(rect.height-17,v.bottom-rect.top-17);
    const shownX=limit(x,Math.min(left,right),Math.max(left,right)),shownY=limit(y,Math.min(top,bottom),Math.max(top,bottom)),offscreen=Math.abs(shownX-x)>.5||Math.abs(shownY-y)>.5;
    button.style.left=shownX+'px';button.style.top=shownY+'px';button.classList.toggle('offscreen',offscreen);
    const dx=x-shownX,dy=y-shownY;button.dataset.offscreenArrow=offscreen?(Math.abs(dx)<1?(dy<0?'↑':'↓'):Math.abs(dy)<1?(dx<0?'←':'→'):dy<0?(dx<0?'↖':'↗'):(dx<0?'↙':'↘')):'';
    button.title=offscreen?'光源位于当前视野外；拖动此标记可放到画布上，方向键可微调原位置':target?'拖动照射目标；方向键微调':'拖动光源；方向键微调，Shift 加速';
   }});for(const button of pane.overlay.children)if(!keys.has(button.dataset.widgetKey))button.remove();
  }
 }
 function bindWidget(button,pane,id,target){let drag=null;
  const change=(dx,dy)=>{const light=editor.draft?.lights.find(item=>item.id===id);if(!light)return;const aspect=(photo()?.width||1)/(photo()?.height||1);if(target){light.target_x=limit(drag.originX+dx,-1,2);light.target_y=limit(drag.originY+dy,-1,2);}else if(light.type==='directional'){const x=(drag.originX+dx-.5)*aspect,y=drag.originY+dy-.5;light.azimuth=Math.atan2(y,x)*180/Math.PI;light.elevation=limit(Math.acos(limit(Math.hypot(x,y)/.45,0,1))*180/Math.PI,5,90);}else{light.x=limit(drag.originX+dx,-1,2);light.y=limit(drag.originY+dy,-1,2);}markChanged();updateValues();};
  button.addEventListener('pointerdown',event=>{if(event.button!==0||state.locked||editor.saving||previewHolding)return;event.preventDefault();event.stopPropagation();editor.dragCancel?.();const light=editor.draft.lights.find(item=>item.id===id);editor.selected=id;renderLights();const rect=pane.stage.getBoundingClientRect();drag={pointer:event.pointerId,screenX:event.clientX,screenY:event.clientY,width:rect.width,height:rect.height,originX:parseFloat(button.style.left)/rect.width,originY:parseFloat(button.style.top)/rect.height};editor.dragCancel=cancel;button.setPointerCapture(event.pointerId);button.focus({preventScroll:true});updateWidgets();});
  button.addEventListener('pointermove',event=>{if(!drag||event.pointerId!==drag.pointer)return;change((event.clientX-drag.screenX)/drag.width,(event.clientY-drag.screenY)/drag.height);});
  const finish=event=>{if(!drag||(event&&event.pointerId!==drag.pointer))return;const pointer=drag.pointer;drag=null;if(editor.dragCancel===cancel)editor.dragCancel=null;if(button.hasPointerCapture(pointer))button.releasePointerCapture(pointer);},cancel=()=>finish();for(const name of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(name,finish);
  button.addEventListener('keydown',event=>{if(state.locked||editor.saving||previewHolding||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Delete'].includes(event.key))return;event.preventDefault();event.stopPropagation();editor.selected=id;if(event.key==='Delete'){removeSelected();return;}const light=selectedLight(),xy=projected(light,(photo()?.width||1)/(photo()?.height||1)),step=event.shiftKey?.05:.01;drag={originX:target?light.target_x:xy[0],originY:target?light.target_y:xy[1]};change(({ArrowLeft:-step,ArrowRight:step})[event.key]||0,({ArrowUp:-step,ArrowDown:step})[event.key]||0);drag=null;renderLights();});
 }
 async function loadImage(url){return new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>resolve(image);image.onerror=()=>reject(new Error('无法读取布光预览，请重新分析照片。'));image.src=url;});}
 async function acceptPrepared(result,sequence){
  if(sequence!==editor.sequence||result.photo_id!==state.current||state.module!=='lighting')return;
  const compatibility=compatibilityIssue();if(compatibility){feedback(compatibility,true);renderStatus();return;}
  const p=photo();if(p.version!==result.version){editor.error='照片已调整，请重新分析后布光。';renderStatus();return;}
  const images={};await Promise.all(['base','normal','depth','mask'].map(async key=>{images[key]=await loadImage(result[key+'_url']);}));
  if(sequence!==editor.sequence||result.photo_id!==state.current||state.module!=='lighting')return;
  if(photo()?.version!==result.version){editor.error='照片已调整，请重新分析后布光。';renderStatus();return;}
  editor.renderer?.dispose();editor.renderer=new window.PortraitLightingRenderer.LightingRenderer();editor.renderer.setAssets(images);editor.images=images;editor.prepared=result;editor.photoId=result.photo_id;editor.saved=fromPhoto(p);editor.draft=copy(editor.saved);editor.selected=editor.draft.lights[0]?.id||null;editor.error=null;editor.previewPending=true;editor.previewMessage=geometryChanged()?'照片分析已更新，点击“保留调整”后应用新选区。':editor.draft.lights.length?'已载入上次保留的布光，可继续调整。':'照片分析已完成。请选择预设或添加光源。';ensurePanes();renderLights();renderPresets();renderStatus();refreshPreview();feedback('正在显示布光预览…');
 }
 async function poll(){clearTimeout(editor.pollTimer);if(!active())return;const id=editor.job.id,sequence=editor.sequence;
  try{const {job}=await api('/api/lighting/jobs/'+id);if(editor.job?.id!==id)return;editor.job=job;renderStatus();if(job.status==='completed'){if(job.action==='download'){await refreshStatus();if(state.module==='lighting'&&photo())await prepare();}else if(job.result){await acceptPrepared(job.result,sequence);if(job.result.photo_id!==state.current&&state.module==='lighting'&&photo())await prepare();}}
   else if(['failed','interrupted'].includes(job.status)){editor.error=job.error||'照片分析没有完成，请重试。';feedback(editor.error,true);renderStatus();}
   else if(job.status==='cancelled'){feedback('已停止分析，可重新开始。');renderStatus();}
  }catch(error){editor.error='无法获取分析进度：'+errorMessage(error);feedback(editor.error,true);renderStatus();}
  if(active())editor.pollTimer=setTimeout(poll,800);
 }
 async function refreshStatus(){try{editor.status=await api('/api/lighting/status');editor.statusError=null;renderPresets();if(editor.status.active_job&&!active()){editor.job=editor.status.active_job;poll();}}catch(error){editor.statusError=errorMessage(error);}renderStatus();return editor.status;}
 async function prepare({refreshGeometry=false}={}){
  const p=photo();if(!p||editor.loading||active()||editor.saving||state.locked)return false;const compatibility=compatibilityIssue();if(compatibility){feedback(compatibility,true);renderStatus();return false;}if(dirty()&&!await beforeLeave('重新分析照片',{restoreGeometry:false}))return false;
  editor.loading=true;editor.error=null;renderStatus();
  try{await flushPhotos([p.id]);if(photo()?.id!==p.id)return false;editor.sequence++;releasePrepared();refreshPreview();const {job}=await post('/api/lighting/prepare',{photo_id:p.id,device:byId('lightingDevice').value,refresh_geometry:!!refreshGeometry});editor.job=job;poll();return true;}
  catch(error){editor.error=errorMessage(error);feedback(editor.error,true);return false;}finally{editor.loading=false;renderStatus();}
 }
 async function download(){if(active()||editor.loading||state.locked)return;editor.error=null;editor.loading=true;renderStatus();try{const {job}=await post('/api/lighting/models/download',{});editor.job=job;poll();}catch(error){editor.error=errorMessage(error);feedback(editor.error,true);}finally{editor.loading=false;renderStatus();}}
 async function commit(){
  if(editor.previewPending){await new Promise(resolve=>requestAnimationFrame(resolve));if(editor.previewPending)return false;}
  if(editor.previewError)return false;
  if(!dirty())return true;const p=photo();if(editor.saving||state.locked||!editor.prepared||editor.prepared.photo_id!==p?.id)return false;
  const previous=copy({params:p.params,strokes:p.strokes||[],local_adjustment:p.local_adjustment??null,lighting:p.lighting||editor.status?.defaults||fallbackConfig,lighting_geometry:p.lighting_geometry||null});editor.saving=true;editor.error=null;renderStatus();
  try{const result=await post(`/api/photos/${p.id}/lighting`,{lighting:copy(editor.draft),expected_version:editor.prepared.version,geometry_key:editor.prepared.geometry_key});recordHistory(p.id,previous);mergePhoto(result.photo);editor.saved=fromPhoto(result.photo);editor.draft=copy(editor.saved);editor.prepared.version=result.photo.version;displayPhoto();renderLibrary();renderControls();renderLights();feedback(`已为《${p.name}》保留布光，可撤销或继续编辑。`);const banner=toast(`已为《${p.name}》保留布光调整，原文件不变。`);banner.dataset.lightingPhoto=p.id;banner.dataset.lightingState='saved';return true;}
  catch(error){feedback('保留失败：'+errorMessage(error),true);throw error;}finally{editor.saving=false;renderStatus();refreshPreview();}
 }
 async function discard({reloadGeometry=true}={}){if(!editor.saved)return;const changedGeometry=geometryChanged();editor.draft=copy(editor.saved);editor.selected=editor.draft.lights[0]?.id||null;editor.preset=null;if(changedGeometry)releasePrepared();renderLights();renderPresets();if(changedGeometry){renderStatus();refreshPreview();feedback('已恢复上次保留的布光与选区。');if(reloadGeometry&&state.module==='lighting')await prepare();}else markChanged(true,null,false,false);}
 async function beforeLeave(action,{restoreGeometry=true}={}){if(editor.saving)return false;if(!dirty())return true;if(editor.leavePromise)return editor.leavePromise;
  byId('lightingLeaveMessage').textContent=`${action||'继续操作'}前，请先保留或放弃当前照片的布光调整。`;byId('lightingLeaveError').hidden=true;
  editor.leavePromise=new Promise(resolve=>{let finished=false;const end=value=>{if(finished)return;finished=true;leave.close();editor.leavePromise=null;resolve(value);};byId('lightingLeaveStay').onclick=()=>end(false);byId('lightingLeaveDiscard').onclick=async()=>{await discard({reloadGeometry:restoreGeometry});end(true);};byId('lightingLeaveSave').onclick=async()=>{byId('lightingLeaveSave').disabled=true;byId('lightingLeaveDiscard').disabled=true;byId('lightingLeaveStay').disabled=true;try{if(await commit())end(true);}catch(error){byId('lightingLeaveError').hidden=false;byId('lightingLeaveError').textContent=errorMessage(error);}finally{byId('lightingLeaveSave').disabled=false;byId('lightingLeaveDiscard').disabled=false;byId('lightingLeaveStay').disabled=false;}};leave.oncancel=event=>{event.preventDefault();end(false);};leave.onclose=()=>{if(!finished)end(false);};leave.showModal();});return editor.leavePromise;
 }
 function releasePrepared(){editor.dragCancel?.();if(editor.frame)cancelAnimationFrame(editor.frame);editor.frame=null;editor.renderer?.dispose();editor.renderer=null;editor.prepared=null;editor.images=null;editor.selectionMap=null;editor.previewPending=false;editor.previewError=null;editor.previewMessage=null;editor.previewNotice=null;}
 function resetForPhoto(){editor.sequence++;editor.photoId=state.current;releasePrepared();editor.draft=fromPhoto(photo());editor.saved=copy(editor.draft);editor.selected=editor.draft.lights[0]?.id||null;editor.error=null;editor.preset=null;renderLights();renderStatus();refreshPreview();}
 async function open(){ensurePanes();if(editor.photoId!==state.current)resetForPhoto();await refreshStatus();if(state.module==='lighting'&&photo()&&editor.status?.ready&&!editor.prepared&&!active())await prepare();else refreshPreview();}
 function removeSelected(){if(!editor.draft||state.locked||editor.saving)return;editor.dragCancel?.();editor.draft.lights=editor.draft.lights.filter(light=>light.id!==editor.selected);editor.selected=editor.draft.lights[0]?.id||null;renderLights();markChanged();}
 byId('lightingType').onchange=event=>{const light=selectedLight();if(!light)return;light.type=event.target.value;renderLights();markChanged();};
 byId('lightingEnabled').onchange=event=>{if(!editor.draft)return;editor.draft.enabled=event.target.checked;markChanged();};byId('lightingScope').onchange=event=>{if(!editor.draft)return;editor.draft.scope=event.target.value;updateValues();markChanged(true,null,true,false);};
 byId('lightingShowSelection').onchange=event=>{editor.showSelection=event.target.checked;refreshPreview();feedback(editor.showSelection?'选区叠加 · 非最终效果。拖动光源或调整参数时将自动关闭叠加。':previewMessage());};
 byId('lightingAdd').onclick=()=>{if(!editor.draft||editor.draft.lights.length>=4)return;const light=copy(editor.status?.light_defaults||fallbackLight);light.id='light-'+(crypto.randomUUID?.()||Date.now().toString(36));light.type='area';editor.draft.lights.push(light);editor.draft.enabled=true;if(editor.draft.lights.length===1)editor.draft.ambient=.75;editor.selected=light.id;renderLights();markChanged();};
 byId('lightingRemove').onclick=removeSelected;byId('lightingApply').onclick=()=>commit().catch(()=>{});byId('lightingDiscard').onclick=()=>discard();byId('lightingPrepare').onclick=()=>prepare({refreshGeometry:true});byId('lightingDownload').onclick=download;
 byId('lightingCancelJob').onclick=async()=>{if(!active())return;try{const {job}=await post(`/api/lighting/jobs/${editor.job.id}/cancel`,{});editor.job=job;renderStatus();}catch(error){feedback(errorMessage(error),true);}};
 byId('lightingImport').onclick=()=>requestImport(false,'lighting');byId('lightingImportFolder').onclick=()=>requestImport(true,'lighting');
 if(byId('settingsAIDownload'))byId('settingsAIDownload').onclick=()=>editor.status?.ready?refreshStatus():download();
 document.addEventListener('portraitlab-ready',refreshStatus);document.addEventListener('portraitlab-settings-open',refreshStatus);document.addEventListener('portraitlab-model-state',renderStatus);document.addEventListener('portraitlab-edit-state',renderStatus);
 document.addEventListener('portraitlab-preview-geometry',updateWidgets);document.addEventListener('portraitlab-preview-view',refreshPreview);
 document.addEventListener('portraitlab-module-change',()=>{refreshPreview();renderStatus();});
 document.addEventListener('portraitlab-photo-selected',()=>{resetForPhoto();if(state.module==='lighting'&&editor.status?.ready&&!active())prepare();});
 document.addEventListener('portraitlab-photos-removed',event=>{if(event.detail?.ids?.includes(editor.photoId))resetForPhoto();});
 document.addEventListener('portraitlab-photo-updated',()=>{if(editor.saving)return;const p=photo();if(editor.prepared&&p?.id===editor.photoId&&p.version!==editor.prepared.version){releasePrepared();editor.draft=fromPhoto(p);editor.saved=copy(editor.draft);renderStatus();refreshPreview();if(state.module==='lighting'&&!active())prepare();}});
 window.addEventListener('blur',()=>editor.dragCancel?.());
 window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
 window.PortraitLighting={open,beforeLeave,isDirty:dirty,hasActiveJob:active,refreshPreview,refreshControls:renderStatus,refreshStatus,prepare,commit,discard,onPhotoSelected:resetForPhoto,diagnostics:()=>({photoId:editor.photoId,prepared:!!editor.prepared,dirty:dirty(),mode:editor.renderer?.mode,frames:editor.renderer?.frameCount,submissionMilliseconds:editor.renderer?.lastSubmissionMilliseconds,dimensions:editor.renderer?[editor.renderer.canvas.width,editor.renderer.canvas.height]:null})};
 ensurePanes();renderStatus();
})();
