"""Serve file-based .pen tools over MCP stdio using Python 3.10+."""

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock

from edit_pen_text import load_pen, walk_layers, write_pen_copy
from pen_design import MAX_LAYER_PAGE, absolute_path, edit_design, inspect_layers, layer_page_end
from pen_live_preview import LivePreview
from pen_render import MAX_BATCH_FRAMES, export_png, export_png_batch, preview_png
from pen_working_copy import (
    edit_and_reopen, edit_temporary_design, load_task_design, save_selected_design, task_design,
    task_design_for_path, task_directory,
)


FILE_PATH = {"type": "string", "description": "Absolute path to a saved local .pen JSON file."}
TASK_DIR = {"type": "string", "description": "Absolute task directory. Defaults to the MCP server's working directory."}
REOPEN = {
    "type": "boolean",
    "description": "Omit for routine edits. Reopens Pen by default unless a live preview is active.",
}
EDIT_OUTPUT_PATH = {
    "type": ["string", "null"],
    "description": "Omit or use null to update the selected design in place. Supply a new path only for a requested separate file.",
}
SEPARATE_OUTPUT = {
    "type": "boolean", "default": False,
    "description": "Allow a new persistent .pen file when a task design is selected. Use only if the user explicitly asks to save a separate file.",
}
RENDER_PROPERTIES = {
    "file_path": FILE_PATH,
    "task_dir": TASK_DIR,
    "layer_id": {"type": "string", "description": "ID of a fixed-size frame to render."},
    "scale": {"type": "integer", "enum": [1, 2, 3]},
}
LAYER_PAGING = {
    "limit": {"type": "integer", "minimum": 1, "default": 30,
              "description": f"Requested page size. The server returns at most {MAX_LAYER_PAGE} layers per call."},
    "offset": {"type": "integer", "minimum": 0, "default": 0,
               "description": "For the next page, use the previous response's next_offset. Stop when it is null."},
}
TOOLS = [
    {
        "name": "live_preview",
        "description": (
            "Start, show status, or stop the local browser live preview for the selected task design. "
            "Start only when requested and give the user the fixed per-task URL to open themselves. "
            "While active, the existing tab refreshes after each save. Do not open or refresh it with browser tools "
            "unless the user explicitly requests browser interaction. "
            "The preview shows the entire canvas, including all visible artboards in their original layout."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"enum": ["start", "status", "stop"], "default": "start"},
                "task_dir": TASK_DIR,
                "scale": RENDER_PROPERTIES["scale"],
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "task_design",
        "description": (
            "Get the task's selected design, or remember the .pen file the user chose for this task. "
            "Select it once, then inspect, edit and preview without repeating its path or task directory in this session. "
            "The file must be inside the task directory. For multiple tasks in one workspace, pass each task's own directory. "
            "Only provide file_path when the user chooses the task's design."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_dir": TASK_DIR, "file_path": FILE_PATH},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "find_layers",
            "description": "Find saved .pen layers by exact ID or partial name or plain text. Omit file_path to use the task design and query to list layers in tree order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": FILE_PATH,
                "task_dir": TASK_DIR,
                "query": {"type": "string", "default": ""},
                **LAYER_PAGING,
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "update_text",
        "description": (
            "Replace the plain text of one layer by exact ID. Omit file_path and output_path to update "
            "the selected task design using the current display mode. Use separate_output=true "
            "with a new output_path only when the user asks to save a separate file. "
            "Local resource paths are adjusted when saving to another directory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": FILE_PATH,
                "task_dir": TASK_DIR,
                "layer_id": {"type": "string", "minLength": 1},
                "text": {"type": "string"},
                "output_path": EDIT_OUTPUT_PATH,
                "reopen": REOPEN,
                "separate_output": SEPARATE_OUTPUT,
            },
            "required": ["layer_id", "text"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "inspect_layers",
        "description": (
            "Inspect a saved .pen layer and its subtree, or list top-level layers with their properties. "
            "Omit file_path to use the selected task design. "
            "Returns stored layout and style properties, not rendered bounds. Use depth 0 for just the selected "
            "layer or top level. Use next_offset to fetch further results."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": FILE_PATH,
                "task_dir": TASK_DIR,
                "layer_id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 0, "maximum": 8, "default": 1},
                **LAYER_PAGING,
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "edit_design",
        "description": (
            "Draw new frames or edit existing frames in the selected .pen file. Omit file_path and output_path "
            "to save in place using the current display mode. During live preview, save the edit and let the user's existing tab "
            "refresh automatically. For a comparison on the canvas, add editable frames in the same file. "
            "To start a separate file, provide a new output_path and set separate_output=true when a task design is selected. "
            "For an internal draft or if the user only asks for a preview, set temporary=true and omit output_path. "
            "The returned temporary path can be used with inspect_layers, edit_design and preview_png until this MCP server exits. "
            "Use a persistent output_path only for an explicitly requested deliverable. "
            "Local resource paths are adjusted when saving to another directory. "
            "add: layer (type and native .pen properties, optional id and children), optional parent_id and index. "
            "update: id and properties. For a text change use properties: {\"content\": \"New label\"}; "
            "frame layout properties include gap and padding. "
            "Add reusable: true to a new component definition, then add a ref instance pointing to it. "
            "override: instance id, descendant ID path, and properties to merge into the instance override. "
            "duplicate: id, optional new_id, parent_id, index and root properties. "
            "move: id, optional parent_id and index. remove: id. "
            "Use parent_id null for the document root. An omitted parent_id for move or duplicate uses the "
            "existing parent. Indices are zero-based, after removal for moves. "
            "Any invalid operation aborts without creating the output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {**FILE_PATH, "type": ["string", "null"]},
                "task_dir": TASK_DIR,
                "output_path": EDIT_OUTPUT_PATH,
                "reopen": REOPEN,
                "separate_output": SEPARATE_OUTPUT,
                "temporary": {"type": "boolean", "default": False,
                              "description": "Save a disposable draft outside the task folder and clean it up on server exit."},
                "operations": {
                    "type": "array", "minItems": 1, "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"enum": ["add", "update", "override", "duplicate", "move", "remove"]},
                            "id": {"type": "string"},
                            "new_id": {"type": "string"},
                            "path": {"type": "string", "description": "Descendant ID or nested instance/descendant path for override."},
                            "parent_id": {"type": ["string", "null"]},
                            "index": {"type": "integer", "minimum": 0},
                            "layer": {"type": "object"},
                            "properties": {"type": "object"},
                        },
                        "required": ["op"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "preview_png",
        "description": "Render a fixed-size frame as an inline PNG, including same-document shared components. Omit file_path to preview the selected task design. Defaults to 1×. Set refresh=true to force a render.",
        "inputSchema": {
            "type": "object", "properties": {**RENDER_PROPERTIES, "refresh": {"type": "boolean", "default": False}},
            "required": ["layer_id"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "export_png",
        "description": "Export one fixed-size frame from a saved .pen to a new local PNG file, including shared components from the same document. Defaults to 2×. Uses the locally installed Pen CLI export command.",
        "inputSchema": {
            "type": "object", "properties": {
                **RENDER_PROPERTIES,
                "output_path": {"type": "string", "description": "Absolute path to a new PNG in an existing directory."},
            },
            "required": ["layer_id", "output_path"], "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "export_png_batch",
        "description": (
            "Export multiple fixed-size frames from one saved .pen document as separate PNGs. "
            "Preserves same-document shared components. Files are numbered in request order. "
            "Provide a new output directory with an existing parent. Defaults to 2×. "
            "If any render fails, no PNGs are published. Split large or slow jobs into smaller batches."
        ),
        "inputSchema": {
            "type": "object", "properties": {
                "file_path": FILE_PATH,
                "task_dir": TASK_DIR,
                "layer_ids": {"type": "array", "items": {"type": "string", "minLength": 1},
                              "minItems": 1, "maxItems": MAX_BATCH_FRAMES, "uniqueItems": True},
                "output_dir": {"type": "string", "description": "Absolute path to a new directory."},
                "scale": {**RENDER_PROPERTIES["scale"], "default": 2},
            },
            "required": ["layer_ids", "output_dir"], "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False, "openWorldHint": False},
    },
]


def find_layers(file_path, query="", limit=30, offset=0):
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    end = layer_page_end(limit, offset)

    document = load_pen(absolute_path(file_path))
    needle = query.casefold()
    matches = []
    for layer, parent_id in walk_layers(document["children"]):
        layer_id, name, content = (layer.get(key) for key in ("id", "name", "content"))
        if needle and layer_id != query and not any(
            isinstance(value, str) and needle in value.casefold() for value in (name, content)
        ):
            continue
        entry = {"id": layer_id, "type": layer.get("type"), "name": name, "parent_id": parent_id}
        if isinstance(content, str):
            entry["text"] = content[:300]
            if len(content) > 300:
                entry["text_truncated"] = True
        matches.append(entry)

    return {
        "version": document["version"], "total": len(matches), "layers": matches[offset:end],
        "next_offset": end if end < len(matches) else None,
    }


def update_text(file_path, layer_id, text, output_path):
    if not isinstance(layer_id, str) or not layer_id or not isinstance(text, str):
        raise ValueError("provide a nonempty layer_id and a string for text")
    source = absolute_path(file_path)
    destination = absolute_path(output_path)
    document = load_pen(source)
    matches = [layer for layer, _ in walk_layers(document["children"]) if layer.get("id") == layer_id]
    if len(matches) != 1:
        raise ValueError(f"expected one layer with ID {layer_id!r}, found {len(matches)}")

    layer = matches[0]
    old_text = layer.get("content")
    if layer.get("type") != "text" or not isinstance(old_text, str) or old_text.startswith("$"):
        raise ValueError("layer must contain plain text rather than a variable or structured text")
    if text.startswith("$"):
        raise ValueError("new text must not start with the variable prefix '$'")
    if old_text == text:
        raise ValueError("new text must differ from the layer's current text")

    layer["content"] = text
    write_pen_copy(document, destination, source)
    return {"output_path": str(destination), "version": document["version"], "layer_id": layer_id}


RENDER_TOOLS = {"preview_png": preview_png, "export_png": export_png, "export_png_batch": export_png_batch}


def call_tool(params, request=None, *, live=None):
    try:
        handler = {
            "find_layers": find_layers, "update_text": update_text,
            "inspect_layers": inspect_layers, "edit_design": edit_design, "task_design": task_design,
            **({"live_preview": live.command} if live is not None else {}),
            **RENDER_TOOLS,
        }.get(params.get("name"))
        if handler is None:
            raise ValueError("unknown tool")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict) or "request" in arguments:
            raise ValueError("invalid tool arguments")
        if handler is task_design or params["name"] == "live_preview":
            output = handler(**arguments)
        else:
            arguments = dict(arguments)
            task_dir = arguments.pop("task_dir", None)
            editing = handler in (update_text, edit_design)
            temporary = arguments.pop("temporary", False) if handler is edit_design else False
            if type(temporary) is not bool:
                raise ValueError("temporary must be a boolean")
            separate = arguments.pop("separate_output", False) if editing else False
            if type(separate) is not bool:
                raise ValueError("separate_output must be a boolean")
            if editing and arguments.get("output_path") is None:
                arguments.pop("output_path", None)
            has_output = "output_path" in arguments
            target = None
            if (editing and not temporary) or "file_path" not in arguments:
                target = load_task_design(task_directory(task_dir))
            if editing and not temporary and target is None:
                target = task_design_for_path(arguments.get("file_path")) or task_design_for_path(arguments.get("output_path"))
            same_output = editing and not temporary and target is not None and has_output and (
                absolute_path(arguments["output_path"]).resolve() == target
            )
            in_place = editing and not temporary and (not has_output or same_output)
            background = in_place and live is not None and live.window is not None
            reopen = arguments.pop("reopen", in_place and not background) if editing else False
            if type(reopen) is not bool:
                raise ValueError("reopen must be a boolean")
            if background and (target != live.window.source or reopen):
                raise ValueError("live preview is active: edit the previewed task without reopening Pen, "
                                 "start a preview for the selected task, or stop live preview first")
            if editing and not temporary and not (reopen or background) and not has_output:
                raise ValueError("omit reopen and output_path to edit the selected Pen design in place")
            if temporary and (reopen or has_output):
                raise ValueError("temporary drafts cannot specify output_path or request reopen")
            if separate and (reopen or temporary or not has_output):
                raise ValueError("separate_output requires a new output_path and no reopen or temporary flag")
            if editing and target is not None and has_output and not (reopen or background or separate):
                raise ValueError("edit the selected Pen design in place by omitting output_path; use separate_output only if the user asked for a new file")
            if (reopen or background) and arguments.get("file_path") is None:
                arguments.pop("file_path", None)
            # A provided destination with no source still creates a blank document.
            needs_source = "file_path" not in arguments and (reopen or background or temporary or handler is not edit_design)
            if needs_source and target is not None:
                arguments["file_path"] = str(target)
            if (reopen or background) and target is not None:
                arguments.setdefault("output_path", str(target))
            if target is None and (needs_source or (reopen and not has_output)):
                raise ValueError("select this task's design with task_design or provide explicit file paths")
            if background:
                output = save_selected_design(handler, arguments, target)
            elif reopen:
                output = edit_and_reopen(handler, arguments, task_file_path=target)
            elif temporary:
                output = edit_temporary_design(handler, arguments)
            else:
                output = handler(**arguments, request=request) if request is not None else handler(**arguments)
    except (OSError, TypeError, ValueError, RecursionError) as error:
        return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    image = None
    if handler is preview_png:
        output, image = output
    content = [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}]
    if image is not None:
        content.append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(image).decode("ascii")})
    return {"structuredContent": output, "content": content}


def error(code, message):
    return {"error": {"code": code, "message": message}}


def dispatch(request, *, live=None):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return error(-32600, "Invalid request")
    if "id" not in request:
        return None

    method = request["method"]
    params = request.get("params", {})
    if not isinstance(params, dict):
        return error(-32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        return {"result": {
            "protocolVersion": version if version in supported else supported[-1],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "local_pen", "version": "1.17.1"},
            "instructions": (
                "For Pen design work, use task_design to get the designated document. Only select a new "
                "document when the user designates it. The last task confirmed with task_design is used "
                "for subsequent path-free calls in that session. For routine requests to draw, update, or show a design, make editable "
                "changes in the selected Pen document by omitting file_path and output_path. Draw comparison "
                "frames and interaction states on that same canvas. When the user requests live preview, call live_preview "
                "and give the user its URL to open themselves. For routine edits during live preview, omit reopen and let the existing tab "
                "refresh automatically. Do not open, reload, or capture that tab with browser tools unless the user requests browser interaction. "
                "Use preview_png when you need to visually check the design. Report background saving only when display is live_preview. "
                "Otherwise verify reopen.ok. Never force a window or desktop switch as a workaround. "
                "Use temporary=true for internal trials or explicitly preview-only requests. A PNG preview is for checking "
                "the design, not a substitute for applying it to the canvas. Only save separate files or export images "
                "when the user asks for them. If in-place editing or reopening fails, resolve the reported issue or tell "
                "the user. Do not substitute a separate draft, an export, or a manual file replacement."
            ),
        }}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"result": call_tool(params, live=live)}
    return error(-32601, "Method not found")


class RenderRequest:
    def __init__(self, progress_token, send):
        self.cancelled = Event()
        self.lock = Lock()
        self.committed = False
        self.progress_token = progress_token
        self.send = send

    def cancel(self):
        with self.lock:
            if not self.committed:
                self.cancelled.set()

    @contextmanager
    def publish(self):
        with self.lock:
            if self.cancelled.is_set():
                raise ValueError("render request cancelled")
            yield
            self.committed = True

    def progress(self, completed, total, name):
        with self.lock:
            if self.progress_token is not None and not self.cancelled.is_set():
                self.send({"method": "notifications/progress", "params": {
                    "progressToken": self.progress_token, "progress": completed, "total": total,
                    "message": f"Rendered {name}",
                }})


def main():
    output_lock = Lock()
    active_lock = Lock()
    active = {}
    session_task_dir = None

    def send(message):
        with output_lock:
            print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)

    def render(request_id, params, state):
        try:
            if not state.cancelled.is_set():
                result = call_tool(params, state)
                with state.lock:
                    if not state.cancelled.is_set():
                        send({"id": request_id, "result": result})
                        state.committed = True
        finally:
            with active_lock:
                active.pop(request_id, None)

    with LivePreview() as live, ThreadPoolExecutor(max_workers=1) as executor:
        for line in sys.stdin.buffer:
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                send({"id": None, **error(-32700, "Parse error")})
                continue

            if isinstance(request, dict) and request.get("jsonrpc") == "2.0":
                method = request.get("method")
                request_id = request.get("id")
                params = request.get("params")
                if isinstance(params, dict):
                    if method == "notifications/cancelled" and "id" not in request:
                        key = params.get("requestId")
                        with active_lock:
                            state = active.get(key) if type(key) in (str, int) else None
                        if state is not None:
                            state.cancel()
                        continue
                    name = params.get("name")
                    arguments = params.get("arguments", {})
                    if (method == "tools/call" and session_task_dir is not None
                            and isinstance(arguments, dict) and "task_dir" not in arguments
                            and arguments.get("file_path") is None):
                        params = {**params, "arguments": {**arguments, "task_dir": session_task_dir}}
                        request = {**request, "params": params}
                    if (method == "tools/call" and type(request_id) in (str, int)
                            and isinstance(name, str) and name in RENDER_TOOLS):
                        meta = params.get("_meta")
                        token = meta.get("progressToken") if isinstance(meta, dict) else None
                        state = RenderRequest(token if type(token) in (str, int) else None, send)
                        with active_lock:
                            if request_id in active:
                                send({"id": request_id, **error(-32600, "Duplicate active request ID")})
                                continue
                            active[request_id] = state
                        executor.submit(render, request_id, params, state)
                        continue

            response = dispatch(request, live=live)
            if response is not None:
                params = request.get("params", {}) if isinstance(request, dict) else {}
                if isinstance(params, dict) and request.get("method") == "tools/call":
                    selection = response.get("result", {}).get("structuredContent")
                    if selection is not None and params.get("name") == "task_design":
                        session_task_dir = selection["task_dir"]
                    if selection and params.get("name") == "live_preview" and "task_dir" in selection:
                        session_task_dir = selection["task_dir"]
                request_id = request.get("id") if isinstance(request, dict) else None
                send({"id": request_id, **response})

        with active_lock:
            pending = list(active.values())
        for state in pending:
            state.cancel()


if __name__ == "__main__":
    main()
