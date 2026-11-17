"""Inspect and edit saved JSON .pen documents without an editor connection."""

import math
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from edit_pen_text import load_pen, walk_layers, write_pen_copy


CONTAINERS = {"frame", "group"}
TYPES = CONTAINERS | {"rectangle", "ellipse", "polygon", "path", "text", "icon", "note", "ref"}
NEW_VERSION = "2.17"  # Matches the format verified in the installed desktop app.
MAX_LAYER_PAGE = 100


def absolute_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("provide an absolute file path")
    return Path(value)


def layer_index(children):
    index = {}
    for layer, parent_id in walk_layers(children):
        layer_id = layer.get("id")
        if not isinstance(layer_id, str) or not layer_id or layer_id in index:
            raise ValueError(f"missing or duplicate layer ID: {layer_id!r}")
        if not isinstance(layer.get("type"), str) or not layer["type"]:
            raise ValueError(f"layer {layer_id!r} is missing a valid type")
        index[layer_id] = (layer, parent_id)
    return index


def select(index, layer_id):
    if not isinstance(layer_id, str) or layer_id not in index:
        raise ValueError(f"unknown layer ID: {layer_id!r}")
    return index[layer_id]


def layer_page_end(limit, offset):
    if type(limit) is not int or limit < 1:
        raise ValueError(f"limit must be a positive integer; each page returns up to {MAX_LAYER_PAGE} layers")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer; use next_offset from the previous page")
    return offset + min(limit, MAX_LAYER_PAGE)


def inspect_layers(file_path, layer_id=None, depth=1, limit=30, offset=0):
    if type(depth) is not int or not 0 <= depth <= 8:
        raise ValueError("depth must be an integer from 0 to 8")
    end = layer_page_end(limit, offset)
    document = load_pen(absolute_path(file_path))
    roots = document["children"]
    parent_id = None
    if layer_id is not None:
        layer, parent_id = select(layer_index(roots), layer_id)
        roots = [layer]

    def visit(nodes, parent, level):
        for layer in nodes:
            if not isinstance(layer, dict):
                raise ValueError("each layer must be an object")
            layer_id = layer.get("id")
            if not isinstance(layer_id, str) or not layer_id:
                raise ValueError("layer is missing a valid id")
            layer_type = layer.get("type")
            if not isinstance(layer_type, str) or not layer_type:
                raise ValueError(f"layer {layer_id!r} is missing a valid type")
            children = layer.get("children", [])
            if not isinstance(children, list):
                raise ValueError(f"layer {layer_id!r} must have a children array")
            yield {
                "id": layer_id, "type": layer_type, "name": layer.get("name"),
                "parent_id": parent, "depth": level, "child_count": len(children),
                "properties": {k: v for k, v in layer.items() if k not in {
                    "id", "type", "name", "children", "metadata",
                }},
            }
            if level < depth:
                yield from visit(children, layer_id, level + 1)

    layers = list(visit(roots, parent_id, 0))
    return {"version": document["version"], "total": len(layers), "layers": layers[offset:end],
            "next_offset": end if end < len(layers) else None,
            "variable_names": list(document.get("variables", {})),
            "imports": document.get("imports", {})}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def variable(value):
    return isinstance(value, str) and value.startswith("$") and len(value) > 1


def numeric(value):
    return number(value) or variable(value)


def nonnegative(value):
    return variable(value) or (number(value) and value >= 0)


def size(value):
    return nonnegative(value) or (isinstance(value, str) and bool(
        re.fullmatch(r"(?:fit_content|fill_container)(?:\([0-9]+(?:\.[0-9]+)?\))?", value)
    ))


def color(value):
    return variable(value) or (isinstance(value, str) and bool(
        re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", value)
    ))


def fill(value):
    if isinstance(value, dict):
        if value.get("type") == "color":
            return value.keys() <= {"type", "color"} and color(value.get("color"))
        if value.get("type") == "image":
            url = value.get("url")
            return (value.keys() <= {"type", "url", "mode"} and isinstance(url, str) and bool(url)
                    and not Path(url).is_absolute() and ":" not in url
                    and value.get("mode", "cover") in ("cover", "contain", "stretch"))
        return False
    return color(value)


def insets(value, lengths):
    return nonnegative(value) or (isinstance(value, list) and len(value) in lengths
                                  and all(nonnegative(item) for item in value))


def string(value):
    return isinstance(value, str)


def boolean(value):
    return isinstance(value, bool) or variable(value)


