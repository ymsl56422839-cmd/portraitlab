/* Relighting inputs, model readiness, and real device selection. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
const ai={status:null,job:null,timer:null,ids:[],primaryId:null,batchIds:[],batchSource:'照片库已选',imported:new Set(),checking:false,pollErrors:0,initialized:false,error:null};
const lightPresets=[
 {id:'window',name:'窗边柔光',group:'实用',description:'柔和侧光，适合日常人像',prompt:'soft natural window light, gentle side shadows',direction:'left',denoise:.35},
 {id:'bright',name:'明亮柔光',group:'实用',description:'明亮均匀的正面柔光',prompt:'large softbox frontal lighting, soft fill light, bright gentle lighting',direction:'none',denoise:.35},
 {id:'studio',name:'影棚塑形',group:'实用',description:'侧面主光与轻轮廓光',prompt:'soft side key light, subtle rim light, sculpted facial lighting',direction:'right',denoise:.45},
 {id:'top',name:'顶侧柔光',group:'实用',description:'从上方照入，增加面部明暗',prompt:'large soft overhead key light, gentle shadows under cheekbones, studio portrait lighting',direction:'top',denoise:.40},
 {id:'cloudy',name:'阴天漫射',group:'户外',description:'清冷、柔和的漫射日光',prompt:'overcast daylight, broad diffused skylight, soft cool ambient light',direction:'none',denoise:.35},
 {id:'sunset',name:'日落金光',group:'户外',description:'温暖的金色侧光',prompt:'warm golden hour sunlight, amber highlights, soft warm side light',direction:'left',denoise:.45},
 {id:'morning',name:'清晨日光',group:'户外',description:'较清爽的淡暖晨光',prompt:'soft early morning sunlight, delicate warm highlights, fresh natural daylight',direction:'right',denoise:.35},
 {id:'cinema',name:'冷暖电影光',group:'氛围',description:'暖色主光配冷蓝补光',prompt:'warm key light, cool blue fill light, cinematic lighting',direction:'right',denoise:.45},
 {id:'neon',name:'夜景霓虹光',group:'氛围',description:'青色与洋红色灯光',prompt:'magenta and cyan accent lighting, soft colored rim light, night portrait lighting',direction:'none',denoise:.50},
 {id:'dramatic',name:'暗调侧光',group:'氛围',description:'单侧主光与较深阴影',prompt:'single soft side key light, deep gentle shadows, low key portrait lighting',direction:'left',denoise:.45}
];
const aiMarkup=`
<section id="aiDialog" class="ai-dialog" aria-label="光影重塑工具">
 <div class="dialog-heading"><h2>光影重塑</h2><button id="aiClose" class="icon-button" hidden aria-label="关闭">×</button></div>
 <div class="dialog-import"><button id="aiImportFiles" type="button">导入照片</button><button id="aiImportFolder" type="button">导入文件夹</button><span>拖入的照片会加入本次输入列表</span></div>
 <div class="ai-inputs"><div class="ai-input-heading"><strong>本次输入 <span id="aiCount">0</span> 张</strong><div><button id="aiUseCurrent" class="text-button">使用当前照片</button><button id="aiClearInputs" class="text-button">清空输入</button></div></div><label class="ai-check"><input id="aiBatch" type="checkbox"><span id="aiBatchLabel">使用照片库已选照片</span></label><ul id="aiInputList"></ul><p id="aiInputHint" class="small-print"></p></div>
 <h3 class="ai-section-heading">选择光线</h3><div id="aiPresetList" class="ai-presets"></div><p id="aiPresetDescription" class="small-print"></p>
 <div class="ai-grid"><label class="field-label">运行设备<select id="aiDevice"><option value="auto">自动选择</option><option value="cuda">NVIDIA GPU</option><option value="cpu">CPU（较慢）</option></select></label><label class="field-label">生成尺寸<select id="aiSize"><option value="256">长边 256（CPU 试用）</option><option value="512" selected>长边 512</option><option value="768">长边 768</option><option value="1024">长边 1024</option></select></label></div>
 <label class="ai-check"><input id="aiLowVram" type="checkbox" checked>低显存模式</label><p id="aiDeviceHint" class="small-print"></p>
 <details><summary>调整光线和生成参数</summary><label class="field-label">光线描述<textarea id="aiPrompt" maxlength="2000" required></textarea><small>预设已填好英文描述，可以自行修改。</small></label>
 <div class="ai-grid"><label class="field-label">主光方向<select id="aiDirection"><option value="left">画面左侧</option><option value="right">画面右侧</option><option value="top">画面上方</option><option value="bottom">画面下方</option><option value="none">由描述决定</option></select></label><label class="field-label">随机种子<input id="aiSeed" type="number" min="-1" max="4294967295" step="1" value="12345"><small>-1 为随机；同批次依次加 1。</small></label><label class="field-label">每阶段步数<input id="aiSteps" type="number" min="10" max="50" step="1" value="20"></label><label class="field-label">二次细化强度<input id="aiDenoise" type="number" min="0.1" max="0.9" step="0.05" value="0.35"></label></div>
 <label class="field-label">负向描述<textarea id="aiNegative" maxlength="2000">low quality, blurry, deformed face, extra fingers, oversaturated</textarea></label></details>
 <div class="ai-state"><strong id="aiModelState">正在检查</strong><div id="aiStatusText"></div><button id="aiDownload" disabled>正在检查</button><button id="aiProbe" class="text-button">检查设备</button></div>
 <p class="ai-license">使用当前编辑结果生成新照片。人物、衣物和背景都可能变化；保护笔刷不约束生成。输出是所选尺寸，原片保留。模型条款见 <a href="/licenses/CreativeML-OpenRAIL-M.txt" target="_blank" rel="noreferrer">OpenRAIL-M</a>。</p>
 <div class="dialog-actions"><button id="aiCancelDialog" hidden>返回修片</button><button id="aiStart" class="primary" disabled>生成新照片</button></div>
</section>
<div id="aiJobPanel" class="ai-job" hidden><div class="job-heading"><span><span id="aiJobIcon" class="spinner"></span><strong id="aiJobTitle">正在准备</strong></span><button id="aiJobHide" class="text-button" aria-label="收起 AI 任务">×</button></div><div class="job-progress"><div id="aiProgress"></div></div><div id="aiJobDetails" class="job-details" aria-live="polite"></div><div class="job-actions"><button id="aiRetryPoll" class="text-button" hidden>重新连接</button><button id="aiJobStop" class="text-button">停止任务</button><button id="aiViewResult" class="primary" hidden>查看生成照片</button></div></div>`;
$('lightingToolHost').insertAdjacentHTML('beforeend',aiMarkup);$('taskDock').append($('aiJobPanel'));
function setLightPreset(preset){
 $('aiPrompt').value='portrait photograph, realistic skin texture, '+preset.prompt;$('aiDirection').value=preset.direction;$('aiDenoise').value=preset.denoise;
 $('aiPresetDescription').textContent=preset.description+'；方向与二次细化已设置。';for(const button of document.querySelectorAll('[data-light]')){const active=button.dataset.light===preset.id;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}
}
for(const preset of lightPresets){const button=document.createElement('button');button.type='button';button.dataset.light=preset.id;const title=document.createElement('strong');title.textContent=preset.name;const note=document.createElement('span');note.textContent=preset.description;button.append(title,note);button.onclick=()=>{setLightPreset(preset);toast(`光影重塑：已选择《${preset.name}》，用于本次 ${ai.ids.length} 张输入；尚未开始生成。`);};$('aiPresetList').append(button);}setLightPreset(lightPresets[0]);
function clearLightActive(){for(const button of document.querySelectorAll('[data-light]')){button.classList.remove('active');button.setAttribute('aria-pressed','false');}$('aiPresetDescription').textContent='光线参数已手动修改。';}
for(const id of ['aiPrompt','aiDirection','aiDenoise'])$(id).addEventListener('input',clearLightActive);
function refreshAIInputs(){
 ai.batchIds=ai.batchIds.filter(id=>photo(id));ai.ids=($('aiBatch').checked?ai.batchIds:ai.primaryId?[ai.primaryId]:[]).filter(id=>photo(id));
 $('aiCount').textContent=ai.ids.length;$('aiBatchLabel').textContent=`批量使用${ai.batchSource}（${ai.batchIds.length} 张）`;$('aiBatch').disabled=!ai.batchIds.length||state.locked;
 const list=$('aiInputList');list.replaceChildren();for(const id of ai.ids.slice(0,8)){const item=document.createElement('li');const name=document.createElement('button');name.type='button';name.className='ai-input-name';name.textContent=photo(id).name;name.classList.toggle('active',id===state.current);name.onclick=()=>selectPhoto(id);const remove=document.createElement('button');remove.className='text-button';remove.textContent='移除';remove.setAttribute('aria-label','从本次输入移除'+photo(id).name);remove.disabled=state.locked||(aiActive()&&ai.job.action==='generate');remove.onclick=()=>{if(state.locked||(aiActive()&&ai.job.action==='generate'))return;ai.batchIds=ai.ids.filter(value=>value!==id);ai.primaryId=ai.batchIds[0]||null;ai.batchSource='本次输入';$('aiBatch').checked=ai.batchIds.length>1;refreshAIInputs();};item.append(name,remove);list.append(item);}if(ai.ids.length>8){const item=document.createElement('li');item.textContent=`另有 ${ai.ids.length-8} 张`;list.append(item);}
 $('aiInputHint').textContent=ai.ids.length?'仅处理上方列出的照片，按顺序生成。':'请导入照片，或在左侧照片库选择一张照片。';aiButtons();
}
function aiActive(){return !!ai.job&&activeStatuses.includes(ai.job.status);}
function aiButtons(){
 const active=aiActive();const inputLocked=state.locked||(active&&ai.job.action==='generate');$('relightBtn').disabled=!state.token||state.locked;
 $('aiStart').disabled=state.locked||active||ai.checking||!ai.status?.ready||!ai.ids.length||!state.boot?.models?.ready;$('aiStart').textContent=ai.ids.length?`生成 ${ai.ids.length} 张新照片`:'生成新照片';
 for(const id of ['aiImportFiles','aiImportFolder','aiUseCurrent','aiClearInputs'])$(id).disabled=inputLocked||state.importing;
 $('aiBatch').disabled=inputLocked||!ai.batchIds.length;
 for(const button of document.querySelectorAll('[data-light],#aiInputList .text-button'))button.disabled=inputLocked;
 for(const id of ['aiDevice','aiSize','aiLowVram','aiPrompt','aiNegative','aiDirection','aiSeed','aiSteps','aiDenoise'])$(id).disabled=inputLocked;
 const cuda=ai.status?.cuda_available??state.hardware?.cuda_available;const gpu=$('aiDevice').querySelector('[value="cuda"]');gpu.disabled=!cuda;
 const cpu=$('aiDevice').value==='cpu'||($('aiDevice').value==='auto'&&cuda===false);
 $('aiDeviceHint').textContent=cpu?'CPU 已验证 256 小图；较大尺寸耗时更多，可先用 256 试一张。':cuda?`${ai.status?.gpu_name||state.hardware?.gpu_name||'NVIDIA GPU'}；建议从 512 档、低显存模式开始。`:'正在检查 GPU。自动模式会选择可用设备。';
 if($('aiDevice').value==='cuda'&&!cuda){$('aiStart').disabled=true;$('aiDeviceHint').textContent='当前 GPU 不可用，请选择自动或 CPU。';}
 renderAIModelState();
}
function renderAIModelState(){
 const s=ai.status;const downloading=aiActive()&&ai.job.action==='download';const downloadFailed=ai.job?.action==='download'&&['failed','completed_with_errors'].includes(ai.job.status);
 let label='正在检查',message='',button='正在检查',kind='checking',disabled=true;
 if(downloading){label='正在下载或校验';message=ai.job.progress?.message||'处理模型文件，请稍候。';button='准备中…';kind='running';}
 else if(ai.checking&&!s){message='正在读取组件状态。';}
 else if(ai.error){label=ai.error.kind==='download'?'模型准备失败':'检查失败';message=ai.error.message;button=ai.error.kind==='download'?'重试下载与校验':'重试检查';kind='failed';disabled=ai.checking||aiActive();}
 else if(!s?.runtime_ready){label=s?'运行组件未安装':'正在检查';message=s?'请使用包含 AI 组件的完整软件包。':'';button='需要 AI 运行组件';kind='missing';}
 else if(downloadFailed){label='模型准备失败';message=ai.job.error||'请检查网络后重试。';button='重试下载与校验';kind='failed';disabled=aiActive()||ai.checking;}
 else if(s.ready){label='模型已下载';message='生成前会校验文件；无需重复下载。';button='校验模型';kind='ready';disabled=aiActive()||ai.checking;}
 else{label='模型未下载完整';message='首次需下载约 3.6 GiB，请保持网络连接。';button='下载并校验模型';kind='missing';disabled=aiActive()||ai.checking;}
 if(ai.job?.action==='download'&&ai.job.status==='completed'&&s?.ready){label='模型已就绪';message='下载与校验已完成。';}
 if(!state.boot?.models?.ready&&s?.runtime_ready)message+=' 生成还需要基础处理模型，请先在设置中准备。';
 for(const id of ['aiModelState','settingsAIState']){$(id).textContent=label;$(id).dataset.state=kind;}
 $('aiStatusText').textContent=message;$('settingsAIMessage').textContent=message;
 for(const id of ['aiDownload','settingsAIDownload']){$(id).textContent=button;$(id).disabled=disabled||state.locked;}
}
async function refreshAI(probe=false){
 if(ai.checking)return;ai.checking=true;aiButtons();
 try{ai.status=await api('/api/relight/status'+(probe?'?probe=true':''));ai.error=null;}catch(error){ai.error={kind:'probe',message:errorMessage(error)};$('aiStatusText').textContent=errorMessage(error);$('settingsAIMessage').textContent=errorMessage(error);toast('无法检查 AI 组件：'+errorMessage(error),true);}finally{ai.checking=false;aiButtons();}
}
async function openAI(){
 if(!state.token||state.locked)return;
 if(!ai.initialized){ai.primaryId=state.current;ai.batchIds=[...state.selected];ai.batchSource='照片库已选';$('aiBatch').checked=false;ai.initialized=true;}
 setModule('lighting');refreshAIInputs();if(ai.primaryId&&photo(ai.primaryId))await selectPhoto(ai.primaryId);if(aiActive())showAIJob(ai.job);await refreshAI(!ai.status?.hardware_checked);
}
$('relightBtn').onclick=openAI;$('aiClose').onclick=()=>setModule('retouch');$('aiCancelDialog').onclick=()=>setModule('retouch');$('aiProbe').onclick=()=>refreshAI(true);
$('aiImportFiles').onclick=()=>requestImport(false,'ai');$('aiImportFolder').onclick=()=>requestImport(true,'ai');$('aiBatch').onchange=refreshAIInputs;
$('aiUseCurrent').onclick=()=>{ai.primaryId=state.current;ai.batchIds=[...state.selected];ai.batchSource='照片库已选';$('aiBatch').checked=false;refreshAIInputs();};
$('aiClearInputs').onclick=()=>{ai.primaryId=null;ai.batchIds=[];ai.batchSource='本次输入';$('aiBatch').checked=false;refreshAIInputs();};
$('aiDevice').onchange=aiButtons;
document.addEventListener('portraitlab-photo-selected',event=>{if(state.module!=='lighting')return;if(!$('aiBatch').checked&&!aiActive()){ai.primaryId=event.detail.id;}refreshAIInputs();});
document.addEventListener('portraitlab-imported',event=>{if(event.detail.target!=='ai')return;const previous=ai.batchSource==='本次输入'?ai.batchIds:ai.ids;ai.batchIds=[...new Set([...previous,...event.detail.ids])];ai.primaryId=ai.primaryId||event.detail.ids[0]||null;ai.batchSource='本次输入';$('aiBatch').checked=ai.batchIds.length>1;refreshAIInputs();});
async function downloadAIModels(){if(aiActive())return;if(ai.error?.kind==='probe'){await refreshAI(true);return;}ai.error=null;try{const {job}=await post('/api/relight/download',{});showAIJob(job);pollAI();}catch(error){ai.error={kind:'download',message:errorMessage(error)};toast(errorMessage(error),true);}finally{aiButtons();}}
$('aiDownload').onclick=downloadAIModels;$('settingsAIDownload').onclick=downloadAIModels;
$('aiStart').onclick=async()=>{
 const ids=[...ai.ids];if(state.locked||aiActive()||!ids.length)return;
 for(const control of $('aiDialog').querySelectorAll('input[type="number"],textarea')){if(!control.checkValidity()){control.closest('details')?.setAttribute('open','');control.reportValidity();return;}}
 const params={prompt:$('aiPrompt').value.trim(),negative_prompt:$('aiNegative').value,direction:$('aiDirection').value,long_side:Number($('aiSize').value),device:$('aiDevice').value,seed:Number($('aiSeed').value),steps:Number($('aiSteps').value),denoise:Number($('aiDenoise').value),cfg:2,low_vram:$('aiLowVram').checked};
 lockEditing(true);aiButtons();try{await flushPhotos(ids);const {job}=await post('/api/relight/jobs',{ids,params});showAIJob(job);pollAI();toast(`光影重塑：已提交 ${ids.length} 张，使用${params.device==='cpu'?' CPU':params.device==='cuda'?' GPU':'自动设备'}，长边 ${params.long_side} 像素。`);}catch(error){toast(errorMessage(error),true);}finally{lockEditing(false);aiButtons();}
};
function showAIJob(job){
 ai.job=job;$('aiJobPanel').hidden=false;const active=aiActive();state.lightingTask=active?(job.action==='download'?'光影模型准备中':'光影重塑处理中'):null;updateConnectionStatus();const p=job.progress||{};$('aiJobIcon').className=active?'spinner':'';$('aiJobIcon').textContent=active?'':job.status==='completed'?'✓':'!';
 $('aiJobTitle').textContent=(job.action==='download'?'模型准备':'光影重塑')+' · '+({queued:'排队中',running:'处理中',cancelling:'正在停止',completed:'已完成',completed_with_errors:'部分失败',failed:'失败',cancelled:'已停止',interrupted:'已中断'}[job.status]||job.status);
 const fraction=p.total>0?Math.min(1,p.current/p.total):0;const overall=job.action==='download'?fraction:Math.min(1,(job.done+(active?fraction:0))/Math.max(1,job.total));$('aiProgress').style.width=(job.status==='completed'?100:overall*100)+'%';
 const lines=[job.action==='generate'?`${job.done} / ${job.total} 张 · 长边 ${job.params.long_side}`:'模型文件只需下载一次'];if(active&&p.message)lines.push(p.message);if(job.error)lines.push(job.error);for(const item of job.items||[])if(item.error)lines.push(item.name+'：'+item.error);if(job.status==='completed'&&job.action==='generate')lines.push('生成照片已加入照片库。');$('aiJobDetails').textContent=lines.join('\n');$('aiJobDetails').style.whiteSpace='pre-line';$('aiJobStop').hidden=!active;$('aiJobStop').disabled=job.status==='cancelling';$('aiViewResult').hidden=active||!job.items?.some(item=>item.photo_id);aiButtons();
}
async function importAIResults(job){const ids=(job.items||[]).filter(item=>item.photo_id&&!ai.imported.has(item.photo_id)).map(item=>item.photo_id);if(!ids.length)return;const result=await api('/api/photos');for(const p of result.photos)if(ids.includes(p.id)){if(!photo(p.id))mergePhoto(p);ai.imported.add(p.id);}renderLibrary();}
async function pollAI(){
 clearTimeout(ai.timer);if(!aiActive())return;
 try{const {job}=await api('/api/relight/jobs/'+ai.job.id);ai.pollErrors=0;$('aiRetryPoll').hidden=true;showAIJob(job);await importAIResults(job);if(!aiActive()){await refreshAI();if(job.status==='completed')toast(job.action==='generate'?'生成完成，结果已加入照片库。':'AI 模型已准备。');}}
 catch(error){ai.pollErrors++;if(ai.pollErrors>=3){$('aiRetryPoll').hidden=false;$('aiJobDetails').textContent='暂时无法获取进度，任务可能仍在后台运行。点击重新连接。';return;}}
 if(aiActive())ai.timer=setTimeout(pollAI,1200);
}
$('aiRetryPoll').onclick=()=>{ai.pollErrors=0;$('aiRetryPoll').hidden=true;pollAI();};
$('aiJobStop').onclick=async()=>{if(!ai.job)return;try{const {job}=await post('/api/relight/jobs/'+ai.job.id+'/cancel',{});showAIJob(job);pollAI();}catch(error){toast(errorMessage(error),true);}};$('aiJobHide').onclick=()=>$('aiJobPanel').hidden=true;
$('aiViewResult').onclick=async()=>{const item=ai.job?.items.find(item=>item.photo_id);if(item){state.selected=new Set([item.photo_id]);await selectPhoto(item.photo_id);}};
let classicModelTimer=null,classicModelState=null,hardwareBusy=false;
function classicState(){
 const data=state.boot?.models||{};const installation=classicModelState||{};const records=Object.values(data.models||{});const prep=data.preparation_state||(records.length&&records.every(item=>item.exists&&item.verified)?'ready':records.length&&records.every(item=>item.exists)?'downloaded':'missing');
 const active=installation.status==='running';const phase=installation.phase||'';let label,action,kind;
 if(active){label=phase==='downloading'?'正在下载':phase==='verifying'?'正在校验':'正在检查';action=label+'…';kind='running';}
 else if(installation.status==='failed'||phase==='failed'){label='模型准备失败';action='重试下载与校验';kind='failed';}
 else if(prep==='ready'){label='模型已就绪';action='重新校验';kind='ready';}
 else if(prep==='downloaded'){label='模型已下载';action='校验模型';kind='downloaded';}
 else{label='模型未下载完整';action='下载模型';kind='missing';}
 $('classicModelState').textContent=label;$('classicModelState').dataset.state=kind;$('downloadClassicModels').textContent=action;$('downloadClassicModels').disabled=active||state.locked||!state.token;
 const progress=installation.total_bytes?' · '+Math.round((installation.bytes||0)/installation.total_bytes*100)+'%':'';
 $('classicModelProgress').textContent=active?(installation.message||label)+progress:kind==='failed'?(installation.message||'请检查网络后重试。'):prep==='ready'?'基础处理模型可用，无需重复下载。':prep==='downloaded'?'模型文件已在本机。校验后即可使用。':'准备后可使用修肤、皮肤选区和 AI 前景分析。';
 updateHardwareFooter();
}
async function refreshClassicModels(){
 clearTimeout(classicModelTimer);const wasRunning=classicModelState?.status==='running';try{const result=await api('/api/models/status');state.boot.models=result.models;classicModelState=result.installation;modelInfo();classicState();aiButtons();if(wasRunning&&classicModelState.status==='completed'){const retry=new Set(state.dirty);if(photo()?.status!=='ready'&&state.current)retry.add(state.current);for(const id of retry)queuePreview(id,0);}if(classicModelState.status==='running')classicModelTimer=setTimeout(refreshClassicModels,900);}catch(error){$('classicModelProgress').textContent=errorMessage(error);$('classicModelState').textContent='检查失败';$('classicModelState').dataset.state='failed';$('downloadClassicModels').disabled=false;$('downloadClassicModels').textContent='重试准备模型';}
}
$('downloadClassicModels').onclick=async()=>{if(state.locked)return;$('downloadClassicModels').disabled=true;try{await post('/api/models/download',{});await refreshClassicModels();}catch(error){toast(errorMessage(error),true);$('downloadClassicModels').disabled=false;}};
function updateHardwareFooter(){
 const hardware=state.hardware;const data=state.boot?.models;const last=state.lastProcessingDevice?.preference===hardware?.preference?state.lastProcessingDevice:null;const actual=last?.actual_device||hardware?.actual_device;
 const label=!hardware?'设置 · 检查设备':actual?`${hardware.preference==='auto'?'自动 · ':''}合成 ${actual==='cuda'?'GPU':'CPU'}`:hardware.preference==='cpu'?'CPU 已选':hardware.preference==='cuda'?'GPU 已选':hardware.classic_gpu_available?'自动 · GPU 可用':'自动 · CPU 可用';
 $('modelStatusText').textContent=label+(data?.ready?'':' · 需准备模型');$('modelStatusText').title=last?.warning||hardware?.stage_note||'查看处理设置';
}
function renderHardware(){
 const hardware=state.hardware;if(!hardware)return;$('hardwareDevice').value=hardware.preference||'auto';$('hardwareDevice').querySelector('[value="cuda"]').disabled=!hardware.classic_gpu_available;$('hardwareDevice').disabled=hardwareBusy||state.locked;
 $('hardwareSummary').textContent=hardware.message||((hardware.gpu_name?hardware.gpu_name+'。':'')+(hardware.classic_gpu_available?'可使用 GPU 合成。':'使用 CPU 处理。'));$('hardwareStages').textContent=(state.lastProcessingDevice?.preference===hardware.preference&&state.lastProcessingDevice.warning?state.lastProcessingDevice.warning+' ':'')+(hardware.stage_note||'不同处理步骤可能使用不同设备。')+(!hardware.classic_gpu_available&&hardware.preference==='cpu'?' 要检查 GPU，请先选择自动。':'');$('hardwareRefresh').disabled=hardwareBusy;updateHardwareFooter();aiButtons();
}
async function refreshHardware(){if(hardwareBusy)return;hardwareBusy=true;$('hardwareRefresh').disabled=true;try{state.hardware=await api('/api/hardware');}catch(error){$('hardwareSummary').textContent='无法检查设备：'+errorMessage(error);}finally{hardwareBusy=false;renderHardware();$('hardwareRefresh').disabled=false;}}
$('hardwareRefresh').onclick=refreshHardware;
$('hardwareDevice').onchange=async()=>{if(hardwareBusy)return;const preference=$('hardwareDevice').value;hardwareBusy=true;$('hardwareDevice').disabled=true;try{state.hardware=await post('/api/hardware',{device:preference});toast('处理设备设置已保存。');if(state.current)queuePreview(state.current,0);}catch(error){toast(errorMessage(error),true);}finally{hardwareBusy=false;renderHardware();}};
document.addEventListener('portraitlab-model-state',classicState);document.addEventListener('portraitlab-render-device',()=>{updateHardwareFooter();if(state.hardware)renderHardware();});
document.addEventListener('portraitlab-edit-state',()=>{classicState();aiButtons();if(state.hardware)$('hardwareDevice').disabled=hardwareBusy||state.locked;});
document.addEventListener('portraitlab-settings-open',()=>{refreshClassicModels();refreshHardware();refreshAI();});
document.addEventListener('portraitlab-ready',async()=>{
 classicState();aiButtons();refreshHardware();refreshClassicModels();refreshAI();
 try{const {jobs}=await api('/api/relight/jobs');const job=jobs.find(item=>activeStatuses.includes(item.status));if(job){showAIJob(job);pollAI();}}catch(error){toast(errorMessage(error),true);}
});
