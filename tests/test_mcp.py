"""Tests for the provtrail MCP server tool functions.

Requires the 'mcp' package (the [mcp] extra, Python 3.10+); skipped
entirely when it is not importable.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_ROOT, "src")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    import mcp  # noqa: F401
    _HAVE_MCP = True
except ImportError:
    _HAVE_MCP = False


@unittest.skipUnless(_HAVE_MCP, "the 'mcp' package is not installed")
class McpToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.ledger_path = os.path.join(self.tmpdir, "ledger.jsonl")

        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        with open(os.path.join(self.tmpdir, ".provtrail.json"), "w", encoding="utf-8") as f:
            json.dump({"ledger": "./ledger.jsonl"}, f)

        sys.modules.pop("provtrail.mcp_server", None)
        import provtrail.mcp_server

        self.mod = provtrail.mcp_server

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmpdir.cleanup()

    def test_add_with_url_succeeds(self):
        result = self.mod.provtrail_add(source_url="https://example.com/a")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["record"]["source_url"], "https://example.com/a")

    def test_no_ledger_configured_returns_error(self):
        os.remove(os.path.join(self.tmpdir, ".provtrail.json"))
        result = self.mod.provtrail_add(source_url="https://example.com/a")
        self.assertFalse(result["ok"])

    def test_session_id_defaults_from_env(self):
        old = os.environ.get("CLAUDE_CODE_SESSION_ID")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-env"
        try:
            result = self.mod.provtrail_add(source_url="https://example.com/a")
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = old
        self.assertEqual(result["record"].get("session_id"), "sess-env")

    def test_explicit_session_id_overrides_env(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-env"
        try:
            result = self.mod.provtrail_add(
                source_url="https://example.com/a", session_id="sess-explicit"
            )
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual(result["record"].get("session_id"), "sess-explicit")

    def test_content_path_outside_ledger_dir_is_rejected(self):
        outside_dir = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(outside_dir, "secret.txt")
            with open(outside_file, "w", encoding="utf-8") as f:
                f.write("outside content")
            result = self.mod.provtrail_add(content_path=outside_file)
            self.assertFalse(result["ok"])
            self.assertIn("ledger", result["error"])
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_content_path_inside_ledger_dir_succeeds(self):
        inside_file = os.path.join(self.tmpdir, "note.txt")
        with open(inside_file, "w", encoding="utf-8") as f:
            f.write("note content")
        result = self.mod.provtrail_add(content_path=inside_file, kind="file")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["record"]["content_hash"].startswith("sha256:"))

    def test_content_and_content_path_together_rejected(self):
        inside_file = os.path.join(self.tmpdir, "note.txt")
        with open(inside_file, "w", encoding="utf-8") as f:
            f.write("note content")
        result = self.mod.provtrail_add(content="text", content_path=inside_file)
        self.assertFalse(result["ok"])

    def test_verify_reports_ok(self):
        self.mod.provtrail_add(source_url="https://example.com/a")
        report = self.mod.provtrail_verify()
        self.assertTrue(report["ok"], report)

    def test_verify_no_ledger_configured_returns_error(self):
        os.remove(os.path.join(self.tmpdir, ".provtrail.json"))
        report = self.mod.provtrail_verify()
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
