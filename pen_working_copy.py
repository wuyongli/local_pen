"""Publish and reopen an explicitly configured Pen working copy on macOS."""

import json
import os
import plistlib
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from edit_pen_text import load_pen, rebase_pen_resources, write_pen_copy
from pen_design import absolute_path


TASK_DESIGN_CONFIG = ".local-pen.json"


@lru_cache(maxsize=1)
def session_drafts():
    return TemporaryDirectory(prefix="local-pen-drafts-")


def edit_temporary_design(handler, arguments):
    # Keep the directory alive for subsequent edit and preview calls in this server.
    draft = Path(session_drafts().name) / f"{uuid4().hex}.pen"
    return {**handler(**arguments, output_path=str(draft)), "temporary": True}


def task_directory(task_dir):
    directory = absolute_path(task_dir).resolve(strict=True) if task_dir is not None else Path.cwd()
    if not directory.is_dir():
        raise ValueError("task_dir must be an existing directory")
    return directory


def task_file(directory, path):
    if path.is_symlink() or path.suffix != ".pen":
        raise ValueError("select a .pen file inside the task directory without a symbolic link")
    resolved = path.resolve()
    if not resolved.is_relative_to(directory) or not resolved.is_file():
        raise ValueError("select an existing .pen file inside the task directory")
    return resolved


def load_task_design(directory):
    config = directory / TASK_DESIGN_CONFIG
    if config.is_symlink():
        raise ValueError("the task design configuration cannot be a symbolic link")
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(data, dict) or set(data) != {"file_path"}
            or not isinstance(data["file_path"], str) or Path(data["file_path"]).is_absolute()):
        raise ValueError("invalid task design configuration")
    return task_file(directory, directory / data["file_path"])


def task_directory_for_path(file_path):
    if file_path is None:
        return None
    parent = absolute_path(file_path).parent.resolve()
    for directory in (parent, *parent.parents):
        config = directory / TASK_DESIGN_CONFIG
        if config.exists() or config.is_symlink():
            return directory
    return None


def task_design(task_dir=None, file_path=None):
    directory = task_directory(task_dir)
    if file_path is None:
        selected = load_task_design(directory)
    else:
        selected = task_file(directory, absolute_path(file_path))
        load_pen(selected)
        config = directory / TASK_DESIGN_CONFIG
        if config.is_symlink():
            raise ValueError("the task design configuration cannot be a symbolic link")
        record = {"file_path": selected.relative_to(directory).as_posix()}
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as pending:
            temporary = Path(pending.name)
            try:
                json.dump(record, pending, ensure_ascii=False, indent=2)
                pending.write("\n")
                pending.close()
                os.replace(temporary, config)
            finally:
                temporary.unlink(missing_ok=True)
    return {"task_dir": str(directory), "file_path": str(selected) if selected is not None else None}


def desktop_document(action, path):
    helper = Path(__file__).with_name("pen-reopen").resolve()
    if action == "close":
        if not os.access(helper, os.X_OK):
            raise ValueError("missing pen-reopen helper; compile pen_reopen.swift with swiftc")
        close_desktop_document(helper, path)
        return
    try:
        result = subprocess.run(["/usr/bin/open", "-b", "dev.pencil.desktop", str(path)],
                                capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        raise ValueError("Pen window open timed out") from None
    if result.returncode:
        raise ValueError(result.stderr.strip() or "could not open the Pen working copy")


def close_desktop_document(helper, path):
    label = f"local-pen-reopen-{uuid4().hex}"
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"

    def launchctl(*arguments):
        return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=3)

    # Launch independently so macOS checks the permission granted to pen-reopen.
    with TemporaryDirectory(dir="/private/tmp", prefix="local-pen-reopen-") as directory:
        job = Path(directory) / "close.plist"
        errors = Path(directory) / "stderr"
        errors.touch(mode=0o600)
        job.write_bytes(plistlib.dumps({
            "Label": label, "ProgramArguments": [str(helper), "close", str(path)],
            "RunAtLoad": True, "StandardErrorPath": str(errors),
        }))
        try:
            started = launchctl("bootstrap", domain, str(job))
            if started.returncode:
                raise ValueError(started.stderr.strip() or "could not start the Pen window helper")
            deadline = monotonic() + 8
            while monotonic() < deadline:
                status = launchctl("print", service)
                if status.returncode:
                    raise ValueError(status.stderr.strip() or "could not read the Pen window helper status")
                code = re.search(r"^\s*last exit code = (\d+)\s*$", status.stdout, re.MULTILINE)
                if code:
                    if code[1] != "0":
                        raise ValueError(errors.read_text().strip() or f"Pen window helper exited with code {code[1]}")
                    return
                sleep(0.05)
            raise ValueError("Pen window close timed out")
        except subprocess.TimeoutExpired:
            raise ValueError("Pen window close timed out") from None
        finally:
            launchctl("bootout", service)


def file_revision(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def edit_and_reopen(handler, arguments, *, task_file_path=None, revisions=None):
    if sys.platform != "darwin":
        raise ValueError("automatic reopening requires the macOS Pen desktop app")
    configured = str(task_file_path) if task_file_path is not None else os.environ.get("LOCAL_PEN_WORKING_COPY")
    if not configured:
        raise ValueError("select this task's design with task_design before updating it in place")
    return save_selected_design(handler, arguments, absolute_path(configured), reopen=True, revisions=revisions)


def save_selected_design(handler, arguments, target, *, reopen=False, revisions=None):
    source = absolute_path(arguments.get("file_path"))
    destination = absolute_path(arguments.get("output_path"))
    if target.is_symlink() or source.is_symlink() or destination.is_symlink():
        raise ValueError("the working copy must be a regular .pen file without a symbolic link")
    target = target.resolve()
    if target.suffix != ".pen" or source.resolve() != target or destination.resolve() != target:
        raise ValueError("file_path and output_path must both match the selected task design")
    import fcntl

    # Serialize across MCP clients without leaving a companion lock file.
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError("the working copy must be an existing .pen file with a single link")
        before = file_revision(target)
        if revisions is not None and revisions.get(target, before) != before:
            raise ValueError("the task design changed since this session read it; inspect the latest layers and retry")
        with TemporaryDirectory(dir=target.parent, prefix=".local-pen-edit-") as directory:
            draft = Path(directory) / "draft.pen"
            result = handler(**{**arguments, "file_path": str(target), "output_path": str(draft)})
            document = load_pen(draft)
            rebase_pen_resources(document, draft.parent, target.parent)
            ready = draft.with_name("ready.pen")
            write_pen_copy(document, ready)

            if reopen:
                desktop_document("close", target)
            display = {"display": "saved"}
            try:
                if file_revision(target) != before:
                    raise ValueError("the working copy changed during the edit; reload it and retry")
                os.replace(ready, target)
                if revisions is not None:
                    revisions[target] = file_revision(target)
            finally:
                if reopen:
                    try:
                        desktop_document("open", target)
                        display = {"reopen": {"ok": True}}
                    except (OSError, ValueError) as error:
                        display = {"reopen": {"ok": False, "message": str(error)}}
        return {**result, "output_path": str(target), **display}
    except BlockingIOError:
        raise ValueError("another working copy update is in progress; inspect the latest layers before retrying") from None
    finally:
        os.close(directory_fd)
