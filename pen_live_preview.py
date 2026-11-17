"""Serve the selected design's canvas to a local browser without activating it."""

import errno
import json
import os
import re
import stat
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
from time import monotonic, time_ns

from edit_pen_text import load_pen
from pen_render import canvas_scale, render_canvas_png, validate_render_scale
from pen_working_copy import file_revision, load_task_design, task_directory


PAGE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时画板 · local_pen</title>
<style>
  html { color-scheme: dark; }
  body { margin: 0; color: #d7dcda; background: #1e1e1e; font: 14px/1.5 system-ui; }
  main { position: fixed; inset: 0; overflow: auto; cursor: grab; outline-offset: -3px; }
  main[data-dragging] { cursor: grabbing; }
  #surface { box-sizing: border-box; display: grid; place-items: center; padding: 36px 16px 16px;
             min-width: 100%; min-height: 100%; width: max-content; height: max-content; }
  #scene { position: relative; }
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
  #zoom-level { min-width: 42px; text-align: center; font-variant-numeric: tabular-nums; }
  output { position: fixed; left: 16px; bottom: 16px; max-width: calc(100vw - 64px);
           padding: 7px 12px; background: #242726; border: 1px solid #565c59; border-radius: 6px; }
</style>
<main tabindex="0" aria-label="画板区域">
  <div id="surface"><div id="scene"><img alt="实时画板" draggable="false" hidden><div id="artboard-titles"></div></div></div>
</main>
<nav aria-label="画板缩放">
  <button type="button" aria-label="缩小" id="zoom-out">−</button>
  <span id="zoom-level">100%</span>
  <button type="button" aria-label="放大" id="zoom-in">+</button>
  <button type="button" id="fit">适合窗口</button>
</nav>
<output role="status">正在渲染画板…</output>
<script>
const viewport = document.querySelector('main');
const canvas = document.querySelector('img');
const titleLayer = document.querySelector('#artboard-titles');
const status = document.querySelector('output');
const zoomLevel = document.querySelector('#zoom-level');
let revision = '';
let fontFallback = false;
let imageURL;
let ratio = 1;
let width = 0;
let height = 0;
let drag;
let gestureRatio;

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
  canvas.style.width = width * scale + 'px';
  canvas.style.height = height * scale + 'px';
  zoomLevel.textContent = Math.round(scale * 100) + '%';
  const image = canvas.getBoundingClientRect();
  const [x, y] = point ?? viewportCenter();
  viewport.scrollLeft += image.left + image.width * anchor[0] - x;
  viewport.scrollTop += image.top + image.height * anchor[1] - y;
}

function zoom(next, point) {
  const anchor = imageAnchor(point);
  ratio = Math.max(0.25, Math.min(8, next));
  layout(anchor, point);
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
}

