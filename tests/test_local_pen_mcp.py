import json
import base64
import os
import plistlib
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER = Path(__file__).resolve().parents[1] / "local_pen_mcp.py"
sys.path.insert(0, str(SERVER.parent))

from local_pen_mcp import call_tool
import pen_working_copy


def png_size(data):
    return struct.unpack(">II", data[16:24])


@unittest.skipUnless(os.name == "posix", "private cache uses POSIX ownership and permissions")
class FontCacheDirectoryTest(unittest.TestCase):
    def cache_path(self, temporary):
        return subprocess.check_output(
            [sys.executable, "-B", "-c",
             "from pen_render import renderer_environment; "
             "print((renderer_environment() or {}).get('LOCAL_PEN_FONT_CACHE_DIR', ''))"],
            cwd=SERVER.parent, env={**os.environ, "TMPDIR": temporary}, text=True,
        ).strip()

    def test_independent_servers_reuse_private_font_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(self.cache_path(temporary))
            self.assertTrue(first.is_dir())
            self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(self.cache_path(temporary)), first)

    def test_font_cache_refuses_accessible_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(self.cache_path(temporary))
            cache.chmod(0o755)
            fallback = self.cache_path(temporary)
            self.assertNotEqual(fallback, str(cache))
            cache.rmdir()
            target = Path(temporary) / "redirected"
            target.mkdir(mode=0o700)
            cache.symlink_to(target, target_is_directory=True)
            self.assertNotIn(self.cache_path(temporary), {str(cache), str(target)})


class LocalPenMcpTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "source.pen"
        self.copy = self.source.with_name("copy.pen")
        self.document = {"version": "2.17", "children": [{
            "id": "frame", "type": "frame", "name": "账号与设置", "width": 80, "height": 60, "children": [
                {"id": "heading", "type": "text", "name": "分区标题", "content": "帮助与支持"},
                {"id": "other", "type": "text", "content": "帮助与支持"},
            ],
        }]}
        self.original_text = json.dumps(self.document, ensure_ascii=False)
        self.source.write_text(self.original_text, encoding="utf-8")
        self.env = os.environ.copy()
        self.env["XDG_STATE_HOME"] = str(self.source.parent / "state")

    def rpc(self, *calls, after_reply=None, cwd=None):
        requests = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        requests.extend(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            for request_id, (method, params) in enumerate(calls, 1)
        )
        with subprocess.Popen([sys.executable, "-B", str(SERVER)], bufsize=0, env=self.env, cwd=cwd,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
            try:
                replies = []
                for request in requests:
                    process.stdin.write((json.dumps(request) + "\n").encode())
                    if "id" not in request:
                        continue
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0], "MCP did not respond")
                    replies.append(json.loads(process.stdout.readline()))
                    if after_reply is not None:
                        after_reply(request["id"])
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
        self.assertEqual([reply["id"] for reply in replies], list(range(len(calls) + 1)))
        self.assertIn("tools", replies[0]["result"]["capabilities"])
        return [reply["result"] for reply in replies[1:]]

    def call(self, name, **arguments):
        return "tools/call", {"name": name, "arguments": {"file_path": str(self.source), **arguments}}

    def edit(self, layer_id="heading", **arguments):
        return self.call("update_text", **{
            "layer_id": layer_id, "text": "帮助与客服", "output_path": str(self.copy), **arguments,
        })

    @unittest.skipUnless(os.name == "posix", "in-place saves need POSIX file locks")
    def test_live_preview_saves_selected_design_without_desktop_and_stops(self):
        from urllib.error import URLError
        from urllib.request import urlopen

        def request(name, **arguments):
            return "tools/call", {"name": name, "arguments": arguments}

        select_design = request("task_design", task_dir=str(self.source.parent), file_path=str(self.source))
        start = request("live_preview")
        edit = request("update_text", layer_id="heading", text="副屏更新")
        force_reopen = request("update_text", layer_id="heading", text="不应保存", reopen=True)
        refresh = request("live_preview", layer_id="frame", scale=2)
        status = request("live_preview", action="status")
        stop = request("live_preview", action="stop")
        _, started, saved, rejected, refreshed, current, restored, stopped = self.rpc(
            select_design, start, edit, force_reopen, refresh, status, start, stop, cwd=self.source.parent,
        )
        self.assertFalse(started.get("isError"), started)
        preview = started["structuredContent"]
        self.assertEqual(preview["view"], "canvas")
        self.assertIsNone(preview["layer_id"])
        self.assertTrue(preview["url"].startswith("http://127.0.0.1:"))
        self.assertEqual(preview["url"], current["structuredContent"]["url"])
        self.assertEqual(current["structuredContent"]["view"], "canvas")
        self.assertIsNone(current["structuredContent"]["layer_id"])
        self.assertEqual(refreshed["structuredContent"]["url"], preview["url"])
        self.assertEqual(current["structuredContent"]["scale"], 2)
        self.assertEqual(restored["structuredContent"]["url"], preview["url"])
        self.assertIsNone(restored["structuredContent"]["layer_id"])
        self.assertEqual(saved["structuredContent"]["display"], "live_preview")
        self.assertNotIn("reopen", saved["structuredContent"])
        self.assertTrue(rejected["isError"], rejected)
        self.assertIn("副屏更新", self.source.read_text())
        self.assertNotIn("不应保存", self.source.read_text())
        self.assertFalse(stopped["structuredContent"]["active"])
        with self.assertRaises(URLError):
            urlopen(preview["url"], timeout=2)

    @unittest.skipUnless(os.name == "posix", "live preview requires POSIX")
    def test_live_preview_keeps_the_task_url_between_mcp_processes(self):
        directory = self.source.parent
        start = ("tools/call", {"name": "live_preview"})
        bind = ("tools/call", {"name": "task_design", "arguments": {
            "task_dir": str(directory), "file_path": str(self.source),
        }})
        _, previous = self.rpc(bind, start, cwd=directory)
        current, = self.rpc(start, cwd=directory)
        self.assertFalse(current.get("isError"), current)
        self.assertEqual(current["structuredContent"]["url"], previous["structuredContent"]["url"])

        other_task = directory / "other-task"
        other_task.mkdir()
        other_design = other_task / self.source.name
        other_design.write_text(self.original_text, encoding="utf-8")
        _, other = self.rpc(("tools/call", {"name": "task_design", "arguments": {
            "task_dir": str(other_task), "file_path": str(other_design),
        }}), start, cwd=other_task)
        self.assertNotEqual(other["structuredContent"]["url"], current["structuredContent"]["url"])

    @unittest.skipUnless(os.name == "posix", "live preview requires POSIX")
    def test_live_preview_reconnect_returns_the_new_canvas_at_the_saved_url(self):
        from urllib.request import Request, urlopen
        from pen_live_preview import LivePreview

        directory = str(self.source.parent)
        call_tool({"name": "task_design", "arguments": {"task_dir": directory, "file_path": str(self.source)}})
        rendered = [({"artboards": [{"name": "旧画板"}]}, b"old"),
                    ({"artboards": [{"name": "新画板"}]}, b"new")]
        with (patch.dict(os.environ, {"XDG_STATE_HOME": self.env["XDG_STATE_HOME"]}),
              patch("pen_live_preview.render_canvas_png", side_effect=rendered)):
            with LivePreview() as live:
                url = live.command(task_dir=directory)["url"]
                with urlopen(url + "canvas.png", timeout=2) as response:
                    self.assertEqual(response.read(), b"old")
                    previous = response.headers["ETag"]
            with LivePreview() as live:
                self.assertEqual(live.command(task_dir=directory)["url"], url)
                request = Request(url + "canvas.png", headers={"If-None-Match": previous})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.read(), b"new")
                    current = response.headers["ETag"]
                    self.assertNotEqual(current, previous)
                with urlopen(Request(url + "artboards.json", headers={"If-Match": current}), timeout=2) as response:
                    self.assertEqual(json.load(response), [{"name": "新画板"}])

    @unittest.skipUnless(os.name == "posix", "live preview requires POSIX")
    def test_live_preview_preserves_the_url_when_the_task_selects_another_file(self):
        self.copy.write_text(self.original_text, encoding="utf-8")
        directory = str(self.source.parent)
        start = ("tools/call", {"name": "live_preview"})
        select_source, select_copy = [
            ("tools/call", {"name": "task_design", "arguments": {"task_dir": directory, "file_path": str(path)}})
            for path in (self.source, self.copy)
        ]
        _, first, _, second = self.rpc(select_source, start, select_copy, start, cwd=directory)
        self.assertFalse(first.get("isError"), first)
        self.assertFalse(second.get("isError"), second)
        self.assertEqual(first["structuredContent"]["url"], second["structuredContent"]["url"])
        self.assertEqual(second["structuredContent"]["file_path"], str(self.copy.resolve()))

    def test_live_canvas_renders_all_layers_without_repositioning_them(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        from pen_live_preview import PreviewWindow
        from pen_render import PNG_END, PNG_HEADER, load_pen

        document = {"version": "2.17", "children": [
            {"type": "frame", "id": "right", "name": "结算详情", "x": 200, "width": 80, "height": 60,
             "children": [{"type": "ref", "id": "instance", "ref": "component"}]},
            {"type": "frame", "id": "component", "name": "组件", "x": -150, "y": -50, "width": 100,
             "layout": "vertical", "reusable": True},
            {"type": "rectangle", "id": "shape", "x": 300, "y": 220, "width": 16, "height": 12},
            {"type": "frame", "id": "hidden", "name": "隐藏画板", "enabled": False, "x": -400, "y": -300},
        ]}
        self.source.write_text(json.dumps(document))
        window = PreviewWindow(self.source.resolve(), 1)
        self.addCleanup(window.close)
        png = PNG_HEADER + struct.pack(">II", 466, 282) + b"preview data" + PNG_END

        def run(command, *_args, **_kwargs):
            self.assertEqual(load_pen(Path(command[command.index("--in") + 1])), document)
            self.assertEqual(command[command.index("--export-scale") + 1], "1")
            Path(command[command.index("--export") + 1]).write_bytes(png)
            return 0

        with (patch("pen_render.renderer_command", return_value=["pen"]),
              patch("pen_render.run_renderer", side_effect=run) as renderer,
              urlopen(window.status()["url"] + "canvas.png", timeout=2) as response):
            self.assertEqual(response.read(), png)
            tag = response.headers["ETag"]
        url = window.status()["url"] + "artboards.json"
        with urlopen(Request(url, headers={"If-Match": tag}), timeout=2) as response:
            titles = json.load(response)
            self.assertEqual([(title["id"], title["name"], title["x"], title["y"]) for title in titles],
                             [("right", "结算详情", 350, 50), ("component", "组件", 0, 0)])
        with self.assertRaises(HTTPError) as missing_revision:
            urlopen(Request(url, headers={"If-Match": '"999"'}), timeout=2)
        self.assertEqual(missing_revision.exception.code, 412)
        self.assertEqual(renderer.call_count, 1)

    def test_live_canvas_reduces_scale_for_a_large_document(self):
        from pen_render import render_canvas_png

        self.document["children"].append({
            "id": "right", "type": "rectangle", "x": 14_000, "y": 11_000, "width": 80, "height": 60,
        })
        self.source.write_text(json.dumps(self.document))
        with patch("pen_render.render_staged_png", return_value=({}, b"png")) as render:
            render_canvas_png(str(self.source), 1)
        scale = render.call_args.args[3]
        self.assertLess(scale, 1)
        self.assertLessEqual(14_080 * 11_060 * scale ** 2, 24_000_000)

    @unittest.skipUnless(sys.platform == "darwin", "local render isolation is provided by macOS")
    def test_live_canvas_shows_local_render_when_only_a_font_is_missing(self):
        from urllib.request import urlopen

        from pen_live_preview import PreviewWindow
        from pen_render import PNG_END, PNG_HEADER

        window = PreviewWindow(self.source.resolve(), 1)
        self.addCleanup(window.close)
        png = PNG_HEADER + struct.pack(">II", 80, 60) + b"preview data" + PNG_END

        def run(command, *_args, env):
            self.assertEqual(command[0], "/usr/bin/sandbox-exec")
            Path(env["LOCAL_PEN_MISSING_RESOURCES"]).write_text("font\n")
            Path(command[command.index("--export") + 1]).write_bytes(png)
            return 0

        with (patch("pen_render.renderer_command", return_value=["pen"]),
              patch("pen_render.run_renderer", side_effect=run) as renderer,
              urlopen(window.status()["url"] + "canvas.png", timeout=2) as response):
            self.assertEqual(response.headers["X-Local-Pen-Font-Fallback"], "1")
            self.assertEqual(response.read(), png)
        self.assertEqual(renderer.call_count, 1)

    def test_live_preview_caches_revisions_and_checks_its_local_address(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        from pen_live_preview import PreviewWindow

        with patch("pen_live_preview.render_canvas_png", side_effect=[({}, b"first"), ({}, b"second")]) as renderer:
            window = PreviewWindow(self.source.resolve(), 1)
            self.addCleanup(window.close)
            url = window.status()["url"]
            with self.assertRaises(HTTPError) as bad_host:
                urlopen(Request(url, headers={"Host": "other.example"}), timeout=2)
            self.assertEqual(bad_host.exception.code, 400)
            with urlopen(url + "canvas.png", timeout=2) as first:
                self.assertEqual(first.read(), b"first")
                tag = first.headers["ETag"]
            unchanged = Request(url + "canvas.png", headers={"If-None-Match": tag})
            with self.assertRaises(HTTPError) as not_modified:
                urlopen(unchanged, timeout=2)
            self.assertEqual(not_modified.exception.code, 304)
            ready = self.source.with_suffix(".new")
            ready.write_text(self.original_text.replace("帮助与支持", "副屏更新"))
            ready.replace(self.source)
            with urlopen(unchanged, timeout=2) as second:
                self.assertEqual(second.read(), b"second")
                self.assertNotEqual(second.headers["ETag"], tag)
            self.assertEqual(renderer.call_count, 2)

    def test_live_preview_discards_a_render_superseded_during_saving(self):
        from urllib.request import urlopen

        from pen_live_preview import PreviewWindow

        window = PreviewWindow(self.source.resolve(), 1)
        self.addCleanup(window.close)
        url = window.status()["url"] + "canvas.png"

        def render(*args, request, **kwargs):
            self.assertFalse(request.cancelled.is_set())
            if self.source.read_text() == self.original_text:
                self.source.write_text(self.original_text + "\n")
                self.assertTrue(request.cancelled.is_set())
                return {}, b"obsolete"
            return {}, b"latest"

        with patch("pen_live_preview.render_canvas_png", side_effect=render) as renderer:
            with urlopen(url, timeout=2) as first:
                self.assertEqual(first.status, 202)
            with urlopen(url, timeout=2) as second:
                self.assertEqual(second.read(), b"latest")
            self.assertEqual(renderer.call_count, 2)

    @unittest.skipUnless(sys.platform == "darwin", "local render isolation is provided by macOS")
    def test_live_preview_uses_local_render_and_falls_back_for_missing_resources(self):
        from pen_render import PNG_END, PNG_HEADER, PREVIEW_BYTE_LIMIT, PREVIEW_PIXEL_LIMIT, render_png

        png = PNG_HEADER + struct.pack(">II", 80, 60) + b"preview data" + PNG_END
        for scenario in ("cached", "font-miss", "remote-image", "unavailable"):
            with self.subTest(scenario=scenario):
                document = json.loads(self.original_text)
                if scenario == "remote-image":
                    document["children"][0]["fill"] = {"type": "image", "url": "https://example.com/asset.png"}
                self.source.write_text(json.dumps(document))
                calls = []

                def run(command, timeout, request, *, env=None):
                    is_local = command[0] == "/usr/bin/sandbox-exec"
                    calls.append(is_local)
                    output = Path(command[command.index("--export") + 1])
                    if not is_local:
                        self.assertFalse(output.exists())
                    output.write_bytes(png)
                    if (is_local and scenario == "font-miss") or scenario == "unavailable":
                        Path(env["LOCAL_PEN_MISSING_RESOURCES"]).touch()
                    return 0

                with patch("pen_render.run_renderer", side_effect=run), patch("pen_render.renderer_command", return_value=["pen"]):
                    if scenario == "unavailable":
                        with self.assertRaisesRegex(ValueError, "resources could not be loaded"):
                            render_png(str(self.source), "frame", 1, PREVIEW_PIXEL_LIMIT,
                                       PREVIEW_BYTE_LIMIT, prefer_local=True)
                    else:
                        _, data = render_png(str(self.source), "frame", 1, PREVIEW_PIXEL_LIMIT,
                                             PREVIEW_BYTE_LIMIT, prefer_local=True)
                        self.assertEqual(data, png)
                expected = {"cached": [True], "font-miss": [True, False], "remote-image": [False],
                            "unavailable": [True, False]}
                self.assertEqual(calls, expected[scenario])

    def test_task_design_persists_and_keeps_tasks_separate(self):
        tasks = [self.source.parent / name for name in ("first", "second")]
        designs = []
        for task in tasks:
            task.mkdir()
            design = task / "design.pen"
            design.write_text(self.original_text, encoding="utf-8")
            designs.append(design)
            selected, = self.rpc(self.call("task_design", task_dir=str(task), file_path=str(design)))
            self.assertEqual(selected.get("structuredContent", {}).get("file_path"), str(design.resolve()), selected)
        self.env["LOCAL_PEN_WORKING_COPY"] = str(self.source)
        for task, design in zip(tasks, designs):
            selected, = self.rpc(("tools/call", {"name": "task_design", "arguments": {"task_dir": str(task)}}))
            self.assertEqual(selected["structuredContent"]["file_path"], str(design.resolve()))
        rejected, = self.rpc(self.call("task_design", task_dir=str(tasks[0]), file_path=str(designs[1])))
        self.assertTrue(rejected["isError"])

    def test_session_remembers_selected_task_outside_working_directory(self):
        docs = self.source.parent
        code = docs / "code"
        code.mkdir()
        other_task = code / "other"
        other_task.mkdir()
        bind = self.call("task_design", task_dir=str(docs))
        inspect = ("tools/call", {"name": "inspect_layers", "arguments": {"layer_id": "heading"}})
        invalid_edit = ("tools/call", {"name": "edit_design", "arguments": {
            "output_path": None, "reopen": True,
            "operations": [{"op": "update", "id": "heading", "properties": {"padding": 8}}],
        }})
        clear_previous = ("tools/call", {"name": "task_design", "arguments": {"task_dir": str(other_task)}})
        _, found, invalid, _, missing = self.rpc(bind, inspect, invalid_edit, clear_previous, inspect, cwd=code)
        self.assertEqual(found.get("structuredContent", {}).get("layers", [{}])[0].get("id"), "heading", found)
        self.assertTrue(invalid["isError"])
        self.assertIn("padding", invalid["content"][0]["text"])
        self.assertTrue(missing["isError"])
        self.assertEqual(self.source.read_text(), self.original_text)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_task_edit_treats_null_output_as_in_place(self):
        task = self.source.parent
        self.rpc(self.call("task_design", task_dir=str(task)))
        arguments = {"task_dir": str(task), "file_path": None, "output_path": None, "operations": [
            {"op": "update", "id": "heading", "properties": {"content": "已修改"}},
        ]}
        with patch.object(pen_working_copy, "desktop_document") as helper:
            result = call_tool({"name": "edit_design", "arguments": arguments})
            self.assertTrue(result.get("structuredContent", {}).get("reopen", {}).get("ok"), result)
            conflict = call_tool({"name": "edit_design", "arguments": {**arguments, "reopen": False}})
            self.assertIn("omit reopen", conflict["content"][0]["text"])
        self.assertEqual(helper.call_count, 2)
        self.assertFalse(self.copy.exists())

    def test_temporary_design_can_be_inspected_and_previewed_then_is_removed(self):
        self.fake_renderer("assert board['children'][0]['content'] == '临时方案'", needs_local_assets=False)
        self.rpc(self.call("task_design", task_dir=str(self.source.parent)))
        temporary = self.source.parent / "system-temp"
        temporary.mkdir()
        self.env["TMPDIR"] = str(temporary)
        edit = ("tools/call", {"name": "edit_design", "arguments": {
            "task_dir": str(self.source.parent), "temporary": True,
            "operations": [{"op": "update", "id": "heading", "properties": {"content": "临时方案"}}],
        }})
        inspect = self.call("find_layers", query="临时方案")
        preview = self.call("preview_png", layer_id="frame")
        pending = []

        def use_draft(request_id):
            if request_id == 1:
                drafts = list(temporary.rglob("*.pen"))
                self.assertEqual(len(drafts), 1)
                pending.extend(drafts)
                for _, params in (inspect, preview):
                    params["arguments"]["file_path"] = str(drafts[0])

        edited, found, image = self.rpc(edit, inspect, preview, after_reply=use_draft)
        self.assertTrue(edited["structuredContent"]["temporary"])
        self.assertEqual(Path(edited["structuredContent"]["output_path"]).resolve(), pending[0].resolve())
        self.assertEqual(found["structuredContent"]["layers"][0]["id"], "heading")
        self.assertTrue(any(block["type"] == "image" for block in image["content"]))
        self.assertEqual(list(temporary.glob("local-pen-drafts-*")), [])
        self.assertEqual(self.source.read_text(), self.original_text)
        self.assertCountEqual([item.name for item in self.source.parent.glob("*.pen")], ["source.pen"])

    def test_temporary_design_rejects_explicit_output_or_reopen(self):
        arguments = {"temporary": True, "operations": [
            {"op": "update", "id": "heading", "properties": {"content": "临时方案"}},
        ]}
        with_output, with_reopen = self.rpc(
            self.call("edit_design", **arguments, output_path=str(self.copy)),
            self.call("edit_design", **arguments, reopen=True),
        )
        self.assertIn("temporary", with_output["content"][0]["text"])
        self.assertIn("temporary", with_reopen["content"][0]["text"])
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.source.read_text(), self.original_text)

    def test_task_edit_requires_explicit_separate_output_with_paths(self):
        self.rpc(self.call("task_design", task_dir=str(self.source.parent)))
        arguments = {"output_path": str(self.copy), "operations": [
            {"op": "update", "id": "heading", "properties": {"content": "新标题"}},
        ]}
        blank = ("tools/call", {"name": "edit_design", "arguments": {
            "output_path": str(self.copy), "operations": [
                {"op": "add", "layer": {"id": "new", "type": "frame", "width": 80, "height": 60}},
            ],
        }})
        for method, payload in (self.call("edit_design", **arguments), self.edit(), blank):
            rejected, = self.rpc((method, payload))
            self.assertTrue(rejected.get("isError"), rejected)
            self.assertIn("separate_output", rejected["content"][0]["text"])
            self.assertFalse(self.copy.exists())
        selected, = self.rpc(self.call("edit_design", **arguments, separate_output=True))
        self.assertEqual(selected["structuredContent"]["output_path"], str(self.copy))
        self.assertEqual(self.source.read_text(), self.original_text)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_explicit_current_task_path_reopens_without_a_reopen_flag(self):
        self.rpc(self.call("task_design", task_dir=str(self.source.parent)))
        _, params = self.edit(output_path=str(self.source))
        with patch.object(pen_working_copy, "desktop_document") as helper:
            result = call_tool(params)
        self.assertTrue(result.get("structuredContent", {}).get("reopen", {}).get("ok"), result)
        self.assertEqual([call.args for call in helper.call_args_list],
                         [("close", self.source.resolve()), ("open", self.source.resolve())])
        self.assertFalse(self.copy.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_task_edit_uses_bound_file_and_reopens_by_default(self):
        task = self.source.parent
        self.copy.write_text(self.original_text, encoding="utf-8")
        self.rpc(self.call("task_design", task_dir=str(task), file_path=str(self.copy)))
        operations = [{"op": "update", "id": "heading", "properties": {"fontSize": 18}}]
        arguments = {"task_dir": str(task), "operations": operations}
        params = {"name": "edit_design", "arguments": arguments}
        with patch.dict(os.environ, LOCAL_PEN_WORKING_COPY=str(self.source)):
            with patch.object(pen_working_copy, "desktop_document") as helper:
                reply = call_tool(params)
                self.assertTrue(reply.get("structuredContent", {}).get("reopen", {}).get("ok"), reply)
                self.assertEqual([call.args for call in helper.call_args_list],
                                 [("close", self.copy.resolve()), ("open", self.copy.resolve())])
                mismatch = call_tool({"name": "edit_design", "arguments": {**arguments, "file_path": str(self.source)}})
                self.assertTrue(mismatch["isError"])
                self.assertEqual(helper.call_count, 2)
        self.assertEqual(self.source.read_text(), self.original_text)
        self.assertEqual(json.loads(self.copy.read_bytes())["children"][0]["children"][0]["fontSize"], 18)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_window_helper_runs_as_one_shot_gui_job_and_is_removed(self):
        calls = []

        def launchctl(command, **options):
            calls.append(command)
            action = command[1]
            if action == "bootstrap":
                job = plistlib.loads(Path(command[-1]).read_bytes())
                self.assertTrue(job["RunAtLoad"])
                self.assertFalse(job.get("KeepAlive", False))
                self.assertEqual(job["ProgramArguments"][1:], ["close", str(self.copy)])
            if action == "print":
                return subprocess.CompletedProcess(command, 0, "state = not running\nruns = 1\nlast exit code = 0\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (patch.object(pen_working_copy.os, "access", return_value=True),
              patch.object(pen_working_copy.subprocess, "run", side_effect=launchctl)):
            pen_working_copy.desktop_document("close", self.copy)
        self.assertEqual([command[1] for command in calls], ["bootstrap", "print", "bootout"])
        self.assertEqual(calls[-1][-1], calls[-2][-1])

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_reopen_updates_only_configured_working_copy(self):
        import pen_working_copy

        self.copy.write_text(self.original_text, encoding="utf-8")
        _, params = self.edit(file_path=str(self.copy), reopen=True)

        def desktop(action, path):
            self.assertEqual(path, self.copy.resolve())
            expected = "帮助与支持" if action == "close" else "帮助与客服"
            self.assertEqual(json.loads(path.read_bytes())["children"][0]["children"][0]["content"], expected)

        with patch.dict(os.environ, LOCAL_PEN_WORKING_COPY=str(self.copy)):
            with patch.object(pen_working_copy, "desktop_document", side_effect=desktop) as helper:
                reply = call_tool(params)
                self.assertTrue(reply["structuredContent"]["reopen"]["ok"])
                self.assertEqual([call.args[0] for call in helper.call_args_list], ["close", "open"])
                params["arguments"].update(file_path=str(self.source), output_path=str(self.source))
                self.assertTrue(call_tool(params)["isError"])
                self.assertEqual(helper.call_count, 2)
        self.assertEqual(self.source.read_text(), self.original_text)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_failed_window_close_keeps_working_copy(self):
        import pen_working_copy

        self.copy.write_text(self.original_text, encoding="utf-8")
        _, params = self.edit(file_path=str(self.copy), reopen=True)
        with patch.dict(os.environ, LOCAL_PEN_WORKING_COPY=str(self.copy)):
            with patch.object(pen_working_copy, "desktop_document", side_effect=ValueError("unsaved")) as helper:
                reply = call_tool(params)
                self.assertTrue(reply["isError"])
                self.assertIn("unsaved", reply["content"][0]["text"])
                helper.assert_called_once_with("close", self.copy.resolve())
        self.assertEqual(self.copy.read_text(), self.original_text)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_reopen_does_not_overwrite_a_concurrent_pen_save(self):
        import pen_working_copy

        self.copy.write_text(self.original_text, encoding="utf-8")
        _, params = self.edit(file_path=str(self.copy), reopen=True)
        saved = self.original_text.replace("账号与设置", "手动保存")

        def desktop(action, path):
            if action == "close":
                path.write_text(saved, encoding="utf-8")

        with patch.dict(os.environ, LOCAL_PEN_WORKING_COPY=str(self.copy)):
            with patch.object(pen_working_copy, "desktop_document", side_effect=desktop) as helper:
                reply = call_tool(params)
                self.assertTrue(reply["isError"])
                self.assertIn("changed during the edit", reply["content"][0]["text"])
                self.assertEqual([call.args[0] for call in helper.call_args_list], ["close", "open"])
        self.assertEqual(self.copy.read_text(), saved)

    @unittest.skipUnless(sys.platform == "darwin", "Pen desktop refresh uses macOS")
    def test_open_failure_reports_saved_draft(self):
        import pen_working_copy

        self.copy.write_text(self.original_text, encoding="utf-8")
        _, params = self.edit(file_path=str(self.copy), reopen=True)
        with patch.dict(os.environ, LOCAL_PEN_WORKING_COPY=str(self.copy)):
            with patch.object(pen_working_copy, "desktop_document", side_effect=[None, ValueError("open failed")]):
                reply = call_tool(params)
                self.assertEqual(reply["structuredContent"]["reopen"], {"ok": False, "message": "open failed"})
                self.assertEqual(reply["structuredContent"]["output_path"], str(self.copy.resolve()))
        self.assertIn("帮助与客服", self.copy.read_text())

    def test_lists_tools_and_finds_matching_layers_with_pagination(self):
        catalog, first, second = self.rpc(
            ("tools/list", {}),
            self.call("find_layers", query="帮助与支持", limit=1),
            self.call("find_layers", query="帮助与支持", limit=1, offset=1),
        )
        self.assertEqual({tool["name"] for tool in catalog["tools"]}, {
            "find_layers", "update_text", "inspect_layers", "edit_design", "preview_png", "export_png",
            "export_png_batch", "task_design", "live_preview",
        })
        live, = [tool for tool in catalog["tools"] if tool["name"] == "live_preview"]
        self.assertNotIn("layer_id", live["inputSchema"]["properties"])
        self.assertEqual(first["structuredContent"]["layers"][0]["id"], "heading")
        self.assertEqual(first["structuredContent"]["layers"][0]["parent_id"], "frame")
        self.assertEqual(first["structuredContent"]["next_offset"], 1)
        self.assertEqual(second["structuredContent"]["layers"][0]["id"], "other")
        self.assertIsNone(second["structuredContent"]["next_offset"])

    def test_changes_only_the_requested_id_and_preserves_original(self):
        result, = self.rpc(self.edit())
        self.assertFalse(result.get("isError", False))
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.original_text)
        self.document["children"][0]["children"][0]["content"] = "帮助与客服"
        self.assertEqual(json.loads(self.copy.read_text(encoding="utf-8")), self.document)

    def test_rejects_non_text_unknown_and_duplicate_ids(self):
        self.document["children"][0]["children"][1]["id"] = "heading"
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        for result in self.rpc(self.edit("frame"), self.edit("unknown"), self.edit()):
            self.assertTrue(result["isError"])
        self.assertFalse(self.copy.exists())

    def test_rejects_original_and_existing_copy(self):
        self.copy.write_text("existing", encoding="utf-8")
        for result in self.rpc(self.edit(output_path=str(self.source)), self.edit()):
            self.assertTrue(result["isError"])
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.original_text)
        self.assertEqual(self.copy.read_text(encoding="utf-8"), "existing")

    def batch(self, operations, **arguments):
        return self.call("edit_design", operations=operations, output_path=str(self.copy), **arguments)

    def test_inspects_selected_subtree(self):
        result, = self.rpc(self.call("inspect_layers", layer_id="frame", depth=1, limit=2))
        data = result["structuredContent"]
        self.assertEqual([node["id"] for node in data["layers"]], ["frame", "heading"])
        self.assertEqual(data["layers"][1]["properties"]["content"], "帮助与支持")
        self.assertEqual(data["next_offset"], 2)

    def test_layer_reads_cap_large_limits_and_continue_at_next_offset(self):
        root = self.document["children"][0]
        root["children"] = [{"type": "text", "id": f"layer-{i}", "content": "text"} for i in range(204)]
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        expected = [root["id"], *(layer["id"] for layer in root["children"])]

        for tool, arguments in (("inspect_layers", {"layer_id": "frame", "depth": 8}), ("find_layers", {})):
            with self.subTest(tool=tool):
                catalog, *pages = self.rpc(
                    ("tools/list", {}),
                    *(self.call(tool, **arguments, limit=200, offset=offset) for offset in (0, 100, 200)),
                )
                for page in pages:
                    self.assertFalse(page.get("isError"), page)
                records = [page["structuredContent"] for page in pages]
                self.assertEqual([len(page["layers"]) for page in records], [100, 100, 5])
                self.assertEqual([page["next_offset"] for page in records], [100, 200, None])
                self.assertEqual([page["total"] for page in records], [205, 205, 205])
                self.assertEqual([layer["id"] for page in records for layer in page["layers"]], expected)
                schema = next(item["inputSchema"] for item in catalog["tools"] if item["name"] == tool)
                self.assertNotIn("maximum", schema["properties"]["limit"])

    def test_layer_reads_keep_rejecting_invalid_limit_and_offset(self):
        for tool in ("inspect_layers", "find_layers"):
            with self.subTest(tool=tool):
                results = self.rpc(*(self.call(tool, **arguments) for arguments in (
                    {"limit": 0}, {"limit": "200"}, {"limit": True}, {"offset": -1}, {"offset": False},
                )))
                self.assertTrue(all(result.get("isError") for result in results), results)

    def test_inspect_reports_malformed_layer_without_exiting(self):
        self.document["children"][0]["children"].append({"type": "text", "content": "缺少 ID"})
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        result, ping = self.rpc(self.call("inspect_layers", depth=1), ("ping", {}))
        self.assertTrue(result["isError"], result)
        self.assertIn("id", result["content"][0]["text"])
        self.assertEqual(ping, {})

    def test_batch_updates_and_reorganizes_a_design_copy(self):
        result, = self.rpc(self.batch([
            {"op": "update", "id": "frame", "properties": {"layout": "vertical", "gap": 12}},
            {"op": "add", "parent_id": "frame", "layer": {
                "id": "card", "type": "frame", "fill": "#FFFFFF", "padding": [8, 12], "children": [
                    {"id": "caption", "type": "text", "content": "客服", "fontSize": 16},
                ],
            }},
            {"op": "move", "id": "heading", "parent_id": "card", "index": 0},
            {"op": "remove", "id": "other"},
        ]))
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.original_text)
        frame, = json.loads(self.copy.read_bytes())["children"]
        self.assertEqual(frame["gap"], 12)
        card, = frame["children"]
        self.assertEqual([layer["id"] for layer in card["children"]], ["heading", "caption"])

    def test_invalid_batch_leaves_no_copy(self):
        for operations in (
            [{"op": "move", "id": "frame", "parent_id": "frame"}],
            [{"op": "add", "layer": {"type": "text", "id": "heading"}}],
            [{"op": "update", "id": "heading", "properties": {"content": "changed"}},
             {"op": "update", "id": "heading", "properties": {"gap": 12}}],
        ):
            result, = self.rpc(self.batch(operations))
            self.assertTrue(result["isError"], result)
            self.assertFalse(self.copy.exists())

    def test_protects_component_descendant_references(self):
        frame = self.document["children"][0]
        frame["reusable"] = True
        self.document["children"].append({"id": "instance", "type": "ref", "ref": "frame",
                                         "descendants": {"heading": {"content": "override"}}})
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        for layer_id in ("frame", "heading"):
            result, = self.rpc(self.batch([{"op": "remove", "id": layer_id}]))
            self.assertIn("component", result["content"][0]["text"])
            self.assertFalse(self.copy.exists())

    def test_creates_a_new_design_and_refuses_overwrite(self):
        operations = [{"op": "add", "layer": {"type": "frame", "name": "新稿", "width": 375, "height": 812}}]
        first, second = self.rpc(self.batch(operations, file_path=None), self.batch(operations, file_path=None))
        self.assertFalse(first.get("isError", False), first)
        self.assertTrue(second["isError"], second)
        frame, = json.loads(self.copy.read_bytes())["children"]
        self.assertEqual(frame["name"], "新稿")
        self.assertEqual(first["structuredContent"]["changes"][0]["id"], frame["id"])

    def test_duplicate_regenerates_ids_and_keeps_existing_instance_target(self):
        self.document["children"].extend([
            {"id": "component", "type": "frame", "reusable": True},
        ])
        self.document["children"][0]["children"].append({"id": "instance", "type": "ref", "ref": "component"})
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        result, = self.rpc(self.batch([{"op": "duplicate", "id": "frame", "new_id": "variant",
                                        "properties": {"name": "备选"}}]))
        self.assertFalse(result.get("isError", False), result)
        original, _, duplicate = json.loads(self.copy.read_bytes())["children"]
        self.assertEqual(duplicate["id"], "variant")
        self.assertNotEqual(duplicate["children"][0]["id"], original["children"][0]["id"])
        self.assertEqual(duplicate["children"][-1]["ref"], "component")

    def fake_renderer(self, assertions="", wrong_size=False, *, needs_local_assets=True):
        cli = self.source.with_name("renderer.py")
        cli.write_text(f'''
import json, struct, sys, zlib
from pathlib import Path
flags = dict(zip(sys.argv[1::2], sys.argv[2::2]))
source = Path(flags['--in'])
assert not {needs_local_assets!r} or (source.parent / 'assets' / 'proof').exists()
doc = json.loads(source.read_bytes())
board, = doc['children']
assert board['x'] == board['y'] == 0
assert flags['--export-type'] == 'png'
{assertions}
if flags['--export-scale'] != '3':
    scale = int(flags['--export-scale'])
    w, h = int(board['width'] * scale), int(board['height'] * scale)
    if {wrong_size!r}:
        w += 1
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = bytes.fromhex('89504e470d0a1a0a')
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes((w*3+1)*h))) + chunk(b'IEND', b'')
    Path(flags['--export']).write_bytes(png)
''', encoding="utf-8")
        self.env.update(LOCAL_PEN_CLI=str(cli), LOCAL_PEN_NODE=sys.executable)
        assets = self.source.parent / "assets"
        assets.mkdir()
        (assets / "proof").touch()

    def test_returns_png_image_and_exports_without_touching_source(self):
        self.fake_renderer()
        png = self.source.with_name("export.png")
        preview, export = self.rpc(
            self.call("preview_png", layer_id="frame"),
            self.call("export_png", layer_id="frame", output_path=str(png), scale=2),
        )
        self.assertFalse(preview.get("isError"), preview)
        image, = [block for block in preview["content"] if block["type"] == "image"]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(png_size(base64.b64decode(image["data"])), (80, 60))
        self.assertEqual(export["structuredContent"]["width"], 160)
        self.assertEqual(png_size(png.read_bytes()), (160, 120))
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.original_text)
        self.assertEqual(list(self.source.parent.glob('*.pen')), [self.source])

    def test_preview_reuses_png_until_frame_or_local_image_changes(self):
        self.fake_renderer("with (source.parent / 'renders').open('a') as log: log.write('1')")
        image = self.source.parent / "assets" / "logo.png"
        image.write_bytes(b"abc")
        self.document["children"][0]["fill"] = {"type": "image", "url": "assets/logo.png"}
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        draft = self.source.with_name("draft.pen")
        changed = self.source.with_name("changed.pen")
        method, params = self.call("preview_png", layer_id="frame")
        def replace_image(request_id):
            if request_id == 7:
                image.write_bytes(b"xyz")

        before, repeat, _, equivalent, _, edited, refreshed, image_changed = self.rpc(
            (method, params), (method, params),
            self.call("edit_design", output_path=str(draft), operations=[
                {"op": "add", "layer": {"id": "unrelated", "type": "frame"}},
            ]),
            self.call("preview_png", file_path=str(draft), layer_id="frame"),
            self.call("edit_design", file_path=str(draft), output_path=str(changed), operations=[
                {"op": "update", "id": "heading", "properties": {"content": "文字已修改"}},
            ]),
            self.call("preview_png", file_path=str(changed), layer_id="frame"),
            self.call("preview_png", file_path=str(changed), layer_id="frame", refresh=True),
            self.call("preview_png", file_path=str(changed), layer_id="frame"),
            after_reply=replace_image,
        )
        self.assertEqual([result["structuredContent"]["cached"] for result in
                          (before, repeat, equivalent, edited, refreshed, image_changed)],
                         [False, True, True, False, False, False])
        self.assertEqual((self.source.parent / "renders").read_text(), "1111")

    def test_rejects_bad_targets_and_missing_render_without_overwriting(self):
        self.fake_renderer()
        png = self.source.with_name("export.png")
        png.write_bytes(b"existing")
        missing = self.source.with_name("missing.png")
        for result in self.rpc(
            self.call("preview_png", layer_id="heading"),
            self.call("export_png", layer_id="frame", output_path=str(png)),
            self.call("export_png", layer_id="frame", output_path=str(missing), scale=3),
        ):
            self.assertTrue(result["isError"], result)
        self.assertEqual(png.read_bytes(), b"existing")
        self.assertFalse(missing.exists())

    def test_render_returns_error_for_layer_without_type(self):
        del self.document["children"][0]["type"]
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        result, ping = self.rpc(self.call("preview_png", layer_id="frame"), ("ping", {}))
        self.assertTrue(result["isError"], result)
        self.assertIn("type", result["content"][0]["text"])
        self.assertEqual(ping, {})

    def test_renders_cross_board_components_with_nested_refs_and_overrides(self):
        board = self.document["children"][0]
        board["children"].extend([
            {"id": "instance", "type": "ref", "ref": "card", "descendants": {
                "body/label": {"content": "实例覆盖"},
                "replace": {"children": [{"type": "ref", "id": "replacement", "ref": "badge"}]},
            }},
            {"id": "repeat", "type": "ref", "ref": "card"},
            {"id": "nested-instance", "type": "ref", "ref": "nested"},
        ])
        self.document["children"].extend([
            {"id": "library1", "type": "frame", "children": [
                {"id": "card", "type": "frame", "reusable": True, "children": [
                    {"id": "body", "type": "ref", "ref": "label-box"},
                    {"id": "nested", "type": "rectangle", "reusable": True},
                    {"id": "replace", "type": "frame"},
                ]},
                {"id": "unused", "type": "frame", "reusable": True},
            ]},
            {"id": "library2", "type": "frame", "children": [
                {"id": "label-box", "type": "frame", "reusable": True, "children": [
                    {"id": "label", "type": "text", "content": "默认文案"},
                ]},
                {"id": "badge", "type": "rectangle", "reusable": True},
            ]},
        ])
        self.document["variables"] = {"green": {"type": "color", "value": "#00AA55"}}
        self.source.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")
        original = self.source.read_bytes()
        self.fake_renderer('''
assert doc['variables']['green']['value'] == '#00AA55'
instance = next(child for child in board['children'] if child['id'] == 'instance')
assert instance['ref'] == 'card'
assert instance['descendants']['body/label']['content'] == '实例覆盖'
stash = board['children'][-1]
assert stash['enabled'] is False and stash['layoutPosition'] == 'absolute'
definitions = {node['id']: node for node in stash['children']}
assert definitions.keys() == {'card', 'label-box', 'badge'}
assert [node['id'] for node in definitions['card']['children']] == ['body', 'nested', 'replace']
''')
        png = self.source.with_name("shared.png")
        preview, export = self.rpc(
            self.call("preview_png", layer_id="frame"),
            self.call("export_png", layer_id="frame", output_path=str(png)),
        )
        self.assertFalse(preview.get("isError"), preview)
        self.assertTrue(any(block["type"] == "image" for block in preview["content"]))
        self.assertFalse(export.get("isError"), export)
        self.assertEqual(png_size(png.read_bytes()), (160, 120))
        self.assertEqual(self.source.read_bytes(), original)

    def test_batch_export_keeps_order_and_uses_distinct_new_files(self):
        self.document["children"].append({
            "id": "second", "type": "frame", "name": "账号与设置", "width": 80, "height": 60,
        })
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        original = self.source.read_bytes()
        self.fake_renderer("assert board['id'] in ('frame', 'second')")
        destination = self.source.with_name("export")
        result, = self.rpc(self.call("export_png_batch", layer_ids=["second", "frame"],
                                     output_dir=str(destination)))
        self.assertFalse(result.get("isError"), result)
        exports = result["structuredContent"]["exports"]
        self.assertEqual([item["layer_id"] for item in exports], ["second", "frame"])
        paths = [Path(item["output_path"]) for item in exports]
        self.assertEqual(set(destination.iterdir()), set(paths))
        self.assertEqual(len(set(paths)), 2)
        for path in paths:
            self.assertEqual(png_size(path.read_bytes()), (160, 120))
        self.assertEqual(self.source.read_bytes(), original)

    def test_batch_export_does_not_publish_after_render_failure(self):
        self.document["children"].append({
            "id": "second", "type": "frame", "name": "第二页", "width": 80, "height": 60,
        })
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        self.fake_renderer("if board['id'] == 'second': sys.exit(0)")
        destination = self.source.with_name("export")
        result, = self.rpc(self.call("export_png_batch", layer_ids=["frame", "second"],
                                     output_dir=str(destination)))
        self.assertTrue(result["isError"], result)
        self.assertIn("second", result["content"][0]["text"])
        self.assertFalse(destination.exists())

    def test_batch_export_rejects_duplicate_ids_and_existing_directory(self):
        destination = self.source.with_name("existing")
        destination.mkdir()
        existing = destination / "keep.png"
        existing.write_bytes(b"existing")
        unused = self.source.with_name("unused")
        duplicate, occupied = self.rpc(
            self.call("export_png_batch", layer_ids=["frame", "frame"], output_dir=str(unused)),
            self.call("export_png_batch", layer_ids=["frame"], output_dir=str(destination)),
        )
        self.assertIn("duplicate", duplicate["content"][0]["text"])
        self.assertIn("already exists", occupied["content"][0]["text"])
        self.assertFalse(unused.exists())
        self.assertEqual(list(destination.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"existing")

    def test_copy_to_new_folder_rebases_assets_and_library(self):
        assets = self.source.parent / "assets"
        assets.mkdir()
        image = assets / "logo.png"
        library = assets / "ui.lib.pen"
        image.write_bytes(b"asset")
        library.write_text('{"version":"2.17","children":[]}', encoding="utf-8")
        self.document["imports"] = {"ui": "assets/ui.lib.pen"}
        self.document["children"][0]["fill"] = {"type": "image", "url": "assets/logo.png"}
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        target = self.source.parent / "delivery"
        target.mkdir()
        draft = target / "design.pen"
        text_copy = target / "text.pen"
        result, edited_text = self.rpc(self.call("edit_design", output_path=str(draft), operations=[
            {"op": "update", "id": "frame", "properties": {"name": "新设计"}},
        ]), self.edit(output_path=str(text_copy)))
        for path, response in ((draft, result), (text_copy, edited_text)):
            self.assertFalse(response.get("isError"), response)
            copied = json.loads(path.read_bytes())
            self.assertEqual((target / copied["imports"]["ui"]).resolve(), library.resolve())
            self.assertEqual((target / copied["children"][0]["fill"]["url"]).read_bytes(), b"asset")

    def test_creates_component_then_overrides_and_duplicates_an_instance(self):
        result, = self.rpc(self.batch([
            {"op": "add", "layer": {"id": "component", "type": "frame", "reusable": True, "children": [
                {"id": "caption", "type": "text", "content": "默认文案"},
            ]}},
            {"op": "add", "parent_id": "frame", "layer": {"id": "instance", "type": "ref", "ref": "component"}},
            {"op": "override", "id": "instance", "path": "caption", "properties": {"content": "实例文字"}},
            {"op": "duplicate", "id": "frame", "new_id": "variant"},
        ]))
        self.assertFalse(result.get("isError"), result)
        frame, definition, copied = json.loads(self.copy.read_bytes())["children"]
        instance = frame["children"][-1]
        duplicate = copied["children"][-1]
        self.assertNotEqual(instance["id"], duplicate["id"])
        self.assertEqual(duplicate["ref"], definition["id"])
        self.assertEqual(duplicate["descendants"], {"caption": {"content": "实例文字"}})
        self.assertEqual(definition["children"][0]["content"], "默认文案")
        inspected, = self.rpc(self.call("inspect_layers", file_path=str(self.copy), layer_id=duplicate["id"]))
        self.assertEqual(inspected["structuredContent"]["layers"][0]["properties"]["descendants"], duplicate["descendants"])

    def test_export_clips_a_frame_and_rejects_mismatched_png_dimensions(self):
        self.fake_renderer("assert board['clip'] is True", wrong_size=True)
        png = self.source.with_name("wrong.png")
        result, = self.rpc(self.call("export_png", layer_id="frame", output_path=str(png)))
        self.assertTrue(result.get("isError"), result)
        self.assertIn("161", result["content"][0]["text"])
        self.assertFalse(png.exists())

    def test_rejects_overrides_on_replaced_descendants(self):
        self.document["children"].extend([
            {"id": "button", "type": "frame", "reusable": True, "children": [
                {"id": "container", "type": "frame", "children": [
                    {"id": "label", "type": "text", "content": "默认文字"},
                ]},
            ]},
            {"id": "wrapper", "type": "frame", "reusable": True, "children": [
                {"id": "nested", "type": "ref", "ref": "button", "descendants": {"container": {"children": []}}},
            ]},
            {"id": "instance", "type": "ref", "ref": "wrapper"},
        ])
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        result, = self.rpc(self.batch([
            {"op": "override", "id": "instance", "path": "nested/label", "properties": {"content": "不存在"}},
        ]))
        self.assertTrue(result["isError"], result)
        self.assertIn("structural", result["content"][0]["text"])
        self.assertFalse(self.copy.exists())

    def test_ping_and_cancel_during_batch_export(self):
        self.fake_renderer("(source.parent / 'rendering').touch(); __import__('time').sleep(2)")
        destination = self.source.with_name("cancelled")
        with subprocess.Popen([sys.executable, "-B", str(SERVER)], bufsize=0, env=self.env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
            def send(**message):
                process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())

            try:
                method, params = self.call("export_png_batch", layer_ids=["frame"], output_dir=str(destination))
                send(id=1, method=method, params=params)
                deadline = time.monotonic() + 2
                while not self.source.with_name("rendering").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.source.with_name("rendering").exists())
                send(id=2, method="ping")
                self.assertTrue(select.select([process.stdout], [], [], 1)[0], "ping waits for renderer")
                self.assertEqual(json.loads(process.stdout.readline())["id"], 2)
                send(method="notifications/cancelled", params={"requestId": 1})
                process.stdin.close()
                self.assertEqual(process.wait(timeout=1), 0, "cancel did not stop the renderer")
                self.assertEqual(process.stdout.read(), b"")
                self.assertFalse(destination.exists())
                self.assertFalse(list(self.source.parent.glob(".local-pen-*")))
            finally:
                if process.poll() is None:
                    process.kill()

    def test_disconnect_cancels_pending_exports(self):
        self.fake_renderer("(source.parent / 'rendering').touch(); __import__('time').sleep(2)")
        destination = self.source.with_name("disconnect")
        with subprocess.Popen([sys.executable, "-B", str(SERVER)], bufsize=0, env=self.env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
            try:
                method, params = self.call("export_png_batch", layer_ids=["frame"], output_dir=str(destination))
                request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                process.stdin.write((json.dumps(request) + "\n").encode())
                deadline = time.monotonic() + 2
                while not self.source.with_name("rendering").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.source.with_name("rendering").exists())
                process.stdin.close()
                self.assertEqual(process.wait(timeout=1), 0)
                self.assertFalse(destination.exists())
            finally:
                if process.poll() is None:
                    process.kill()


if __name__ == "__main__":
    unittest.main()
