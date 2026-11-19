/* Uses the shared camera, gestures and title controls defined by the preview page. */
(() => {
  const layer = document.querySelector('#tiles');
  const visibleTiles = new Map();
  const pending = new Map();
  let manifest;
  let epoch = 0;
  let scheduled;
  let stopped = false;
  let waitingRevision = false;
  let polling = false;
  let pollTimer;
  let retry;
  let wanted = [];
  let failures = 0;
  const maxImages = 64;

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
  function useLegacy() {
    stopped = true;
    discardPending();
    clearTimeout(pollTimer); clearTimeout(retry); clearTimeout(scheduled);
    for (const key of visibleTiles.keys()) remove(key);
    layer.replaceChildren(); delete layer.dataset.ready;
    refresh();
  }
  function density() {
    const scale = canvas.getBoundingClientRect().width / width;
    return Math.max(manifest.minLevel, Math.min(manifest.maxLevel,
      Math.ceil(Math.log2(scale * Math.min(3, window.devicePixelRatio || 1)))));
  }
  function camera() {
    const rect = canvas.getBoundingClientRect();
    const scale = rect.width / width;
    return {rect, scale};
  }
  function position(tile) {
    const {scale} = camera();
    const factor = 2 ** tile.level;
    const edge = manifest.tilePixels / factor;
    const pad = manifest.padding / factor;
    Object.assign(tile.element.style, {left: tile.col*edge*scale+'px', top: tile.row*edge*scale+'px',
      width: edge*scale+'px', height: edge*scale+'px'});
    Object.assign(tile.image.style, {left: -pad*scale+'px', top: -pad*scale+'px',
      width: (edge+2*pad)*scale+'px', height: (edge+2*pad)*scale+'px'});
  }
  function selection() {
    const {rect, scale} = camera();
    const view = viewport.getBoundingClientRect();
    let level = density(), edge = manifest.tilePixels / 2**level;
    const left = Math.max(0, (view.left-rect.left)/scale), top = Math.max(0, (view.top-rect.top)/scale);
    const right = Math.min(width, (view.right-rect.left)/scale), bottom = Math.min(height, (view.bottom-rect.top)/scale);
    if (right <= left || bottom <= top) return [];
    while (level > manifest.minLevel &&
      (Math.ceil(right/edge)-Math.floor(left/edge))*(Math.ceil(bottom/edge)-Math.floor(top/edge)) > maxImages) {
      level--; edge *= 2;
    }
    const cells = [];
    for (let row=Math.floor(top/edge); row<Math.ceil(bottom/edge); row++) {
      for (let col=Math.floor(left/edge); col<Math.ceil(right/edge); col++) {
        cells.push({key:`${level}:${col}:${row}`, level, col, row,
          distance:Math.hypot((col+.5)*edge-(left+right)/2, (row+.5)*edge-(top+bottom)/2)});
      }
    }
    return cells.sort((a,b) => a.distance-b.distance).slice(0,maxImages);
  }
  function schedule() {
    clearTimeout(scheduled);
    scheduled = setTimeout(draw, 50);
  }
  function draw() {
    if (!manifest || stopped) return;
    wanted = selection();
    const keys = new Set(wanted.map(tile => tile.key));
    for (const [key, item] of pending) {
      if (!keys.has(key)) { item.controller.abort(); pending.delete(key); }
    }
    for (const tile of visibleTiles.values()) position(tile);
    // Keep lower-resolution tiles underneath while sharper replacements arrive.
    for (const [key, tile] of visibleTiles) {
      tile.element.style.zIndex = tile.level === wanted[0]?.level ? '2' : '1';
      if (visibleTiles.size > maxImages && !keys.has(key)) remove(key);
    }
    if (!wanted.every(tile => visibleTiles.has(tile.key))) delete layer.dataset.ready;
    pump();
  }
  function pump() {
    if (stopped || waitingRevision) return;
    for (const cell of wanted) {
      if (pending.size >= 4) break;
      if (pending.has(cell.key) || visibleTiles.has(cell.key)) continue;
      const controller = new AbortController();
      const item = {controller};
      pending.set(cell.key, item);
      void load(cell, item, epoch);
    }
    if (wanted.length && wanted.every(tile => visibleTiles.has(tile.key))) {
      const keys = new Set(wanted.map(tile => tile.key));
      for (const key of visibleTiles.keys()) if (!keys.has(key)) remove(key);
      layer.dataset.ready = manifest.etag;
      failures = 0;
      notice(manifest.font_fallback ? '部分字体未缓存，预览文字可能与 Pen 稍有差异。' : '');
    }
  }
  async function load(cell, item, generation) {
    let url;
    let retryNeeded = false;
    try {
      const query = new URLSearchParams({level:cell.level,col:cell.col,row:cell.row});
      const response = await fetch('tile.png?'+query, {cache:'no-store',signal:item.controller.signal,
        headers:{'If-Match':manifest.etag}});
      if (generation !== epoch || item.controller.signal.aborted) return;
      if (response.status === 501) { useLegacy(); return; }
      if (response.status === 412) {
        waitingRevision = true; discardPending();
        notice('正在更新画板…'); void poll(); return;
      }
      if (response.status === 202) { retryNeeded = true; return; }
      if (!response.ok) throw new Error('暂时无法更新局部画面，正在重试。');
      url = URL.createObjectURL(await response.blob());
      const image = new Image(); image.draggable = false; image.alt = '';
      image.src = url; await image.decode();
      if (generation !== epoch || item.controller.signal.aborted) return;
      const element = document.createElement('div'); element.className = 'tile';
      element.dataset.tile = cell.key; element.append(image);
      const tile = {...cell, element, image, url};
      remove(cell.key);
      visibleTiles.set(cell.key,tile); url = undefined;
      position(tile); element.style.zIndex = '2'; layer.append(element);
      const keys = new Set(wanted.map(tile => tile.key));
      for (const key of visibleTiles.keys()) {
        if (visibleTiles.size <= maxImages) break;
        if (!keys.has(key)) remove(key);
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        failures++;
        retryNeeded = true;
        notice(error.message);
      }
    } finally {
      if (url) URL.revokeObjectURL(url);
      if (pending.get(cell.key) === item) pending.delete(cell.key);
      if (generation === epoch && !stopped) {
        if (retryNeeded) {
          clearTimeout(retry);
          retry = setTimeout(schedule, failures ? 1000 : 120);
        } else pump();
      }
    }
  }
  async function poll() {
    if (stopped || polling) return;
    polling = true;
    clearTimeout(pollTimer);
    try {
      const response = await fetch('manifest.json', {cache:'no-store',
        headers:manifest ? {'If-None-Match':manifest.etag} : {}});
      if (response.status === 202) { if (!manifest) notice('正在准备画板…'); return; }
      if (response.status === 501) {
        // The pinned adapter is optional; retain the original whole-image preview.
        useLegacy(); return;
      }
      if (response.status === 304) return;
      if (!response.ok) throw new Error('暂时无法更新画板，正在重试。');
      const next = await response.json();
      if (next.etag === manifest?.etag) return;
      const oldCamera = camera();
      const centre = viewportCenter();
      const previous = manifest;
      const world = previous ? [previous.bounds.x+(centre[0]-oldCamera.rect.left)/oldCamera.scale,
                                previous.bounds.y+(centre[1]-oldCamera.rect.top)/oldCamera.scale] : null;
      discardPending();
      for (const key of visibleTiles.keys()) remove(key);
      manifest = next;
      waitingRevision = false;
      delete layer.dataset.ready;
      const resized = width !== next.bounds.width || height !== next.bounds.height;
      width = next.bounds.width; height = next.bounds.height;
      if (!previous) {
        imageURL = URL.createObjectURL(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'],
          {type:'image/svg+xml'}));
        canvas.src = imageURL;
        canvas.hidden = false;
      }
      if (resized || previous && (previous.bounds.x !== next.bounds.x || previous.bounds.y !== next.bounds.y)) {
        if (previous && !autoFit) {
          const fitted = Math.min(1,Math.max(1,viewport.offsetWidth-32)/width,
                                  Math.max(1,viewport.offsetHeight-52)/height);
          ratio = oldCamera.scale/fitted;
          layout([(world[0]-next.bounds.x)/width,(world[1]-next.bounds.y)/height]);
        } else { ratio=1; layout(); }
      }
      showArtboardTitles(next.artboards);
      notice('正在更新画板…');
      draw();
    } catch (error) { notice('实时预览正在重连…'); }
    finally { polling = false; if (!stopped) pollTimer = setTimeout(poll,500); }
  }
  viewport.addEventListener('scroll',schedule,{passive:true});
  window.addEventListener('local-pen-view-change',schedule);
  window.addEventListener('beforeunload',() => {stopped=true;discardPending();clearTimeout(pollTimer);clearTimeout(retry);clearTimeout(scheduled);});
  void poll();
})();
