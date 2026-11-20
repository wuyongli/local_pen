// Optional offline browser acceptance; all data and requests are isolated fixtures.
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {execFileSync}=require('node:child_process');
const {deflateSync}=require('node:zlib');
const {chromium}=require(process.env.LOCAL_PEN_PLAYWRIGHT || 'playwright-core');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
function chunk(type,data){const name=Buffer.from(type),body=Buffer.concat([name,data]);let crc=0xffffffff;for(const byte of body){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0)}const size=Buffer.alloc(4),sum=Buffer.alloc(4);size.writeUInt32BE(data.length);sum.writeUInt32BE((crc^0xffffffff)>>>0);return Buffer.concat([size,body,sum])}
function png(rgb,alpha=255){const h=Buffer.alloc(13);h.writeUInt32BE(1,0);h.writeUInt32BE(1,4);h[8]=8;h[9]=6;return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',h),chunk('IDAT',deflateSync(Buffer.from([0,...rgb,alpha]))),chunk('IEND',Buffer.alloc(0))])}
let browser,html;
before(async()=>{
 html=JSON.parse(execFileSync(process.env.LOCAL_PEN_PYTHON || 'python3',['-B','-c','import json; from pen_live_preview import PAGE; print(json.dumps(PAGE.decode()))'],{cwd:root,encoding:'utf8'}));
 browser=await chromium.launch({headless:true,...(process.env.LOCAL_PEN_BROWSER?{executablePath:process.env.LOCAL_PEN_BROWSER}:{})});
});
after(async()=>{if(browser)await browser.close()});
async function fixture(t,options={}){
 let pageHtml=html;
 if(options.taskName){
  pageHtml=JSON.parse(execFileSync(process.env.LOCAL_PEN_PYTHON || 'python3',['-B','-c',
   'import json,sys,tempfile; from pathlib import Path; from pen_live_preview import PreviewWindow; '+
   'folder=tempfile.TemporaryDirectory(); root=Path(folder.name); '+
   'view=PreviewWindow(root/"design.pen",1,task_dir=root/sys.argv[1]); '+
   'print(json.dumps(view.page.decode())); view.close(); folder.cleanup()',options.taskName],{cwd:root,encoding:'utf8'}));
 }
 const context=await browser.newContext({viewport:{width:1200,height:900},deviceScaleFactor:options.dpr || 2});
 if(options.blockStorage)await context.addInitScript(()=>{for(const key of ['localStorage','sessionStorage'])Object.defineProperty(window,key,{get(){throw new DOMException('Storage unavailable','SecurityError')}})});
 const page=await context.newPage();let version=1,hold=false,unsupported=false,documentKey='design-a';const waiting=[],requests=[],errors=[];
 const boards=Array.from({length:57},(_,i)=>({id:'board'+i,name:i===17?'18 结款详情':`${i+1} 页面`,x:(i%8)*1200,y:Math.floor(i/8)*1700,width:375,height:880}));
 boards[22].name='<img src=x onerror=alert(1)> & 特殊名称';
 if(options.flat)boards.splice(0);
 const legacyBoards=options.legacyLayers ? JSON.parse(execFileSync(process.env.LOCAL_PEN_PYTHON || 'python3',['-B','-c','import json,sys; from pen_render import artboard_titles; print(json.dumps(artboard_titles(json.loads(sys.argv[1]),json.loads(sys.argv[2]))))',JSON.stringify(options.legacyLayers),JSON.stringify(options.legacyScale || 1)],{cwd:root,encoding:'utf8'})) : [];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:pageHtml});
  if(u.pathname==='/preview.js')return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,'pen_preview.js'),'utf8')});
  if(u.pathname==='/manifest.json' && unsupported)return route.fulfill({status:501,body:'unsupported'});
  if(u.pathname==='/canvas' || u.pathname==='/canvas.png')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="4000" height="4500"><rect width="4000" height="4500" fill="#cee8e0"/></svg>',headers:{ETag:'legacy','X-Local-Pen-Image':'svg'}});
  if(u.pathname==='/artboards.json')return route.fulfill({json:legacyBoards});
  if(u.pathname==='/manifest.json')return route.fulfill({json:{etag:'v'+version,documentKey,bounds:{x:0,y:0,width:11460,height:13130},artboards:boards,tilePixels:1024,padding:32,minLevel:-16,maxLevel:4}});
  if(u.pathname==='/tile.png'){
   const requested=version;requests.push({url:u.href,version});
   if(route.request().headers()['if-match']!=='v'+version)return route.fulfill({status:412,body:'changed'});
   if(hold)await new Promise(resolve=>waiting.push(resolve));
   try{return await route.fulfill({contentType:'image/png',body:png(options.colour || (requested===1?[206,232,224]:[255,225,230]),options.alpha ?? 255)})}catch{return}
  }
  return route.abort();
 });
 t.after(async()=>{hold=false;waiting.splice(0).forEach(r=>r());await context.close();assert.deepEqual(errors,[])});
 const ready=()=>page.waitForFunction(()=>!!document.querySelector('#tiles').dataset.ready,null,{timeout:10000});
 await page.goto('http://local-pen-viewer.test/');await ready();
 async function actual(){await page.evaluate(()=>zoom(1/Math.min(1,(viewport.offsetWidth-32)/width,(viewport.offsetHeight-52)/height)));await page.waitForFunction(()=>document.querySelector('#tiles').dataset.ready && [...document.querySelectorAll('.tile:not([hidden])')].some(n=>n.dataset.tile.startsWith(Math.ceil(Math.log2(Math.min(3,devicePixelRatio)))+':')),null,{timeout:10000});}
 return {page,context,boards,requests,ready,actual,setUnsupported(){unsupported=true;},releaseOne(){waiting.shift()?.()},setHold(value){hold=value;if(!hold)waiting.splice(0).forEach(r=>r())},next(){version++;},setDocument(key){documentKey=key;version++;}};
}

