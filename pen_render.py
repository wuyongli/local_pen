"""Render saved .pen documents with the standalone Pen CLI export command."""

import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
from collections import OrderedDict, deque
from contextlib import nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit
from xml.etree import ElementTree

from edit_pen_text import load_pen, walk_layers, write_pen_copy
from pen_design import absolute_path, layer_index, number, select
from pen_font_library import font_cache_directory, font_cache_limit


PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
MAX_BATCH_FRAMES = 20
EXPORT_PIXEL_LIMIT = 24_000_000
EXPORT_BYTE_LIMIT = 64_000_000
PREVIEW_PIXEL_LIMIT = 4_000_000
PREVIEW_BYTE_LIMIT = 3_000_000
CANVAS_PIXEL_LIMIT = EXPORT_PIXEL_LIMIT
CANVAS_BYTE_LIMIT = 20_000_000
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
MAX_PNG_EDGE = 8192
PREVIEW_CACHE_SIZE = 8
PREVIEW_CACHE_AGE = 300
PREVIEW_CACHE = OrderedDict()
FONT_CACHE_PRELOAD = Path(__file__).with_name("pen_font_cache.cjs").resolve()
MACOS_NETWORK_ISOLATION = "/usr/bin/sandbox-exec"
LOCAL_PREVIEW_TIMEOUT = 7
LOCAL_CANVAS_TIMEOUT = 15


class CanvasTooLarge(ValueError):
    pass


def renderer_command():
    cli = os.environ.get("LOCAL_PEN_CLI") or shutil.which("pen")
    if not cli:
        raise ValueError("Pen CLI is unavailable. Set LOCAL_PEN_CLI to the installed pen command.")
    node = os.environ.get("LOCAL_PEN_NODE")
    return [node, cli] if node else [cli]




def renderer_environment():
    if not FONT_CACHE_PRELOAD.is_file():
        return None
    try:
        directory = font_cache_directory()
    except OSError:
        return None
    env = os.environ.copy()
    option = f"--require {json.dumps(str(FONT_CACHE_PRELOAD), ensure_ascii=False)}"
    env["NODE_OPTIONS"] = f"{env.get('NODE_OPTIONS', '')} {option}".strip()
    env["LOCAL_PEN_FONT_CACHE_DIR"] = directory
    env["LOCAL_PEN_FONT_CACHE_MAX_BYTES"] = str(font_cache_limit())
    env["LOCAL_PEN_CACHE_PYTHON"] = sys.executable
    return env


def component_refs(layer):
    """Follow regular layers and replacement layers inside instance overrides."""
    pending = [layer]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        if node.get("type") == "ref" and isinstance(node.get("ref"), str):
            yield node["ref"]
        if isinstance(node.get("children"), list):
            pending.extend(node["children"])
        if isinstance(node.get("descendants"), dict):
            pending.extend(node["descendants"].values())


def stage_frame(layer, index):
    frame = {**layer, "x": 0, "y": 0, "clip": True}
    included = {node["id"] for node, _ in walk_layers([layer])}
    pending = deque(component_refs(layer))
    dependencies = []
    while pending:
        layer_id = pending.popleft()
        if layer_id in included or layer_id not in index:
            continue
        definition, _ = index[layer_id]
        subtree = {node["id"] for node, _ in walk_layers([definition])}
        if layer["id"] in subtree:
            raise ValueError("selected frame references an enclosing component")
        # A newly discovered parent can already contain an earlier dependency.
        dependencies = [node for node in dependencies if node["id"] not in subtree]
        dependencies.append(definition)
        included.update(subtree)
        pending.extend(component_refs(definition))

    if dependencies:
        # Keep native instance semantics while excluding definitions from layout and export bounds.
        hidden_id = "local-pen-preview-components"
        while hidden_id in index:
            hidden_id += "-"
        hidden = {
            "id": hidden_id, "type": "frame", "enabled": False, "layoutPosition": "absolute",
            "x": 0, "y": 0, "width": 1, "height": 1, "layout": "none", "clip": True,
            "children": dependencies,
        }
        frame["children"] = [*frame.get("children", []), hidden]
    return frame


def check_cancelled(request):
    if request is not None and request.cancelled.is_set():
        raise ValueError("render request cancelled")


