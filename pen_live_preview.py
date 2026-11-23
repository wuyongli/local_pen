"""Serve the selected design's canvas to a local browser without activating it."""

import errno
import hmac
import json
import os
import re
import select
import stat
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
from time import monotonic, time_ns
from urllib.parse import parse_qs, urlsplit

from edit_pen_text import load_pen
from pen_render import canvas_scale, render_canvas_png, render_canvas_svg, validate_render_scale
from pen_working_copy import file_revision, load_task_design, task_directory
from pen_tiles import MIN_LEVEL, MAX_LEVEL, InvalidTile, NativeUnsupported, RendererBusy, StaleRevision, TilesPending, ViewportRenderer


PREVIEW_CONTROL = "/_local_pen_preview"
SIGNATURE_HEADER = "X-Local-Pen-Signature"
PREVIEW_IDLE_SECONDS = 600
PREVIEW_VERSION = "1.22.1"


def control_signature(token, message):
    return hmac.digest(token.encode("ascii"), message, "sha256").hex()


PAGE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时画板 · local_pen</title>
<style>
  html { color-scheme: dark; }
  body { margin: 0; color: #d7dcda; background: #1e1e1e; font: 14px/1.5 system-ui; }
  main { position: fixed; inset: 0; overflow: auto; overflow-anchor: none; cursor: grab; outline-offset: -3px; }
  main[data-dragging] { cursor: grabbing; }
  #surface { box-sizing: border-box; display: grid; place-items: center; padding: 36px 16px 16px;
             min-width: 100%; min-height: 100%; width: max-content; height: max-content; }
  #surface[data-pannable] { padding: max(50vh, 36px) max(50vw, 16px); }
  #scene { position: relative; }
  #tile-world { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
  #previous-frame { position: absolute; pointer-events: none; }
  #tiles { position: absolute; inset: 0; overflow: hidden; pointer-events: none; }
  .tile { position: absolute; overflow: hidden; }
  .tile img { position: absolute; max-width: none; box-shadow: none; }
  #artboard-titles { position: absolute; inset: 0; pointer-events: none; }
  .artboard-title { position: absolute; box-sizing: border-box; transform: translateY(calc(-100% - 4px));
                    padding: 0 3px; border-radius: 3px; background: #1e1e1e; color: #d7dcda;
                    font-size: 11px; line-height: 16px; white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; pointer-events: auto; user-select: none; }
  img { display: block; user-select: none; box-shadow: 0 2px 24px #0006; }
  img[hidden] { display: none; }
  nav { position: fixed; top: 12px; right: 12px; display: flex; align-items: center; gap: 3px;
        padding: 4px; background: #242726; border: 1px solid #565c59; border-radius: 7px; }
  button { color: inherit; font: inherit; background: transparent; border: none;
           border-radius: 4px; min-height: 34px; padding: 0 8px; cursor: pointer; }
  button:hover { background: #383d3a; }
  #artboard-panel { position: fixed; top: 62px; right: 12px; width: min(320px, calc(100vw - 32px));
                    box-sizing: border-box; padding: 12px; border: 1px solid #565c59; border-radius: 8px;
                    background: #242726; box-shadow: 0 8px 32px #0006; }
  #artboard-panel[hidden] { display: none; }
  #artboard-panel h2 { margin: 0 0 8px; font-size: 14px; }
  #board-search { box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #565c59;
                  border-radius: 5px; background: #1e1e1e; color: inherit; font: inherit; }
  #board-count { display: block; margin: 8px 0; color: #a9b3ae; font-size: 12px; }
  #board-list { list-style: none; margin: 0; padding: 0; max-height: min(60vh, 520px); overflow: auto; }
  #board-list button { width: 100%; text-align: left; overflow-wrap: anywhere; }
  #board-list button[aria-current] { background: #34473f; }
  #zoom-level { min-width: 42px; text-align: center; font-variant-numeric: tabular-nums; }
  output { position: fixed; left: 16px; bottom: 16px; max-width: calc(100vw - 64px);
           padding: 7px 12px; background: #242726; border: 1px solid #565c59; border-radius: 6px; }
</style>
<main tabindex="0" aria-label="画板区域">
  <div id="surface"><div id="scene"><img alt="实时画板" draggable="false" hidden><div id="tiles"><div id="tile-world"></div></div><div id="artboard-titles"></div></div></div>
</main>
<nav aria-label="画板缩放">
  <button type="button" aria-label="缩小" id="zoom-out">−</button>
  <button type="button" id="zoom-level" aria-label="实际大小 100%" title="点击恢复实际大小（100%）">100%</button>
  <button type="button" aria-label="放大" id="zoom-in">+</button>
  <button type="button" id="fit">适合窗口</button>
  <button type="button" id="boards-toggle" aria-label="画板目录" aria-expanded="false" aria-controls="artboard-panel">画板</button>
</nav>
<aside id="artboard-panel" aria-label="画板目录" hidden>
  <h2>画板目录</h2>
  <input id="board-search" type="search" aria-label="查找画板" placeholder="输入画板名称…" autocomplete="off">
  <span id="board-count" role="status"></span><ul id="board-list"></ul>
</aside>
<output role="status">正在渲染画板…</output>
<script>
const viewport = document.querySelector('main');
const surface = document.querySelector('#surface');
const canvas = document.querySelector('img');
const titleLayer = document.querySelector('#artboard-titles');
const status = document.querySelector('output');
const zoomLevel = document.querySelector('#zoom-level');
let revision = '';
let fontFallback = false;
let preferVector = true;
let imageURL;
let ratio = 1;
let autoFit = true;
let width = 0;
let height = 0;
let drag;
let gestureRatio;
let sceneBounds = {x: 0, y: 0};
let viewDocumentKey = '';
let viewPersistenceActive = true;
let lastView;
let lastViewport;
let saveViewTimer;
let directory = [];
let selectedBoard;


function viewportCenter() {
  const view = viewport.getBoundingClientRect();
  return [view.left + viewport.clientWidth / 2, view.top + viewport.clientHeight / 2];
}

function imageAnchor([x, y] = viewportCenter()) {
  const image = canvas.getBoundingClientRect();
  const clamp = number => Math.max(0, Math.min(1, number));
  return image.width && image.height ? [
    clamp((x - image.left) / image.width),
    clamp((y - image.top) / image.height)
  ] : [0.5, 0.5];
}

function layout(anchor = [0.5, 0.5], point) {
  if (!width || !height) return;
  const fitted = Math.min(1, Math.max(1, viewport.offsetWidth - 32) / width,
                         Math.max(1, viewport.offsetHeight - 52) / height);
  const scale = fitted * ratio;
  surface.toggleAttribute('data-pannable', ratio > 1);
  canvas.style.width = width * scale + 'px';
  canvas.style.height = height * scale + 'px';
  zoomLevel.textContent = Math.round(scale * 100) + '%';
  const image = canvas.getBoundingClientRect();
  const [x, y] = point ?? viewportCenter();
  viewport.scrollLeft += image.left + image.width * anchor[0] - x;
  viewport.scrollTop += image.top + image.height * anchor[1] - y;
  rememberView(true);
  window.dispatchEvent(new Event('local-pen-view-change'));
}

function zoom(next, point) {
  autoFit = false;
  const anchor = imageAnchor(point);
  const fitted = Math.min(1, Math.max(1, viewport.offsetWidth - 32) / width,
                         Math.max(1, viewport.offsetHeight - 52) / height);
  ratio = Math.max(0.25, Math.min(8 / fitted, next));
  layout(anchor, point);
}

function getViewState() {
  if (!width || !height) return null;
  const image = canvas.getBoundingClientRect();
  const scale = image.width / width;
  if (!(scale > 0)) return null;
  const [x, y] = viewportCenter();
  return {version: 1, x: sceneBounds.x + (x - image.left) / scale,
          y: sceneBounds.y + (y - image.top) / scale, scale, fit: autoFit};
}

function validView(value) {
  return value && value.version === 1 && typeof value.fit === 'boolean'
    && ['x', 'y', 'scale'].every(key => typeof value[key] === 'number' && Number.isFinite(value[key]))
    && Math.abs(value.x) < 1e9 && Math.abs(value.y) < 1e9 && value.scale > 0 && value.scale <= 8;
}

function saveView() {
  clearTimeout(saveViewTimer);
  if (!viewPersistenceActive || !viewDocumentKey || !validView(lastView)) return;
  for (const name of ['sessionStorage', 'localStorage']) {
    try { window[name].setItem('local-pen:camera:v1:' + viewDocumentKey, JSON.stringify(lastView)); }
    catch { /* Viewing still works when browser storage is unavailable. */ }
  }
}

function rememberView(force = false) {
  if (!force && lastViewport && (lastViewport[0] !== viewport.clientWidth || lastViewport[1] !== viewport.clientHeight)) return;
  const next = getViewState();
  if (!next) return;
  lastView = next;
  lastViewport = [viewport.clientWidth, viewport.clientHeight];
  clearTimeout(saveViewTimer);
  saveViewTimer = setTimeout(saveView, 150);
}

function pauseViewPersistence() {
  rememberView(true); saveView();
  viewPersistenceActive = false;
  clearTimeout(saveViewTimer);
}

function restoreView(value) {
  autoFit = !validView(value) || value.fit;
  const fitted = Math.min(1, Math.max(1, viewport.offsetWidth - 32) / width,
                         Math.max(1, viewport.offsetHeight - 52) / height);
  ratio = autoFit ? 1 : value.scale / fitted;
  layout(autoFit ? undefined : [(value.x - sceneBounds.x) / width, (value.y - sceneBounds.y) / height]);
}

function updateViewDocument(next) {
  viewPersistenceActive = true;
  const key = next.documentKey || location.pathname;
  let state = getViewState();
  if (key !== viewDocumentKey) {
    saveView();
    state = null;
    for (const name of ['sessionStorage', 'localStorage']) {
      try {
        const saved = JSON.parse(window[name].getItem('local-pen:camera:v1:' + key));
        if (validView(saved)) { state = saved; break; }
      } catch { /* Ignore stale or invalid saved positions. */ }
    }
    viewDocumentKey = key;
    selectedBoard = undefined;
  }
  sceneBounds = next.bounds;
  width = next.bounds.width;
  height = next.bounds.height;
  restoreView(state);
}

function renderDirectory() {
  const tokens = document.querySelector('#board-search').value.trim().toLocaleLowerCase().split(/\\s+/).filter(Boolean);
  const found = directory.filter(board => tokens.every(token => board.name.toLocaleLowerCase().includes(token)));
  const list = document.createDocumentFragment();
  for (const board of found.slice(0, 200)) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = board.name;
    if (board.id === selectedBoard) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => focusArtboard(board));
    item.append(button); list.append(item);
  }
  document.querySelector('#board-list').replaceChildren(list);
  document.querySelector('#board-count').textContent = !found.length ? '未找到画板'
    : tokens.length ? `找到 ${found.length} 张画板${found.length > 200 ? '，请继续输入以缩小范围' : ''}`
    : `${directory.length} 张画板${found.length > 200 ? '，输入名称查找更多' : ''}`;
}

function toggleDirectory(open) {
  const panel = document.querySelector('#artboard-panel');
  panel.hidden = !open;
  document.querySelector('#boards-toggle').setAttribute('aria-expanded', String(open));
  if (open) document.querySelector('#board-search').focus({preventScroll: true});
}

function focusArtboard(board) {
  selectedBoard = board.id;
  const sized = Number.isFinite(board.width) && board.width > 0 && Number.isFinite(board.height) && board.height > 0;
  const bw = board.width, bh = board.height;
  toggleDirectory(false);
  restoreView({version: 1, fit: false, x: sceneBounds.x + board.x + (sized ? bw / 2 : 0),
               y: sceneBounds.y + board.y + (sized ? bh / 2 : 0),
               scale: sized ? Math.min(1, Math.max(1, viewport.clientWidth - 96) / bw,
                                      Math.max(1, viewport.clientHeight - 96) / bh)
                            : 1});
  renderDirectory(); viewport.focus({preventScroll: true});
}

function showArtboardTitles(artboards) {
  const byX = [...artboards].sort((a, b) => a.x - b.x);
  const labels = document.createDocumentFragment();
  for (const board of artboards) {
    const label = document.createElement('span');
    label.className = 'artboard-title';
    label.dataset.artboardId = board.id;
    label.textContent = board.name;
    label.title = board.name;
    label.style.left = board.x / width * 100 + '%';
    label.style.top = board.y / height * 100 + '%';
    const neighbor = byX.find(other => other.x > board.x && Math.abs(other.y - board.y) < 96);
    const room = (neighbor?.x ?? width) - board.x;
    label.style.maxWidth = `min(260px, max(0px, ${room / width * 100}% - 4px))`;
    labels.append(label);
  }
  titleLayer.replaceChildren(labels);
  directory = artboards.filter(board => typeof board.name === 'string');
  renderDirectory();
}

document.querySelector('#zoom-in').addEventListener('click', () => zoom(ratio * 1.25));
document.querySelector('#zoom-out').addEventListener('click', () => zoom(ratio / 1.25));
document.querySelector('#fit').addEventListener('click', () => { autoFit = true; ratio = 1; layout(); });
document.querySelector('#zoom-level').addEventListener('click', () => {
  const state = getViewState(); if (state) restoreView({...state, scale: 1, fit: false});
});
document.querySelector('#boards-toggle').addEventListener('click', () => toggleDirectory(document.querySelector('#artboard-panel').hidden));
document.querySelector('#board-search').addEventListener('input', renderDirectory);
window.addEventListener('resize', () => { if (width && height) restoreView(lastView || getViewState()); });
viewport.addEventListener('scroll', () => rememberView(), {passive: true});
window.addEventListener('pagehide', () => { rememberView(true); saveView(); });
window.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !document.querySelector('#artboard-panel').hidden) {
    toggleDirectory(false); document.querySelector('#boards-toggle').focus({preventScroll: true}); return;
  }
  if (event.target.closest('input,textarea,[contenteditable="true"]')) return;
  if (event.key === '/' || (event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey))) {
    event.preventDefault(); toggleDirectory(true);
  } else if (!event.metaKey && !event.ctrlKey && event.key === '1') document.querySelector('#zoom-level').click();
});

