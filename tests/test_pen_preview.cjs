const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname,'..','pen_preview.js'),'utf8');

function start(fetch, manual=false, pausedFrames=false) {
  const timers = new Map();
  let nextTimer=0;
  const rect = {left:0,top:0,right:1024,bottom:1024,width:1024,height:1024};
  const layer = {dataset:{},style:{},replaceChildren(){}};
  let legacy = 0;
  const context = {console,URL,URLSearchParams,Blob,AbortController,Map,Set,Math,
    setTimeout(fn,ms){const id=manual ? ++nextTimer : setTimeout(fn,ms);timers.set(id,{fn,ms});return id;},
    clearTimeout(id){timers.delete(id);if(!manual)clearTimeout(id);},
    document:{visibilityState:'visible',querySelector:()=>layer,addEventListener(){}},status:{},viewport:{getBoundingClientRect:()=>rect,addEventListener(){}},
    canvas:{getBoundingClientRect:()=>rect,hidden:true},width:0,height:0,autoFit:true,imageURL:undefined,
    viewportCenter:()=>[512,512],imageAnchor:()=>[.5,.5],layout(){},showArtboardTitles(){},
    window:{devicePixelRatio:1,addEventListener(){}},refresh(){legacy++;},fetch};
  context.pauseViewPersistence=()=>{};
  context.updateViewDocument=next=>{context.width=next.bounds.width;context.height=next.bounds.height;};
  context.requestAnimationFrame=fn=>pausedFrames ? 999 : context.setTimeout(fn,16);
  context.cancelAnimationFrame=context.clearTimeout;
  vm.runInNewContext(script,context);
  return {close(){if(!manual)for(const id of timers.keys())clearTimeout(id);},get legacy(){return legacy;},
    run(ms){const entry=[...timers].find(([,timer])=>timer.ms===ms);assert.ok(entry,`No ${ms}ms timer`);
      timers.delete(entry[0]);entry[1].fn();}};
}
const manifest = {etag:'one',bounds:{x:0,y:0,width:1024,height:1024},artboards:[],
  minLevel:-16,maxLevel:4,tilePixels:1024,padding:32};

test('stale tile generation stops requesting while new manifest is pending',async t=>{
  let tiles=0,manifests=0;
  const page=start(async url=>{
    if(url==='manifest.json'){
      if(++manifests>1)return new Promise(()=>{});
      return {status:200,ok:true,json:async()=>manifest};
    }
    if(++tiles>=100)return new Promise(()=>{});
    return {status:412};
  });t.after(()=>page.close());
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.ok(tiles<=4,`stale generation made ${tiles} tile requests`);
  assert.equal(manifests,2);
});

test('transient manifest failure retries tiled mode rather than making fallback permanent',async t=>{
  let calls=0;
  const page=start(async()=>{calls++;return {status:503,ok:false};});
  t.after(()=>page.close());
  await new Promise(resolve=>setTimeout(resolve,550));
  assert.equal(page.legacy,0);
  assert.ok(calls>=2);
});

test('explicit unsupported renderer uses existing whole-image route',async t=>{
  const page=start(async()=>({status:501,ok:false}));t.after(()=>page.close());
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(page.legacy,1);
});


test('late stale response cannot suspend a newer manifest generation',async t=>{
  let calls=0,manifests=0,oldReply;
  const page=start(async url=>{
    if(url==='manifest.json'){
      manifests++;
      if(manifests>2)return {status:304};
      return {status:200,ok:true,json:async()=>({...manifest,etag:manifests===1?'one':'two'})};
    }
    calls++;
    if(calls===1)return new Promise(resolve=>{oldReply=resolve;});
    return {status:202};
  },true);t.after(()=>page.close());
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();page.run(16);await flush();page.run(500);await flush();page.run(16);await flush();
  assert.equal(calls,2);
  oldReply({status:412});await flush();
  page.run(500);await flush();
  page.run(120);page.run(16);await flush();
  assert.ok(calls>=3,'late stale response stopped the new generation');
});


test('visible viewport continues tile work when animation callbacks are paused',async t=>{
  let tiles=0;
  const page=start(async url=>{
    if(url==='manifest.json')return {status:200,ok:true,json:async()=>manifest};
    tiles++;return new Promise(()=>{});
  },true,true);t.after(()=>page.close());
  await new Promise(resolve=>setImmediate(resolve));
  page.run(80);await new Promise(resolve=>setImmediate(resolve));
  assert.ok(tiles>0);
});
