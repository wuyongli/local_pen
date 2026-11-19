// Optional real-renderer acceptance: node tests/verify_pen_tiles.cjs /path/to/source.pen
// LOCAL_PEN_PLAYWRIGHT points to an installed playwright-core; no browser is opened visibly.
const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const {mkdtemp, readFile, writeFile} = require('node:fs/promises');
const {createInterface} = require('node:readline');
const {once} = require('node:events');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.LOCAL_PEN_PLAYWRIGHT || 'playwright-core');
const root = path.resolve(__dirname,'..');

async function main() {
  const source = path.resolve(process.argv[2]);
  const directory = await mkdtemp(path.join(os.tmpdir(),'local-pen-tiles-acceptance-'));
  const design = path.join(directory,'design.pen');
  execFileSync(process.env.LOCAL_PEN_PYTHON || 'python3',['-B','-c',
    'from pathlib import Path; import sys; from edit_pen_text import load_pen,write_pen_copy; '+
    's,d=map(Path,sys.argv[1:]);write_pen_copy(load_pen(s),d,s)',source,design],{cwd:root});
  const env = {...process.env,XDG_STATE_HOME:path.join(directory,'state')};
  const children = [];
  function connect() {
    const child = spawn(process.env.LOCAL_PEN_PYTHON || 'python3',['-B',path.join(root,'local_pen_mcp.py')],
      {cwd:directory, env, stdio:['pipe','pipe','inherit']});
    const exit = once(child,'exit');
    const reader = createInterface({input:child.stdout})[Symbol.asyncIterator]();
    let id = 0;
    const rpc = async(method,params={}) => {
      child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:++id,method,params})+'\n');
      const response = JSON.parse((await reader.next()).value);
      assert.ok(!response.error && !response.result.isError,JSON.stringify(response));
      return response.result;
    };
    const peer = {tool:(name,args={}) => rpc('tools/call',{name,arguments:args}),rpc,
      close:async() => {child.stdin.end();assert.equal((await exit)[0],0);}};
    children.push(peer);
    return peer;
  }
  let browser;
  let active;
  const errors = [];
  try {
    const first = connect();
    await first.rpc('initialize',{protocolVersion:'2025-11-25'});
    await first.tool('task_design',{task_dir:directory,file_path:design});
    active = (await first.tool('live_preview')).structuredContent;
    const url = active.url;
    const origin = new URL(url).origin;
    browser = await chromium.launch({headless:true,
      ...(process.env.LOCAL_PEN_BROWSER ? {executablePath:process.env.LOCAL_PEN_BROWSER} : {})});
    const page = await browser.newPage({viewport:{width:1200,height:900},deviceScaleFactor:2});
    page.on('pageerror',error => errors.push(error.message));
    const requests = [];
    page.on('request',r => {if(r.url().includes('tile.png')) requests.push(r.url());});
    await page.route('**/*',route => new URL(route.request().url()).origin===origin ? route.continue():route.abort());
    const opening = performance.now();
    await page.goto(url);
    await page.waitForFunction(()=>!!document.querySelector('#tiles').dataset.ready,null,{timeout:60000});
    const openedMs = Math.round(performance.now()-opening);
    const response = await fetch(new URL('manifest.json',url));
    const manifest = await response.json();
    assert.ok(manifest.bounds.height>0);
    const roots = JSON.parse(await readFile(design,'utf8')).children.filter(n=>n.enabled!==false && n.type==='frame' && n.name?.trim());
    assert.equal(await page.locator('[data-artboard-id]').count(),roots.length);
    const fitted = await page.locator('#zoom-level').textContent();
    const initialBrowserScale = await page.evaluate(()=>visualViewport.scale);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.synthesizePinchGesture',{x:600,y:450,scaleFactor:2,gestureSourceType:'mouse'});
    await page.waitForFunction(label=>document.querySelector('#zoom-level').textContent!==label,fitted);
    assert.equal(await page.evaluate(()=>visualViewport.scale),initialBrowserScale);
    await page.waitForTimeout(500);
    const named = await page.locator('[data-artboard-id]').first().textContent();
    // Position a real board in the centre at 100%, using the same public zoom controls/camera.
    await page.evaluate(()=>{
      const fitted=Math.min(1,(viewport.offsetWidth-32)/width,(viewport.offsetHeight-52)/height);
      zoom(1/fitted);
    });
    const target=manifest.artboards.find(b=>b.id==='settle18') || manifest.artboards[0];
    await page.evaluate(board=>{
      const rect=canvas.getBoundingClientRect();
      viewport.scrollLeft+=rect.left+board.x-100;
      viewport.scrollTop+=rect.top+board.y-100;
    },target);
    await page.waitForFunction(()=>{
      const wanted=[...document.querySelectorAll('.tile')];
      return wanted.length>0 && wanted.every(n=>n.dataset.tile.startsWith('1:'));
    },null,{timeout:60000});
    assert.equal(await page.locator('#zoom-level').textContent(),'100%');
    const beforeCamera=await page.evaluate(()=>({ratio,left:viewport.scrollLeft,top:viewport.scrollTop}));
    const previous=await page.locator('#tiles').getAttribute('data-ready');
    async function sample() {
      return page.evaluate(() => {
        const x=105,y=400;
        const tile=[...document.querySelectorAll('.tile')].reverse().find(n=>{
          const b=n.getBoundingClientRect();return b.left<=x && b.top<=y && b.right>x && b.bottom>y;
        });
        if(!tile)throw new Error('Visible sample has no tile');
        const image=tile.querySelector('img'), b=image.getBoundingClientRect();
        const c=document.createElement('canvas');c.width=c.height=1;
        const ctx=c.getContext('2d');
        ctx.drawImage(image,(x-b.left)*image.naturalWidth/b.width,(y-b.top)*image.naturalHeight/b.height,1,1,0,0,1,1);
        return [...ctx.getImageData(0,0,1,1).data];
      });
    }
    async function waitChanged(old) {
      await page.waitForFunction(old=>{
        const current=document.querySelector('#tiles').dataset.ready;
        return current && current!==old;
      },old,{timeout:60000});
    }
    const started=performance.now();
    await first.tool('edit_design',{operations:[{op:'update',id:target.id,properties:{fill:'#FFE1E6'}}]});
    await waitChanged(previous);
    const refreshedMs=Math.round(performance.now()-started);
    assert.deepEqual(await sample(),[255,225,230,255]);
    assert.deepEqual(await page.evaluate(()=>({ratio,left:viewport.scrollLeft,top:viewport.scrollTop})),beforeCamera);
    assert.equal(await page.locator('[data-artboard-id]').first().textContent(),named);
    await first.close();children.splice(children.indexOf(first),1);
    const second=connect();await second.rpc('initialize',{protocolVersion:'2025-11-25'});
    const shared=(await second.tool('live_preview')).structuredContent;
    assert.equal(shared.url,url);
    assert.equal((await second.tool('live_preview',{layer_id:'ignored'})).structuredContent.url,url);
    const secondRevision=await page.locator('#tiles').getAttribute('data-ready');
    await second.tool('edit_design',{operations:[{op:'update',id:target.id,properties:{fill:'#E3F1FF'}}]});
    await waitChanged(secondRevision);
    assert.deepEqual(await sample(),[227,241,255,255]);
    const worldPoint=await page.evaluate(b=>{
      const r=canvas.getBoundingClientRect(),scale=r.width/width;
      return [b.x+(600-r.left)/scale,b.y+(450-r.top)/scale];
    },manifest.bounds);
    const growRevision=await page.locator('#tiles').getAttribute('data-ready');
    await second.tool('edit_design',{operations:[
      {op:'add',layer:{id:'viewport_growth_left',type:'rectangle',x:manifest.bounds.x-100,
        y:manifest.bounds.y-100,width:1,height:1,fill:'#FFFFFF'}},
      {op:'add',layer:{id:'viewport_growth_right',type:'rectangle',x:manifest.bounds.x+manifest.bounds.width+1000,
        y:manifest.bounds.y,width:10,height:10,fill:'#FFFFFF'}}]});
    await waitChanged(growRevision);
    assert.equal(await page.locator('#zoom-level').textContent(),'100%');
    const grown=await (await fetch(new URL('manifest.json',url))).json();
    const afterWorld=await page.evaluate(b=>{
      const r=canvas.getBoundingClientRect(),scale=r.width/width;
      return [b.x+(600-r.left)/scale,b.y+(450-r.top)/scale];
    },grown.bounds);
    assert.ok(afterWorld.every((v,i)=>Math.abs(v-worldPoint[i])<1),'World camera moved when canvas bounds grew');
    assert.deepEqual(await sample(),[227,241,255,255]);
    assert.ok(requests.length>0);
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(directory,'viewport.png')});
    const result={directory,url,artboards:roots.length,bounds:manifest.bounds,openedMs,refreshedMs,
      tileRequests:requests.length,pageErrors:errors};
    await writeFile(path.join(directory,'results.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
    await second.tool('live_preview',{action:'stop'});active=undefined;
  } finally {
    if(browser)await browser.close();
    if(active && children.length)try{await children.at(-1).tool('live_preview',{action:'stop'});}catch{}
    for(const child of children)await child.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