test('different task tabs keep their own names after page reload',async t=>{
 const a=await fixture(t,{taskName:'2026-11-20-产地通小程序'});
 const b=await fixture(t,{taskName:'2026-11-20_采购 & <结算>'});
 assert.equal(await a.page.title(),'产地通小程序 · local_pen');
 assert.equal(await b.page.title(),'采购 & <结算> · local_pen');
 await a.page.reload();await a.ready();
 assert.equal(await a.page.title(),'产地通小程序 · local_pen');
 assert.equal(await b.page.title(),'采购 & <结算> · local_pen');
});

test('revision keeps previous pixels until the complete visible frame is ready',async t=>{
 const f=await fixture(t);await f.actual();f.setHold(true);f.next();
 await f.page.waitForSelector('#previous-frame',{state:'visible',timeout:3000});
 assert.ok(await f.page.locator('#previous-frame').evaluate(c=>c.width*c.height<=8000000));
 const colour=await f.page.locator('#previous-frame').evaluate(c=>[...c.getContext('2d').getImageData(Math.floor(c.width/2),Math.floor(c.height/2),1,1).data]);
 assert.deepEqual(colour,[206,232,224,255]);
 assert.equal(await f.page.locator('#tiles .tile:not([hidden])').count(),0);
 f.setHold(false);await f.ready();assert.equal(await f.page.locator('#previous-frame').count(),0);
 const updated=await f.page.locator('#tile-frame').evaluate(c=>[...c.getContext('2d').getImageData(Math.floor(c.width/2),Math.floor(c.height/2),1,1).data]);
 assert.deepEqual(updated,[255,225,230,255]);
});

test('continuous scroll requests new tiles before the gesture stops',async t=>{
 const f=await fixture(t);await f.actual();await f.page.waitForTimeout(150);f.requests.splice(0);
 const motion=await f.page.evaluate(async()=>{
   const start=viewport.scrollLeft,max=viewport.scrollWidth-viewport.clientWidth;
   const step=start>max/2?-120:120;
   for(let i=0;i<30;i++){viewport.scrollLeft+=step;viewport.dispatchEvent(new Event('scroll'));await new Promise(r=>setTimeout(r,16))}
   return {start,end:viewport.scrollLeft,max,tiles:document.querySelectorAll('.tile').length,scale:canvas.getBoundingClientRect().width/width,left:canvas.getBoundingClientRect().left,visibility:document.visibilityState,keys:[...document.querySelectorAll('.tile')].map(n=>n.dataset.tile)};
 });
 assert.ok(f.requests.length>0,'No tile requests during the continuous gesture: '+JSON.stringify(motion));
});

