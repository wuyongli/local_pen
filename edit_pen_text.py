"""Replace the exact content of one text layer in a plain JSON .pen copy."""

import argparse
import json
import os
import re
from tempfile import NamedTemporaryFile
from pathlib import Path


def walk_layers(children, parent_id=None):
    for node in children:
        if not isinstance(node, dict):
            continue
        yield node, parent_id
        nested = node.get("children")
        if isinstance(nested, list):
            yield from walk_layers(nested, node.get("id"))


def load_pen(source):
    if source.suffix != ".pen" or not source.is_file():
        raise ValueError("source must be an existing .pen file")
    document = json.loads(source.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("version"), str)
        or not isinstance(document.get("children"), list)
    ):
        raise ValueError("expected a .pen JSON document with a version and children")
    return document


def rebase_pen_resources(document, source_dir, destination_dir):
    def rebase(value):
        if not isinstance(value, str) or not value or value.startswith(("/", "$", "#")):
            return value
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
            return value
        return Path(os.path.relpath(source_dir / value, destination_dir)).as_posix()

    imports = document.get("imports")
    if isinstance(imports, dict):
        for alias, uri in imports.items():
            imports[alias] = rebase(uri)

    pending = list(document["children"])
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            kind = value.get("type")
            if kind in ("image", "shader") and "url" in value:
                value["url"] = rebase(value["url"])
            if kind == "script" and "scriptUri" in value:
                value["scriptUri"] = rebase(value["scriptUri"])
            pending.extend(child for key, child in value.items() if key not in ("metadata", "context"))


def write_pen_copy(document, destination, source=None):
    if destination.suffix != ".pen":
        raise ValueError("destination must have a .pen extension")
    if source is not None and source.resolve().parent != destination.parent.resolve():
        rebase_pen_resources(document, source.resolve().parent, destination.parent.resolve())
    output = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with NamedTemporaryFile(dir=destination.parent, mode="w", encoding="utf-8", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(output)
            temporary.close()
            # Publishing with a hard link fails atomically if the destination already exists.
            destination.hardlink_to(temporary_path)
        except FileExistsError:
            raise ValueError(f"destination already exists: {destination}") from None
        finally:
            temporary_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("old_text")
    parser.add_argument("new_text")
    args = parser.parse_args()

    if args.source.suffix != ".pen" or args.destination.suffix != ".pen":
        parser.error("source and destination must have a .pen extension")
    if args.old_text == args.new_text:
        parser.error("old and new text must differ")

    try:
        document = load_pen(args.source)
        matches = [
            layer for layer, _ in walk_layers(document["children"])
            if layer.get("type") == "text" and layer.get("content") == args.old_text
        ]
        if len(matches) != 1:
            parser.error(f"expected one matching text layer, found {len(matches)}")

        layer = matches[0]
        layer["content"] = args.new_text
        write_pen_copy(document, args.destination, args.source)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    print(f"{args.destination} (.pen {document['version']}, layer {layer.get('id', '?')})")


if __name__ == "__main__":
    main()