def run_renderer(command, timeout, request, *, env=None):
    check_cancelled(request)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"),
                          env=renderer_environment() if env is None else env) as process:
        deadline = monotonic() + timeout
        try:
            while True:
                check_cancelled(request)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ValueError("Pen CLI timed out during PNG export")
                try:
                    return process.wait(timeout=min(0.15, remaining) if request else remaining)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass


def render_png(file_path, layer_id, scale, pixel_limit, byte_limit, request=None, *, prefer_local=False):
    source = absolute_path(file_path).resolve()
    document = load_pen(source)
    index = layer_index(document["children"])
    return render_frame_png(source, document, index, layer_id, scale, pixel_limit, byte_limit,
                            request=request, prefer_local=prefer_local)


def validate_render_scale(scale):
    if type(scale) is not int or scale not in (1, 2, 3):
        raise ValueError("scale must be 1, 2 or 3")


def visible_canvas_layers(layers):
    return [layer for layer in layers if isinstance(layer, dict) and layer.get("enabled") is not False]


def artboard_titles(layers, scale):
    def position(layer, axis):
        value = layer.get(axis)
        return value if number(value) else 0

    visible = visible_canvas_layers(layers)
    origin_x = min(position(layer, "x") for layer in visible)
    origin_y = min(position(layer, "y") for layer in visible)
    return [{"id": layer["id"], "name": layer["name"],
             "x": (position(layer, "x") - origin_x) * scale,
             "y": (position(layer, "y") - origin_y) * scale,
             **{axis: layer[axis] * scale for axis in ("width", "height")
                if number(layer.get(axis)) and layer[axis] > 0}}
            for layer in visible if layer.get("type") == "frame" and isinstance(layer.get("id"), str)
            and isinstance(layer.get("name"), str) and layer["name"].strip()]


def canvas_scale(layers, requested):
    # Estimate from the top-level coordinates. Pen resolves auto layout and the final bounds.
    visible = visible_canvas_layers(layers)
    if not visible:
        raise ValueError("the canvas has no visible layers")

    def extent(axis, dimension):
        starts, ends = [], []
        for layer in visible:
            start, size = layer.get(axis), layer.get(dimension)
            start = start if type(start) in (int, float) else 0
            size = size if type(size) in (int, float) and size > 0 else 0
            starts.append(start)
            ends.append(start + size)
        return max(1, max(ends) - min(starts))

    width, height = extent("x", "width"), extent("y", "height")
    limit = min((MAX_PNG_EDGE - 1) / max(width, height),
                math.sqrt(CANVAS_PIXEL_LIMIT * 0.99) / math.sqrt(width) / math.sqrt(height))
    return min(requested, limit)


def render_canvas_png(file_path, scale, request=None):
    validate_render_scale(scale)
    check_cancelled(request)
    source = absolute_path(file_path).resolve()
    document = load_pen(source)
    scale = canvas_scale(document["children"], scale)
    for _ in range(3):
        check_cancelled(request)
        try:
            metadata, data = render_staged_png(source, document, None, scale, CANVAS_PIXEL_LIMIT, CANVAS_BYTE_LIMIT,
                                               45, request, prefer_local=True)
            return {**metadata, "artboards": artboard_titles(document["children"], scale)}, data
        except CanvasTooLarge:
            scale /= 2
    raise ValueError("the full canvas exceeds the preview size limit")


def renderable_frame(index, layer_id, scale, pixel_limit):
    validate_render_scale(scale)
    layer, _ = select(index, layer_id)
    if layer["type"] != "frame":
        raise ValueError("select a frame ID to render")
    width, height = layer.get("width"), layer.get("height")
    if not all(type(value) in (int, float) and 0 < value <= MAX_PNG_EDGE / scale for value in (width, height)):
        raise ValueError("the frame must have fixed positive dimensions within 8192 pixels at this scale")
    if width * height * scale ** 2 > pixel_limit:
        raise ValueError("frame too large at this scale")
    return layer


def render_canvas_svg(file_path, request=None):
    check_cancelled(request)
    source = absolute_path(file_path).resolve()
    document = load_pen(source)
    metadata, data = render_staged_image(source, document, None, 1, CANVAS_PIXEL_LIMIT, CANVAS_BYTE_LIMIT,
                                         45, request, prefer_local=True, image_format="svg")
    return {**metadata, "artboards": artboard_titles(document["children"], 1)}, data