test('actual scale and world centre survive resizing and page reload',async t=>{
 const f=await fixture(t);await f.actual();
 const before=await f.page.evaluate(()=>{const r=canvas.getBoundingClientRect(),s=r.width/width;return [(viewport.clientWidth/2-r.left)/s,(viewport.clientHeight/2-r.top)/s]});
 await f.page.setViewportSize({width:1600,height:1100});await f.page.waitForTimeout(200);await f.ready();
 assert.equal(await f.page.locator('#zoom-level').textContent(),'100%');
 const afterResize=await f.page.evaluate(()=>{const r=canvas.getBoundingClientRect(),s=r.width/width;return [(viewport.clientWidth/2-r.left)/s,(viewport.clientHeight/2-r.top)/s]});
 assert.ok(afterResize.every((v,i)=>Math.abs(v-before[i])<1));
 await f.page.waitForTimeout(250);await f.page.reload();await f.ready();
 assert.equal(await f.page.locator('#zoom-level').textContent(),'100%');
 const afterReload=await f.page.evaluate(()=>{const r=canvas.getBoundingClientRect(),s=r.width/width;return [(viewport.clientWidth/2-r.left)/s,(viewport.clientHeight/2-r.top)/s]});
 assert.ok(afterReload.every((v,i)=>Math.abs(v-before[i])<1));
});

test('artboard search locates a frame while keeping the complete document',async t=>{
 const f=await fixture(t);
 await f.page.getByRole('button',{name:'画板目录',exact:true}).click({timeout:2000});
 const input=f.page.getByRole('searchbox',{name:'查找画板'});await input.fill('结款');
 const panel=f.page.locator('#artboard-panel');
 assert.equal(await panel.getByRole('button',{name:'18 结款详情',exact:true}).count(),1);
 await panel.getByRole('button',{name:'18 结款详情',exact:true}).click();
 await f.page.waitForTimeout(100);await f.ready();
 assert.equal(await f.page.locator('[data-artboard-id]').count(),57);
 assert.ok(await panel.isHidden());
 const centre=await f.page.evaluate(()=>{const r=canvas.getBoundingClientRect(),s=r.width/width;return [(viewport.clientWidth/2-r.left)/s,(viewport.clientHeight/2-r.top)/s]});
 const board=f.boards[17];assert.ok(Math.abs(centre[0]-board.x-board.width/2)<2);assert.ok(Math.abs(centre[1]-board.y-board.height/2)<2);
 await f.page.getByRole('button',{name:'实际大小 100%',exact:true}).click();
 assert.equal(await f.page.locator('#zoom-level').textContent(),'100%');
});


test('rapid revisions retain only one old frame and commit the latest revision',async t=>{
 const f=await fixture(t);await f.actual();await f.page.waitForTimeout(100);
 f.setHold(true);f.next();await f.page.waitForSelector('#previous-frame',{state:'visible'});
 f.releaseOne();await f.page.waitForTimeout(100);
 assert.equal(await f.page.locator('#previous-frame').count(),1);
 assert.equal(await f.page.locator('#tiles .tile:not([hidden])').count(),0);
 f.next();await f.page.waitForFunction(()=>document.querySelector('#tiles').dataset.pending==='v3');
 assert.equal(await f.page.locator('#previous-frame').count(),1);
 f.setHold(false);await f.page.waitForFunction(()=>document.querySelector('#tiles').dataset.ready==='v3');
 assert.equal(await f.page.locator('#previous-frame').count(),0);
 assert.ok(await f.page.locator('#tiles .tile').count()<=64);
});

