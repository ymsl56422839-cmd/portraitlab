/* Native-resolution tiled portrait editor prototype. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const workspace = () => window.PortraitWorkspace;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const same = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
  const state = {token:null,photos:[],current:null,layout:'single',zoom:1,fit:true,center:{x:0,y:0},
    importing:false,analyzing:false,exporting:false,mutating:false,historyPending:false,desired:[],dirty:false,sequence:0,
    message:'导入照片后开始。',error:null,hovered:null,focused:null,poll:null,frame:null,pendingTiles:0,
    tool:'browse',brushDiameter:24,spacePan:false,draftStrokes:[],draftBase:null,draftRequestId:null,draftNeedsReview:false,draftSerial:0,manualPending:false,manualError:false,jobKind:null};
  const panes = Object.fromEntries(['primary','secondary'].map(name => [name,{name,
    element:document.querySelector(`[data-pane="${name}"]`),viewport:$(name+'Viewport'),canvas:$(name+'Canvas'),
    mode:name==='primary'?'result':'original',split:.5,drag:null,splitPointer:null,brushDrag:null,pointer:null}]));
  const photo = () => state.photos.find(p => p.id===state.current);
  const activePanes = () => state.layout==='dual' ? Object.values(panes) : [panes.primary];
  const candidates = () => photo()?.candidates || [];
  const selected = id => state.desired.includes(id);
  const busy = () => state.importing || state.exporting || state.manualPending || !!workspace()?.isBusy();
  const editable = () => !!photo() && !busy() && !state.historyPending && photo().analysis?.status!=='running' && (workspace()?.canUseCommittedActions()??true);
  const hasDraft = () => state.draftStrokes.length>0;
  const drawing = () => Object.values(panes).some(p=>p.brushDrag)||!!workspace()?.isDrawing();
  const canMark = () => editable()&&!state.mutating&&!state.dirty&&(workspace()?.manualAvailable()??true);
  const tileCache = new Map(), tileQueue=[];
  let activeLoads=0,usedTiles=new Set(),thumbnailCanvases=[];
  const tileKey = (p,layer,level,x,y,quality) => `${p.id}|${layer}|${layer==='original'?0:layer==='preview'?p.version+':'+p.draft?.id:p.version}|${quality}|${level}|${x}|${y}`;
  const currentTile = item => item.photoId===state.current&&(item.layer==='original'||item.version===photo()?.version)&&(item.layer!=='preview'||item.draftId===photo()?.draft?.id);
  function requestTile(p,layer,level,x,y){
    const quality=layer==='original'?'native':workspace()?.tileQuality(level)||'native',key=tileKey(p,layer,level,x,y,quality);let item=tileCache.get(key);
    if(item){item.used=performance.now();return item;}
    item={key,photoId:p.id,layer,version:p.version,draftId:p.draft?.id,state:'queued',image:null,used:performance.now(),
      url:`/api/photos/${encodeURIComponent(p.id)}/tiles/${layer}/${level}/${x}/${y}.png?v=${layer==='original'?0:p.version}&quality=${quality}${layer==='preview'?'&draft='+encodeURIComponent(p.draft.id):''}`};
    tileCache.set(key,item);tileQueue.push(item);pumpTiles();return item;
  }
  function pumpTiles(){
    while(activeLoads<6 && tileQueue.length){
      const item=tileQueue.shift();
      if(!currentTile(item)){tileCache.delete(item.key);continue;}
      activeLoads++;item.state='loading';const image=new Image();item.image=image;
      const finish=loaded=>{
        activeLoads--;
        const stale=!currentTile(item);
        if(stale){item.state='stale';tileCache.delete(item.key);}else item.state=loaded?'ready':'error';
        pruneTiles();scheduleDraw();pumpTiles();
      };
      image.onload=()=>finish(true);image.onerror=()=>finish(false);
      image.src=item.url;
    }
  }
  function pruneTiles(){
    if(tileCache.size<=160)return;
    const discard=[...tileCache.values()].filter(t=>!usedTiles.has(t.key)&&['ready','error'].includes(t.state)).sort((a,b)=>a.used-b.used);
    for(const item of discard){if(tileCache.size<=160)break;tileCache.delete(item.key);}
  }
  function scheduleDraw(){if(state.frame===null)state.frame=requestAnimationFrame(()=>{state.frame=null;drawAll();});}
  function viewportSize(pane){const r=pane.viewport.getBoundingClientRect();return {width:Math.max(1,r.width),height:Math.max(1,r.height)};}
  function transform(pane){const s=viewportSize(pane);return {...s,left:s.width/2-state.center.x*state.zoom,top:s.height/2-state.center.y*state.zoom};}
  function imagePoint(pane,event){const r=pane.viewport.getBoundingClientRect();return {x:state.center.x+(event.clientX-r.left-r.width/2)/state.zoom,y:state.center.y+(event.clientY-r.top-r.height/2)/state.zoom};}
  function levelFor(p){const levels=Math.max(1,Number(p.levels)||1);return clamp(Math.floor(Math.log2(1/state.zoom)),0,levels-1);}
  function drawLayer(ctx,pane,p,layer,level,clipLeft,clipRight){
    if(clipRight<=clipLeft)return {pending:0,errors:0,tiles:0};
    const t=transform(pane),scale=2**level,tile=p.tile_size||512,span=tile*scale;
    const worldLeft=(Math.max(0,clipLeft)-t.left)/state.zoom,worldRight=(Math.min(t.width,clipRight)-t.left)/state.zoom;
    const worldTop=-t.top/state.zoom,worldBottom=(t.height-t.top)/state.zoom;
    const cols=Math.ceil(p.width/span),rows=Math.ceil(p.height/span);
    const x0=Math.max(0,Math.floor(worldLeft/span)),x1=Math.min(cols-1,Math.floor(worldRight/span));
    const y0=Math.max(0,Math.floor(worldTop/span)),y1=Math.min(rows-1,Math.floor(worldBottom/span));
    let pending=0,errors=0,tiles=0;
    ctx.save();ctx.beginPath();ctx.rect(clipLeft,0,Math.max(0,clipRight-clipLeft),t.height);ctx.clip();
    for(let y=y0;y<=y1;y++)for(let x=x0;x<=x1;x++){
      tiles++;
      const item=requestTile(p,layer,level,x,y);usedTiles.add(item.key);
      if(item.state!=='ready'){pending++;if(item.state==='error')errors++;continue;}
      const worldW=Math.min(span,p.width-x*span),worldH=Math.min(span,p.height-y*span);
      const dpr=Math.min(2,window.devicePixelRatio||1),snap=value=>Math.round(value*dpr)/dpr;
      const dx=snap(t.left+x*span*state.zoom),dy=snap(t.top+y*span*state.zoom);
      const dw=snap(t.left+(x*span+worldW)*state.zoom)-dx,dh=snap(t.top+(y*span+worldH)*state.zoom)-dy;
      ctx.imageSmoothingEnabled=state.zoom<1;
      ctx.drawImage(item.image,0,0,item.image.naturalWidth,item.image.naturalHeight,
        dx,dy,dw,dh);
    }
    ctx.restore();return {pending,errors,tiles};
  }
  function markerGeometry(pane,candidate){const t=transform(pane),b=candidate.bounds;return {x:t.left+(b[0]+b[2])*.5*state.zoom,y:t.top+(b[1]+b[3])*.5*state.zoom,r:Math.max(10,Math.max(b[2]-b[0],b[3]-b[1])*.5*state.zoom+5)};}
  function drawMarkers(ctx,pane){
    if(!$('showMarkers').checked||workspace()?.showCandidates()===false||workspace()?.hideSourceOverlay(pane))return;
    for(let i=0;i<candidates().length;i++){
      const c=candidates()[i],m=markerGeometry(pane,c),on=selected(c.id),focus=c.id===state.focused;
      ctx.beginPath();ctx.arc(m.x,m.y,m.r,0,Math.PI*2);ctx.lineWidth=focus?2:1.3;ctx.strokeStyle=on?'#b9eee3':'#e6c886';ctx.stroke();
      const labelX=m.x+m.r*.72+4,labelY=m.y-m.r*.72-4;
      ctx.beginPath();ctx.arc(labelX,labelY,8,0,Math.PI*2);ctx.fillStyle='#222a2ee6';ctx.fill();
      ctx.fillStyle=on?'#bbf0e2':'#f5ddab';ctx.font='11px Segoe UI';ctx.textAlign='center';ctx.textBaseline='middle';
      ctx.fillText(String(i+1),labelX,labelY);
    }
  }
  function drawDrafts(ctx,pane){
    const p=photo();if(!p||!hasDraft()||state.draftBase?.photoId!==p.id||workspace()?.hideSourceOverlay(pane))return;
    const t=transform(pane);ctx.save();ctx.beginPath();ctx.rect(t.left,t.top,p.width*state.zoom,p.height*state.zoom);ctx.clip();
    ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=ctx.fillStyle='rgba(242,115,174,.42)';
    for(const stroke of state.draftStrokes){
      const points=stroke.points;ctx.beginPath();
      if(points.length===1){ctx.arc(t.left+points[0][0]*state.zoom,t.top+points[0][1]*state.zoom,stroke.radius*state.zoom,0,Math.PI*2);ctx.fill();}
      else{ctx.lineWidth=stroke.radius*2*state.zoom;points.forEach(([x,y],i)=>i?ctx.lineTo(t.left+x*state.zoom,t.top+y*state.zoom):ctx.moveTo(t.left+x*state.zoom,t.top+y*state.zoom));ctx.stroke();}
    }ctx.restore();
  }
  function drawPane(pane){
    const p=photo(),s=viewportSize(pane),dpr=Math.min(2,window.devicePixelRatio||1),canvas=pane.canvas;
    const w=Math.round(s.width*dpr),h=Math.round(s.height*dpr);
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
    const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle='#16181b';ctx.fillRect(0,0,s.width,s.height);
    if(!p)return {pending:0,errors:0};
    const level=levelFor(p),resultLayer=p.draft?'preview':'result',layers=pane.mode==='compare'?[['original',0,s.width*pane.split],[resultLayer,s.width*pane.split,s.width]]:[[pane.mode==='result'?resultLayer:pane.mode,0,s.width]];
    let pending=0,errors=0,previewTiles=0;
    for(const [layer,left,right] of layers){const r=drawLayer(ctx,pane,p,layer,level,left,right);pending+=r.pending;errors+=r.errors;if(layer==='preview')previewTiles+=r.tiles;}
    const interactive=pane.mode!=='original'&&workspace()?.tileQuality(level)==='interactive';
    drawMarkers(ctx,pane);drawDrafts(ctx,pane);workspace()?.drawOverlay(ctx,pane);pane.element.querySelector('.pane-loading').hidden=!pending&&!interactive;
    pane.element.querySelector('.pane-loading').textContent=errors?'部分图块加载失败':interactive?(pending?'正在加载交互预览…':'交互预览 · 松开后细化'):p.draft?'正在细化原尺寸画面…':'加载原尺寸细节…';
    canvas.dataset.level=String(level);canvas.dataset.zoom=String(state.zoom);canvas.dataset.version=String(p.version);canvas.dataset.preview=p.draft?.id||'';canvas.dataset.mode=pane.mode;canvas.dataset.previewTiles=String(previewTiles);canvas.dataset.quality=workspace()?.tileQuality(level)||'native';
    return {pending,errors};
  }
  function drawThumbs(){
    const p=photo();if(!p)return;
    const visible=document.querySelector('.tool-scroll').getBoundingClientRect();
    for(const {canvas,candidate} of thumbnailCanvases){
      const rect=canvas.getBoundingClientRect();if(rect.bottom<visible.top||rect.top>visible.bottom)continue;
      const ctx=canvas.getContext('2d');ctx.fillStyle='#171b1f';ctx.fillRect(0,0,116,116);
      const b=candidate.bounds,cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2,size=Math.max(72,(b[2]-b[0])*4,(b[3]-b[1])*4);
      const x0=clamp(cx-size/2,0,Math.max(0,p.width-size)),y0=clamp(cy-size/2,0,Math.max(0,p.height-size)),tile=p.tile_size||512;
      for(let y=Math.floor(y0/tile);y<=Math.min(Math.ceil(p.height/tile)-1,Math.floor((y0+size)/tile));y++)for(let x=Math.floor(x0/tile);x<=Math.min(Math.ceil(p.width/tile)-1,Math.floor((x0+size)/tile));x++){
        const item=requestTile(p,'original',0,x,y);usedTiles.add(item.key);
        if(item.state==='ready'){ctx.imageSmoothingEnabled=true;ctx.drawImage(item.image,(x*tile-x0)*116/size,(y*tile-y0)*116/size,item.image.naturalWidth*116/size,item.image.naturalHeight*116/size);}
      }
      ctx.strokeStyle='#e3c891';ctx.lineWidth=1;ctx.strokeRect((b[0]-x0)*116/size,(b[1]-y0)*116/size,(b[2]-b[0])*116/size,(b[3]-b[1])*116/size);
    }
  }
  function drawAll(){
    usedTiles=new Set();let pending=0,errors=0;
    for(const pane of activePanes()){const r=drawPane(pane);pending+=r.pending;errors+=r.errors;}
    drawThumbs();state.pendingTiles=pending;state.tileErrors=errors;renderStatus();workspace()?.onDrawComplete();
  }
  function fit(){
    const p=photo();if(!p||drawing())return;
    const sizes=activePanes().map(viewportSize);
    state.zoom=clamp(Math.min(...sizes.map(s=>Math.min((s.width-32)/p.width,(s.height-32)/p.height))),.005,8);
    state.center={x:p.width/2,y:p.height/2};state.fit=true;updateZoom();scheduleDraw();
  }
  function setZoom(value,pane=panes.primary,clientX=null,clientY=null){
    const p=photo();if(!p||drawing())return;
    const r=pane.viewport.getBoundingClientRect(),dx=clientX===null?0:clientX-r.left-r.width/2,dy=clientY===null?0:clientY-r.top-r.height/2;
    const anchor={x:state.center.x+dx/state.zoom,y:state.center.y+dy/state.zoom};
    state.zoom=clamp(value,.005,8);state.center={x:anchor.x-dx/state.zoom,y:anchor.y-dy/state.zoom};state.fit=false;updateZoom();scheduleDraw();
  }
  function updateZoom(){
    $('zoomValue').textContent=photo()?(state.zoom*100).toFixed(state.zoom<1?1:0)+'%':'—';
    $('zoomSlider').value=String(Math.log(state.zoom/.005)/Math.log(1600)*1000);
    $('zoomNative').classList.toggle('active',!!photo()&&Math.abs(state.zoom-1)<1e-9);
    updateBrushCursors();
  }
  function setMode(pane,mode){if(drawing()||!['original','compare','result'].includes(mode))return;pane.mode=mode;renderModes();scheduleDraw();}
  function renderModes(){
    for(const pane of Object.values(panes)){
      const comparing=!!photo()&&pane.mode==='compare',handle=pane.element.querySelector('.split-handle');
      handle.hidden=!comparing;handle.style.left=(pane.split*100)+'%';handle.setAttribute('aria-valuenow',String(Math.round(pane.split*100)));
      pane.element.querySelectorAll('.split-label').forEach(e=>e.hidden=!comparing);
      pane.element.querySelectorAll('[data-pane-mode]').forEach(b=>{const on=b.dataset.paneMode.split(':')[1]===pane.mode;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));b.disabled=!photo();});
    }
  }
  function setLayout(layout){
    if(drawing())return;
    state.layout=layout;panes.secondary.element.hidden=layout!=='dual';$('paneGrid').classList.toggle('dual',layout==='dual');
    document.querySelectorAll('[data-layout]').forEach(b=>{const active=b.dataset.layout===layout;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
    if(state.fit)requestAnimationFrame(()=>{if(state.fit)fit();});else scheduleDraw();
  }
  function ensureResult(){if(activePanes().every(p=>p.mode==='original'))setMode(panes.primary,'result');}
  async function api(path,options={}){
    const headers=new Headers(options.headers||{});if(state.token)headers.set('X-PortraitLab-Token',state.token);
    if(options.body&&!(options.body instanceof FormData)){headers.set('Content-Type','application/json');options.body=JSON.stringify(options.body);}
    const response=await fetch(path,{credentials:'same-origin',...options,headers});let data;
    try{data=await response.json();}catch{throw new Error('服务未返回有效数据，请检查本机服务。');}
    if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:data.detail?.message||data.message||JSON.stringify(data.detail||data));error.status=response.status;error.detail=data.detail;throw error;}return data;
  }
  const post=(path,body)=>api(path,{method:'POST',body});
  function merge(updated){
    const i=state.photos.findIndex(p=>p.id===updated.id);
    if(i<0)state.photos.push(updated);else state.photos[i]={...state.photos[i],...updated};
    for(const [key,item] of tileCache)if(item.photoId===updated.id&&item.layer!=='original'&&(item.version!==updated.version||(item.layer==='preview'&&item.draftId!==updated.draft?.id)))tileCache.delete(key);
  }
  function mutateSelection(next){
    if(!editable()||hasDraft()||same(next,state.desired))return;
    state.desired=[...next];state.sequence++;state.dirty=true;state.error=null;ensureResult();renderCandidateStates();renderControls();scheduleDraw();flushSelection();
  }
  async function flushSelection(){
    if(state.mutating||!state.dirty||!photo())return;
    state.mutating=true;renderControls();renderLibrary();
    try{
      let reconciled=false;
      while(state.dirty){
        const p=photo(),id=p.id,seq=state.sequence,requested=[...state.desired];
        let result;
        try{result=await post(`/api/photos/${encodeURIComponent(id)}/selection`,{selected_ids:requested,expected_version:p.version});}
        catch(error){
          if(error.status!==409||reconciled)throw error;
          reconciled=true;
          const data=await api(`/api/photos/${encodeURIComponent(id)}`),latest=data.photo||data;
          const signature=v=>JSON.stringify({generation:v.analysis_generation,face:v.face_index??v.analysis?.face_index,candidates:(v.candidates||[]).map(c=>({id:c.id,bounds:c.bounds}))});
          merge(latest);
          if(signature(p)!==signature(latest)||latest.analysis?.status==='running'){
            state.desired=[...(latest.selected_ids||[])];state.dirty=false;renderPhoto();
            throw new Error('照片分析状态已变化，已重新载入候选；请确认后再选择。');
          }
          continue;
        }
        const updated=result.photo||result;merge(updated);
        if(state.current!==id)break;
        if(seq===state.sequence){state.desired=[...(updated.selected_ids||requested)];state.dirty=false;state.message=state.desired.length?`已保留 ${state.desired.length} 处修补。`:'已关闭全部自动与手动修补；修片、塑形和灯光参数已保留。';}
        renderLibrary();renderCandidateStates();scheduleDraw();
      }
    }catch(e){state.error=e.message;state.message='本次修补尚未保留。';}
    finally{state.mutating=false;renderControls();renderLibrary();scheduleDraw();}
  }
  function toggleCandidate(id){
    const candidate=candidates().find(c=>c.id===id);if(!candidate)return;
    const ids=candidate.kind==='manual'&&candidate.group_id?candidates().filter(c=>c.kind==='manual'&&c.group_id===candidate.group_id).map(c=>c.id):[id];
    const allSelected=ids.every(selected),next=allSelected?state.desired.filter(x=>!ids.includes(x)):[...new Set([...state.desired,...ids])];mutateSelection(next);
  }
  async function refreshConflict(error,id){
    if(error.status!==409)return error.message;
    try{const data=await api(`/api/photos/${encodeURIComponent(id)}`);merge(data.photo||data);if(state.current===id){state.desired=[...(photo().selected_ids||[])];state.dirty=false;state.focused=null;renderPhoto();}}
    catch(refreshError){return error.message+' 重新载入失败：'+refreshError.message;}
    return '照片状态已变化，已重新载入。请检查当前画面后再次操作。';
  }
  async function applyHistory(direction){
    const p=photo(),stack=direction==='undo'?p?.history:p?.redo;
    if(!editable()||hasDraft()||state.mutating||state.dirty||!stack?.length)return;
    state.mutating=true;state.historyPending=true;state.error=null;renderControls();renderLibrary();
    try{const data=await post(`/api/photos/${encodeURIComponent(p.id)}/${direction}`,{expected_version:p.version});merge(data.photo||data);state.desired=[...(photo().selected_ids||[])];state.message=direction==='undo'?'已撤销上一项修补。':'已重做修补。';ensureResult();}
    catch(error){state.error=await refreshConflict(error,p.id);}
    finally{state.mutating=false;state.historyPending=false;renderControls();renderLibrary();scheduleDraw();}
  }
  const undo=()=>hasDraft()?undoDraftStroke():workspace()?.undoDraft()?undefined:applyHistory('undo'),redo=()=>workspace()?.redoDraft()?undefined:applyHistory('redo');
  async function selectPhoto(id){
    if(workspace()&&!workspace().canLeavePhoto()){renderStatus();return;}
    if(busy()||state.mutating||state.dirty){state.error='请等待当前修补保留完成后再切换照片。';renderStatus();return;}
    if(hasDraft()){state.error='有未提交的标记，请先修补或清除标记后再切换照片。';renderStatus();return;}
    clearTimeout(state.poll);state.poll=null;clearDraft(false);state.current=id;state.focused=null;state.error=null;
    try{const data=await api(`/api/photos/${encodeURIComponent(id)}`);if(state.current!==id)return;merge(data.photo||data);}catch(e){if(state.current!==id)return;state.error=e.message;}
    state.desired=[...(photo()?.selected_ids||[])];state.message='照片已打开。100% 可查看原片细节。';$('exportResult').hidden=true;
    if(photo()?.analysis?.status==='running'){state.jobKind=photo().analysis.kind;state.manualPending=state.jobKind==='manual';}
    renderPhoto();fit();if(photo()?.analysis?.status==='running')pollAnalysis(id);
  }
  function renderLibrary(){
    if(workspace()?.renderLibrary())return;
    $('photoCount').textContent=state.photos.length;$('photoList').replaceChildren();
    for(const p of state.photos){const button=document.createElement('button');button.className='photo-card'+(p.id===state.current?' active':'');button.disabled=busy()||state.mutating||state.dirty;
      const img=document.createElement('img');img.src=p.thumbnail_url||'';img.alt='';
      const copy=document.createElement('span');copy.className='copy';const name=document.createElement('strong');name.textContent=p.name;const meta=document.createElement('small');meta.textContent=p.width+' × '+p.height;copy.append(name,meta);button.append(img,copy);button.onclick=()=>selectPhoto(p.id);$('photoList').append(button);}
  }
  function renderPhoto(){
    workspace()?.photoChanged(photo());
    const p=photo();document.querySelector('.empty-state').hidden=!!p;$('photoName').textContent=p?.name||'尚未导入照片';$('photoMeta').textContent=p?`${p.width} × ${p.height} · 原尺寸图块显示`:'原尺寸图块 · 可逆局部修补';
    renderLibrary();renderFaces();renderCandidates();renderModes();renderControls();scheduleDraw();
  }
  function renderFaces(){
    const p=photo(),faces=p?.faces||[];$('faceSelect').replaceChildren();
    if(!faces.length){const o=new Option('自动选择主要人物','');$('faceSelect').append(o);}else for(const f of faces){const o=new Option(`人物 ${Number(f.face_index)+1}`,String(f.face_index));$('faceSelect').append(o);}
    if(Number.isInteger(p?.analysis?.face_index??p?.face_index))$('faceSelect').value=String(p.analysis?.face_index??p.face_index);
  }
  function renderCandidates(){
    $('candidateList').replaceChildren();thumbnailCanvases=[];const list=candidates();$('candidateCount').textContent=list.length;
    if(!list.length){const hint=document.createElement('p');hint.className='candidate-empty';hint.textContent=photo()?.analysis?.status==='ready'?'本次没有找到候选位置。这不代表照片没有瑕疵；本原型不会强行修改。':'分析完成后，点击候选查看原尺寸细节，勾选后修补。取消勾选可恢复该处。';$('candidateList').append(hint);}
    const seenGroups=new Set();let manualCount=0;
    list.forEach((c,i)=>{
      const manual=c.kind==='manual'||c.source==='manual',groupKey=manual&&c.group_id;
      if(groupKey&&seenGroups.has(groupKey))return;if(groupKey)seenGroups.add(groupKey);
      const members=groupKey?list.filter(item=>item.kind==='manual'&&item.group_id===groupKey):[c];
      const card=document.createElement('div');card.className='candidate-card'+(members.length>1?' candidate-group':'');card.dataset.candidate=c.id;card.dataset.candidateIds=JSON.stringify(members.map(item=>item.id));
      const canvas=document.createElement('canvas');canvas.width=canvas.height=116;canvas.setAttribute('aria-label',`查看位置 ${i+1} 原尺寸细节`);canvas.tabIndex=0;canvas.setAttribute('role','button');canvas.onclick=()=>focusCandidate(thumb.candidate.id);canvas.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();focusCandidate(thumb.candidate.id);}};
      const copy=document.createElement('div');copy.className='candidate-copy';const focus=document.createElement('button');focus.className='candidate-focus';focus.textContent=manual?`手动修补 ${++manualCount} · 查看细节`:`候选 ${i+1} · 查看细节`;focus.onclick=()=>focusCandidate(thumb.candidate.id);
      const meta=document.createElement('small');meta.textContent=manual?(members.length>1?`本次标记含 ${members.length} 个区域 · 整组开关`:'来自手动标记 · 可单独取消'):'疑似瑕疵 · 由你决定是否保留';copy.append(focus,meta);
      const label=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.setAttribute('aria-label',members.length>1?`开关本组 ${members.length} 个手动修补区域`:`修补候选 ${i+1}`);checkbox.onchange=()=>toggleCandidate(c.id);label.append(checkbox,document.createTextNode(members.length>1?'整组':'修补'));
      const thumb={canvas,candidate:c};card.append(canvas,copy,label);
      if(members.length>1){const locations=document.createElement('div');locations.className='group-locations';for(const member of members){const button=document.createElement('button');button.textContent='位置 '+(list.indexOf(member)+1);button.dataset.location=member.id;button.title='定位到此处原尺寸细节';button.onclick=()=>{thumb.candidate=member;canvas.setAttribute('aria-label',`查看位置 ${list.indexOf(member)+1} 原尺寸细节`);focusCandidate(member.id);};locations.append(button);}card.append(locations);}
      $('candidateList').append(card);thumbnailCanvases.push(thumb);
    });renderCandidateStates();
  }
  function renderCandidateStates(){
    for(const card of $('candidateList').children){if(!card.dataset.candidate)continue;const ids=JSON.parse(card.dataset.candidateIds||'[]'),all=ids.every(selected),some=ids.some(selected);card.classList.toggle('selected',some);card.classList.toggle('focused',ids.includes(state.focused));const checkbox=card.querySelector('input');checkbox.checked=all;checkbox.indeterminate=some&&!all;checkbox.disabled=!editable()||hasDraft();card.querySelectorAll('[data-location]').forEach(button=>button.classList.toggle('active',button.dataset.location===state.focused));}
    $('selectionSummary').textContent=candidates().length?`${state.desired.length} / ${candidates().length} 处已选择`:'尚无候选';
  }
  function renderControls(){
    const p=photo(),analysis=p?.analysis||{},running=analysis.status==='running';
    $('importButton').disabled=busy()||state.mutating||state.dirty||hasDraft();$('emptyImport').disabled=busy();
    $('exportButton').disabled=!p||busy()||state.dirty||state.mutating||running||hasDraft();
    for(const id of ['zoomNative','zoomFit','zoomIn','zoomOut','zoomSlider'])$(id).disabled=!p||drawing();
    $('analyzeButton').disabled=!p||busy()||running||state.mutating||state.dirty||hasDraft();$('analyzeButton').textContent=running?(analysis.kind==='manual'?'正在修补标记…':'正在分析候选…'):candidates().length?'重新分析候选':'分析候选瑕疵';
    $('faceSelect').disabled=!p||running||busy()||!(p.faces||[]).length;$('focusFace').disabled=!p||!(p.faces||[]).length;
    $('deviceSelect').disabled=running||busy();$('clearSelection').disabled=$('restoreAll').disabled=!editable()||!state.desired.length||drawing();
    $('restoreState').textContent=state.desired.length?`已应用 ${state.desired.length} 处修补`:'当前未应用修补';
    $('restoreAll').title=state.desired.length?'关闭全部自动与手动修补，清除未提交标记；可撤销。':'当前未应用任何修补';
    $('undoButton').disabled=!editable()||state.mutating||state.dirty||(!hasDraft()&&!p?.history?.length);$('undoButton').textContent=hasDraft()?'撤销标记':'撤销';$('redoButton').disabled=!editable()||hasDraft()||state.mutating||state.dirty||!p?.redo?.length;
    let analysisText=analysis.message||(running?'正在分析照片。候选位置需要你确认。':analysis.status==='ready'?'分析已完成。没有自动应用任何新修补。':'分析不会自动修改照片；若重新分析，将替换本次候选列表。');
    if(analysis.status==='ready')analysisText=candidates().length?`共有 ${candidates().length} 处修补位置，${state.desired.length?`已选择 ${state.desired.length} 处修补。`:'尚未选择修补，原片保持不变。'}`:'本次没有找到候选位置。';
    if(analysis.status==='error'&&candidates().length)analysisText=(analysis.kind==='manual'?'本次修补未完成，原有修补仍保留。 ':'本次分析未完成，以下保留上一轮候选。 ')+analysisText;
    const analyzedFace=analysis.face_index??p?.face_index;
    if(!running&&candidates().length&&Number.isInteger(analyzedFace)){
      analysisText=`自动候选来自人物 ${analyzedFace+1}。 `+analysisText;
      if($('faceSelect').value!==String(analyzedFace))analysisText+=` 已选人物 ${Number($('faceSelect').value)+1}，重新分析后替换候选。`;
    }
    $('analysisMessage').textContent=analysisText;
    renderManualControls();renderCandidateStates();workspace()?.renderControls();renderStatus();
  }
  function renderStatus(){
    let text=state.message;const analysisError=photo()?.analysis?.status==='error'?photo().analysis.message:null;
    if(state.importing)text='正在导入照片并准备原尺寸图块…';
    else if(state.exporting)text='正在导出原尺寸 PNG…';
    else if(state.manualPending)text=photo()?.analysis?.message||'正在修补标记区域，请等待处理完成…';
    else if(state.mutating)text='正在更新局部修补…';
    else if(state.dirty)text='修补尚未保留。';
    else if(photo()?.analysis?.status==='running')text=photo().analysis.message||'正在分析候选瑕疵…';
    else if(state.pendingTiles)text=state.tileErrors?'部分原尺寸图块加载失败。':'正在加载原尺寸图块…';
    if(state.error)text+=' '+state.error;else if(analysisError)text+=(photo()?.analysis?.kind==='manual'?' 修补未完成：':' 分析未完成：')+analysisError;
    $('statusText').textContent=text;$('statusBar').classList.toggle('error',!!state.error||!!state.tileErrors||!!analysisError);$('retryButton').hidden=!(state.error||state.tileErrors||analysisError);
    $('retryButton').textContent=state.dirty||state.tileErrors||!state.token||workspace()?.state.previewError||(state.manualError&&!state.draftNeedsReview)||(!hasDraft()&&analysisError)?'重试':'关闭提示';
  }
  function focusCandidate(id){
    if(drawing())return;
    const c=candidates().find(c=>c.id===id);if(!c)return;state.focused=id;state.center={x:(c.bounds[0]+c.bounds[2])/2,y:(c.bounds[1]+c.bounds[3])/2};state.zoom=1;state.fit=false;state.message='已定位候选。100% 查看原片细节，勾选后修补。';renderCandidateStates();updateZoom();scheduleDraw();
  }
  function focusFace(){
    if(drawing())return;
    const p=photo(),f=p?.faces?.find(f=>String(f.face_index)===$('faceSelect').value)||p?.faces?.[0];if(!f?.bounds)return;
    const b=f.bounds,sizes=activePanes().map(viewportSize);state.center={x:(b[0]+b[2])/2,y:(b[1]+b[3])/2};state.zoom=clamp(Math.min(...sizes.map(s=>Math.min((s.width-40)/((b[2]-b[0])*1.25),(s.height-40)/((b[3]-b[1])*1.25)))),.005,8);state.fit=false;updateZoom();scheduleDraw();
  }
  function inPhoto(point){const p=photo();return !!p&&point.x>=0&&point.y>=0&&point.x<p.width&&point.y<p.height;}
  function setTool(tool){if(drawing()||state.manualPending||!['browse','manual'].includes(tool))return;state.tool=tool;state.spacePan=false;renderManualControls();scheduleDraw();}
  function updateBrushCursors(){
    for(const pane of Object.values(panes)){
      if(!pane.cursor)continue;
      const point=pane.pointer?imagePoint(pane,pane.pointer):null;
      const visible=state.tool==='manual'&&!state.spacePan&&canMark()&&point&&inPhoto(point)&&pane.splitPointer===null;
      pane.cursor.hidden=!visible;pane.viewport.classList.toggle('manual-marking',state.tool==='manual');pane.viewport.classList.toggle('brush-visible',!!visible);pane.viewport.classList.toggle('space-pan',state.spacePan);
      if(visible){const r=pane.viewport.getBoundingClientRect(),diameter=state.brushDiameter*state.zoom;pane.cursor.style.left=(pane.pointer.clientX-r.left)+'px';pane.cursor.style.top=(pane.pointer.clientY-r.top)+'px';pane.cursor.style.width=diameter+'px';pane.cursor.style.height=diameter+'px';}
    }
    workspace()?.updateCursors();
  }
  function renderManualControls(){
    $('manualToolbar').hidden=state.tool!=='manual'&&!hasDraft()&&!state.manualPending;
    for(const [id,tool] of [['browseTool','browse'],['manualTool','manual']]){const on=state.tool===tool;$(id).classList.toggle('active',on);$(id).setAttribute('aria-pressed',String(on));$(id).disabled=!photo()||state.manualPending||drawing();}
    $('brushDiameter').disabled=!canMark()||drawing();$('brushSizeValue').textContent=state.brushDiameter+' px';
    $('undoStroke').disabled=$('clearDraft').disabled=!hasDraft()||state.manualPending;
    $('applyManual').disabled=!hasDraft()||!canMark()||drawing();$('applyManual').textContent=state.manualPending?'正在修补标记…':state.draftNeedsReview?'确认标记并修补':'修补标记区域';
    const count=new Set(state.draftStrokes.map(s=>s.group)).size;
    $('draftMessage').textContent=state.manualPending?'正在处理已提交的标记，完成后可撤销本次修补。':state.draftNeedsReview?'照片状态已变化。标记仍保留，请核对后点击“确认标记并修补”。':hasDraft()?`${count} 笔草稿，尚未修补。直径以原图像素计；空格平移，Esc 清除。`:'在任一视图点击或涂抹；粉色标记是草稿。按住空格平移，Esc 清除。';
    updateBrushCursors();
  }
  function draftChanged(){
    state.draftRequestId=hasDraft()?crypto.randomUUID():null;state.manualError=false;state.draftNeedsReview=false;
    if(!hasDraft())state.draftBase=null;
    renderControls();scheduleDraw();
  }
  function addBrushPoint(pane,event){
    const drag=pane.brushDrag;if(!drag)return;const point=imagePoint(pane,event);
    const viewport=pane.viewport.getBoundingClientRect();
    if(event.clientX<viewport.left||event.clientX>=viewport.right||event.clientY<viewport.top||event.clientY>=viewport.bottom||!inPhoto(point)){drag.stroke=null;return;}
    const pointsCount=state.draftStrokes.reduce((n,s)=>n+s.points.length,0);
    if(pointsCount>=5000||(!drag.stroke&&state.draftStrokes.length>=100)){state.error='本次标记数量已达上限，请先提交这一批，再继续标记。';renderStatus();return;}
    if(!drag.stroke){
      if(!state.draftBase){const p=photo();state.draftBase={photoId:p.id,version:p.version,generation:p.analysis_generation};}
      drag.stroke={radius:drag.radius,points:[],group:drag.group};state.draftStrokes.push(drag.stroke);
    }
    const previous=drag.stroke.points.at(-1);if(previous&&Math.hypot(point.x-previous[0],point.y-previous[1])<Math.max(.75,drag.radius/4))return;
    drag.stroke.points.push([point.x,point.y]);draftChanged();
  }
  function endBrush(pane,cancel=false){
    const drag=pane.brushDrag;if(!drag)return;pane.brushDrag=null;
    if(pane.viewport.hasPointerCapture?.(drag.id))pane.viewport.releasePointerCapture(drag.id);
    if(cancel){state.draftStrokes=state.draftStrokes.filter(s=>s.group!==drag.group);draftChanged();}else renderControls();
    updateBrushCursors();scheduleDraw();
  }
  function clearDraft(notify=true){
    if(state.manualPending)return;
    for(const pane of Object.values(panes))endBrush(pane);
    state.draftStrokes=[];state.draftBase=null;state.draftRequestId=null;state.draftNeedsReview=false;state.manualError=false;
    if(notify){state.error=null;state.message='未提交的标记已清除，已保留的修补不受影响。';}
    renderControls();scheduleDraw();
  }
  function undoDraftStroke(){
    if(!hasDraft()||state.manualPending)return;
    for(const pane of Object.values(panes))endBrush(pane);
    const group=state.draftStrokes.at(-1).group;state.draftStrokes=state.draftStrokes.filter(s=>s.group!==group);state.message='已撤销上一笔标记。';draftChanged();
  }
  function clearRepairs(){if(!editable()||!state.desired.length||drawing())return;clearDraft(false);mutateSelection([]);}
  function restoreAll(){if(workspace())return workspace().resetPhoto();return clearRepairs();}
  async function submitManual(){
    if(!canMark()||!hasDraft()||drawing()||state.manualPending)return;
    const p=photo(),id=p.id;if(state.draftBase?.photoId!==id)return;
    const previousAnalysis=p.analysis,body={expected_version:state.draftBase.version,request_id:state.draftRequestId,device:$('deviceSelect').value,strokes:state.draftStrokes.map(s=>({radius:s.radius,points:s.points.map(point=>[...point])}))};
    state.manualPending=true;state.jobKind='manual';state.manualError=false;state.error=null;p.analysis={status:'running',kind:'manual',message:'正在提交标记区域…'};renderControls();renderLibrary();
    try{const data=await post(`/api/photos/${encodeURIComponent(id)}/manual`,body);merge(data.photo||data);pollAnalysis(id);}
    catch(error){
      state.manualPending=false;
      if(error.status===409){
        state.error=await refreshConflict(error,id);state.draftNeedsReview=true;state.draftRequestId=crypto.randomUUID();state.draftBase={photoId:id,version:photo().version,generation:photo().analysis_generation};
        if(photo().analysis?.status==='running'){state.jobKind='external';pollAnalysis(id);}else state.jobKind=null;
      }
      else{photo().analysis=previousAnalysis;state.error=error.message;state.manualError=true;}
      if(error.status!==409)state.jobKind=null;renderControls();renderLibrary();scheduleDraw();
    }
  }
  async function analyze(){
    if(!photo()||$('analyzeButton').disabled)return;
    if(state.desired.length){state.error='重新分析前，请先点击“恢复全部”清除当前修补。';renderStatus();return;}
    const id=photo().id,body={device:$('deviceSelect').value};if($('faceSelect').value!=='')body.face_index=Number($('faceSelect').value);
    state.error=null;state.jobKind='automatic';photo().analysis={status:'running',kind:'automatic',message:'正在准备分析…'};renderControls();
    try{const data=await post(`/api/photos/${encodeURIComponent(id)}/analyze`,body);if(data.photo)merge(data.photo);else if(data.id===id)merge(data);if(state.current===id){renderControls();pollAnalysis(id);}}
    catch(e){if(state.current===id){photo().analysis={status:'error',message:e.message};state.error=e.message;renderControls();}}
  }
  async function pollAnalysis(id){
    clearTimeout(state.poll);if(state.current!==id)return;
    try{const data=await api(`/api/photos/${encodeURIComponent(id)}`);if(state.current!==id)return;const updated=data.photo||data;merge(updated);
      if(updated.analysis?.status==='running'){renderControls();state.poll=setTimeout(()=>pollAnalysis(id),700);return;}
      const external=state.jobKind==='external',wasManual=!external&&(state.manualPending||state.jobKind==='manual');state.manualPending=false;state.jobKind=null;
      state.desired=[...(updated.selected_ids||[])];state.dirty=false;state.focused=null;
      if(wasManual&&updated.analysis?.status!=='error'){clearDraft(false);ensureResult();}
      if(external&&hasDraft()){state.draftBase={photoId:id,version:updated.version,generation:updated.analysis_generation};state.draftRequestId=crypto.randomUUID();state.draftNeedsReview=true;}
      state.manualError=wasManual&&updated.analysis?.status==='error';
      state.message=external?'其他处理已结束，标记草稿仍保留。请核对后确认修补。':updated.analysis?.status==='error'?(wasManual?'标记修补未完成，草稿已保留。':'分析未完成。'):wasManual?'标记区域已修补；可撤销本次修补，也可取消单项。':`分析完成，发现 ${updated.candidates?.length||0} 处候选，尚未自动修补。`;state.error=updated.analysis?.status==='error'?updated.analysis.message:null;renderPhoto();
    }catch(e){state.error=e.message;renderStatus();state.poll=setTimeout(()=>pollAnalysis(id),2000);}
  }
  async function importFiles(files){
    if(workspace()&&!workspace().canLeavePhoto()){renderStatus();return;}
    if(busy()||state.mutating||state.dirty)return;
    if(hasDraft()){state.error='有未提交的标记，请先修补或清除标记后再导入照片。';renderStatus();return;}
    const accepted=[...files].filter(f=>/\.(jpe?g|png|tiff?|webp)$/i.test(f.name));if(!accepted.length){state.error='请选择 JPEG、PNG、TIFF 或 WebP 照片。';renderStatus();return;}
    const form=new FormData();accepted.forEach(f=>form.append('files',f));state.importing=true;state.error=null;renderControls();
    let ids=[],importError=null;
    try{const data=await api('/api/import',{method:'POST',body:form});for(const p of data.photos||[]){merge(p);ids.push(p.id);}if(data.errors?.length)state.error=data.errors.map(e=>typeof e==='string'?e:e.error||e.message).join('；');}
    catch(e){state.error=e.message;}
    finally{state.importing=false;$('fileInput').value='';renderControls();renderLibrary();}
    importError=state.error;if(ids.length)await selectPhoto(ids[0]);if(importError){state.error=importError;renderStatus();}
  }
  async function exportPhoto(){
    const p=photo();if(!p||$('exportButton').disabled)return;state.exporting=true;state.error=null;renderControls();
    try{const data=await post(`/api/photos/${encodeURIComponent(p.id)}/export`,{expected_version:p.version});const box=$('exportResult');box.replaceChildren();box.hidden=false;const text=document.createElement('div');text.textContent='原尺寸 PNG 已导出：'+(data.path||'');box.append(text);if(data.url){const a=document.createElement('a');a.href=data.url;a.download='';a.textContent='下载导出的 PNG';box.append(a);}state.message='原尺寸 PNG 已导出。';}
    catch(e){state.error=await refreshConflict(e,p.id);}
    finally{state.exporting=false;renderControls();}
  }
  function hitCandidate(pane,event){
    if(!$('showMarkers').checked||workspace()?.showCandidates()===false||workspace()?.hideSourceOverlay(pane))return null;const rect=pane.viewport.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
    return candidates().map(c=>({c,m:markerGeometry(pane,c)})).map(v=>({...v,d:Math.hypot(x-v.m.x,y-v.m.y)})).filter(v=>v.d<=Math.max(13,v.m.r)).sort((a,b)=>a.d-b.d)[0]?.c;
  }
  for(const pane of Object.values(panes)){
    pane.cursor=document.createElement('div');pane.cursor.className='brush-cursor';pane.cursor.hidden=true;pane.cursor.setAttribute('aria-hidden','true');pane.viewport.append(pane.cursor);
    pane.viewport.addEventListener('pointerenter',e=>{state.hovered=pane.name;pane.pointer={clientX:e.clientX,clientY:e.clientY};pane.viewport.classList.add('is-hover-target');updateBrushCursors();});
    pane.viewport.addEventListener('pointerleave',()=>{if(!pane.drag&&!pane.brushDrag)state.hovered=null;pane.pointer=null;pane.viewport.classList.remove('is-hover-target');updateBrushCursors();});
    pane.viewport.addEventListener('wheel',e=>{if(!photo())return;e.preventDefault();if(pane.drag||drawing())return;pane.pointer={clientX:e.clientX,clientY:e.clientY};setZoom(state.zoom*Math.exp(-clamp(e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?pane.viewport.clientHeight:1),-500,500)*.0015),pane,e.clientX,e.clientY);},{passive:false});
    pane.viewport.addEventListener('pointerdown',e=>{
      if(!photo()||e.button!==0||e.target.closest('.split-handle')||drawing())return;e.preventDefault();pane.viewport.focus({preventScroll:true});
      if(workspace()?.pointerDown(pane,e))return;
      if(state.tool==='manual'&&!state.spacePan){
        if(!canMark()||!inPhoto(imagePoint(pane,e)))return;
        pane.brushDrag={id:e.pointerId,radius:state.brushDiameter/2,group:++state.draftSerial,stroke:null};pane.viewport.setPointerCapture(e.pointerId);state.error=null;addBrushPoint(pane,e);return;
      }
      pane.drag={id:e.pointerId,x:e.clientX,y:e.clientY,cx:state.center.x,cy:state.center.y,moved:false,panOnly:state.spacePan||state.tool==='manual'};pane.viewport.setPointerCapture(e.pointerId);
    });
    pane.viewport.addEventListener('pointermove',e=>{
      pane.pointer={clientX:e.clientX,clientY:e.clientY};updateBrushCursors();
      if(workspace()?.pointerMove(pane,e))return;
      if(pane.brushDrag?.id===e.pointerId){addBrushPoint(pane,e);return;}
      const d=pane.drag;if(!d||e.pointerId!==d.id)return;if(Math.hypot(e.clientX-d.x,e.clientY-d.y)>3)d.moved=true;if(d.moved){state.fit=false;state.center={x:d.cx-(e.clientX-d.x)/state.zoom,y:d.cy-(e.clientY-d.y)/state.zoom};pane.viewport.classList.add('is-panning');scheduleDraw();}
    });
    const end=(e,cancel=false)=>{
      if(workspace()?.pointerEnd(pane,e,cancel))return;
      if(pane.brushDrag?.id===e.pointerId){if(!cancel)addBrushPoint(pane,e);endBrush(pane,cancel);return;}
      const d=pane.drag;if(!d||e.pointerId!==d.id)return;pane.drag=null;pane.viewport.classList.remove('is-panning');if(pane.viewport.hasPointerCapture?.(d.id))pane.viewport.releasePointerCapture(d.id);
      if(!cancel&&!d.moved&&!d.panOnly){const c=hitCandidate(pane,e);if(c){state.focused=c.id;toggleCandidate(c.id);}}
    };
    pane.viewport.addEventListener('pointerup',e=>end(e));pane.viewport.addEventListener('pointercancel',e=>end(e,true));pane.viewport.addEventListener('lostpointercapture',e=>end(e,true));
    pane.viewport.addEventListener('dblclick',e=>{if(!photo()||state.tool==='manual'||workspace()?.isToolActive()||state.spacePan||e.target.closest('.split-handle'))return;setZoom(1,pane,e.clientX,e.clientY);});
    const handle=pane.element.querySelector('.split-handle');
    handle.addEventListener('pointerdown',e=>{if(e.button!==0||drawing())return;e.preventDefault();e.stopPropagation();pane.splitPointer=e.pointerId;handle.setPointerCapture(e.pointerId);updateBrushCursors();});
    handle.addEventListener('pointermove',e=>{if(e.pointerId!==pane.splitPointer)return;const r=pane.viewport.getBoundingClientRect();pane.split=clamp((e.clientX-r.left)/r.width,.02,.98);renderModes();scheduleDraw();});
    const endSplit=e=>{if(e.pointerId===pane.splitPointer){pane.splitPointer=null;if(handle.hasPointerCapture?.(e.pointerId))handle.releasePointerCapture(e.pointerId);}};
    for(const name of ['pointerup','pointercancel','lostpointercapture'])handle.addEventListener(name,endSplit);
    handle.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();pane.split=e.key==='Home' ? .02 : e.key==='End' ? .98 : clamp(pane.split+(e.key==='ArrowLeft'?-.01:.01)*(e.shiftKey?10:1),.02,.98);renderModes();scheduleDraw();});
    new ResizeObserver(()=>{if(state.fit)fit();else scheduleDraw();}).observe(pane.viewport);
  }
  for(const b of document.querySelectorAll('[data-layout]'))b.onclick=()=>setLayout(b.dataset.layout);
  for(const b of document.querySelectorAll('[data-pane-mode]'))b.onclick=()=>{const [name,mode]=b.dataset.paneMode.split(':');setMode(panes[name],mode);};
  $('zoomIn').onclick=()=>setZoom(state.zoom*1.25);$('zoomOut').onclick=()=>setZoom(state.zoom/1.25);$('zoomSlider').oninput=e=>setZoom(.005*1600**(Number(e.target.value)/1000));$('zoomNative').onclick=()=>setZoom(1);$('zoomFit').onclick=fit;
  for(const id of ['importButton','emptyImport'])$(id).onclick=()=>$('fileInput').click();$('fileInput').onchange=e=>importFiles(e.target.files);
  $('analyzeButton').onclick=analyze;$('focusFace').onclick=focusFace;$('faceSelect').onchange=()=>{focusFace();renderControls();};$('showMarkers').onchange=scheduleDraw;
  $('browseTool').onclick=()=>{workspace()?.clearTool();setTool('browse');};$('manualTool').onclick=()=>{workspace()?.clearTool();setTool('manual');};
  $('brushDiameter').oninput=e=>{state.brushDiameter=clamp(Number(e.target.value),4,160);renderManualControls();};
  $('deviceSelect').onchange=()=>{if(hasDraft()&&!state.manualPending)state.draftRequestId=crypto.randomUUID();};
  $('undoStroke').onclick=undoDraftStroke;$('clearDraft').onclick=()=>clearDraft();$('applyManual').onclick=submitManual;
  document.querySelector('.tool-scroll').addEventListener('scroll',scheduleDraw,{passive:true});
  $('clearSelection').onclick=clearRepairs;$('restoreAll').onclick=restoreAll;$('undoButton').onclick=undo;$('redoButton').onclick=redo;$('exportButton').onclick=exportPhoto;
  $('retryButton').onclick=()=>{state.error=null;for(const [key,item] of tileCache)if(item.state==='error')tileCache.delete(key);if(!state.token)boot();else if(workspace()?.retryPreview())return;else if(state.dirty)flushSelection();else if(state.manualError&&!state.draftNeedsReview)submitManual();else if(!hasDraft()&&photo()?.analysis?.status==='error')analyze();scheduleDraw();};
  document.addEventListener('keydown',e=>{
    const typing=!!e.target.closest?.('input:not([type=range]):not([type=checkbox]),textarea,select,[contenteditable=true]');if(typing||e.isComposing)return;
    if(workspace()?.handleKey(e))return;
    if(e.key==='Escape'&&hasDraft()&&!state.manualPending){e.preventDefault();clearDraft();return;}
    if(e.code==='Space'&&state.hovered&&photo()&&!e.target.closest?.('button,input')){e.preventDefault();for(const pane of Object.values(panes))endBrush(pane);state.spacePan=true;updateBrushCursors();return;}
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo();return;}
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='y'){e.preventDefault();redo();return;}
    if(e.key.toLowerCase()==='v'&&!e.ctrlKey&&!e.altKey&&!e.metaKey&&!e.shiftKey&&!e.repeat&&state.hovered&&photo()){
      const pane=panes[state.hovered];if(pane.drag||drawing()||pane.splitPointer!==null||pane.element.hidden)return;e.preventDefault();const modes=['original','compare','result'];setMode(pane,modes[(modes.indexOf(pane.mode)+1)%3]);
    }
  });
  document.addEventListener('keyup',e=>{if(e.code==='Space'){state.spacePan=false;updateBrushCursors();workspace()?.updateCursors();}});
  let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();dragDepth++;document.body.classList.add('drop-active');}});
  document.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});document.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0;document.body.classList.remove('drop-active');}});
  document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;document.body.classList.remove('drop-active');importFiles(e.dataTransfer?.files||[]);});
  window.addEventListener('blur',()=>{state.hovered=null;state.spacePan=false;for(const pane of Object.values(panes)){
    endBrush(pane,true);pane.pointer=null;
    if(pane.drag&&pane.viewport.hasPointerCapture?.(pane.drag.id))pane.viewport.releasePointerCapture(pane.drag.id);
    const handle=pane.element.querySelector('.split-handle');if(pane.splitPointer!==null&&handle.hasPointerCapture?.(pane.splitPointer))handle.releasePointerCapture(pane.splitPointer);
    pane.drag=null;pane.splitPointer=null;pane.viewport.classList.remove('is-panning','is-hover-target');
  }workspace()?.cancelPointer();updateBrushCursors();});
  async function boot(){try{const data=await api('/api/bootstrap');state.token=data.token;state.photos=data.photos||[];state.message='本机服务已连接。';renderPhoto();if(state.photos.length)await selectPhoto(data.current_id||state.photos[0].id);}catch(e){state.error=e.message;renderStatus();}}
  // The desktop host awaits this contract before terminating its owned service.
  // Keep closing explicit: server drafts and brush marks must never disappear silently.
  window.portraitlabPrepareToQuit=async()=>{
    if(['running','downloading'].includes(workspace()?.state.models?.download?.status))
      throw new Error('模型正在下载，请等待下载结束后再退出。');
    if(busy()||state.mutating||state.dirty||state.historyPending||state.photos.some(p=>p.analysis?.status==='running'))
      throw new Error('仍有处理或保存任务，请等待完成后再退出。');
    if(hasDraft())throw new Error('手动标记尚未修补，请先修补标记区域，或清除标记后再退出。');
    if(!(workspace()?.canUseCommittedActions()??true))
      throw new Error('调整尚未保留，请先点击“保留调整”或“取消草稿”后再退出。');
  };
  // Expose state and coordinate helpers for deterministic local prototype QA.
  window.PortraitV2={state,panes,photo,levelFor,imagePoint,transform,setZoom,fit,setMode,setLayout,focusCandidate,toggleCandidate,undo,redo,flushSelection,drawAll,api,tileCache,setTool,clearDraft,undoDraftStroke,submitManual,restoreAll,merge,renderPhoto,renderControls,renderLibrary,renderModes,scheduleDraw,selectPhoto,ensureResult,renderStatus,exportPhoto};
  renderPhoto();boot();
})();
