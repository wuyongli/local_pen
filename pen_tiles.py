"""Bounded, offline native scene reuse and viewport-sized PNG tiles."""

import json
import math
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from threading import Event, RLock, Thread
from time import monotonic

from edit_pen_text import load_pen, write_pen_copy
from pen_render import MACOS_NETWORK_ISOLATION, preview_dependencies, renderer_command, renderer_environment, read_rendered_png
from pen_working_copy import file_revision

TILE_PIXELS = 1024
TILE_PADDING = 32
MIN_LEVEL, MAX_LEVEL = -16, 4
CACHE_BYTES = 48 * 1024 * 1024
CLI_ANCHOR = "let p=await L9(u,l,n);for(let t of e['libraries']??[])"


class NativeUnsupported(ValueError):
    pass


class RendererBusy(ValueError):
    pass


class TilesPending(ValueError):
    pass


class StaleRevision(ValueError):
    pass


class InvalidTile(ValueError):
    pass


def validate_cli(entry):
    entry = Path(entry).resolve()
    package = next((p/'package.json' for p in list(entry.parents)[:3] if (p/'package.json').is_file()), None)
    try:
        info = json.loads(package.read_text()) if package else {}
        if not isinstance(info, dict) or info.get('name') != '@pen.dev/cli' or info.get('version') != '0.3.16':
            raise NativeUnsupported('Viewport rendering requires Pen CLI 0.3.16')
        if entry.stat().st_size > 32_000_000 or entry.read_text().count(CLI_ANCHOR) != 1:
            raise NativeUnsupported('Unsupported CLI renderer structure')
    except (OSError, json.JSONDecodeError) as error:
        raise NativeUnsupported('Cannot verify Pen CLI 0.3.16') from error
    return entry