test('different documents restore their own view and tolerate malformed saved data',async t=>{
 const f=await fixture(t);await f.actual();
 const first=await f.page.evaluate(()=>getViewState());await f.page.waitForTimeout(200);
 f.setDocument('design-b');await f.page.waitForFunction(()=>document.querySelector('#tiles').dataset.ready==='v2');
 assert.notEqual(await f.page.locator('#zoom-level').textContent(),'100%');
 await f.page.evaluate(()=>restoreView({version:1,fit:false,scale:1,x:2200,y:3200}));
 await f.page.waitForTimeout(200);
 f.setDocument('design-a');await f.page.waitForFunction(()=>document.querySelector('#tiles').dataset.ready==='v3');
 const restored=await f.page.evaluate(()=>getViewState());
 assert.equal(await f.page.locator('#zoom-level').textContent(),'100%');
 assert.ok(Math.abs(restored.x-first.x)<1 && Math.abs(restored.y-first.y)<1);
 await f.context.addInitScript(()=>{for(const storage of [sessionStorage,localStorage])storage.setItem('local-pen:camera:v1:design-a','invalid-json')});
 await f.page.reload();await f.ready();assert.notEqual(await f.page.locator('#zoom-level').textContent(),'100%');
});

test('blocked browser storage still supports the canvas and literal-name search',async t=>{
 const f=await fixture(t,{blockStorage:true});await f.actual();
 await f.page.getByRole('button',{name:'画板目录',exact:true}).click();
 const search=f.page.getByRole('searchbox',{name:'查找画板'});await search.fill('特殊名称');
 assert.equal(await f.page.locator('#board-list img').count(),0);
 assert.equal(await f.page.locator('#board-list button').textContent(),'<img src=x onerror=alert(1)> & 特殊名称');
 await search.fill('不存在的画板');assert.equal(await f.page.locator('#board-count').textContent(),'未找到画板');
 await f.page.keyboard.press('Escape');assert.ok(await f.page.locator('#artboard-panel').isHidden());
 await f.page.setViewportSize({width:1400,height:1000});await f.page.waitForTimeout(100);
 assert.equal(await f.page.locator('#zoom-level').textContent(),'100%');
});


test('leaving immediately after a pan persists the latest world centre',async t=>{
 const f=await fixture(t);await f.actual();
 const saved=await f.page.evaluate(()=>{
   viewport.scrollLeft+=120;
   window.dispatchEvent(new PageTransitionEvent('pagehide'));
   return {current:getViewState(),saved:JSON.parse(sessionStorage.getItem('local-pen:camera:v1:design-a'))};
 });
 assert.ok(Math.abs(saved.current.x-saved.saved.x)<1 && Math.abs(saved.current.y-saved.saved.y)<1);
});


test('legacy raster fallback does not overwrite the saved native camera',async t=>{
 const f=await fixture(t);await f.actual();await f.page.waitForTimeout(250);
 const before=await f.page.evaluate(()=>JSON.parse(sessionStorage.getItem('local-pen:camera:v1:design-a')));
 f.setUnsupported();await f.page.waitForFunction(()=>width===4000 && height===4500);
 await f.page.waitForTimeout(250);
 const after=await f.page.evaluate(()=>JSON.parse(sessionStorage.getItem('local-pen:camera:v1:design-a')));
 assert.deepEqual(after,before);
});


test('legacy directory centres and fits the actual wide frame',async t=>{
 const layers=[{id:'bg',type:'rectangle',x:0,y:0,width:4000,height:4500},
  {id:'wide',type:'frame',name:'宽画板',x:2000,y:2000,width:1600,height:200}];
 const f=await fixture(t,{legacyLayers:layers});f.setUnsupported();await f.page.waitForFunction(()=>width===4000 && height===4500);
 await f.page.getByRole('button',{name:'画板目录',exact:true}).click();
 await f.page.locator('#board-list').getByRole('button',{name:'宽画板',exact:true}).click();
 const state=await f.page.evaluate(()=>getViewState());
 assert.ok(Math.abs(state.x-2800)<1 && Math.abs(state.y-2100)<1,JSON.stringify(state));
 assert.ok(Math.abs(state.scale-.69)<.001);
});