def render_staged_png(source, staged_document, layer, scale, pixel_limit, byte_limit, timeout, request,
                      *, prefer_local=False):
    return render_staged_image(source, staged_document, layer, scale, pixel_limit, byte_limit, timeout, request,
                               prefer_local=prefer_local)


def render_staged_image(source, staged_document, layer, scale, pixel_limit, byte_limit, timeout, request,
                        *, prefer_local=False, image_format="png"):
    reader = read_rendered_svg if image_format == "svg" else read_rendered_png
    # Stage beside the source so relative assets and library imports still resolve.
    with NamedTemporaryFile(dir=source.parent, prefix=".local-pen-render-", delete=True) as marker:
        staged = Path(marker.name + ".pen")
        write_pen_copy(staged_document, staged)
        try:
            with TemporaryDirectory(prefix="local-pen-png-") as temporary:
                output = Path(temporary) / f"render.{image_format}"
                command = renderer_command() + [
                    "--in", str(staged), "--export", str(output),
                    "--export-type", image_format, "--export-scale", str(scale),
                ]
                images = set()
                if prefer_local:
                    local, images = preview_dependencies(staged_document)
                    if local and all((source.parent / image).is_file() for image in images):
                        result = render_local_preview(command, output, layer, scale, pixel_limit, byte_limit, request, reader)
                        if result is not None:
                            return result
                if image_format == "svg":
                    raise ValueError("an isolated SVG export is unavailable")
                missing = output.with_suffix(".missing")
                env = renderer_environment() if prefer_local else None
                if env is not None:
                    missing.unlink(missing_ok=True)
                    env["LOCAL_PEN_MISSING_RESOURCES"] = str(missing)
                    env["LOCAL_PEN_PREVIEW_IMAGES"] = json.dumps(remote_asset_keys(images))
                    env.pop("LOCAL_PEN_OFFLINE", None)
                    returncode = run_renderer(command, timeout, request, env=env)
                else:
                    returncode = run_renderer(command, timeout, request)
                check_cancelled(request)
                if prefer_local and missing.exists():
                    raise ValueError("Some preview resources could not be loaded")
                if returncode or not output.is_file():
                    raise ValueError("Pen CLI did not produce a PNG. Check CLI login and the design's renderability.")
                return read_rendered_png(output, layer, scale, pixel_limit, byte_limit)
        finally:
            staged.unlink()


def render_local_preview(command, output, layer, scale, pixel_limit, byte_limit, request, reader):
    if sys.platform != "darwin" or not os.access(MACOS_NETWORK_ISOLATION, os.X_OK):
        return None
    env = renderer_environment()
    if env is None:
        return None
    missing = output.with_suffix(".missing")
    env["LOCAL_PEN_MISSING_RESOURCES"] = str(missing)
    env["LOCAL_PEN_OFFLINE"] = "1"
    isolated = [MACOS_NETWORK_ISOLATION, "-p", "(version 1) (allow default) (deny network*)", *command]
    try:
        timeout = LOCAL_CANVAS_TIMEOUT if layer is None else LOCAL_PREVIEW_TIMEOUT
        code = run_renderer(isolated, timeout, request, env=env)
        check_cancelled(request)
        if not code:
            if not missing.exists():
                return reader(output, layer, scale, pixel_limit, byte_limit)
            if layer is None and set(missing.read_text().splitlines()) == {"font"}:
                metadata, data = reader(output, layer, scale, pixel_limit, byte_limit)
                return {**metadata, "font_fallback": True}, data
    except CanvasTooLarge:
        raise
    except (OSError, ValueError):
        check_cancelled(request)
    output.unlink(missing_ok=True)
    return None


def read_rendered_png(output, layer, scale, pixel_limit, byte_limit):
    if output.stat().st_size > byte_limit:
        if layer is None:
            raise CanvasTooLarge("canvas PNG exceeds the byte limit")
        raise ValueError("PNG is too large at this scale. Try a lower scale.")
    data = output.read_bytes()
    if len(data) < 45 or not data.startswith(PNG_HEADER) or not data.endswith(PNG_END):
        raise ValueError("Pen CLI produced an invalid PNG")
    width, height = struct.unpack(">II", data[16:24])
    if layer is not None:
        expected = tuple(math.ceil(layer[axis] * scale) for axis in ("width", "height"))
        if (width, height) != expected:
            raise ValueError(f"PNG dimensions {width}×{height} do not match the frame's {expected[0]}×{expected[1]}")
    if layer is None and (max(width, height) > MAX_PNG_EDGE or width * height > pixel_limit):
        raise CanvasTooLarge("canvas PNG exceeds the dimension limit")
    if not (0 < width <= MAX_PNG_EDGE and 0 < height <= MAX_PNG_EDGE and width * height <= pixel_limit):
        raise ValueError("PNG dimensions are invalid or exceed the size limit")
    return {"layer_id": layer["id"] if layer else None, "width": width, "height": height, "scale": scale}, data


