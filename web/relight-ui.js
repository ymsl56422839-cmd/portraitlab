/* Optional IC-Light controls. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const ai = {status:null, job:null, timer:null, ids:[], imported:new Set()};
const aiMarkup = `
<dialog id="aiDialog" class="ai-dialog">
 <div class="dialog-heading"><div><span class="eyebrow">GENERATIVE RELIGHTING</span><h2>为人像，重新布一束光。</h2></div><button id="aiClose" class="icon-button" aria-label="关闭">×</button></div>
 <p class="ai-intro">对 <b id="aiCount">0</b> 张照片逐张运行 IC-Light。使用当前修肤结果与人物前景，生成独立的新照片。</p>
 <div class="ai-presets"><button data-light="window">窗边柔光</button><button data-light="studio">影棚轮廓光</button><button data-light="sunset">日落暖光</button><button data-light="cinema">冷暖电影光</button></div>
 <label class="field-label">光线描述<textarea id="aiPrompt" maxlength="2000">portrait, natural soft window light, realistic skin texture, photographic</textarea><small>英文提示词通常更稳定；选择上方预设可以直接使用。</small></label>
 <div class="ai-grid">
  <label class="field-label">主光方向<select id="aiDirection"><option value="left">从左侧照入</option><option value="right">从右侧照入</option><option value="top">从上方照入</option><option value="bottom">从下方照入</option><option value="none">由提示词决定</option></select></label>
  <label class="field-label">生成尺寸<select id="aiSize"><option value="512">长边 512 · 低显存推荐</option><option value="768">长边 768 · 平衡</option><option value="1024">长边 1024 · 更多显存</option></select></label>
  <label class="field-label">运行设备<select id="aiDevice"><option value="auto">自动选择</option><option value="cuda">NVIDIA GPU（CUDA）</option><option value="cpu">CPU · 速度较慢</option></select></label>
  <label class="field-label">随机种子<input id="aiSeed" type="number" min="-1" max="4294967295" step="1" value="12345"><small>-1 随机；同批照片依次加 1，实际值随结果保存。</small></label>
 </div>
 <label class="ai-check"><input id="aiLowVram" type="checkbox" checked>低显存模式 · 减少显存占用，速度可能较慢</label>
 <details><summary>更多参数</summary><div class="ai-grid"><label class="field-label">每阶段采样步数<input id="aiSteps" type="number" min="10" max="50" value="20"></label><label class="field-label">二次细化强度<input id="aiDenoise" type="number" min="0.1" max="0.9" step="0.05" value="0.5"></label></div><label class="field-label">负向提示词<textarea id="aiNegative" maxlength="2000">low quality, blurry, deformed face, extra fingers, oversaturated</textarea></label></details>
 <div class="ai-state"><div id="aiStatusText">正在检查 AI 组件…</div><button id="aiDownload" hidden>下载并校验模型</button> <button id="aiProbe" class="text-button">检查硬件</button></div>
 <p class="ai-license">生成模型可能改变五官、发丝、衣物和背景；修肤保护笔刷不会约束生成模型。生成尺寸与原片分辨率不同，人物前景用于条件引导。模型使用须遵守 <a href="/licenses/CreativeML-OpenRAIL-M.txt" target="_blank" rel="noreferrer">CreativeML OpenRAIL-M</a>，首次模型下载约 3.6 GiB。</p>
 <div class="dialog-actions"><button id="aiCancelDialog">返回编辑</button><button id="aiStart" class="ai-button" disabled>生成新照片</button></div>
</dialog>
<div id="aiJobPanel" class="ai-job" hidden><div class="job-heading"><span><span id="aiJobIcon" class="spinner"></span><strong id="aiJobTitle">正在准备</strong></span><button id="aiJobHide" class="text-button" aria-label="收起AI任务">×</button></div><div class="job-progress"><div id="aiProgress"></div></div><div id="aiJobDetails" class="job-details" aria-live="polite"></div><div class="job-actions"><button id="aiJobStop" class="text-button">停止任务</button><button id="aiViewResult" class="ai-button" hidden>查看生成照片</button></div></div>`;
document.body.insertAdjacentHTML('beforeend', aiMarkup);
const lightPresets = {
 window:['portrait, soft natural window light, gentle shadows, realistic skin texture, photographic','left'],
 studio:['portrait, studio lighting, soft key light, delicate rim light, dark studio background, photographic','right'],
 sunset:['portrait, warm golden hour sunlight, soft sunset glow, natural skin texture, photographic','left'],
 cinema:['portrait, cinematic lighting, warm key light, cool blue fill light, realistic skin texture, photographic','right']
};
document.querySelectorAll('[data-light]').forEach(button=>button.onclick=()=>{const [prompt,direction]=lightPresets[button.dataset.light];$('aiPrompt').value=prompt;$('aiDirection').value=direction;});
function aiButtons(){const active=ai.job&&activeStatuses.includes(ai.job.status);$('relightBtn').disabled=!state.token;$('aiStart').disabled=active||!ai.status?.ready||!ai.ids.length;$('aiDownload').disabled=active||!ai.status?.runtime_ready;$('aiDownload').hidden=!!ai.status?.ready||!ai.status?.runtime_ready;}
async function refreshAI(probe=false){
 $('aiStatusText').textContent=probe?'正在检测运行设备…':'正在检查 AI 组件…';
 try{ai.status=await api('/api/relight/status'+(probe?'?probe=true':''));const s=ai.status;let message=s.message||'状态已更新';if(s.hardware_checked){if(s.cuda_available){message+=' · '+s.gpu_name+' · '+(s.gpu_total_bytes/1024**3).toFixed(1)+' GiB 显存';message+=s.gpu_total_bytes<10*1024**3?'，建议 512 档与低显存模式。':'，建议从 512 或 768 档开始。';}else message+=' · 将使用 CPU，生成速度较慢。';}$('aiStatusText').textContent=message;}catch(error){$('aiStatusText').textContent=errorMessage(error);}aiButtons();
}
async function openAI(){if(!state.token)return;ai.ids=choices();$('aiCount').textContent=ai.ids.length;$('aiDialog').showModal();aiButtons();await refreshAI(!ai.status?.hardware_checked);}
$('relightBtn').onclick=openAI;$('relightPanelBtn').onclick=openAI;$('aiClose').onclick=()=>$('aiDialog').close();$('aiCancelDialog').onclick=()=>$('aiDialog').close();$('aiProbe').onclick=()=>refreshAI(true);
$('aiDownload').onclick=async()=>{try{const {job}=await post('/api/relight/download',{});showAIJob(job);pollAI();aiButtons();}catch(error){toast(errorMessage(error),true);}};
$('aiStart').onclick=async()=>{
 const ids=[...ai.ids];if(!ids.length)return;lockEditing(true);$('aiStart').disabled=true;
 try{await flushPhotos(ids);const params={prompt:$('aiPrompt').value.trim(),negative_prompt:$('aiNegative').value, direction:$('aiDirection').value,long_side:Number($('aiSize').value),device:$('aiDevice').value,seed:Number($('aiSeed').value),steps:Number($('aiSteps').value),denoise:Number($('aiDenoise').value),cfg:2,low_vram:$('aiLowVram').checked};const {job}=await post('/api/relight/jobs',{ids,params});$('aiDialog').close();showAIJob(job);pollAI();}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);aiButtons();}
};
function showAIJob(job){
 ai.job=job;$('aiJobPanel').hidden=false;const active=activeStatuses.includes(job.status);$('aiJobIcon').className=active?'spinner':'';$('aiJobIcon').textContent=active?'':job.status==='completed'?'✓':'!';const label=job.action==='download'?'模型准备':'AI 重打光';$('aiJobTitle').textContent=label+' · '+({queued:'排队中',running:'处理中',cancelling:'正在停止',completed:'已完成',completed_with_errors:'部分失败',failed:'失败',cancelled:'已停止',interrupted:'已中断'}[job.status]||job.status);
 const p=job.progress||{};const fraction=p.total>0?Math.min(1,p.current/p.total):0;const overall=job.action==='download'?fraction:Math.min(1,(job.done+(active?fraction:0))/Math.max(1,job.total));$('aiProgress').style.width=(job.status==='completed'?100:overall*100)+'%';
 const lines=[job.action==='generate'?`${job.done} / ${job.total} 张 · ${job.params.long_side} 像素档`:'模型只需准备一次'];if(active&&p.message)lines.push(p.message);if(job.error)lines.push(job.error);for(const item of job.items||[])if(item.error)lines.push(item.name+'：'+item.error);if(!active&&job.status==='completed'&&job.action==='generate')lines.push('新照片已加入照片库，原片保持不变。');$('aiJobDetails').textContent=lines.join('\n');$('aiJobDetails').style.whiteSpace='pre-line';$('aiJobStop').hidden=!active;$('aiJobStop').disabled=job.status==='cancelling';$('aiViewResult').hidden=active||!job.items?.some(i=>i.photo_id);aiButtons();
}
async function importAIResults(job){const ids=(job.items||[]).filter(i=>i.photo_id&&!ai.imported.has(i.photo_id)).map(i=>i.photo_id);if(!ids.length)return;const result=await api('/api/photos');for(const p of result.photos)if(ids.includes(p.id)){if(!photo(p.id))mergePhoto(p);ai.imported.add(p.id);}renderLibrary();}
async function pollAI(){clearTimeout(ai.timer);if(!ai.job||!activeStatuses.includes(ai.job.status))return;try{const {job}=await api('/api/relight/jobs/'+ai.job.id);showAIJob(job);await importAIResults(job);if(!activeStatuses.includes(job.status)){await refreshAI();if(job.status==='completed')toast(job.action==='generate'?'重打光结果已加入照片库。':'AI 模型已准备。');}}catch(error){toast(errorMessage(error),true);}if(ai.job&&activeStatuses.includes(ai.job.status))ai.timer=setTimeout(pollAI,1200);}
$('aiJobStop').onclick=async()=>{try{const {job}=await post('/api/relight/jobs/'+ai.job.id+'/cancel',{});showAIJob(job);}catch(error){toast(errorMessage(error),true);}};$('aiJobHide').onclick=()=>$('aiJobPanel').hidden=true;
$('aiViewResult').onclick=async()=>{const item=ai.job?.items.find(i=>i.photo_id);if(item){state.selected=new Set([item.photo_id]);await selectPhoto(item.photo_id);}};
document.addEventListener('portraitlab-ready',async()=>{aiButtons();try{const {jobs}=await api('/api/relight/jobs');const job=jobs.find(j=>activeStatuses.includes(j.status));if(job){showAIJob(job);pollAI();}}catch(error){toast(errorMessage(error),true);}});

let classicModelTimer;
async function refreshClassicModels(){
 try{const result=await api('/api/models/status');state.boot.models=result.models;modelInfo();const install=result.installation;const active=install.status==='running';$('downloadClassicModels').disabled=active;$('downloadClassicModels').textContent=result.models.ready?'重新校验经典模型':'下载并校验经典模型';$('classicModelProgress').textContent=install.message+(install.total_bytes?' · '+Math.round(install.bytes/install.total_bytes*100)+'%':'');if(active)classicModelTimer=setTimeout(refreshClassicModels,800);}catch(error){$('classicModelProgress').textContent=errorMessage(error);$('downloadClassicModels').disabled=false;}
}
$('downloadClassicModels').onclick=async()=>{try{$('downloadClassicModels').disabled=true;await post('/api/models/download',{});clearTimeout(classicModelTimer);await refreshClassicModels();}catch(error){$('downloadClassicModels').disabled=false;toast(errorMessage(error),true);}};
