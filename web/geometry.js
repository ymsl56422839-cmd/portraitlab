/* Shared source/display coordinates. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 const fields=['width_left','width_right','cheek_left','cheek_right','jaw_left','jaw_right','chin_length','chin_width'];
 const empty=()=>({faces:[],strokes:[]});
 function canonical(value){const s=value||empty();return {faces:(s.faces||[]).map(f=>Object.fromEntries([['face_index',f.face_index],...fields.map(k=>[k,Number(f[k]||0)])])).sort((a,b)=>a.face_index-b.face_index),strokes:(s.strokes||[]).map(s=>({mode:s.mode,x:Number(s.x),y:Number(s.y),dx:Number(s.dx||0),dy:Number(s.dy||0),radius:Number(s.radius),strength:Number(s.strength??1)}))};}
 const key=value=>JSON.stringify(canonical(value));
 const hasShape=value=>{const s=canonical(value);return s.strokes.length>0||s.faces.some(f=>fields.some(k=>f[k]!==0));};
 const operations=p=>p?.shape_geometry?.operations||[];
 function inverse(point,ops){let q={x:point.x,y:point.y},x=0,y=0,weight=1;for(let i=ops.length-1;i>=0;i--){const o=ops[i],r2=((q.x-o.cx)/o.rx)**2+((q.y-o.cy)/o.ry)**2;if(r2>=1)continue;const amount=(o.strength??1)*(1-r2)**3;if(o.kind==='restore'){x+=weight*amount*q.x;y+=weight*amount*q.y;weight*=1-amount;}else{q.x-=o.dx*amount;q.y-=o.dy*amount;}}return {x:x+weight*q.x,y:y+weight*q.y};}
 function forward(point,ops){
  let result={...point};
  for(let i=0;i<40;i++){const source=inverse(result,ops),dx=point.x-source.x,dy=point.y-source.y;if(Math.max(Math.abs(dx),Math.abs(dy))<=1e-9)return result;result.x+=dx;result.y+=dy;}
  const h=1e-5;
  for(let i=0;i<20;i++){
   const mapped=inverse(result,ops),ex=point.x-mapped.x,ey=point.y-mapped.y;if(Math.max(Math.abs(ex),Math.abs(ey))<=1e-8)return result;
   const x=inverse({x:result.x+h,y:result.y},ops),y=inverse({x:result.x,y:result.y+h},ops),a=(x.x-mapped.x)/h,b=(y.x-mapped.x)/h,c=(x.y-mapped.y)/h,d=(y.y-mapped.y)/h,det=a*d-c*b;
   let dx=Math.abs(det)>1e-8?(ex*d-ey*b)/det:ex,dy=Math.abs(det)>1e-8?(a*ey-c*ex)/det:ey;dx=Math.max(-.1,Math.min(.1,dx));dy=Math.max(-.1,Math.min(.1,dy));let proposal;
   for(let j=0;j<8;j++){proposal={x:result.x+dx,y:result.y+dy};const m=inverse(proposal,ops);if((point.x-m.x)**2+(point.y-m.y)**2<ex*ex+ey*ey)break;dx*=.5;dy*=.5;proposal={x:result.x+dx,y:result.y+dy};}
   result=proposal;
  }return result;
 }
 const toSource=(point,p=photo())=>inverse(point,operations(p));
 const toView=(point,p=photo())=>forward(point,operations(p));
 const clampPoint=p=>({x:Math.max(0,Math.min(1,p.x)),y:Math.max(0,Math.min(1,p.y))});
 function ready(p=photo(),pane='primary'){
  if(!p)return false;
  if(hasShape(p.face_shape)&&(!p.shape_geometry||key(p.shape_geometry.face_shape)!==key(p.face_shape)))return false;
  if(p.shape_geometry&&key(p.shape_geometry.face_shape)!==key(p.face_shape))return false;
  const image=$(previewPanes[pane]?.result||'resultImage'),expected=p.preview_url||p.original_url;
  const loaded=(img,url)=>!!img?.complete&&img.naturalWidth>0&&img.getAttribute('src')===url&&img.dataset.loadedUrl===url;
  if(!loaded(image,expected))return false;
  if(pane==='primary'&&state.view==='mask'){const mask=$('maskImage');if(!loaded($('originalImage'),expected)||(!mask.hidden&&!loaded(mask,mask.getAttribute('src'))))return false;}
  return true;
 }
 function refresh(){document.dispatchEvent(new Event('portraitlab-coordinate-state'));}
 // A result image and its map become editable together, after the image loads.
 for(const id of [...Object.values(previewPanes).flatMap(p=>[p.result,p.original]),'maskImage']){const image=$(id);image.addEventListener('load',()=>{image.dataset.loadedUrl=image.getAttribute('src')||'';refresh();});image.addEventListener('error',()=>{image.dataset.loadedUrl='';refresh();});}
 function eventPoint(event,stage,p=photo()) {const r=stage.getBoundingClientRect();return clampPoint(toSource({x:(event.clientX-r.left)/Math.max(1,r.width),y:(event.clientY-r.top)/Math.max(1,r.height)},p));}
 function circle(point,radius,p=photo(),segments=48){const size=Math.min(p.width,p.height);return Array.from({length:segments+1},(_,i)=>toView({x:point.x+Math.cos(i*2*Math.PI/segments)*radius*size/Math.max(1,p.width-1),y:point.y+Math.sin(i*2*Math.PI/segments)*radius*size/Math.max(1,p.height-1)},p));}
 function rectPoints(r,p=photo(),segments=16){const points=[];for(let edge=0;edge<4;edge++)for(let i=0;i<segments;i++){const t=i/segments;points.push(toView(edge===0?{x:r.x+t*r.width,y:r.y}:edge===1?{x:r.x+r.width,y:r.y+t*r.height}:edge===2?{x:r.x+(1-t)*r.width,y:r.y+r.height}:{x:r.x,y:r.y+(1-t)*r.height},p));}return points;}
 window.PortraitGeometry={fields,empty,canonical,key,hasShape,operations,inverse,forward,toSource,toView,ready,refresh,eventPoint,circle,rectPoints,clampPoint};
})();