COMMON = {
    "name": string, "x": number, "y": number, "enabled": boolean, "rotation": numeric,
    "opacity": lambda v: variable(v) or (number(v) and 0 <= v <= 1),
    "layoutPosition": lambda v: v in ("auto", "absolute"),
}
SIZE = {"width": size, "height": size}
GRAPHICS = {
    "fill": lambda v: all(map(fill, v)) if isinstance(v, list) else fill(v),
    "stroke": color, "strokeWidth": nonnegative,
}
BOX = {**SIZE, **GRAPHICS, "cornerRadius": lambda v: insets(v, (4,))}
PROPERTIES = {
    "frame": {**BOX, "clip": boolean, "layout": lambda v: v in ("none", "vertical", "horizontal"),
              "gap": nonnegative, "padding": lambda v: insets(v, (2, 4)),
              "justifyContent": lambda v: v in ("start", "center", "end", "space_between", "space_around"),
              "alignItems": lambda v: v in ("start", "center", "end")},
    "group": {},
    "rectangle": BOX,
    "ellipse": {**SIZE, **GRAPHICS},
    "polygon": {**SIZE, **GRAPHICS, "polygonCount": lambda v: type(v) is int and v >= 3},
    "path": {**SIZE, **GRAPHICS, "geometry": string},
    "text": {**SIZE, **GRAPHICS, "content": string, "fontFamily": string, "fontWeight": string,
             "fontStyle": string, "fontSize": nonnegative, "letterSpacing": numeric, "lineHeight": nonnegative,
             "textAlign": lambda v: v in ("left", "center", "right", "justify"),
             "textAlignVertical": lambda v: v in ("top", "middle", "bottom"),
             "textGrowth": lambda v: v in ("auto", "fixed-width", "fixed-width-height")},
    "note": {**SIZE, "content": string, "color": color},
    "icon": {**SIZE, "fill": color, "library": string, "icon": string},
    "ref": {**SIZE},
}


def patch(layer, properties):
    if not isinstance(properties, dict):
        raise ValueError("properties must be an object")
    allowed = {**COMMON, **PROPERTIES.get(layer["type"], {})}
    for key, value in properties.items():
        if key not in allowed or not allowed[key](value):
            raise ValueError(f"unsupported property or value for {layer['type']}: {key}")
    layer.update(properties)


def override_instance(layer, path, properties, index):
    if layer["type"] != "ref" or not isinstance(path, str) or not all(path.split("/")):
        raise ValueError("provide a component instance ID and a descendant ID path")
    target = layer
    segments = path.split("/")
    override_maps = []
    for position, layer_id in enumerate(segments):
        if target["type"] != "ref":
            raise ValueError(f"only nested component instances can prefix an override path: {path!r}")
        existing_overrides = target.get("descendants", {})
        if not isinstance(existing_overrides, dict):
            raise ValueError("invalid component overrides")
        override_maps.append(existing_overrides)
        definition, _ = select(index, target.get("ref"))
        descendants = layer_index(definition.get("children", []))
        target, parent_id = select(descendants, layer_id)
        check_ids = [layer_id]
        while parent_id is not None:
            check_ids.append(parent_id)
            _, parent_id = select(descendants, parent_id)
        for start, overrides_at_level in enumerate(override_maps):
            for candidate in check_ids:
                key = "/".join([*segments[start:position], candidate])
                change = overrides_at_level.get(key, {})
                if not isinstance(change, dict) or "type" in change or "children" in change:
                    raise ValueError("editing descendants of structural component overrides is unsupported")

    overrides = layer.setdefault("descendants", {})
    checked = {"type": target["type"]}
    patch(checked, properties)
    checked.pop("type")
    overrides[path] = {**overrides.get(path, {}), **checked}


def reserve_id(layer_id, used):
    if not isinstance(layer_id, str) or not layer_id or "/" in layer_id or layer_id in used:
        raise ValueError(f"invalid or duplicate new layer ID: {layer_id!r}")
    used.add(layer_id)
    return layer_id


def make_layer(spec, used, index):
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str) or spec["type"] not in TYPES:
        raise ValueError("layer must have a supported .pen type")
    kind = spec["type"]
    layer = {"id": reserve_id(spec.get("id", uuid4().hex[:12]), used), "type": kind}
    structural_keys = {"id", "type", "children"}
    if spec.get("reusable") is True and kind != "ref":
        layer["reusable"] = True
        structural_keys.add("reusable")
    if kind == "ref":
        target, _ = select(index, spec.get("ref"))
        if target.get("reusable") is not True:
            raise ValueError("ref must target an existing reusable component")
        layer["ref"] = target["id"]
        structural_keys.add("ref")
    patch(layer, {key: value for key, value in spec.items() if key not in structural_keys})
    if "children" in spec:
        if kind not in CONTAINERS or not isinstance(spec["children"], list):
            raise ValueError("only frames and groups can have a children array")
        layer["children"] = [make_layer(child, used, index) for child in spec["children"]]
    return layer