document.querySelector('#zoom-in').addEventListener('click', () => zoom(ratio * 1.25));
document.querySelector('#zoom-out').addEventListener('click', () => zoom(ratio / 1.25));
document.querySelector('#fit').addEventListener('click', () => { ratio = 1; layout(); });
window.addEventListener('resize', () => layout(imageAnchor()));

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
    const response = await fetch('canvas.png', {
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
      const next = URL.createObjectURL(blob);
      try {
        const image = new Image();
        image.src = next;
        await image.decode();
        const resized = width !== image.naturalWidth || height !== image.naturalHeight;
        const anchor = resized ? imageAnchor() : null;
        canvas.src = image.src;
        canvas.hidden = false;
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
refresh();
</script>
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


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        view = self.server.view
        if self.headers.get("Host") != view.host:
            self.send_error(400)
            return
        path = self.path.partition("?")[0]
        if path == view.route:
            self.reply(200, "text/html; charset=utf-8", PAGE)
        elif path == view.route + "canvas.png":
            try:
                etag, data, font_fallback = view.image()
            except PreviewPending:
                self.reply(202, "text/plain; charset=utf-8", b"Preview updating")
                return
            except (OSError, ValueError, RecursionError):
                self.reply(503, "text/plain; charset=utf-8", b"Preview temporarily unavailable")
                return
            if self.headers.get("If-None-Match") == etag:
                self.reply(304, "image/png", b"", etag)
            else:
                self.reply(200, "image/png", data, etag, font_fallback=font_fallback)
        elif path == view.route + "artboards.json":
            etag = self.headers.get("If-Match")
            titles = view.artboard_titles(etag)
            if titles is None:
                self.reply(412, "text/plain; charset=utf-8", b"Preview revision changed")
            else:
                self.reply(200, "application/json; charset=utf-8", titles, etag)
        else:
            self.send_error(404)

    def reply(self, status, kind, data, etag=None, *, font_fallback=False):
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src blob:; connect-src 'self'; frame-ancestors 'none'")
        if etag is not None:
            self.send_header("ETag", etag)
        if font_fallback:
            self.send_header("X-Local-Pen-Font-Fallback", "1")
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
    def __init__(self, source, scale, port=0, route_token=None):
        self.source = source
        self.settings = (scale, 0)
        self.route_token = route_token or token_urlsafe(24)
        self.route = f"/{self.route_token}/"
        self.instance_id = token_urlsafe(12)
        self.cancelled = Event()
        self.lock = Lock()
        self.settings_lock = Lock()
        self.saved_revision = None
        self.failed_revision = None
        self.retry_after = 0
        self.version = 0
        self.png = b""
        self.titles = b"[]"
        self.font_fallback = False
        self.server = PreviewServer(("127.0.0.1", port), PreviewHandler)
        self.server.view = self
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def image(self):
        with self.lock:
            if self.cancelled.is_set() or self.source.resolve() != self.source:
                raise ValueError("Preview is closed or its source moved")
            scale, generation = self.settings
            revision = (file_revision(self.source), generation)
            if revision != self.saved_revision:
                if revision == self.failed_revision and monotonic() < self.retry_after:
                    raise ValueError("Preview is waiting to retry")
                request = PreviewRender(self, revision)
                if self.saved_revision is not None:
                    _, _, _, changed_at, _ = revision[0]
                    quiet_time = max(0, min(0.25, 0.25 - (time_ns() - changed_at) / 1e9))
                    if self.cancelled.wait(quiet_time) or request.is_set():
                        raise PreviewPending
                try:
                    metadata, data = render_canvas_png(str(self.source), scale, request=request)
                    titles = json.dumps(metadata.get("artboards", []), separators=(",", ":")).encode("utf-8")
                except (OSError, ValueError, RecursionError):
                    if request.is_set():
                        raise PreviewPending from None
                    self.failed_revision = revision
                    self.retry_after = monotonic() + 5
                    raise
                with self.settings_lock:
                    if request.is_set():
                        raise PreviewPending
                    self.saved_revision = revision
                    self.version += 1
                    self.png = data
                    self.titles = titles
                    self.font_fallback = metadata.get("font_fallback", False)
            return self.etag, self.png, self.font_fallback

    @property
    def etag(self):
        return f'"{self.instance_id}:{self.version}"'

    def artboard_titles(self, etag):
        with self.settings_lock:
            if self.version > 0 and etag == self.etag:
                return self.titles
        return None

    def revision(self):
        try:
            return file_revision(self.source), self.settings[1]
        except OSError:
            return None

    def status(self):
        scale, _ = self.settings
        return {"active": True, "url": f"http://{self.host}{self.route}", "file_path": str(self.source),
                "view": "canvas", "layer_id": None, "scale": scale}

    def configure(self, scale):
        with self.settings_lock:
            self.settings = (scale, self.settings[1] + 1)

    def close(self):
        self.cancelled.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def open_task_preview(directory, source, scale):
    import fcntl

    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local" / "state"
    folder = state / "local_pen" / "previews"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = folder / (sha256(os.fsencode(directory)).hexdigest() + ".json")
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("the local preview address directory must be private to the current user")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if record.is_symlink():
            raise ValueError("the saved preview address cannot be a symbolic link")
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            window = PreviewWindow(source, scale)
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as pending:
                    temporary = Path(pending.name)
                    try:
                        json.dump({"port": window.server.server_port, "token": window.route_token}, pending)
                        pending.close()
                        os.replace(temporary, record)
                    finally:
                        temporary.unlink(missing_ok=True)
            except (OSError, ValueError):
                window.close()
                raise
            return window
        if (not isinstance(saved, dict) or type(saved.get("port")) is not int
                or not 1024 <= saved["port"] <= 65535 or not isinstance(saved.get("token"), str)
                or re.fullmatch(r"[A-Za-z0-9_-]{32}", saved["token"]) is None):
            raise ValueError("the saved preview address is invalid")
        try:
            return PreviewWindow(source, scale, saved["port"], saved["token"])
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            raise ValueError("the saved preview port is already in use; stop the other preview session "
                             "or close the application using that port, then reopen this preview") from None
    finally:
        os.close(fd)


class LivePreview:
    def __init__(self):
        self.window = None
        self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()

    def stop(self):
        if self.window is not None:
            self.window.close()
            self.window = None
            self.directory = None

    def command(self, action="start", task_dir=None, scale=1, *, layer_id=None):
        # Clients with the previous tool schema may still send layer_id.
        if action == "stop":
            self.stop()
            return {"active": False}
        if action == "status":
            return self.window.status() if self.window else {"active": False}
        if action != "start":
            raise ValueError("action must be start, status, or stop")
        if os.name != "posix":
            raise ValueError("background saving requires macOS or Linux")
        directory = task_directory(task_dir)
        source = load_task_design(directory)
        if source is None:
            raise ValueError("select this task's .pen with task_design before starting a live preview")
        validate_render_scale(scale)
        document = load_pen(source)
        canvas_scale(document["children"], scale)
        if self.window is not None and self.directory == directory and self.window.source == source:
            # Keep the same URL when explicitly refreshing linked assets.
            self.window.configure(scale)
        else:
            if self.directory == directory:
                self.stop()
            replacement = open_task_preview(directory, source, scale)
            self.stop()
            self.window = replacement
            self.directory = directory
        return {**self.window.status(), "task_dir": str(directory)}
