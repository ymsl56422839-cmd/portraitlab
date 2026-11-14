/* Preset library and atomic batch undo. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const portraitGroups = [{id:'general',name:'通用'},{id:'senior',name:'长者人像'},{id:'adult',name:'成人人像'},{id:'youth',name:'少年人像'}];
const presetUI = {items:[], categories:[], groups:portraitGroups, favorites:new Set(), group:'general', filter:'all', purpose:'all', query:'', applied:new Map(), saving:false, snapshot:null};
function paramsEqual(a,b){return !!a&&!!b&&Object.keys({...a,...b}).every(key=>key==='acne'?(a[key]??0)===(b[key]??0):a[key]===b[key]);}
function presetScopeKey(p){return `${p.id}:${window.PortraitSelection?.isEditing(p)?'local':'global'}`;}
function presetEditingBlocked(){return !photo()||state.locked||!!window.PortraitSelection?.isArmed();}
function acceptPresetCatalog(data){
 presetUI.items=data.presets||[];presetUI.categories=data.categories||data.preset_categories||[];presetUI.favorites=new Set(data.favorites||data.preset_favorites||[]);
 const groups=data.portrait_groups||data.preset_portrait_groups||[];presetUI.groups=portraitGroups.map(group=>groups.find(item=>item.id===group.id)||group);
 renderPresetCategories();renderPresetCards();updatePresetStatus();
}
function renderPresetCategories(){
 const container=$('presetCategories');container.replaceChildren();
 const groups=document.createElement('div');groups.className='preset-portrait-groups';groups.setAttribute('role','group');groups.setAttribute('aria-label','人像预设主分类');
 for(const group of presetUI.groups){const button=document.createElement('button');button.type='button';button.dataset.portraitGroup=group.id;button.textContent=group.name;const active=group.id===presetUI.group&&!['favorites','mine'].includes(presetUI.filter);button.className=active?'active':'';button.setAttribute('aria-pressed',String(active));button.title=group.description||group.name;button.onclick=()=>{presetUI.group=group.id;presetUI.filter='all';presetUI.purpose='all';presetUI.query='';$('presetSearch').value='';renderPresetCategories();renderPresetCards();};groups.append(button);}container.append(groups);
 const filters=document.createElement('div');filters.className='preset-filters';
 for(const filter of [{id:'all',name:'全部'},{id:'common',name:'常用'},{id:'favorites',name:'收藏'},{id:'mine',name:'我的预设'}]){const button=document.createElement('button');button.type='button';button.dataset.presetFilter=filter.id;button.textContent=filter.name;button.className=filter.id===presetUI.filter?'active':'';button.setAttribute('aria-pressed',String(filter.id===presetUI.filter));button.onclick=()=>{presetUI.filter=filter.id;presetUI.purpose='all';presetUI.query='';$('presetSearch').value='';renderPresetCategories();renderPresetCards();};filters.append(button);}
 const select=document.createElement('select');select.id='presetPurpose';select.setAttribute('aria-label','按用途筛选预设');const placeholder=document.createElement('option');placeholder.value='all';placeholder.textContent='所有用途';select.append(placeholder);for(const category of presetUI.categories.filter(item=>item.id!=='mine')){const option=document.createElement('option');option.value=category.id;option.textContent=category.name;select.append(option);}select.value=presetUI.purpose;select.onchange=()=>{presetUI.purpose=select.value;renderPresetCards();};filters.append(select);container.append(filters);
 const note=document.createElement('p');note.className='preset-group-note';note.textContent=presetUI.filter==='favorites'?'所有分类的收藏':presetUI.filter==='mine'?'保存当前编辑范围的参数，不复制框选位置':presetUI.groups.find(group=>group.id===presetUI.group)?.description||'按照片需要选择预设';container.append(note);
}
function visiblePresets(){
 const search=presetUI.query.trim().toLocaleLowerCase();
 return presetUI.items.filter(preset=>{
  if(presetUI.filter==='favorites'&&!presetUI.favorites.has(preset.id))return false;
  if(presetUI.filter==='mine'&&preset.builtin)return false;
  if(!search&&!['favorites','mine'].includes(presetUI.filter)&&(!preset.builtin||(preset.portrait_group||'general')!==presetUI.group))return false;
  if(presetUI.filter==='common'&&!preset.featured)return false;
  if(presetUI.purpose!=='all'&&preset.category!==presetUI.purpose)return false;
  return !search||[preset.name,preset.description,...(preset.tags||[])].join(' ').toLocaleLowerCase().includes(search);
 });
}
function renderPresetCards(){
 const container=$('presets');container.replaceChildren();const items=visiblePresets();
 $('presetSummary').textContent=`${presetUI.query?'搜索结果 · ':''}${items.length} 个预设 · ${editScopeLabel()}`;
 if(!items.length){const empty=document.createElement('p');empty.className='preset-empty';empty.textContent=presetUI.filter==='favorites'?'点击预设旁的星标收藏。':presetUI.filter==='mine'?'调整照片后，点击“保存当前效果”。':'没有匹配的预设。';container.append(empty);updatePresetStatus();return;}
 for(const preset of items){
  const card=document.createElement('div');card.className='preset-card';card.dataset.preset=preset.id;
  const apply=document.createElement('button');apply.type='button';apply.className='preset-apply';apply.disabled=presetEditingBlocked();apply.title=`${preset.name}：${preset.description||"自定义参数"}。${editScopeLabel()}。`;
  const name=document.createElement('strong');name.textContent=preset.name;const description=document.createElement('span');description.textContent=preset.description||'自定义数值参数';apply.append(name,description);apply.onclick=()=>applyClassicPreset(preset);
  const tools=document.createElement('div');tools.className='preset-card-tools';const star=document.createElement('button');star.type='button';star.className='preset-star';const favorite=presetUI.favorites.has(preset.id);star.textContent=favorite?'★':'☆';star.setAttribute('aria-label',(favorite?'取消收藏':'收藏')+preset.name);star.setAttribute('aria-pressed',String(favorite));
  star.onclick=async()=>{star.disabled=true;try{acceptPresetCatalog(await post(`/api/presets/${encodeURIComponent(preset.id)}/favorite`,{favorite:!favorite}));}catch(error){toast(errorMessage(error),true);star.disabled=false;}};tools.append(star);
  if(!preset.builtin){const remove=document.createElement('button');remove.type='button';remove.className='preset-delete';remove.textContent='删除';remove.setAttribute('aria-label','删除自定义预设'+preset.name);remove.onclick=async()=>{if(!confirm(`删除自定义预设“${preset.name}”？已编辑的照片不会变化。`))return;remove.disabled=true;try{acceptPresetCatalog(await api(`/api/presets/${encodeURIComponent(preset.id)}`,{method:'DELETE'}));}catch(error){toast(errorMessage(error),true);remove.disabled=false;}};tools.append(remove);}
  card.append(apply,tools);container.append(card);
 }
 updatePresetStatus();
}
async function applyClassicPreset(preset){
 const p=photo();if(presetEditingBlocked())return;saveHistory();setEditingParams(p,preset.params);presetUI.applied.set(presetScopeKey(p),{id:preset.id,name:preset.name,params:clone(preset.params)});renderControls();
 const message=`已对《${p.name}》应用《${preset.name}》，${editScopeLabel(p)}；`;const feedback=toast(message+'正在更新预览。');queuePreview(p.id,0);const seq=state.seq.get(p.id);
 // A later photo, preview request or feedback message owns the interface now.
 const stillCurrent=()=>state.current===p.id&&state.seq.get(p.id)===seq&&$('toasts').firstElementChild===feedback;
 try{await runPreview(p.id);if(stillCurrent())toast(message+'预览已更新，可继续微调。');}catch(error){if(stillCurrent())toast(message+'预览更新失败：'+errorMessage(error),true);}
}
function updatePresetStatus(){
 const p=photo();const params=editingParams(p);let base=p&&presetUI.applied.get(presetScopeKey(p));const matched=p&&(presetUI.items.find(item=>item.id===base?.id&&paramsEqual(item.params,params))||presetUI.items.find(item=>paramsEqual(item.params,params)));
 if(matched){base={id:matched.id,name:matched.name,params:matched.params};presetUI.applied.set(presetScopeKey(p),base);}
 $('currentPresetName').textContent=!p?'尚未选择照片':matched?(matched.id==='original'&&window.PortraitSelection?.isEditing(p)?'选区未增加调整':matched.name):base?`${base.name} · 已微调`:'手动调整';
 $('currentPresetHint').textContent=!p?'导入照片后选择预设':window.PortraitSelection?.isArmed()?'先拖出矩形选区，再应用预设':params.opacity===0?(window.PortraitSelection?.isEditing(p)?'选区未增加调整，保留全局修片':'当前显示原图效果'):`${editScopeLabel(p)} · 保护笔刷保留`;
 for(const card of document.querySelectorAll('.preset-card')){const active=matched?.id===card.dataset.preset;card.classList.toggle('active',active);const button=card.querySelector('.preset-apply');const recipe=presetUI.items.find(item=>item.id===card.dataset.preset);button.disabled=presetEditingBlocked();button.title=`${recipe?.name||'预设'}：${recipe?.description||'自定义参数'}。${editScopeLabel(p)}。`;button.setAttribute('aria-pressed',String(active));}
 for(const id of ['savePresetBtn','originalPresetBtn'])$(id).disabled=presetEditingBlocked();$('presetCompareBtn').disabled=!p||state.locked;
 const selecting=!!window.PortraitSelection?.isSelecting();$('syncBtn').disabled=!p||!state.selected.size||state.locked||selecting;$('undoBatchBtn').disabled=!state.batchUndo||state.locked||selecting;$('batchSelectionCount').textContent=`${state.selected.size} 张已选`;if(state.batchUndo)$('batchTools').open=true;
 $('presetSummary').textContent=`${presetUI.query?'搜索结果 · ':''}${visiblePresets().length} 个预设 · ${p?editScopeLabel(p):'导入照片后应用'}`;
}
$('presetSearch').addEventListener('input',event=>{presetUI.query=event.target.value;renderPresetCards();});
$('originalPresetBtn').onclick=()=>{if(presetEditingBlocked())return;const original=presetUI.items.find(item=>item.id==='original'||item.name==='原图');if(original){applyClassicPreset(original);return;}const p=photo();saveHistory();setEditingParams(p,{...clone(state.boot.defaults),smooth:0,blemishes:0,acne:0,even:0,texture:0,exposure:0,relight:0,warmth:0,saturation:1,opacity:0});presetUI.applied.set(presetScopeKey(p),{id:'original',name:'原图',params:clone(editingParams(p))});renderControls();queuePreview(p.id,0);};
const compareButton=$('presetCompareBtn');compareButton.addEventListener('pointerdown',()=>holdBefore(true));compareButton.addEventListener('pointerleave',()=>holdBefore(false));compareButton.addEventListener('keydown',event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();holdBefore(true);}});compareButton.addEventListener('keyup',()=>holdBefore(false));compareButton.addEventListener('blur',()=>holdBefore(false));
$('savePresetBtn').onclick=()=>{if(presetEditingBlocked())return;presetUI.snapshot=clone(editingParams());$('presetName').value='';$('presetDescription').value='';$('presetSaveError').hidden=true;$('savePresetDialog').showModal();$('presetName').focus();};
$('savePresetForm').addEventListener('submit',async event=>{
 event.preventDefault();if(presetUI.saving||!presetUI.snapshot||presetEditingBlocked())return;const name=$('presetName').value.trim();if(!name){$('presetName').focus();return;}
 presetUI.saving=true;$('confirmSavePreset').disabled=true;$('presetSaveError').hidden=true;
 try{const result=await post('/api/presets',{name,description:$('presetDescription').value.trim(),params:presetUI.snapshot});presetUI.filter='mine';presetUI.purpose='all';presetUI.query='';$('presetSearch').value='';acceptPresetCatalog(result);$('savePresetDialog').close();toast('已保存当前编辑范围的参数；未保存框选位置或保护笔刷。');}catch(error){$('presetSaveError').textContent=errorMessage(error);$('presetSaveError').hidden=false;}finally{presetUI.saving=false;$('confirmSavePreset').disabled=false;}
});
async function applyBatchSettings(){
 const current=photo();const ids=[...state.selected];if(state.locked||!current||!ids.length||window.PortraitSelection?.isSelecting())return;lockEditing(true);
 try{
  await flushPhotos([...ids,current.id]);const params=clone(photo(current.id).params);
  const snapshots=ids.map(id=>({id,params:clone(photo(id).params),expected_params:clone(params),history:clone(state.histories.get(id)||[])}));
  const result=await post('/api/settings/apply',{ids,params});state.batchUndo=snapshots;
  for(const updated of result.photos){mergePhoto(updated);state.histories.delete(updated.id);presetUI.applied.delete(`${updated.id}:global`);}
  $('batchHint').textContent=`已套用全局参数到 ${ids.length} 张，可撤销本次批量操作`;renderLibrary();if(ids.includes(state.current))displayPhoto();toast(`已把《${current.name}》的全局参数套用到 ${ids.length} 张，各张局部调整和保护笔刷保留；可撤销本次批量。`);
 }catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);if(photo()?.status==='settings_changed')queuePreview(state.current,0);}
}
async function undoBatchSettings(){
 if(state.locked||!state.batchUndo||window.PortraitSelection?.isSelecting())return;const snapshots=state.batchUndo;lockEditing(true);
 try{
  await flushPhotos(snapshots.map(item=>item.id));const result=await post('/api/settings/restore',{photos:snapshots.map(({id,params,expected_params})=>({id,params,expected_params}))});
  for(const updated of result.photos){mergePhoto(updated);const old=snapshots.find(item=>item.id===updated.id);state.histories.set(updated.id,old.history);presetUI.applied.delete(`${updated.id}:global`);}
  state.batchUndo=null;$('batchHint').textContent='已撤销批量套用；各张保护笔刷保留';renderLibrary();if(snapshots.some(item=>item.id===state.current))displayPhoto();toast(`已恢复 ${result.photos.length} 张的原有参数。`);
 }catch(error){toast('未撤销：'+errorMessage(error)+'。为避免覆盖后续编辑，本次没有改动照片。',true);}finally{lockEditing(false);if(photo()?.status==='settings_changed')queuePreview(state.current,0);}
}
$('syncBtn').onclick=applyBatchSettings;$('undoBatchBtn').onclick=undoBatchSettings;
window.PortraitPresets={initialize:()=>acceptPresetCatalog(state.boot),refresh:updatePresetStatus};
document.addEventListener('portraitlab-ready',()=>acceptPresetCatalog(state.boot));
document.addEventListener('portraitlab-edit-state',updatePresetStatus);