def children_at(roots, index, parent_id):
    if parent_id is None:
        return roots
    parent, _ = select(index, parent_id)
    if parent["type"] not in CONTAINERS:
        raise ValueError("parent must be a frame or group")
    return parent.setdefault("children", [])


def insert(children, layer, position):
    if position is None:
        position = len(children)
    if type(position) is not int or not 0 <= position <= len(children):
        raise ValueError(f"index must be an integer from 0 to {len(children)}")
    children.insert(position, layer)


def component_ancestor(index, layer_id):
    while layer_id is not None:
        layer, layer_id = select(index, layer_id)
        if layer.get("reusable") is True:
            return True
    return False


def referenced_ids(index):
    protected = set()
    for layer, _ in index.values():
        if layer.get("type") == "ref":
            protected.add(layer.get("ref"))
            for path in layer.get("descendants", {}):
                protected.update(path.split("/"))
        if isinstance(layer.get("slot"), list):
            protected.update(layer["slot"])
    return protected


def duplicate_layer(layer, new_id, used):
    cloned = deepcopy(layer)
    nodes = [node for node, _ in walk_layers([cloned])]
    for node in nodes:
        if node.get("reusable") is True:
            raise ValueError("duplicating component definitions is unsupported; add a ref instance instead")
        overrides = node.get("descendants", {})
        if not isinstance(overrides, dict) or any(
            not isinstance(value, dict) or "type" in value or "children" in value for value in overrides.values()
        ):
            raise ValueError("duplicating instances with structural overrides is unsupported")
    cloned["id"] = reserve_id(new_id, used)
    for node in nodes[1:]:
        node["id"] = reserve_id(uuid4().hex[:12], used)
    return cloned


OP_FIELDS = {
    "add": {"layer", "parent_id", "index"},
    "update": {"id", "properties"},
    "override": {"id", "path", "properties"},
    "move": {"id", "parent_id", "index"},
    "remove": {"id"},
    "duplicate": {"id", "new_id", "parent_id", "index", "properties"},
}


def apply_operation(roots, operation):
    if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
        raise ValueError("operation must specify an op")
    action = operation["op"]
    if action not in OP_FIELDS or operation.keys() - {"op"} - OP_FIELDS[action]:
        raise ValueError(f"unsupported operation or fields: {action}")
    index = layer_index(roots)
    used = set(index)
    if action == "add":
        parent_id = operation.get("parent_id")
        layer = make_layer(operation.get("layer"), used, index)
        if component_ancestor(index, parent_id) and any(
            node.get("type") == "ref" for node, _ in walk_layers([layer])
        ):
            raise ValueError("adding instances inside component definitions is unsupported")
        insert(children_at(roots, index, parent_id), layer, operation.get("index"))
        return layer["id"]

    layer_id = operation.get("id")
    layer, old_parent_id = select(index, layer_id)
    if action == "update":
        patch(layer, operation.get("properties"))
        return layer_id
    if action == "override":
        override_instance(layer, operation.get("path"), operation.get("properties"), index)
        return layer_id

    subtree = {node["id"] for node, _ in walk_layers([layer])}
    new_parent_id = operation.get("parent_id", old_parent_id)
    if new_parent_id is not None:
        select(index, new_parent_id)
    if action == "duplicate":
        cloned = duplicate_layer(layer, operation.get("new_id", uuid4().hex[:12]), used)
        patch(cloned, operation.get("properties", {}))
        if component_ancestor(index, new_parent_id) and any(
            node.get("type") == "ref" for node, _ in walk_layers([cloned])
        ):
            raise ValueError("duplicating instances inside component definitions is unsupported")
        insert(children_at(roots, index, new_parent_id), cloned, operation.get("index"))
        return cloned["id"]

    if subtree & referenced_ids(index) or component_ancestor(index, old_parent_id):
        raise ValueError("moving or removing a component reference target or its children is unsupported")
    if action == "move" and (new_parent_id in subtree or component_ancestor(index, new_parent_id)):
        raise ValueError("cannot move a layer into its own subtree or a component definition")
    children_at(roots, index, old_parent_id).remove(layer)
    if action == "move":
        insert(children_at(roots, index, new_parent_id), layer, operation.get("index"))
    return layer_id


def edit_design(output_path, operations, file_path=None):
    destination = absolute_path(output_path)
    source = absolute_path(file_path) if file_path is not None else None
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("provide 1–100 operations")
    document = load_pen(source) if source else {"version": NEW_VERSION, "children": []}
    changes = []
    for position, operation in enumerate(operations, 1):
        try:
            layer_id = apply_operation(document["children"], operation)
        except ValueError as error:
            raise ValueError(f"operation {position}: {error}") from error
        changes.append({"op": operation["op"], "id": layer_id})
    write_pen_copy(document, destination, source)
    return {"output_path": str(destination), "version": document["version"], "changes": changes}
