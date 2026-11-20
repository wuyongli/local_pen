// Adapter for the pinned CLI only. All modifications are in memory, never on disk.
import {readFileSync, realpathSync, writeFileSync, existsSync} from 'node:fs';
import {registerHooks} from 'node:module';
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';

const cli = realpathSync(process.argv[2]);
const entry = pathToFileURL(cli).href;
const anchor = "let p=await L9(u,l,n);for(let t of e['libraries']??[])";
const hook = Symbol.for('local-pen-native-renderer');
registerHooks({
  load(url, context, next) {
    const loaded = next(url, context);
    if (url !== entry) return loaded;
    const source = String(loaded.source);
    if (source.split(anchor).length !== 2) throw new Error('Unsupported CLI renderer structure');
    const replacement = `let p=await L9(u,l,n);
      await globalThis[Symbol.for('local-pen-native-renderer')](p,_0x39bc,{
        bounds:F0,region:async(scene,nodes,region,options)=>{
          const saved=F0;
          F0=()=>ZA.MakeXYWH(region[0],region[1],region[2],region[3]);
          try{return await j0(scene,nodes,options);}finally{F0=saved;}
        }
      }); process.exit(0); for(let t of e['libraries']??[])`;
    return {...loaded, source: source.replace(anchor, () => replacement)};
  },
});

function send(value) { console.log('LOCAL_PEN ' + JSON.stringify(value)); }

globalThis[hook] = async (editor, decode, native) => {
  const scene = editor.sceneManager;
  const graph = scene[decode(0x2947)];
  const props = decode(0xeb8), children = decode(0x2a05);
  const roots = () => graph.getViewportNode()[children].filter(n => n[props].resolved.enabled !== false);
  const input = createInterface({input: process.stdin, crlfDelay: Infinity});
  function resources() {
    const file = process.env.LOCAL_PEN_MISSING_RESOURCES;
    const missing = file && existsSync(file) ? readFileSync(file, 'utf8').trim().split('\n') : [];
    if (missing.some(kind => kind && kind !== 'font')) throw new Error('Local render resources are missing');
    return missing.includes('font');
  }
  async function describe() {
    await native.region(scene, roots(), [0, 0, 1, 1], {type: 0, scale: 1});
    const b = native.bounds(roots(), true);
    const bounds = {x: b.minX, y: b.minY, width: b.width, height: b.height};
    if (!Object.values(bounds).every(Number.isFinite) || bounds.width <= 0 || bounds.height <= 0
        || Math.max(bounds.width,bounds.height) > 1e7) throw new Error('Unsupported canvas bounds');
    const artboards = roots().filter(n => n.type === 'frame').map(n => {
      const p = n[props].resolved, rect = n.getVisualWorldBounds();
      return {id: n.id, name: p.name ?? '', x: rect.minX-b.minX, y: rect.minY-b.minY, width: rect.width, height: rect.height};
    }).filter(n => typeof n.name === 'string' && n.name.trim());
    return {bounds, artboards};
  }
  function stats() { return {pid: process.pid, rssMB: Math.ceil(process.memoryUsage().rss/1048576), font_fallback: resources()}; }
  try {
    send({ready: true, layout: await describe(), ...stats()});
    for await (const line of input) {
      try {
        if (line.length > 8192) throw new Error('Render request too large');
        const request = JSON.parse(line);
        if (request.action === 'load') {
          if (!await scene.open(readFileSync(request.source, 'utf8'))) throw new Error('Document reload failed');
          await scene.waitForRenderResources();
          send({layout: await describe(), ...stats()});
        } else if (request.action === 'render') {
          if (!Array.isArray(request.region) || request.region.length !== 4
              || !request.region.every(Number.isFinite) || !(request.scale > 0)
              || request.region[2] <= 0 || request.region[3] <= 0
              || request.region[2]*request.scale > 2048 || request.region[3]*request.scale > 2048) {
            throw new Error('Invalid render region');
          }
          const image = await native.region(scene, roots(), request.region, {type: 0, scale: request.scale});
          resources();
          writeFileSync(request.output, image.data, {mode: 0o600});
          send({width: image.width, height: image.height, ...stats()});
        } else throw new Error('Unknown render request');
      } catch (error) { send({error: error.message}); }
    }
  } finally { editor.dispose(); }
};
process.argv = [process.execPath, cli, ...process.argv.slice(3)];
await import(entry);
