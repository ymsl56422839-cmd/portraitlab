/* Deterministic image-space lighting preview. SPDX-License-Identifier: AGPL-3.0-only */
'use strict';
(() => {
 const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
 const linear=v=>v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);
 const srgb=v=>v<=.0031308?12.92*v:1.055*Math.pow(Math.max(v,0),1/2.4)-.055;
 const radians=v=>v*Math.PI/180;
 const normalize=(x,y,z)=>{const d=Math.max(1e-8,Math.hypot(x,y,z));return [x/d,y/d,z/d];};
 const tint=k=>{const t=Math.log(k/6500);return [Math.exp(-.35*t),Math.exp(-.04*Math.abs(t)),Math.exp(.45*t)];};
 const vertex=`#version 300 es
 in vec2 position;out vec2 uv;
 void main(){uv=vec2(position.x*.5+.5,.5-position.y*.5);gl_Position=vec4(position,0.,1.);}`;
 const fragment=`#version 300 es
 precision highp float;
 in vec2 uv;out vec4 color;
 uniform sampler2D baseMap,normalMap,depthMap,maskMap;
 uniform float aspect,strength,ambient;
 uniform int maskMode,count;
 uniform vec4 location[4]; // xyz and type: 0 directional, 1 point, 2 spot, 3 area
 uniform vec4 direction[4]; // direction xyz and intensity
 uniform vec4 settings[4]; // radius, softness, outer cosine, inner cosine
 uniform vec3 target[4],tintColor[4];
 vec3 toLinear(vec3 c){return mix(c/12.92,pow((c+.055)/1.055,vec3(2.4)),step(vec3(.04045001),c));}
 vec3 toSRGB(vec3 c){return mix(c*12.92,1.055*pow(max(c,vec3(0.)),vec3(1./2.4))-.055,step(vec3(.003130801),c));}
 vec3 safeNorm(vec3 v){return v/max(length(v),.00000001);}
 float pointLight(vec3 q,vec3 p,vec3 n,float softness){vec3 d=q-p;vec3 l=safeNorm(d);return max(0.,(dot(n,l)+softness*.35)/(1.+softness*.35))/(1.+dot(d,d));}
 void main(){
  vec4 source=texture(baseMap,uv);float subject=texture(maskMap,uv).r;
  float mask=maskMode==1?subject:maskMode==2?1.-subject:1.;
  if(count==0||strength<=0.||mask<=0.){color=source;return;}
  vec3 n=safeNorm(texture(normalMap,uv).rgb*2.-1.);
  vec3 p=vec3((uv.x-.5)*aspect,uv.y-.5,texture(depthMap,uv).r*.25);
  vec3 gain=vec3(ambient);
  for(int i=0;i<4;i++){
   if(i>=count)break;vec3 q=location[i].xyz;float kind=location[i].w,softness=settings[i].y,value=0.;
   if(kind<.5){value=max(0.,(dot(n,direction[i].xyz)+softness*.35)/(1.+softness*.35));}
   else if(kind>2.5){float r=settings[i].x*.5;value=(pointLight(q,p,n,softness)+pointLight(q+vec3(r,r,0.),p,n,softness)+pointLight(q+vec3(r,-r,0.),p,n,softness)+pointLight(q+vec3(-r,r,0.),p,n,softness)+pointLight(q+vec3(-r,-r,0.),p,n,softness))*.2;}
   else{value=pointLight(q,p,n,softness);if(kind>1.5){float cosine=dot(-safeNorm(q-p),safeNorm(target[i]-q));float cone=softness<=0.?step(settings[i].z,cosine):smoothstep(settings[i].z,settings[i].w,cosine);value*=cone;}}
   gain+=direction[i].w*value*tintColor[i];
  }
  vec3 result=toLinear(source.rgb)*mix(vec3(1.),gain,strength*mask);
  color=vec4(clamp(toSRGB(result),0.,1.),source.a);
 }`;

 // Analysis maps stay floating point after pixel-center bilinear sampling. Rounding
 // normals back to RGB8 here can move the hard edge of a narrow spotlight.
 function samplePixels(source,sourceWidth,sourceHeight,width,height){
  if(sourceWidth===width&&sourceHeight===height)return source;
  const sampled=new Float32Array(width*height*4);
  for(let y=0;y<height;y++){
   const sy=clamp((y+.5)*sourceHeight/height-.5,0,sourceHeight-1),y0=Math.floor(sy),y1=Math.min(y0+1,sourceHeight-1),fy=sy-y0;
   for(let x=0;x<width;x++){
    const sx=clamp((x+.5)*sourceWidth/width-.5,0,sourceWidth-1),x0=Math.floor(sx),x1=Math.min(x0+1,sourceWidth-1),fx=sx-x0,i=(y*width+x)*4;
    for(let c=0;c<4;c++)sampled[i+c]=(source[(y0*sourceWidth+x0)*4+c]*(1-fx)+source[(y0*sourceWidth+x1)*4+c]*fx)*(1-fy)+(source[(y1*sourceWidth+x0)*4+c]*(1-fx)+source[(y1*sourceWidth+x1)*4+c]*fx)*fy;
   }
  }
  return sampled;
 }
 function cpuPixels(assets,width,height,params){
  const base=assets.base,normal=assets.normal,depth=assets.depth,mask=assets.mask;
  const out=new Uint8ClampedArray(base);
  const lights=(params.lights||[]).filter(l=>l.enabled!==false&&l.intensity>0);
  if(!params.enabled||!params.strength||!lights.length)return out;
  const aspect=width/height,prepared=lights.map(light=>{
   const az=radians(light.azimuth),el=radians(light.elevation);
   return {...light,q:[(light.x-.5)*aspect,light.y-.5,light.z],direction:[Math.cos(az)*Math.cos(el),Math.sin(az)*Math.cos(el),Math.sin(el)],tint:tint(light.temperature),target:[(light.target_x-.5)*aspect,light.target_y-.5,0],outer:Math.cos(radians(light.beam_angle/2)),inner:Math.cos(radians(light.beam_angle/2*(1-.8*light.softness)))};
  });
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){
   const i=(y*width+x)*4,subject=mask[i]/255,m=params.scope==='all'?1:params.scope==='background'?1-subject:subject;if(!m)continue;
   const n=normalize(normal[i]/127.5-1,normal[i+1]/127.5-1,normal[i+2]/127.5-1),p=[((x+.5)/width-.5)*aspect,(y+.5)/height-.5,depth[i]/255*.25];
   const gain=[params.ambient,params.ambient,params.ambient];
   for(const l of prepared){
    const sample=q=>{const d=[q[0]-p[0],q[1]-p[1],q[2]-p[2]],len=Math.max(1e-8,Math.hypot(...d)),v=d.map(k=>k/len);let result=Math.max(0,(n[0]*v[0]+n[1]*v[1]+n[2]*v[2]+l.softness*.35)/(1+l.softness*.35))/(1+d[0]*d[0]+d[1]*d[1]+d[2]*d[2]);if(l.type==='spot'){const target=normalize(l.target[0]-q[0],l.target[1]-q[1],l.target[2]-q[2]),c=-v[0]*target[0]-v[1]*target[1]-v[2]*target[2];const f=l.softness<=0?(c>=l.outer?1:0):clamp((c-l.outer)/Math.max(1e-8,l.inner-l.outer),0,1);result*=l.softness<=0?f:f*f*(3-2*f);}return result;};
    let value;
    if(l.type==='directional')value=Math.max(0,(n[0]*l.direction[0]+n[1]*l.direction[1]+n[2]*l.direction[2]+l.softness*.35)/(1+l.softness*.35));
    else if(l.type==='area'){const r=l.radius*.5;value=(sample(l.q)+sample([l.q[0]+r,l.q[1]+r,l.q[2]])+sample([l.q[0]+r,l.q[1]-r,l.q[2]])+sample([l.q[0]-r,l.q[1]+r,l.q[2]])+sample([l.q[0]-r,l.q[1]-r,l.q[2]]))*.2;}
    else value=sample(l.q);
    for(let c=0;c<3;c++)gain[c]+=l.intensity*value*l.tint[c];
   }
   for(let c=0;c<3;c++)out[i+c]=Math.round(clamp(srgb(linear(base[i+c]/255)*(1+(gain[c]-1)*params.strength*m)),0,1)*255);
  }
  return out;
 }

 class LightingRenderer{
  constructor(){this.canvas=document.createElement('canvas');this.mode='cpu';this.lastSubmissionMilliseconds=0;this.frameCount=0;this.contextLost=false;
   try{this.gl=this.canvas.getContext('webgl2',{alpha:false,antialias:false,preserveDrawingBuffer:true,premultipliedAlpha:false});if(this.gl){this.setupGL();this.mode='webgl2';}}catch(error){this.error=error.message;this.gl=null;this.canvas=document.createElement('canvas');}
   if(!this.gl)this.context=this.canvas.getContext('2d',{willReadFrequently:true});
   this.canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();this.contextLost=true;});
  }
  setupGL(){
   const gl=this.gl;const compile=(type,source)=>{const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(shader));return shader;};
   const vs=compile(gl.VERTEX_SHADER,vertex),fs=compile(gl.FRAGMENT_SHADER,fragment),program=gl.createProgram();gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);gl.deleteShader(vs);gl.deleteShader(fs);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program));gl.useProgram(program);this.program=program;
   const vertices=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,vertices);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);const pos=gl.getAttribLocation(program,'position');gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
   this.uniforms={};for(const name of ['baseMap','normalMap','depthMap','maskMap','aspect','strength','ambient','maskMode','count','location[0]','direction[0]','settings[0]','target[0]','tintColor[0]'])this.uniforms[name]=gl.getUniformLocation(program,name);
   gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,false);gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL,false);gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL,gl.NONE);
   this.textures=[];['baseMap','normalMap','depthMap','maskMap'].forEach((name,i)=>{const texture=gl.createTexture();this.textures.push(texture);gl.activeTexture(gl.TEXTURE0+i);gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.uniform1i(this.uniforms[name],i);});
  }
  setAssets(images){
   this.images=images;const originalWidth=images.base.naturalWidth||images.base.width,originalHeight=images.base.naturalHeight||images.base.height;
   const scale=this.mode==='cpu'?Math.min(1,384/Math.max(originalWidth,originalHeight)):1;
   const width=Math.max(1,Math.round(originalWidth*scale)),height=Math.max(1,Math.round(originalHeight*scale));this.canvas.width=width;this.canvas.height=height;
   if(this.gl){const gl=this.gl;gl.viewport(0,0,width,height);['base','normal','depth','mask'].forEach((key,i)=>{gl.activeTexture(gl.TEXTURE0+i);gl.bindTexture(gl.TEXTURE_2D,this.textures[i]);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,images[key]);});}
   else{this.pixels={};const scratch=document.createElement('canvas'),context=scratch.getContext('2d',{willReadFrequently:true});for(const key of ['base','normal','depth','mask']){const sourceWidth=key==='base'?width:(images[key].naturalWidth||images[key].width),sourceHeight=key==='base'?height:(images[key].naturalHeight||images[key].height);scratch.width=sourceWidth;scratch.height=sourceHeight;context.drawImage(images[key],0,0,sourceWidth,sourceHeight);const pixels=context.getImageData(0,0,sourceWidth,sourceHeight).data;this.pixels[key]=key==='base'?pixels:samplePixels(pixels,sourceWidth,sourceHeight,width,height);}}
  }
  render(params){
   if(!this.images)return;if(this.contextLost){this.mode='cpu';this.gl=null;this.canvas=document.createElement('canvas');this.context=this.canvas.getContext('2d',{willReadFrequently:true});this.contextLost=false;this.setAssets(this.images);}
   const start=performance.now();if(!this.gl){const bytes=cpuPixels(this.pixels,this.canvas.width,this.canvas.height,params);this.context.putImageData(new ImageData(bytes,this.canvas.width,this.canvas.height),0,0);}
   else{
    const gl=this.gl,u=this.uniforms,aspect=this.canvas.width/this.canvas.height,lights=params.enabled?(params.lights||[]).filter(l=>l.enabled!==false&&l.intensity>0).slice(0,4):[];
    const location=new Float32Array(16),direction=new Float32Array(16),settings=new Float32Array(16),targets=new Float32Array(12),tints=new Float32Array(12);
    lights.forEach((light,i)=>{const az=radians(light.azimuth),el=radians(light.elevation);location.set([(light.x-.5)*aspect,light.y-.5,light.z,({directional:0,point:1,spot:2,area:3})[light.type]],i*4);direction.set([Math.cos(az)*Math.cos(el),Math.sin(az)*Math.cos(el),Math.sin(el),light.intensity],i*4);settings.set([light.radius,light.softness,Math.cos(radians(light.beam_angle/2)),Math.cos(radians(light.beam_angle/2*(1-.8*light.softness)))],i*4);targets.set([(light.target_x-.5)*aspect,light.target_y-.5,0],i*3);tints.set(tint(light.temperature),i*3);});
    gl.useProgram(this.program);gl.uniform1f(u.aspect,aspect);gl.uniform1f(u.strength,params.strength||0);gl.uniform1f(u.ambient,params.ambient??1);gl.uniform1i(u.maskMode,params.scope==='subject'?1:params.scope==='background'?2:0);gl.uniform1i(u.count,lights.length);gl.uniform4fv(u['location[0]'],location);gl.uniform4fv(u['direction[0]'],direction);gl.uniform4fv(u['settings[0]'],settings);gl.uniform3fv(u['target[0]'],targets);gl.uniform3fv(u['tintColor[0]'],tints);gl.drawArrays(gl.TRIANGLES,0,6);
   }
   // Measures CPU work and command submission, not completed GPU time or frame rate.
   this.lastSubmissionMilliseconds=performance.now()-start;this.frameCount++;return this.canvas;
  }
  dispose(){if(this.gl){this.textures?.forEach(t=>this.gl.deleteTexture(t));this.gl.deleteProgram(this.program);this.gl.getExtension('WEBGL_lose_context')?.loseContext();}this.images=null;}
 }
 window.PortraitLightingRenderer={LightingRenderer,cpuPixels,samplePixels,vertexShader:vertex,fragmentShader:fragment};
})();
