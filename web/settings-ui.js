/* Classic model and processing-device settings. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
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
 $('classicModelProgress').textContent=active?(installation.message||label)+progress:kind==='failed'?(installation.message||'请检查网络后重试。'):prep==='ready'?'基础处理模型可用，无需重复下载。':prep==='downloaded'?'模型文件已在本机。校验后即可使用。':'准备后可使用修肤、皮肤选区和人物选区分析。';
 updateHardwareFooter();
}
async function refreshClassicModels(){
 clearTimeout(classicModelTimer);const wasRunning=classicModelState?.status==='running';try{const result=await api('/api/models/status');state.boot.models=result.models;classicModelState=result.installation;modelInfo();classicState();window.PortraitLighting?.refreshControls?.();if(wasRunning&&classicModelState.status==='completed'){const retry=new Set(state.dirty);if(photo()?.status!=='ready'&&state.current)retry.add(state.current);for(const id of retry)queuePreview(id,0);}if(classicModelState.status==='running')classicModelTimer=setTimeout(refreshClassicModels,900);}catch(error){$('classicModelProgress').textContent=errorMessage(error);$('classicModelState').textContent='检查失败';$('classicModelState').dataset.state='failed';$('downloadClassicModels').disabled=false;$('downloadClassicModels').textContent='重试准备模型';}
}
$('downloadClassicModels').onclick=async()=>{if(state.locked)return;$('downloadClassicModels').disabled=true;try{await post('/api/models/download',{});await refreshClassicModels();}catch(error){toast(errorMessage(error),true);$('downloadClassicModels').disabled=false;}};
function updateHardwareFooter(){
 const hardware=state.hardware;const data=state.boot?.models;const last=state.lastProcessingDevice?.preference===hardware?.preference?state.lastProcessingDevice:null;const actual=last?.actual_device||hardware?.actual_device;
 const label=!hardware?'设置 · 检查设备':actual?`${hardware.preference==='auto'?'自动 · ':''}合成 ${actual==='cuda'?'GPU':'CPU'}`:hardware.preference==='cpu'?'CPU 已选':hardware.preference==='cuda'?'GPU 已选':hardware.classic_gpu_available?'自动 · GPU 可用':'自动 · CPU 可用';
 $('modelStatusText').textContent=label+(data?.ready?'':' · 需准备模型');$('modelStatusText').title=last?.warning||hardware?.stage_note||'查看处理设置';
}
function renderHardware(){
 const hardware=state.hardware;if(!hardware)return;$('hardwareDevice').value=hardware.preference||'auto';$('hardwareDevice').querySelector('[value="cuda"]').disabled=!hardware.classic_gpu_available;$('hardwareDevice').disabled=hardwareBusy||state.locked;
 $('hardwareSummary').textContent=hardware.message||((hardware.gpu_name?hardware.gpu_name+'。':'')+(hardware.classic_gpu_available?'可使用 GPU 合成。':'使用 CPU 处理。'));$('hardwareStages').textContent=(state.lastProcessingDevice?.preference===hardware.preference&&state.lastProcessingDevice.warning?state.lastProcessingDevice.warning+' ':'')+(hardware.stage_note||'不同处理步骤可能使用不同设备。')+(!hardware.classic_gpu_available&&hardware.preference==='cpu'?' 要检查 GPU，请先选择自动。':'');$('hardwareRefresh').disabled=hardwareBusy;updateHardwareFooter();window.PortraitLighting?.refreshControls?.();
}
async function refreshHardware(){if(hardwareBusy)return;hardwareBusy=true;$('hardwareRefresh').disabled=true;try{state.hardware=await api('/api/hardware');}catch(error){$('hardwareSummary').textContent='无法检查设备：'+errorMessage(error);}finally{hardwareBusy=false;renderHardware();$('hardwareRefresh').disabled=false;}}
$('hardwareRefresh').onclick=refreshHardware;
$('hardwareDevice').onchange=async()=>{if(hardwareBusy)return;const preference=$('hardwareDevice').value;hardwareBusy=true;$('hardwareDevice').disabled=true;try{state.hardware=await post('/api/hardware',{device:preference});toast('处理设备设置已保存。');if(state.current)queuePreview(state.current,0);}catch(error){toast(errorMessage(error),true);}finally{hardwareBusy=false;renderHardware();}};
document.addEventListener('portraitlab-model-state',classicState);document.addEventListener('portraitlab-render-device',()=>{updateHardwareFooter();if(state.hardware)renderHardware();});
document.addEventListener('portraitlab-edit-state',()=>{classicState();window.PortraitLighting?.refreshControls?.();if(state.hardware)$('hardwareDevice').disabled=hardwareBusy||state.locked;});
document.addEventListener('portraitlab-settings-open',()=>{refreshClassicModels();refreshHardware();window.PortraitLighting?.refreshStatus?.();});
document.addEventListener('portraitlab-ready',()=>{classicState();refreshHardware();refreshClassicModels();});