viewport.addEventListener('wheel', event => {
  if (!event.ctrlKey) return;
  event.preventDefault();
  if (gestureRatio !== undefined || !event.deltaY) return;
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
  zoom(ratio * Math.exp(-event.deltaY * unit / 100), [event.clientX, event.clientY]);
}, {passive: false});

// Safari and WebKit views may deliver trackpad pinching as gesture events.
viewport.addEventListener('gesturestart', event => {
  event.preventDefault();
  gestureRatio = ratio;
}, {passive: false});
viewport.addEventListener('gesturechange', event => {
  event.preventDefault();
  if (gestureRatio === undefined || !Number.isFinite(event.scale) || event.scale <= 0) return;
  const point = Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
    ? [event.clientX, event.clientY] : undefined;
  zoom(gestureRatio * event.scale, point);
}, {passive: false});
viewport.addEventListener('gestureend', event => {
  event.preventDefault();
  gestureRatio = undefined;
}, {passive: false});

viewport.addEventListener('pointerdown', event => {
  if (event.button !== 0 || event.pointerType === 'touch') return;
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY,
          left: viewport.scrollLeft, top: viewport.scrollTop};
  viewport.setPointerCapture(event.pointerId);
  viewport.setAttribute('data-dragging', '');
});
viewport.addEventListener('pointermove', event => {
  if (drag?.id !== event.pointerId) return;
  viewport.scrollLeft = drag.left + drag.x - event.clientX;
  viewport.scrollTop = drag.top + drag.y - event.clientY;
});
viewport.addEventListener('lostpointercapture', event => {
  if (drag?.id !== event.pointerId) return;
  drag = undefined;
  viewport.removeAttribute('data-dragging');
});

