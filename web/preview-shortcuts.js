/* Pointer-scoped preview shortcuts. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 let hovered=null,pointer=null,pressed=false;
 function clear(){hovered=null;pointer=null;pressed=false;for(const pane of document.querySelectorAll('.preview-pane'))pane.classList.remove('is-hover-target');}
 function track(name,event){hovered=name;pointer={x:event.clientX,y:event.clientY};for(const [id,pane] of Object.entries(previewPanes))$(pane.viewport).closest('.preview-pane').classList.toggle('is-hover-target',id===name);}
 for(const [name,pane] of Object.entries(previewPanes)){
  const viewport=$(pane.viewport);
  viewport.addEventListener('pointerenter',event=>track(name,event));
  viewport.addEventListener('pointermove',event=>track(name,event));
  viewport.addEventListener('pointerleave',event=>{if(!viewport.contains(event.relatedTarget)&&hovered===name)clear();});
  viewport.addEventListener('pointerdown',()=>{pressed=true;},true);
 }
 document.addEventListener('pointermove',event=>{if(pointer)pointer={x:event.clientX,y:event.clientY};},true);
 document.addEventListener('pointerup',()=>{pressed=false;},true);
 document.addEventListener('pointercancel',clear,true);
 document.addEventListener('pointerout',event=>{if(!event.relatedTarget)clear();});
 window.addEventListener('blur',clear);
 document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')clear();});
 function typing(element){return !!element&&(element.isContentEditable||!!element.closest?.('input,textarea,select,[role="textbox"],[contenteditable]:not([contenteditable="false"])'));}
 document.addEventListener('keydown',event=>{
  if(event.code!=='KeyV'&&event.key?.toLowerCase()!=='v')return;
  if(event.defaultPrevented||event.repeat||event.isComposing||event.keyCode===229||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return;
  if(!hovered||!pointer||!photo()||pressed||drawing||previewHolding||window.PortraitSelection?.isDragging()||window.PortraitSpotRepair?.isDragging()||window.PortraitShape?.isDragging())return;
  if(document.querySelector('dialog[open]')||typing(event.target)||typing(document.activeElement))return;
  if(hovered==='secondary'&&state.layout!=='dual')return;
  const pane=previewPanes[hovered],viewport=$(pane.viewport),bounds=viewport.getBoundingClientRect();
  if(pointer.x<bounds.left||pointer.x>=bounds.right||pointer.y<bounds.top||pointer.y>=bounds.bottom)return;
  if(typeof document.elementFromPoint==='function'){const hit=document.elementFromPoint(pointer.x,pointer.y);if(!hit||!viewport.contains(hit))return;}
  const modes=['original','compare','result'],next=modes[(modes.indexOf(state[pane.view])+1)%modes.length];
  event.preventDefault();setView(next,hovered);
  toast(`${state.layout==='dual'?(hovered==='primary'?'左视图':'右视图'):'图像预览'} · ${{original:'原图',compare:'滑动对比',result:'处理结果'}[next]}`);
 });
})();
