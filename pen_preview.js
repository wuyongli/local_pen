/* Camera controls and legacy image fallback are supplied by the preview page. */
(() => {
  // An already-running older server may serve this updated script after a manual reload.
  if (typeof updateViewDocument !== 'function') { refresh(); return; }
  const layer = document.querySelector('#tiles');
  const world = document.querySelector('#tile-world');
  const visibleTiles = new Map();
  const pending = new Map();
  const maxImages = 64;
  let manifest, snapshot, frame, activeLevel, targetLevel, displayedManifest;
  let epoch = 0, scheduled, frameFallback, pollTimer, retry;
  let stopped = false, waitingRevision = false, polling = false;
  let wanted = [], required = [], failures = 0, titlesRevision;

  function notice(message = '') {
    status.textContent = message;
    status.hidden = !message;
  }
  function remove(key) {
    const tile = visibleTiles.get(key);
    if (tile) { URL.revokeObjectURL(tile.url); tile.element.remove(); visibleTiles.delete(key); }
  }
  function discardPending() {
    epoch++;
    for (const item of pending.values()) item.controller.abort();
    pending.clear();
  }
  function camera() {
    const rect = canvas.getBoundingClientRect();
    return {rect, scale: rect.width / width};
  }
  function positionTile(tile) {
    const edge = manifest.tilePixels/2**tile.level, pad = manifest.padding/2**tile.level;
    Object.assign(tile.element.style,{left:tile.col*edge+'px',top:tile.row*edge+'px',
      width:edge+'px',height:edge+'px',visibility:'hidden'});
    Object.assign(tile.image.style,{left:-pad+'px',top:-pad+'px',width:edge+2*pad+'px',height:edge+2*pad+'px'});
  }
  function paintTiles(image, painted, left, top, right, bottom) {
    const context = image.getContext('2d');
    if (!context) throw new Error('Canvas compositing is unavailable');
    context.clearRect(0,0,image.width,image.height);
    const sx = image.width/(right-left), sy = image.height/(bottom-top);
    for (const tile of painted) {
      const edge = manifest.tilePixels/2**tile.level, pad = manifest.padding/2**tile.level;
      const x0 = Math.round((tile.col*edge-left)*sx), y0 = Math.round((tile.row*edge-top)*sy);
      const x1 = Math.round(((tile.col+1)*edge-left)*sx), y1 = Math.round(((tile.row+1)*edge-top)*sy);
      // Shared integer edges avoid both antialiased gaps and transparent-image overlap.
      context.save(); context.beginPath(); context.rect(x0,y0,x1-x0,y1-y0); context.clip();
      context.drawImage(tile.image,(tile.col*edge-pad-left)*sx,(tile.row*edge-pad-top)*sy,
                        (edge+2*pad)*sx,(edge+2*pad)*sy);
      context.restore();
    }
  }
  function paintVisible(view) {
    if (snapshot) { if (frame) frame.hidden = true; return; }
    const painted = [...visibleTiles.values()].filter(tile => !tile.element.hidden);
    if (!painted.length) return;
    const {rect,scale} = view, port = viewport.getBoundingClientRect();
    const left = Math.max(0,(port.left-rect.left)/scale), top = Math.max(0,(port.top-rect.top)/scale);
    const right = Math.min(width,(port.right-rect.left)/scale), bottom = Math.min(height,(port.bottom-rect.top)/scale);
    if (right <= left || bottom <= top) { if (frame) frame.hidden = true; return; }
    if (!frame) { frame = document.createElement('canvas'); frame.id = 'tile-frame'; layer.append(frame); }
    const pixels = (window.devicePixelRatio || 1)*scale;
    const factor = Math.min(1,Math.sqrt(16000000/((right-left)*(bottom-top)*pixels*pixels)),
                            8192/((right-left)*pixels),8192/((bottom-top)*pixels));
    const w = Math.max(1,Math.floor((right-left)*pixels*factor)), h = Math.max(1,Math.floor((bottom-top)*pixels*factor));
    if (frame.width !== w) frame.width = w;
    if (frame.height !== h) frame.height = h;
    paintTiles(frame,painted,left,top,right,bottom);
    Object.assign(frame.style,{position:'absolute',left:'0',top:'0',
      transform:`translate(${left*scale}px,${top*scale}px)`,width:(right-left)*scale+'px',height:(bottom-top)*scale+'px'});
    frame.hidden = false;
  }
  function applyCamera() {
    if (!manifest) return;
    const view = camera(), {scale} = view;
    world.style.width = width+'px'; world.style.height = height+'px';
    world.style.transform = `scale(${scale})`;
    if (snapshot) {
      snapshot.canvas.style.left = snapshot.x-manifest.bounds.x+'px';
      snapshot.canvas.style.top = snapshot.y-manifest.bounds.y+'px';
    }
    paintVisible(view);
  }
  function freezeFrame() {
    if (snapshot || !manifest) return;
    const painted = [...visibleTiles.values()].filter(tile => !tile.element.hidden);
    if (!painted.length) return;
    const {rect, scale} = camera(), view = viewport.getBoundingClientRect();
    const left = Math.max(0, (view.left-rect.left)/scale), top = Math.max(0, (view.top-rect.top)/scale);
    const right = Math.min(width, (view.right-rect.left)/scale), bottom = Math.min(height, (view.bottom-rect.top)/scale);
    if (right <= left || bottom <= top) return;
    const image = document.createElement('canvas');
    const pixels = Math.min(2, window.devicePixelRatio || 1) * scale;
    const factor = Math.min(1, Math.sqrt(8000000/((right-left)*(bottom-top)*pixels*pixels)),
                            8192/((right-left)*pixels),8192/((bottom-top)*pixels));
    image.width = Math.max(1,Math.floor((right-left)*pixels*factor));
    image.height = Math.max(1,Math.floor((bottom-top)*pixels*factor));
    paintTiles(image,painted,left,top,right,bottom);
    image.id = 'previous-frame';
    image.style.width = right-left+'px'; image.style.height = bottom-top+'px';
    snapshot = {canvas:image,x:manifest.bounds.x+left,y:manifest.bounds.y+top};
    world.append(image); applyCamera();
  }
  function clearSnapshot() {
    if (snapshot) { snapshot.canvas.remove(); snapshot.canvas.width = snapshot.canvas.height = 0; }
    snapshot = undefined;
  }
  function useLegacy() {
    freezeFrame(); pauseViewPersistence(); stopped = true; discardPending();
    clearTimeout(pollTimer); clearTimeout(retry); clearTimeout(frameFallback); cancelAnimationFrame(scheduled);
    for (const key of visibleTiles.keys()) remove(key);
    if (frame) { frame.remove(); frame.width = frame.height = 0; frame = undefined; }
    delete layer.dataset.ready;
    refresh();
  }
  function selection() {
    const {rect, scale} = camera(), view = viewport.getBoundingClientRect();
    let level = Math.max(manifest.minLevel,Math.min(manifest.maxLevel,
      Math.ceil(Math.log2(scale*Math.min(3,window.devicePixelRatio || 1)))));
    let edge = manifest.tilePixels/2**level;
    const left = Math.max(0,(view.left-rect.left)/scale), top = Math.max(0,(view.top-rect.top)/scale);
    const right = Math.min(width,(view.right-rect.left)/scale), bottom = Math.min(height,(view.bottom-rect.top)/scale);
    if (right <= left || bottom <= top) return {level,visible:[],all:[]};
    while (level > manifest.minLevel &&
      (Math.ceil(right/edge)-Math.floor(left/edge))*(Math.ceil(bottom/edge)-Math.floor(top/edge)) > maxImages) {
      level--; edge *= 2;
    }
    const x0 = Math.floor(left/edge), x1 = Math.ceil(right/edge)-1;
    const y0 = Math.floor(top/edge), y1 = Math.ceil(bottom/edge)-1;
    const visible = [], neighbors = [];
    for (let row=Math.max(0,y0-1); row<=Math.min(Math.ceil(height/edge)-1,y1+1); row++) {
      for (let col=Math.max(0,x0-1); col<=Math.min(Math.ceil(width/edge)-1,x1+1); col++) {
        const cell = {key:`${level}:${col}:${row}`,level,col,row,
          distance:Math.hypot((col+.5)*edge-(left+right)/2,(row+.5)*edge-(top+bottom)/2)};
        (col>=x0 && col<=x1 && row>=y0 && row<=y1 ? visible : neighbors).push(cell);
      }
    }
    const order = (a,b) => a.distance-b.distance;
    visible.sort(order); neighbors.sort(order);
    return {level,visible,all:[...visible,...neighbors].slice(0,maxImages)};
  }
  function schedule() {
    if (scheduled !== undefined || stopped) return;
    const run = () => {
      clearTimeout(frameFallback); cancelAnimationFrame(scheduled);
      scheduled = undefined; frameFallback = undefined; draw();
    };
    scheduled = requestAnimationFrame(run);
    // Some visible/occluded browser windows pause rAF; tile I/O must still make bounded progress.
    frameFallback = setTimeout(() => { if (document.visibilityState !== 'hidden') run(); },80);
  }
  function trim() {
    const keys = new Set(wanted.map(tile => tile.key));
    for (const key of visibleTiles.keys()) {
      if (visibleTiles.size <= maxImages) break;
      if (!keys.has(key)) remove(key);
    }
  }
  function draw() {
    if (!manifest || stopped) return;
    applyCamera();
    const next = selection();
    if (activeLevel !== undefined && activeLevel !== next.level) {
      freezeFrame();
      for (const tile of visibleTiles.values()) tile.element.hidden = true;
    }
    targetLevel = next.level; wanted = next.all; required = next.visible;
    const keys = new Set(wanted.map(tile => tile.key));
    for (const [key,item] of pending) {
      if (!keys.has(key)) { item.controller.abort(); pending.delete(key); }
    }
    for (const cell of wanted) {
      const tile = visibleTiles.get(cell.key);
      if (tile) { visibleTiles.delete(cell.key); visibleTiles.set(cell.key,tile); }
    }
    trim();
    if (!required.every(tile => visibleTiles.has(tile.key))) delete layer.dataset.ready;
    pump();
  }
  function pump() {
    if (stopped || waitingRevision) return;
    for (const cell of wanted) {
      if (pending.size >= 4) break;
      if (pending.has(cell.key) || visibleTiles.has(cell.key)) continue;
      const item = {controller:new AbortController()}; pending.set(cell.key,item);
      void load(cell,item,epoch);
    }
    if (required.every(tile => visibleTiles.has(tile.key))) {
      const repaint = !!snapshot || activeLevel !== targetLevel || layer.dataset.ready !== manifest.etag;
      activeLevel = targetLevel;
      for (const tile of visibleTiles.values()) tile.element.hidden = tile.level !== activeLevel;
      clearSnapshot(); if (repaint) paintVisible(camera());
      if (titlesRevision !== manifest.etag) {
        showArtboardTitles(manifest.artboards); titlesRevision = manifest.etag; displayedManifest = manifest;
      }
      layer.dataset.ready = manifest.etag;
      delete layer.dataset.pending;
      failures = 0;
      notice(manifest.font_fallback ? '部分字体未缓存，预览文字可能与 Pen 稍有差异。' : '');
    }
  }
  async function load(cell,item,generation) {
    let url, retryNeeded = false;
    try {
      const response = await fetch('tile.png?'+new URLSearchParams({level:cell.level,col:cell.col,row:cell.row}),
        {cache:'no-store',signal:item.controller.signal,headers:{'If-Match':manifest.etag}});
      if (generation !== epoch || item.controller.signal.aborted) return;
      if (response.status === 501) { useLegacy(); return; }
      if (response.status === 412) {
        waitingRevision = true; discardPending(); notice('正在更新画板…'); void poll(); return;
      }
      if (response.status === 202) { retryNeeded = true; return; }
      if (!response.ok) throw new Error('暂时无法更新局部画面，正在重试。');
      url = URL.createObjectURL(await response.blob());
      const image = new Image(); image.draggable = false; image.alt = ''; image.src = url;
      await image.decode();
      if (generation !== epoch || item.controller.signal.aborted) return;
      const element = document.createElement('div'); element.className = 'tile'; element.dataset.tile = cell.key;

      element.hidden = !!snapshot || activeLevel !== undefined && cell.level !== activeLevel;
      element.append(image); remove(cell.key);
      const tile = {...cell,element,image,url}; positionTile(tile);
      visibleTiles.set(cell.key,tile); url = undefined;
      world.append(element); trim();
    } catch (error) {
      if (generation === epoch && !item.controller.signal.aborted && error.name !== 'AbortError') {
        failures++; retryNeeded = true;
        if (required.some(tile => tile.key === cell.key)) notice(error.message);
      }
    } finally {
      if (url) URL.revokeObjectURL(url);
      if (pending.get(cell.key) === item) pending.delete(cell.key);
      if (generation === epoch && !stopped) {
        if (retryNeeded) { clearTimeout(retry); retry = setTimeout(schedule,failures ? 1000 : 120); }
        else pump();
      }
    }
  }
  async function poll() {
    if (stopped || polling) return;
    polling = true; clearTimeout(pollTimer);
    try {
      const response = await fetch('manifest.json',{cache:'no-store',headers:manifest?{'If-None-Match':manifest.etag}:{}});
      if (response.status === 202) { if (!manifest) notice('正在准备画板…'); return; }
      if (response.status === 501) { useLegacy(); return; }
      if (response.status === 304) return;
      if (!response.ok) throw new Error('暂时无法更新画板，正在重试。');
      const next = await response.json();
      if (next.etag === manifest?.etag) return;
      freezeFrame(); discardPending();
      for (const key of visibleTiles.keys()) remove(key);
      manifest = next; activeLevel = undefined; waitingRevision = false;
      delete layer.dataset.ready; layer.dataset.pending = next.etag;
      if (!imageURL) {
        imageURL = URL.createObjectURL(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'],{type:'image/svg+xml'}));
        canvas.src = imageURL; canvas.hidden = false;
      }
      updateViewDocument(next); applyCamera();
      if (displayedManifest) showArtboardTitles(displayedManifest.artboards.map(board => ({...board,
        x:board.x+displayedManifest.bounds.x-next.bounds.x,y:board.y+displayedManifest.bounds.y-next.bounds.y})));
      notice('正在更新画板…'); schedule();
    } catch { notice('实时预览正在重连…'); }
    finally { polling = false; if (!stopped) pollTimer = setTimeout(poll,500); }
  }
  document.addEventListener('visibilitychange',() => {
    if (document.visibilityState !== 'hidden') {
      cancelAnimationFrame(scheduled); clearTimeout(frameFallback); scheduled = undefined; schedule();
    }
  });
  viewport.addEventListener('scroll',schedule,{passive:true});
  window.addEventListener('local-pen-view-change',() => {
    // Freeze the viewport before enlarging coarse DOM tiles; large raster layers can stall painting.
    if (manifest && activeLevel !== undefined && selection().level !== activeLevel) {
      freezeFrame();
      for (const tile of visibleTiles.values()) tile.element.hidden = true;
    }
    applyCamera(); schedule();
  });
  window.addEventListener('local-pen-legacy-ready',clearSnapshot);
  window.addEventListener('beforeunload',() => {
    stopped = true; discardPending(); clearTimeout(pollTimer); clearTimeout(retry); clearTimeout(frameFallback); cancelAnimationFrame(scheduled);
    for (const key of visibleTiles.keys()) remove(key);
    clearSnapshot();
  });
  function watchDensity() {
    if (!window.matchMedia) return;
    window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener('change',() => {
      schedule(); watchDensity();
    },{once:true});
  }
  watchDensity(); void poll();
})();
