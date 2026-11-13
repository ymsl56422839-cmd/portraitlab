/* Preset library and atomic batch undo. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const presetUI = {items:[], categories:[], favorites:new Set(), category:'common', query:'', applied:new Map(), saving:false, snapshot:null};
function paramsEqual(a,b){return !!a&&!!b&&Object.keys({...a,...b}).every(key=>a[key]===b[key]);}
function acceptPresetCatalog(data){
 presetUI.items=data.presets||[];presetUI.categories=data.categories||data.preset_categories||[];presetUI.favorites=new Set(data.favorites||data.preset_favorites||[]);
 renderPresetCategories();renderPresetCards();updatePresetStatus();
}
function renderPresetCategories(){
 const container=$('presetCategories');container.replaceChildren();
 for(const category of [{id:'common',name:'常用'},{id:'favorites',name:'收藏'},{id:'all',name:'全部'}]){const button=document.createElement('button');button.type='button';button.textContent=category.name;button.className=category.id===presetUI.category?'active':'';button.setAttribute('aria-pressed',String(category.id===presetUI.category));button.onclick=()=>{presetUI.category=category.id;presetUI.query='';$('presetSearch').value='';renderPresetCategories();renderPresetCards();};container.append(button);}
 const select=document.createElement('select');select.setAttribute('aria-label','按用途选择预设分类');const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='按用途分类';select.append(placeholder);for(const category of presetUI.categories){const option=document.createElement('option');option.value=category.id;option.textContent=category.name;select.append(option);}select.value=presetUI.categories.some(item=>item.id===presetUI.category)?presetUI.category:'';select.onchange=()=>{if(!select.value)return;presetUI.category=select.value;presetUI.query='';$('presetSearch').value='';renderPresetCategories();renderPresetCards();};container.append(select);
}
function visiblePresets(){
 const search=presetUI.query.trim().toLocaleLowerCase();
 const result=presetUI.items.filter(preset=>search?[preset.name,preset.description,...(preset.tags||[])].join(' ').toLocaleLowerCase().includes(search):presetUI.category==='all'||(presetUI.category==='common'&&preset.featured)||(presetUI.category==='favorites'&&presetUI.favorites.has(preset.id))||preset.category===presetUI.category);return !search&&presetUI.category==='common'?result.slice(0,4):result;
}
function renderPresetCards(){
 const container=$('presets');container.replaceChildren();const items=visiblePresets();
 $('presetSummary').textContent=presetUI.query?`找到 ${items.length} 个预设`:`${items.length} 个预设 · 点击应用到当前照片`;
 if(!items.length){const empty=document.createElement('p');empty.className='preset-empty';empty.textContent=presetUI.category==='favorites'?'点击预设旁的星标收藏。':presetUI.category==='mine'?'调整照片后，点击“保存当前效果”。':'没有匹配的预设。';container.append(empty);return;}
 for(const preset of items){
  const card=document.createElement('div');card.className='preset-card';card.dataset.preset=preset.id;
  const apply=document.createElement('button');apply.type='button';apply.className='preset-apply';apply.disabled=!photo()||state.locked;apply.title=`${preset.name}：${preset.description||"自定义参数"}。仅应用当前照片。`;
  const name=document.createElement('strong');name.textContent=preset.name;const description=document.createElement('span');description.textContent=preset.description||'自定义数值参数';apply.append(name,description);apply.onclick=()=>applyClassicPreset(preset);
  const tools=document.createElement('div');tools.className='preset-card-tools';const star=document.createElement('button');star.type='button';star.className='preset-star';const favorite=presetUI.favorites.has(preset.id);star.textContent=favorite?'★':'☆';star.setAttribute('aria-label',(favorite?'取消收藏':'收藏')+preset.name);star.setAttribute('aria-pressed',String(favorite));
  star.onclick=async()=>{star.disabled=true;try{acceptPresetCatalog(await post(`/api/presets/${encodeURIComponent(preset.id)}/favorite`,{favorite:!favorite}));}catch(error){toast(errorMessage(error),true);star.disabled=false;}};tools.append(star);
  if(!preset.builtin){const remove=document.createElement('button');remove.type='button';remove.className='preset-delete';remove.textContent='删除';remove.setAttribute('aria-label','删除自定义预设'+preset.name);remove.onclick=async()=>{if(!confirm(`删除自定义预设“${preset.name}”？已编辑的照片不会变化。`))return;remove.disabled=true;try{acceptPresetCatalog(await api(`/api/presets/${encodeURIComponent(preset.id)}`,{method:'DELETE'}));}catch(error){toast(errorMessage(error),true);remove.disabled=false;}};tools.append(remove);}
  card.append(apply,tools);container.append(card);
 }
 updatePresetStatus();
}
async function applyClassicPreset(preset){
 const p=photo();if(!p||state.locked)return;saveHistory();p.params=clone(preset.params);presetUI.applied.set(p.id,{id:preset.id,name:preset.name,params:clone(preset.params)});renderControls();
 const message=`已对《${p.name}》应用《${preset.name}》，仅当前照片；`;const feedback=toast(message+'正在更新预览。');queuePreview(p.id,0);const seq=state.seq.get(p.id);
 // A later photo, preview request or feedback message owns the interface now.
 const stillCurrent=()=>state.current===p.id&&state.seq.get(p.id)===seq&&$('toasts').firstElementChild===feedback;
 try{await runPreview(p.id);if(stillCurrent())toast(message+'预览已更新，可继续微调。');}catch(error){if(stillCurrent())toast(message+'预览更新失败：'+errorMessage(error),true);}
}
function updatePresetStatus(){
 const p=photo();let base=p&&presetUI.applied.get(p.id);const matched=p&&(presetUI.items.find(item=>item.id===base?.id&&paramsEqual(item.params,p.params))||presetUI.items.find(item=>paramsEqual(item.params,p.params)));
 if(matched){base={id:matched.id,name:matched.name,params:matched.params};presetUI.applied.set(p.id,base);}
 $('currentPresetName').textContent=!p?'尚未选择照片':matched?matched.name:base?`${base.name} · 已微调`:'手动调整';
 $('currentPresetHint').textContent=!p?'导入照片后选择预设':p.params.opacity===0?'当前显示原图效果':p.face_count===0?'未检测到人脸；全图曝光与色彩仍可调整':'仅应用当前照片 · 保护笔刷保留';
 for(const card of document.querySelectorAll('.preset-card')){const active=matched?.id===card.dataset.preset;card.classList.toggle('active',active);const button=card.querySelector('.preset-apply');button.disabled=!p||state.locked;button.setAttribute('aria-pressed',String(active));}
 for(const id of ['savePresetBtn','originalPresetBtn','presetCompareBtn'])$(id).disabled=!p||state.locked;
 $('undoBatchBtn').disabled=!state.batchUndo||state.locked;$('batchSelectionCount').textContent=`${state.selected.size} 张已选`;if(state.batchUndo)$('batchTools').open=true;
}
$('presetSearch').addEventListener('input',event=>{presetUI.query=event.target.value;renderPresetCards();});
$('originalPresetBtn').onclick=()=>{const original=presetUI.items.find(item=>item.id==='original'||item.name==='原图');if(original){applyClassicPreset(original);return;}const p=photo();if(!p||state.locked)return;saveHistory();p.params={...clone(state.boot.defaults),smooth:0,blemishes:0,even:0,texture:0,exposure:0,relight:0,warmth:0,saturation:1,opacity:0};presetUI.applied.set(p.id,{id:'original',name:'原图',params:clone(p.params)});renderControls();queuePreview(p.id,0);};
const compareButton=$('presetCompareBtn');compareButton.addEventListener('pointerdown',()=>holdBefore(true));compareButton.addEventListener('pointerleave',()=>holdBefore(false));compareButton.addEventListener('keydown',event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();holdBefore(true);}});compareButton.addEventListener('keyup',()=>holdBefore(false));compareButton.addEventListener('blur',()=>holdBefore(false));
$('savePresetBtn').onclick=()=>{if(!photo()||state.locked)return;presetUI.snapshot=clone(photo().params);$('presetName').value='';$('presetDescription').value='';$('presetSaveError').hidden=true;$('savePresetDialog').showModal();$('presetName').focus();};
$('savePresetForm').addEventListener('submit',async event=>{
 event.preventDefault();if(presetUI.saving||!presetUI.snapshot)return;const name=$('presetName').value.trim();if(!name){$('presetName').focus();return;}
 presetUI.saving=true;$('confirmSavePreset').disabled=true;$('presetSaveError').hidden=true;
 try{const result=await post('/api/presets',{name,description:$('presetDescription').value.trim(),params:presetUI.snapshot});presetUI.category='mine';presetUI.query='';$('presetSearch').value='';acceptPresetCatalog(result);$('savePresetDialog').close();toast('已保存自定义预设。');}catch(error){$('presetSaveError').textContent=errorMessage(error);$('presetSaveError').hidden=false;}finally{presetUI.saving=false;$('confirmSavePreset').disabled=false;}
});
async function applyBatchSettings(){
 const current=photo();const ids=[...state.selected];if(state.locked||!current||!ids.length)return;lockEditing(true);
 try{
  await flushPhotos([...ids,current.id]);const params=clone(photo(current.id).params);
  const snapshots=ids.map(id=>({id,params:clone(photo(id).params),expected_params:clone(params),history:clone(state.histories.get(id)||[])}));
  const result=await post('/api/settings/apply',{ids,params});state.batchUndo=snapshots;
  for(const updated of result.photos){mergePhoto(updated);state.histories.delete(updated.id);presetUI.applied.delete(updated.id);}
  $('batchHint').textContent=`已套用到 ${ids.length} 张，可撤销本次批量操作`;renderLibrary();if(ids.includes(state.current))displayPhoto();toast(`已把《${current.name}》的当前参数套用到 ${ids.length} 张，保护笔刷保留；可撤销本次批量。`);
 }catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);if(photo()?.status==='settings_changed')queuePreview(state.current,0);}
}
async function undoBatchSettings(){
 if(state.locked||!state.batchUndo)return;const snapshots=state.batchUndo;lockEditing(true);
 try{
  await flushPhotos(snapshots.map(item=>item.id));const result=await post('/api/settings/restore',{photos:snapshots.map(({id,params,expected_params})=>({id,params,expected_params}))});
  for(const updated of result.photos){mergePhoto(updated);const old=snapshots.find(item=>item.id===updated.id);state.histories.set(updated.id,old.history);presetUI.applied.delete(updated.id);}
  state.batchUndo=null;$('batchHint').textContent='已撤销批量套用；各张保护笔刷保留';renderLibrary();if(snapshots.some(item=>item.id===state.current))displayPhoto();toast(`已恢复 ${result.photos.length} 张的原有参数。`);
 }catch(error){toast('未撤销：'+errorMessage(error)+'。为避免覆盖后续编辑，本次没有改动照片。',true);}finally{lockEditing(false);if(photo()?.status==='settings_changed')queuePreview(state.current,0);}
}
$('syncBtn').onclick=applyBatchSettings;$('undoBatchBtn').onclick=undoBatchSettings;
window.PortraitPresets={initialize:()=>acceptPresetCatalog(state.boot),refresh:updatePresetStatus};
document.addEventListener('portraitlab-ready',()=>acceptPresetCatalog(state.boot));