async function refresh() {
  const pending = setTimeout(() => {
    status.textContent = '正在更新画板…';
    status.hidden = false;
  }, 300);
  try {
    const response = await fetch(preferVector ? 'canvas' : 'canvas.png', {
      cache: 'no-store',
      headers: revision ? {'If-None-Match': revision} : {}
    });
    if (response.status === 202) {
      status.textContent = '正在更新画板…';
      status.hidden = false;
      return;
    }
    if (response.status !== 304) {
      if (!response.ok) throw new Error('暂时无法更新画板，请检查本地渲染环境。');
      const nextRevision = response.headers.get('ETag');
      const [blob, titlesResponse] = await Promise.all([
        response.blob(), fetch('artboards.json', {cache: 'no-store', headers: {'If-Match': nextRevision}})
      ]);
      if (titlesResponse.status === 412) return;
      if (!titlesResponse.ok) throw new Error('暂时无法更新画板标题。');
      const artboards = await titlesResponse.json();
      const isVector = response.headers.get('X-Local-Pen-Image') === 'svg';
      const next = URL.createObjectURL(isVector ? new Blob([blob], {type: 'image/svg+xml'}) : blob);
      try {
        const image = new Image();
        image.src = next;
        try {
          await image.decode();
        } catch (error) {
          if (!isVector) throw error;
          preferVector = false;
          URL.revokeObjectURL(next);
          return;
        }
        const resized = width !== image.naturalWidth || height !== image.naturalHeight;
        const anchor = resized ? imageAnchor() : null;
        canvas.src = image.src;
        canvas.hidden = false;
        window.dispatchEvent(new Event('local-pen-legacy-ready'));
        if (resized) {
          width = image.naturalWidth;
          height = image.naturalHeight;
          layout(anchor);
        }
        showArtboardTitles(artboards);
      } catch (error) {
        URL.revokeObjectURL(next);
        throw error;
      }
      if (imageURL) URL.revokeObjectURL(imageURL);
      imageURL = next;
      revision = nextRevision;
      fontFallback = response.headers.get('X-Local-Pen-Font-Fallback') === '1';
    }
    status.textContent = fontFallback ? '部分字体未缓存，预览文字可能与 Pen 稍有差异。' : '';
    status.hidden = !fontFallback;
  } catch (error) {
    status.textContent = error instanceof TypeError
      ? '实时预览已断开，正在尝试重连。'
      : '暂时无法更新画板，请检查本地渲染环境。';
    status.hidden = false;
  } finally {
    clearTimeout(pending);
    setTimeout(refresh, 500);
  }
}
</script>
<script src="preview.js"></script>
</html>
""".encode()


class PreviewPending(Exception):
    pass


class PreviewRender:
    def __init__(self, window, revision):
        self.window = window
        self.revision = revision
        self.cancelled = self

    def is_set(self):
        return self.window.cancelled.is_set() or self.window.revision() != self.revision


@dataclass(frozen=True)
class PreviewFrame:
    revision: tuple
    etag: str
    data: bytes
    titles: bytes
    image_format: str
    font_fallback: bool


class PreviewHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        view = self.server.view
        if self.path != PREVIEW_CONTROL or self.headers.get("Host") != view.host:
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or len(length) > 4 or not 1 <= int(length) <= 1024:
            self.send_error(400)
            return
        self.connection.settimeout(1)
        try:
            data = self.rfile.read(int(length))
            signature = self.headers.get(SIGNATURE_HEADER, "")
            if not signature.isascii() or not hmac.compare_digest(signature, control_signature(view.route_token, data)):
                self.send_error(403)
                return
            request = json.loads(data)
            if not isinstance(request, dict) or view.cancelled.is_set():
                self.send_error(409)
                return
            action = request.get("action")
            if action != "status" and request.get("source") != view.source_key:
                self.send_error(409)
                return
            view.last_activity = monotonic()
            if action == "refresh":
                scale = request.get("scale")
                validate_render_scale(scale)
                view.configure(scale)
            elif action == "stop":
                view.close()
            elif action != "status":
                self.send_error(400)
                return
        except (OSError, ValueError):
            self.send_error(400)
            return
        response = json.dumps({"scale": view.settings[0], "source": view.source_key,
                               "worker_version": PREVIEW_VERSION if view.independent else None}).encode("utf-8")
        proof = control_signature(view.route_token, b"response\0" + data + response)
        try:
            self.reply(200, "application/json; charset=utf-8", response, signature=proof)
        finally:
            if action == "stop":
                view.stop_replied.set()

    def do_GET(self):
        view = self.server.view
        if self.headers.get("Host") != view.host:
            self.send_error(400)
            return
        path = self.path.partition("?")[0]
        if path in (view.route, view.route + "canvas", view.route + "canvas.png", view.route + "artboards.json", view.route + "manifest.json", view.route + "tile.png"):
            view.last_activity = monotonic()
        if path == view.route:
            self.reply(200, "text/html; charset=utf-8", view.page)
        elif path == view.route + "preview.js":
            self.reply(200, "text/javascript; charset=utf-8", Path(__file__).with_name("pen_preview.js").read_bytes())
        elif path in (view.route + "manifest.json", view.route + "tile.png"):
            self.serve_tiles(path)
        elif path in (view.route + "canvas", view.route + "canvas.png"):
            try:
                frame = view.image(prefer_vector=path.endswith("/canvas"))
            except PreviewPending:
                self.reply(202, "text/plain; charset=utf-8", b"Preview updating")
                return
            except (OSError, ValueError, RecursionError):
                self.reply(503, "text/plain; charset=utf-8", b"Preview temporarily unavailable")
                return
            content_type = "application/octet-stream" if frame.image_format == "svg" else "image/png"
            if self.headers.get("If-None-Match") == frame.etag:
                self.reply(304, content_type, b"", frame.etag)
            else:
                self.reply(200, content_type, frame.data, frame.etag, font_fallback=frame.font_fallback,
                           image_format=frame.image_format)
        elif path == view.route + "artboards.json":
            etag = self.headers.get("If-Match")
            titles = view.artboard_titles(etag)
            if titles is None:
                self.reply(412, "text/plain; charset=utf-8", b"Preview revision changed")
            else:
                self.reply(200, "application/json; charset=utf-8", titles, etag)
        else:
            self.send_error(404)

    def serve_tiles(self, path):
        view = self.server.view
        try:
            if path.endswith("manifest.json"):
                manifest = view.tiles.manifest()
                etag = manifest["etag"]
                if self.headers.get("If-None-Match") == etag:
                    self.reply(304, "application/json", b"", etag)
                else:
                    self.reply(200, "application/json", json.dumps(manifest, separators=(",", ":")).encode(), etag)
            else:
                try:
                    query = parse_qs(urlsplit(self.path).query, strict_parsing=True, max_num_fields=3)
                    if set(query) != {"level", "col", "row"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid tile parameters")
                    if any(re.fullmatch(r"-?[0-9]{1,9}", v[0]) is None for v in query.values()):
                        raise ValueError("Invalid tile coordinates")
                    level, col, row = (int(query[k][0]) for k in ("level", "col", "row"))
                    if not MIN_LEVEL <= level <= MAX_LEVEL or min(col,row) < 0:
                        raise ValueError("Invalid tile coordinates")
                except ValueError:
                    self.reply(400, "text/plain", b"Invalid tile request")
                    return
                metadata, data = view.tiles.tile(self.headers.get("If-Match"), level, col, row)
                self.reply(200, "image/png", data, font_fallback=metadata.get("font_fallback", False), image_format="tile")
        except NativeUnsupported:
            self.reply(501, "text/plain", b"Native viewport adapter is unsupported; use canvas")
        except InvalidTile:
            self.reply(400, "text/plain", b"Invalid tile coordinates")
        except StaleRevision:
            self.reply(412, "text/plain", b"Preview revision changed")
        except (TilesPending, RendererBusy):
            self.reply(202, "text/plain", b"Preview updating")
        except (OSError, ValueError, RecursionError):
            self.reply(503, "text/plain", b"Viewport render unavailable")

    def reply(self, status, kind, data, etag=None, *, font_fallback=False, image_format=None, signature=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src blob:; connect-src 'self'; frame-ancestors 'none'")
        if etag is not None:
            self.send_header("ETag", etag)
        if font_fallback:
            self.send_header("X-Local-Pen-Font-Fallback", "1")
        if image_format is not None:
            self.send_header("X-Local-Pen-Image", image_format)
        if signature is not None:
            self.send_header(SIGNATURE_HEADER, signature)
        self.end_headers()
        if data:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, *_):
        pass


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_port = False


class PreviewWindow:
    def __init__(self, source, scale, port=0, route_token=None, *, independent=False, task_dir=None):
        self.source = source
        task_name = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ ]*", "", Path(task_dir).name).strip() if task_dir is not None else ""
        title = f"{escape(task_name or source.stem)} · local_pen"
        self.page = PAGE.replace("<title>实时画板 · local_pen</title>".encode("utf-8"),
                                 f"<title>{title}</title>".encode("utf-8"), 1)
        self.source_key = sha256(os.fsencode(source)).hexdigest()
        self.independent = independent
        self.last_activity = monotonic()
        self.settings = (scale, 0)
        self.route_token = route_token or token_urlsafe(24)
        self.route = f"/{self.route_token}/"
        self.instance_id = token_urlsafe(12)
        self.cancelled = Event()
        self.stop_replied = Event()
        self.close_lock = Lock()
        self.lock = Lock()
        self.settings_lock = Lock()
        self.frames = {}
        self.failed = {}
        self.version = 0
        self.server = PreviewServer(("127.0.0.1", port), PreviewHandler)
        self.server.view = self
        self.tiles = ViewportRenderer(source, generation=lambda: self.settings[1], cancelled=self.cancelled)
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def render_image(self, prefer_vector, scale, request):
        if prefer_vector:
            try:
                metadata, data = render_canvas_svg(str(self.source), request=request)
                return "svg", metadata, data
            except (OSError, ValueError, RecursionError):
                if request.is_set():
                    raise PreviewPending from None
        metadata, data = render_canvas_png(str(self.source), scale, request=request)
        return "png", metadata, data

    def image(self, *, prefer_vector=False):
        with self.lock:
            if self.cancelled.is_set() or self.source.resolve() != self.source:
                raise ValueError("Preview is closed or its source moved")
            scale, generation = self.settings
            revision = (file_revision(self.source), generation)
            current = self.frames.get(prefer_vector)
            if current is not None and current.revision == revision:
                return current
            failed_revision, retry_after = self.failed.get(prefer_vector, (None, 0))
            if revision == failed_revision and monotonic() < retry_after:
                raise ValueError("Preview is waiting to retry")
            request = PreviewRender(self, revision)
            if current is not None:
                _, _, _, changed_at, _ = revision[0]
                quiet_time = max(0, min(0.25, 0.25 - (time_ns() - changed_at) / 1e9))
                if self.cancelled.wait(quiet_time) or request.is_set():
                    raise PreviewPending
            try:
                image_format, metadata, data = self.render_image(prefer_vector, scale, request)
                titles = json.dumps(metadata.get("artboards", []), separators=(",", ":")).encode("utf-8")
            except (OSError, ValueError, RecursionError):
                if request.is_set():
                    raise PreviewPending from None
                self.failed[prefer_vector] = (revision, monotonic() + 5)
                raise
            with self.settings_lock:
                if request.is_set():
                    raise PreviewPending
                self.version += 1
                etag = f'"{self.instance_id}:{self.version}"'
                frame = PreviewFrame(revision, etag, data, titles, image_format, metadata.get("font_fallback", False))
                self.frames[prefer_vector] = frame
                if image_format == "png":
                    self.frames[False] = frame
                return frame

    def artboard_titles(self, etag):
        with self.settings_lock:
            for frame in self.frames.values():
                if frame.etag == etag:
                    return frame.titles
        return None

    def revision(self):
        try:
            return file_revision(self.source), self.settings[1]
        except OSError:
            return None

    def status(self):
        if self.cancelled.is_set():
            return {"active": False}
        scale, _ = self.settings
        return {"active": True, "url": f"http://{self.host}{self.route}", "file_path": str(self.source),
                "view": "canvas", "layer_id": None, "scale": scale}

    def configure(self, scale):
        with self.settings_lock:
            self.settings = (scale, self.settings[1] + 1)

    def close(self):
        with self.close_lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            self.tiles.close()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()


class SharedPreview:
    def __init__(self, source, port, token):
        self.source = source
        self.source_key = sha256(os.fsencode(source)).hexdigest()
        self.port = port
        self.token = token

    def request(self, action, **arguments):
        # The loopback port might belong to another app. Prove ownership without sending the preview token.
        data = json.dumps({"source": self.source_key, "action": action,
                           "nonce": token_urlsafe(16), **arguments}).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", self.port, timeout=0.75)
        try:
            connection.request("POST", PREVIEW_CONTROL, body=data,
                               headers={SIGNATURE_HEADER: control_signature(self.token, data)})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("preview owner is unavailable")
            body = response.read(1024)
            signature = response.getheader(SIGNATURE_HEADER, "")
            if not signature.isascii() or not hmac.compare_digest(
                signature, control_signature(self.token, b"response\0" + data + body)
            ):
                raise ValueError("preview owner could not be verified")
            return json.loads(body)
        except (OSError, HTTPException) as error:
            raise ValueError("preview owner is unavailable") from error
        finally:
            connection.close()

    def details(self):
        try:
            state = self.request("status")
        except ValueError:
            return None
        state.setdefault("source", self.source_key)
        return state

    def status(self):
        state = self.details()
        if state is None or state["source"] != self.source_key:
            return {"active": False}
        return {"active": True, "url": f"http://127.0.0.1:{self.port}/{self.token}/",
                "file_path": str(self.source), "view": "canvas", "layer_id": None, "scale": state["scale"]}

    def configure(self, scale):
        self.request("refresh", scale=scale)

    def close(self, *, source_key=None):
        self.request("stop", source=self.source_key if source_key is None else source_key)


def start_preview_process(directory, source, scale, port=0, token=None):
    token = token or token_urlsafe(24)
    worker = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--serve-preview"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        with worker.stdin as pipe:
            pipe.write(json.dumps({"directory": str(directory), "source": str(source), "scale": scale, "port": port,
                                  "token": token}).encode("utf-8") + b"\n")
        with worker.stdout as pipe:
            if not select.select([pipe], [], [], 3)[0]:
                raise ValueError("the independent preview did not start in time")
            response = json.loads(pipe.readline(1024))
        if "errno" in response:
            raise OSError(response["errno"], "could not bind the preview server")
        return SharedPreview(source, response["port"], token)
    except BaseException:
        worker.terminate()
        try:
            worker.wait(timeout=1)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        raise
    finally:
        if worker.poll() is None:
            Thread(target=worker.wait, daemon=True).start()


def serve_preview():
    arguments = json.loads(sys.stdin.buffer.readline())
    directory = Path(arguments["directory"])
    try:
        window = PreviewWindow(Path(arguments["source"]), arguments["scale"],
                               arguments["port"], arguments["token"], independent=True, task_dir=directory)
    except OSError as error:
        print(json.dumps({"errno": error.errno}), flush=True)
        return
    try:
        print(json.dumps({"port": window.server.server_port}), flush=True)
        while not window.stop_replied.wait(5):
            try:
                selected = load_task_design(directory)
            except (OSError, ValueError, RecursionError):
                selected = None
            if selected != window.source or monotonic() - window.last_activity >= PREVIEW_IDLE_SECONDS:
                return
    finally:
        window.close()
        # Wait for an in-flight renderer to notice cancellation and terminate its subprocess.
        with window.lock:
            pass


def preview_address_file(directory):
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local" / "state"
    folder = state / "local_pen" / "previews"
    return folder / (sha256(os.fsencode(directory)).hexdigest() + ".json")


def read_preview_address(record):
    if record.is_symlink():
        raise ValueError("the saved preview address cannot be a symbolic link")
    try:
        saved = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(saved, dict) or type(saved.get("port")) is not int
            or not 1024 <= saved["port"] <= 65535 or not isinstance(saved.get("token"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32}", saved["token"]) is None):
        raise ValueError("the saved preview address is invalid")
    return saved["port"], saved["token"]


def open_task_preview(directory, source, scale):
    import fcntl

    record = preview_address_file(directory)
    folder = record.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("the local preview address directory must be private to the current user")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if load_task_design(directory) != source:
            raise ValueError("the selected task design changed; select it again before starting preview")
        saved = read_preview_address(record)
        if saved is not None:
            shared = SharedPreview(source, *saved)
            owner = shared.details()
            if owner is not None:
                if owner.get("worker_version") == PREVIEW_VERSION and owner["source"] == shared.source_key:
                    return shared
                shared.close(source_key=owner["source"])
            try:
                return start_preview_process(directory, source, scale, *saved)
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        window = start_preview_process(directory, source, scale)
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as pending:
                temporary = Path(pending.name)
                try:
                    json.dump({"port": window.port, "token": window.token}, pending)
                    pending.close()
                    os.replace(temporary, record)
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            window.close()
            raise
        return window
    finally:
        os.close(fd)


class LivePreview:
    def __init__(self):
        self.window = None
        self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.window = None
        self.directory = None

    def stop(self):
        if self.window is not None:
            try:
                if self.window.status()["active"]:
                    self.window.close()
            finally:
                self.window = None
                self.directory = None

    def ensure(self, directory, source, scale=None):
        if self.window is not None and self.directory == directory and self.window.source == source:
            current = self.window.status()
            if current["active"]:
                if scale is not None:
                    self.window.configure(scale)
                    current = self.window.status()
                if current["active"]:
                    return {**current, "task_dir": str(directory)}
        if load_task_design(directory) != source:
            raise ValueError("the selected task design changed; select it again before starting preview")
        document = load_pen(source)
        canvas_scale(document["children"], scale or 1)
        replacement = open_task_preview(directory, source, scale or 1)
        self.window = replacement
        self.directory = directory
        if scale is not None:
            replacement.configure(scale)
        current = replacement.status()
        if not current["active"]:
            raise ValueError("the preview owner stopped while connecting; reopen this preview")
        return {**current, "task_dir": str(directory)}

    def command(self, action="start", task_dir=None, scale=1, *, layer_id=None):
        # Clients with the previous tool schema may still send layer_id.
        if action not in ("start", "status", "stop"):
            raise ValueError("action must be start, status, or stop")
        if os.name != "posix":
            raise ValueError("background saving requires macOS or Linux")
        directory = self.directory if task_dir is None and self.directory is not None else task_directory(task_dir)
        if action != "start" and self.directory == directory and self.window is not None:
            current = self.window.status()
            if current["active"]:
                if action == "stop":
                    self.stop()
                    return {"active": False}
                return current
        source = load_task_design(directory)
        if action != "start":
            saved = read_preview_address(preview_address_file(directory))
            if saved is None:
                return {"active": False}
            peer = SharedPreview(source or directory, *saved)
            if action == "status":
                return peer.status()
            owner = peer.details()
            if owner is not None:
                peer.close(source_key=owner["source"])
            return {"active": False}
        if source is None:
            raise ValueError("select this task's .pen with task_design before starting a live preview")
        validate_render_scale(scale)
        return self.ensure(directory, source, scale=scale)


if __name__ == "__main__" and sys.argv[1:] == ["--serve-preview"]:
    serve_preview()