def read_rendered_svg(output, _layer, scale, _pixel_limit, byte_limit):
    if output.stat().st_size > byte_limit:
        raise ValueError("SVG exceeds the preview size limit")
    data = output.read_bytes()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("SVG contains an unsupported document declaration")
    try:
        root = ElementTree.fromstring(data)
        x, y, width, height = map(float, root.attrib["viewBox"].split())
    except (ElementTree.ParseError, KeyError, ValueError) as error:
        raise ValueError("Pen CLI produced an invalid SVG") from error
    if root.tag != SVG_NAMESPACE + "svg" or (x, y) != (0, 0) or not all(0 < v <= 100_000 for v in (width, height)):
        raise ValueError("SVG has unsupported bounds")
    for node in root.iter():
        if node.tag in {SVG_NAMESPACE + "script", SVG_NAMESPACE + "foreignObject"}:
            raise ValueError("SVG contains an unsupported element")
        for attr in ("href", "{http://www.w3.org/1999/xlink}href"):
            uri = node.get(attr)
            if uri and not uri.startswith(("#", "data:image/")):
                raise ValueError("SVG contains an external reference")
    return {"width": width, "height": height, "scale": scale}, data


def render_frame_png(source, document, index, layer_id, scale, pixel_limit, byte_limit, timeout=45, request=None,
                     *, prefer_local=False):
    check_cancelled(request)
    layer = renderable_frame(index, layer_id, scale, pixel_limit)
    staged_document = {**document, "children": [stage_frame(layer, index)]}
    return render_staged_png(source, staged_document, layer, scale, pixel_limit, byte_limit, timeout, request,
                             prefer_local=prefer_local)


def preview_dependencies(document):
    # Imports and dynamic sources may change outside the staged frame's JSON.
    imports = document.get("imports")
    local = not imports
    pending = list(document["children"])
    images = {uri for uri in imports.values() if isinstance(uri, str)} if isinstance(imports, dict) else set()
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind in ("browser", "script", "shader", "code"):
                local = False
            if kind == "image":
                url = item.get("url")
                if not isinstance(url, str) or not url or url.startswith("$") or ":" in url:
                    local = False
                if isinstance(url, str) and url:
                    images.add(url)
            pending.extend(item.values())
    return local, images


def remote_asset_keys(images):
    keys = set()
    for url in images:
        if not url.lower().startswith(("https://", "http://")):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.hostname:
                address = parsed.hostname + (parsed.path or "/")
                keys.add(hashlib.sha256(address.encode()).hexdigest())
        except ValueError:
            continue
    return sorted(keys)


def preview_cache_key(source, document, scale):
    local, images = preview_dependencies(document)
    if not local:
        return None
    digest = hashlib.sha256(json.dumps(document, ensure_ascii=False, allow_nan=False).encode())
    remaining = 16_000_000
    try:
        for url in sorted(images):
            image = source.parent / url
            if not image.is_file() or image.stat().st_size > remaining:
                return None
            with image.open("rb") as asset:
                data = asset.read(remaining + 1)
            remaining -= len(data)
            if remaining < 0:
                return None
            digest.update(hashlib.sha256(data).digest())
    except OSError:
        return None
    return source.parent, scale, digest.digest()