test('unknown legacy auto height locates the origin without assuming a phone frame',async t=>{
 const layers=[{id:'bg',type:'rectangle',x:0,y:0,width:4000,height:4500},
  {id:'auto',type:'frame',name:'自适应',x:2000,y:2000,width:1200,height:'fit_content'}];
 const f=await fixture(t,{legacyLayers:layers});f.setUnsupported();await f.page.waitForFunction(()=>width===4000 && height===4500);
 await f.page.getByRole('button',{name:'画板目录',exact:true}).click();
 await f.page.locator('#board-list').getByRole('button',{name:'自适应',exact:true}).click();
 const state=await f.page.evaluate(()=>getViewState());
 assert.ok(Math.abs(state.x-2000)<1 && Math.abs(state.y-2000)<1,JSON.stringify(state));
});

async function flatScreenshot(page,expected) {
 const bytes=[...await page.screenshot({clip:{x:100,y:150,width:950,height:500}})];
 return page.evaluate(async ({bytes,expected})=>{
   const url=URL.createObjectURL(new Blob([new Uint8Array(bytes)],{type:'image/png'}));
   try{
     const image=new Image();image.src=url;await image.decode();
     const c=document.createElement('canvas');c.width=image.naturalWidth;c.height=image.naturalHeight;
     const ctx=c.getContext('2d');ctx.drawImage(image,0,0);const d=ctx.getImageData(0,0,c.width,c.height).data;
     const reference=expected;let altered=0,maxDelta=0;const examples=[];
     for(let i=0;i<d.length;i+=4){let delta=0;for(let j=0;j<3;j++)delta=Math.max(delta,Math.abs(d[i+j]-reference[j]));if(delta>3){altered++;if(examples.length<10)examples.push({x:(i/4)%c.width,y:Math.floor(i/4/c.width),rgb:[...d.slice(i,i+4)]})}maxDelta=Math.max(maxDelta,delta)}
     return {reference,altered,maxDelta,pixels:d.length/4,examples};
   }finally{URL.revokeObjectURL(url)}
 },{bytes,expected});
}

test('large viewport limits both live and transition canvas memory',async t=>{
 const f=await fixture(t,{flat:true,dpr:3});
 await f.page.setViewportSize({width:3600,height:2200});
 await f.page.evaluate(()=>restoreView({...getViewState(),scale:1,fit:false}));
 await f.page.waitForFunction(()=>document.querySelector('#tiles').dataset.ready && document.querySelector('#tile-frame').width*document.querySelector('#tile-frame').height>15000000);
 const limits=await f.page.locator('#tile-frame').evaluate(c=>[c.width*c.height,c.width,c.height]);
 assert.ok(limits[0]<=16000000 && limits[1]<=8192 && limits[2]<=8192,JSON.stringify(limits));
 f.setHold(true);f.next();await f.page.waitForSelector('#previous-frame',{state:'visible'});
 assert.ok(await f.page.locator('#previous-frame').evaluate(c=>c.width*c.height<=8000000));
 f.setHold(false);await f.ready();
});

for (const dpr of [1,1.5,2,3]) for (const alpha of [255,128]) {
 test(`fractional zoom has no tile grid in live and transition frames (DPR ${dpr}, alpha ${alpha})`,async t=>{
  const f=await fixture(t,{flat:true,colour:[244,247,245],alpha,dpr});await f.actual();
  for(const scale of [.25,.633,.847,1.217,2.533,4.137,8]){
   await f.page.evaluate(scale=>restoreView({...getViewState(),scale,fit:false}),scale);
   await f.page.waitForTimeout(200);await f.ready();
   const pixels=await flatScreenshot(f.page,[244,247,245].map(c=>Math.round(c*alpha/255+30*(1-alpha/255))));

   assert.equal(pixels.altered,0,`live ${scale}: ${JSON.stringify(pixels)}`);
  }
  f.setHold(true);f.next();await f.page.waitForSelector('#previous-frame',{state:'visible'});
  const pixels=await flatScreenshot(f.page,[244,247,245].map(c=>Math.round(c*alpha/255+30*(1-alpha/255))));
  assert.equal(pixels.altered,0,`transition: ${JSON.stringify(pixels)}`);
  f.setHold(false);await f.ready();
 });
}
