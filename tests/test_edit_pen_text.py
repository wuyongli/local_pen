import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "edit_pen_text.py"


class EditPenTextTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "source.pen"
        self.destination = Path(directory.name) / "copy.pen"

    def run_script(self, document):
        source_text = json.dumps(document, ensure_ascii=False)
        self.source.write_text(source_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), str(self.destination), "原文", "新文"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(self.source.read_text(encoding="utf-8"), source_text)
        return result

    def test_edits_only_the_matching_text_layer(self):
        document = {"version": "2.17", "children": [{
            "type": "frame", "id": "frame", "name": "原文", "children": [
                {"type": "text", "id": "heading", "content": "原文", "fontSize": 20}
            ],
        }]}
        result = self.run_script(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        document["children"][0]["children"][0]["content"] = "新文"
        self.assertEqual(json.loads(self.destination.read_text()), document)

    def test_refuses_ambiguous_text(self):
        document = {"version": "2.17", "children": [
            {"type": "text", "id": "a", "content": "原文"},
            {"type": "text", "id": "b", "content": "原文"},
        ]}
        result = self.run_script(document)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("found 2", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_refuses_to_overwrite_existing_file(self):
        self.destination.write_text("existing")
        document = {"version": "2.17", "children": [
            {"type": "text", "id": "a", "content": "原文"},
        ]}
        result = self.run_script(document)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.destination.read_text(), "existing")


if __name__ == "__main__":
    unittest.main()