def preview_png(file_path, layer_id, scale=1, refresh=False, *, request=None):
    if type(refresh) is not bool:
        raise ValueError("refresh must be a boolean")
    check_cancelled(request)
    source = absolute_path(file_path).resolve()
    document = load_pen(source)
    index = layer_index(document["children"])
    layer = renderable_frame(index, layer_id, scale, PREVIEW_PIXEL_LIMIT)
    staged = {**document, "children": [stage_frame(layer, index)]}
    key = preview_cache_key(source, staged, scale)
    saved = PREVIEW_CACHE.pop(key, None)
    if not refresh and saved is not None and monotonic() - saved[0] < PREVIEW_CACHE_AGE:
        PREVIEW_CACHE[key] = saved
        _, metadata, data = saved
        return {**metadata, "cached": True}, data

    metadata, data = render_staged_png(source, staged, layer, scale, PREVIEW_PIXEL_LIMIT, PREVIEW_BYTE_LIMIT, 45, request,
                                      prefer_local=True)
    check_cancelled(request)
    if key is not None and key == preview_cache_key(source, staged, scale):
        PREVIEW_CACHE[key] = (monotonic(), metadata, data)
        if len(PREVIEW_CACHE) > PREVIEW_CACHE_SIZE:
            PREVIEW_CACHE.popitem(last=False)
    return {**metadata, "cached": False}, data


def export_png(file_path, layer_id, output_path, scale=2, *, request=None):
    destination = absolute_path(output_path)
    if destination.suffix.lower() != ".png" or not destination.parent.is_dir():
        raise ValueError("provide a .png destination in an existing directory")
    if os.path.lexists(destination):
        raise ValueError(f"destination already exists: {destination}")
    metadata, data = render_png(file_path, layer_id, scale, EXPORT_PIXEL_LIMIT, EXPORT_BYTE_LIMIT, request=request)
    with NamedTemporaryFile(dir=destination.parent) as temporary:
        temporary.write(data)
        temporary.flush()
        with request.publish() if request else nullcontext():
            try:
                destination.hardlink_to(temporary.name)
            except FileExistsError:
                raise ValueError(f"destination already exists: {destination}") from None
    return {**metadata, "output_path": str(destination)}


def export_png_batch(file_path, layer_ids, output_dir, scale=2, *, request=None):
    if not isinstance(layer_ids, list) or not 1 <= len(layer_ids) <= MAX_BATCH_FRAMES:
        raise ValueError(f"provide 1–{MAX_BATCH_FRAMES} frame IDs")
    if not all(isinstance(layer_id, str) and layer_id for layer_id in layer_ids):
        raise ValueError("frame IDs must be nonempty strings")
    if len(set(layer_ids)) != len(layer_ids):
        raise ValueError("duplicate frame ID")
    destination = absolute_path(output_dir)
    if os.path.lexists(destination):
        raise ValueError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise ValueError("the output directory must have an existing parent directory")

    source = absolute_path(file_path).resolve()
    document = load_pen(source)
    index = layer_index(document["children"])
    frames = []
    for position, layer_id in enumerate(layer_ids, 1):
        try:
            frame = renderable_frame(index, layer_id, scale, EXPORT_PIXEL_LIMIT)
        except ValueError as error:
            raise ValueError(f"frame {layer_id!r}: {error}") from error
        name = frame.get("name")
        if not isinstance(name, str) or not name:
            name = layer_id
        stem = re.sub(r"[^\w.-]+", "-", name)[:60].strip(".-_") or "frame"
        frames.append((layer_id, name, f"{position:02}-{stem}.png"))

    # Keep the total runtime below the configured Codex tool timeout (960 seconds).
    deadline = monotonic() + 900
    with TemporaryDirectory(dir=destination.parent, prefix=".local-pen-batch-") as temporary:
        staging = Path(temporary)
        exports = []
        for layer_id, name, filename in frames:
            try:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ValueError("batch export timed out; try fewer frames per batch")
                metadata, data = render_frame_png(
                    source, document, index, layer_id, scale, EXPORT_PIXEL_LIMIT, EXPORT_BYTE_LIMIT,
                    timeout=min(90, remaining), request=request,
                )
                (staging / filename).write_bytes(data)
            except (OSError, ValueError) as error:
                raise ValueError(f"frame {layer_id!r} ({name}): {error}") from error
            exports.append({**metadata, "name": name, "output_path": str(destination / filename)})
            if request:
                request.progress(len(exports), len(frames), name)

        # Claim a fresh destination without overwriting one created by another export.
        with request.publish() if request else nullcontext():
            destination.mkdir()
            published = []
            try:
                for _, _, filename in frames:
                    target = destination / filename
                    target.hardlink_to(staging / filename)
                    published.append(target)
            except OSError:
                for target in published:
                    target.unlink()
                try:
                    destination.rmdir()
                except OSError:
                    # Preserve files created by another process in the meantime.
                    pass
                raise
    return {"output_dir": str(destination), "exports": exports}