def native_command(staged, folder):
    if sys.platform != 'darwin' or not os.access(MACOS_NETWORK_ISOLATION, os.X_OK):
        raise NativeUnsupported('Isolated viewport rendering requires macOS')
    command = renderer_command()
    if len(command) == 2:
        node, cli = command
    else:
        node, cli = shutil.which('node'), command[0]
    if not node:
        raise NativeUnsupported('Viewport rendering requires Node.js 22.19+')
    cli = validate_cli(cli)
    try:
        support = subprocess.run([node, '--input-type=module', '-e',
                                  "import {registerHooks} from 'node:module';"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError) as error:
        raise NativeUnsupported('Cannot verify Node.js renderer support') from error
    if support.returncode:
        raise NativeUnsupported('Viewport rendering requires Node.js 22.19+')
    return [MACOS_NETWORK_ISOLATION, '-p', '(version 1) (allow default) (deny network*)', node,
            str(Path(__file__).with_name('pen_native_renderer.mjs')), str(cli),
            '--in', str(staged), '--export', str(folder/'unused.png'), '--export-type', 'png']


class NativeSession:
    def __init__(self, source, *, memory_mb=1280, idle_seconds=60, max_requests=120,
                 timeout=15, command_factory=None, slot_directory=None, max_processes=2):
        self.source = Path(source).resolve()
        self.memory_mb = memory_mb
        self.idle_seconds = idle_seconds
        self.max_requests = max_requests
        self.timeout = timeout
        self.command_factory = command_factory or native_command
        self.slot_directory = Path(slot_directory) if slot_directory else Path(gettempdir())/f'local-pen-render-slots-{os.getuid()}'
        self.max_processes = max_processes
        self.process = None
        self.lock = RLock()
        self.closed = Event()
        self.active = False
        self.last_use = monotonic()
        self.loaded_revision = None
        self.layout = None
        self.count = 0
        self.lease = None
        self.temporary = None
        self.buffer = b''
        self.recycle = False
        self.monitor = Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def _lease(self):
        import fcntl
        self.slot_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.slot_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Renderer slot directory must be private')
        for index in range(self.max_processes):
            fd = os.open(self.slot_directory/str(index), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lease = fd
                return
            except BlockingIOError:
                os.close(fd)
        raise RendererBusy('Local renderer slots are busy')

    def _stage(self):
        staged = Path(self.temporary.name)/'document.pen'
        # Copies and absolute resource paths keep all transient writes away from the source.
        staged.unlink(missing_ok=True)
        write_pen_copy(load_pen(self.source), staged, self.source)
        return staged

    def _start(self, revision, request):
        self._lease()
        try:
            self.temporary = TemporaryDirectory(prefix='local-pen-scene-')
            folder = Path(self.temporary.name)
            staged = self._stage()
            env = renderer_environment()
            if env is None:
                raise NativeUnsupported('Local renderer preload is unavailable')
            env.update(LOCAL_PEN_OFFLINE='1', LOCAL_PEN_MISSING_RESOURCES=str(folder/'missing'))
            self.process = subprocess.Popen(self.command_factory(staged, folder), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                            start_new_session=True, env=env)
            result = self._receive(request)
            if result.get('ready') is not True or not isinstance(result.get('layout'), dict):
                raise NativeUnsupported('Unsupported native renderer handshake')
            self.layout = result
            self.loaded_revision = revision
            self.count = 0
            self._budget(result)
        except BaseException:
            self._stop()
            raise

    def _receive(self, request):
        deadline = monotonic() + self.timeout
        while monotonic() < deadline:
            if self.closed.is_set() or (request is not None and request.is_set()
                                       and not getattr(request, 'finish_stale', False)):
                raise ValueError('Render request cancelled')
            if b'\n' not in self.buffer:
                if not select.select([self.process.stdout], [], [], .05)[0]:
                    continue
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise ValueError('Local renderer exited before replying')
                self.buffer += data
                if len(self.buffer) > 1_000_000:
                    raise ValueError('Local renderer response exceeded limit')
                continue
            line, self.buffer = self.buffer.split(b'\n', 1)
            if not line.startswith(b'LOCAL_PEN '):
                continue
            result = json.loads(line[10:])
            if not isinstance(result, dict) or result.get('error'):
                raise ValueError('Local renderer rejected the document or region')
            return result
        raise ValueError('Local renderer timed out')

    def _exchange(self, message, request):
        self.process.stdin.write(json.dumps(message).encode()+b'\n')
        result = self._receive(request)
        self.count += 1
        self._budget(result)
        return result

    def _budget(self, result):
        rss = result.get('rssMB')
        if type(rss) not in (int, float) or not math.isfinite(rss) or rss < 0:
            raise ValueError('Invalid native renderer memory report')
        self.recycle |= rss >= self.memory_mb or self.count >= self.max_requests

    def _ensure(self, revision, request):
        if self.closed.is_set():
            raise ValueError('Renderer is closed')
        if self.process is not None and (self.recycle or self.process.poll() is not None):
            self._stop()
        if self.process is None:
            self._start(revision, request)
        elif self.loaded_revision != revision:
            self.layout = self._exchange({'action': 'load', 'source': str(self._stage())}, request)
            self.loaded_revision = revision
        return self.layout

    def describe(self, revision, request=None):
        with self.lock:
            self.active = True
            try:
                return self._ensure(revision, request)
            except (OSError, ValueError):
                self._stop()
                raise
            finally:
                self.active = False
                self.last_use = monotonic()

    def render(self, revision, region, scale, request=None):
        with self.lock:
            self.active = True
            try:
                self._ensure(revision, request)
                output = Path(self.temporary.name)/'tile.png'
                output.unlink(missing_ok=True)
                result = self._exchange({'action':'render', 'region':region, 'scale':scale, 'output':str(output)}, request)
                if output.stat().st_size > 8_000_000:
                    raise ValueError('Tile exceeds byte limit')
                _, data = read_rendered_png(output, {'id':'tile', 'width':region[2], 'height':region[3]},
                                            scale, 4_000_000, 8_000_000)
                return result, data
            except (OSError, ValueError):
                self._stop()
                raise
            finally:
                self.active = False
                self.last_use = monotonic()

    def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()
            process.stdin.close()
            process.stdout.close()
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
        self.buffer = b''
        self.loaded_revision = None
        self.recycle = False
        self.layout = None

    def _monitor(self):
        while not self.closed.wait(.25):
            process = self.process
            if process is None:
                continue
            # A separate watchdog can terminate a native allocation even while the request lock is held.
            try:
                rss = int(subprocess.check_output(['ps','-o','rss=','-p',str(process.pid)], stderr=subprocess.DEVNULL,
                                                  timeout=1).strip() or 0) / 1024
                if rss > self.memory_mb * 1.2 and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            if self.lock.acquire(blocking=False):
                try:
                    if not self.active and (self.recycle or monotonic()-self.last_use > self.idle_seconds
                                            or self.process and self.process.poll() is not None):
                        self._stop()
                finally:
                    self.lock.release()

    def close(self):
        self.closed.set()
        with self.lock:
            self._stop()
        self.monitor.join(timeout=2)


class RevisionRequest:
    # Native tile drawing is short and serial; consume its reply before discarding stale output.
    finish_stale = True

    def __init__(self, owner, revision):
        self.owner, self.revision = owner, revision

    def is_set(self):
        try:
            return self.owner.cancelled.is_set() or self.owner.revision() != self.revision
        except (OSError, ValueError, RecursionError):
            return True


class ViewportRenderer:
    def __init__(self, source, *, generation=lambda: 0, cancelled=None, session=None, cache_bytes=CACHE_BYTES):
        self.source = Path(source).resolve()
        self.generation = generation
        self.cancelled = cancelled or Event()
        self.session = session or NativeSession(source)
        self.lock = RLock()
        self.cache = OrderedDict()
        self.cache_size = 0
        self.cache_bytes = cache_bytes
        self.current_revision = None
        self.current = None
        self.dependencies = []
        self.source_revision = None
        self.nonce = os.urandom(12).hex()
        self.retry_after = 0
        self.retry_kind = ValueError

    def revision(self):
        if self.source.resolve() != self.source:
            raise ValueError('Preview source moved')
        source_revision = file_revision(self.source)
        if self.source_revision != source_revision:
            _, images = preview_dependencies(load_pen(self.source))
            self.dependencies = [(self.source.parent/image).resolve() for image in sorted(images) if ':' not in image]
            self.source_revision = source_revision
        assets = []
        for image in self.dependencies:
            try: assets.append(file_revision(image))
            except OSError: assets.append(None)
        return source_revision, tuple(assets), self.generation()

    def _manifest(self):
        revision = self.revision()
        if self.current_revision == revision and self.current is not None:
            return self.current
        if monotonic() < self.retry_after:
            raise self.retry_kind('Native viewport renderer waiting to retry')
        request = RevisionRequest(self, revision)
        try:
            result = self.session.describe(revision, request=request)
        except RendererBusy:
            raise
        except (OSError, ValueError) as error:
            self.retry_kind = NativeUnsupported if isinstance(error, NativeUnsupported) else ValueError
            if request.is_set():
                raise TilesPending('Document changed during layout') from None
            self.retry_after = monotonic() + (30 if isinstance(error, NativeUnsupported) else 2)
            raise
        if request.is_set():
            raise TilesPending('Document changed during layout')
        bounds = result['layout']['bounds']
        if (set(bounds) != {'x', 'y', 'width', 'height'}
                or not all(type(v) in (int,float) and math.isfinite(v) for v in bounds.values())
                or not 0 < bounds['width'] <= 1e7 or not 0 < bounds['height'] <= 1e7):
            raise ValueError('Invalid native layout')
        self.current = {**result['layout'], 'etag':'"'+sha256((self.nonce+repr(revision)).encode()).hexdigest()+'"',
                        'tilePixels':TILE_PIXELS, 'padding':TILE_PADDING, 'minLevel':MIN_LEVEL, 'maxLevel':MAX_LEVEL,
                        'font_fallback':result.get('font_fallback', False)}
        self.current_revision = revision
        self.retry_after = 0
        self.cache.clear()
        self.cache_size = 0
        return self.current

    def manifest(self):
        if self.current is not None and self.current_revision == self.revision():
            return self.current
        if not self.lock.acquire(blocking=False):
            raise TilesPending('Renderer is busy')
        try:
            return self._manifest()
        finally:
            self.lock.release()

    def tile(self, etag, level, col, row):
        if (any(type(n) is not int for n in (level,col,row)) or not MIN_LEVEL <= level <= MAX_LEVEL
                or min(col,row) < 0):
            raise InvalidTile('Invalid tile coordinates')
        if not self.lock.acquire(timeout=.5):
            raise TilesPending('Renderer is busy')
        try:
            if self.current is None or etag != self.current['etag'] or self.revision() != self.current_revision:
                raise StaleRevision('Document changed')
            bounds = self.current['bounds']
            scale = 2 ** level
            edge = TILE_PIXELS/scale
            if col*edge >= bounds['width'] or row*edge >= bounds['height']:
                raise InvalidTile('Tile is outside the canvas')
            key = level,col,row
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            padding = TILE_PADDING/scale
            region = [bounds['x']+col*edge-padding, bounds['y']+row*edge-padding, edge+2*padding, edge+2*padding]
            request = RevisionRequest(self, self.current_revision)
            if monotonic() < self.retry_after:
                raise ValueError('Native viewport renderer waiting to retry')
            try:
                result = self.session.render(self.current_revision, region, scale, request=request)
            except (OSError, ValueError):
                if request.is_set():
                    raise StaleRevision('Document changed during render') from None
                self.retry_after = monotonic() + 2
                raise
            if request.is_set():
                raise StaleRevision('Document changed during render')
            size = len(result[1])
            if size <= self.cache_bytes:
                self.cache[key] = result
                self.cache_size += size
                while self.cache_size > self.cache_bytes or len(self.cache) > 128:
                    _, old = self.cache.popitem(last=False)
                    self.cache_size -= len(old[1])
            return result
        finally:
            self.lock.release()

    def close(self):
        self.cancelled.set()
        self.session.close()
