"""Tests for provtrail.cli: add/verify/check round-trips and exit codes."""

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from provtrail.cli import main  # noqa: E402
from provtrail.ledger import Ledger  # noqa: E402


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.ledger_path = os.path.join(self.tmpdir, "ledger.jsonl")

    def tearDown(self):
        self._tmpdir.cleanup()

    def run_cli(self, args):
        """Run main() capturing stdout/stderr, return (exit_code, stdout, stderr)."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()


class TestAdd(CliTestCase):
    def test_add_with_url_succeeds(self):
        code, out, _ = self.run_cli(["add", self.ledger_path, "--url", "https://example.com"])
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(record["source_url"], "https://example.com")

    def test_add_with_content_text_succeeds(self):
        code, out, _ = self.run_cli(
            ["add", self.ledger_path, "--content-text", "hello"]
        )
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertTrue(record["content_hash"].startswith("sha256:"))

    def test_add_without_locator_fails_with_exit_1(self):
        code, out, err = self.run_cli(["add", self.ledger_path])
        self.assertEqual(code, 1)
        self.assertIn("rejected", err)

    def test_add_to_missing_parent_fails_cleanly(self):
        missing_ledger = os.path.join(self.tmpdir, "missing", "ledger.jsonl")
        code, _out, err = self.run_cli(
            ["add", missing_ledger, "--url", "https://example.com"]
        )
        self.assertEqual(code, 1)
        self.assertIn("rejected", err)


class TestVerify(CliTestCase):
    def test_verify_ok_round_trip(self):
        self.run_cli(["add", self.ledger_path, "--url", "https://example.com/1"])
        self.run_cli(["add", self.ledger_path, "--url", "https://example.com/2"])
        code, out, _ = self.run_cli(["verify", self.ledger_path])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_verify_json_output_parses(self):
        self.run_cli(["add", self.ledger_path, "--url", "https://example.com/1"])
        code, out, _ = self.run_cli(["verify", self.ledger_path, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["record_count"], 1)

    def test_verify_detects_tampering_exit_1(self):
        self.run_cli(["add", self.ledger_path, "--url", "https://example.com/1"])
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            rec = json.loads(f.readline())
        rec["title"] = "tampered"
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

        code, out, _ = self.run_cli(["verify", self.ledger_path, "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertTrue(data["violations"])


class TestAddSessionId(CliTestCase):
    def setUp(self):
        super().setUp()
        self._old_session_env = os.environ.get("CLAUDE_CODE_SESSION_ID")

    def tearDown(self):
        if self._old_session_env is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self._old_session_env
        super().tearDown()

    def test_add_picks_up_claude_code_session_id_env(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-xyz"
        code, out, _ = self.run_cli(["add", self.ledger_path, "--url", "https://example.com"])
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(record.get("session_id"), "sess-xyz")

    def test_add_explicit_session_id_overrides_env(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-env"
        code, out, _ = self.run_cli(
            ["add", self.ledger_path, "--url", "https://example.com", "--session-id", "sess-explicit"]
        )
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(record.get("session_id"), "sess-explicit")


class TestCheckSessionAndUntil(CliTestCase):
    def test_check_session_id_filters_out_other_sessions(self):
        Ledger(self.ledger_path).add(source_url="https://example.com", session_id="session-B")
        code, out, _ = self.run_cli(["check", self.ledger_path, "--session-id", "session-A", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "MISSING")

    def test_check_until_excludes_future_dated_record(self):
        Ledger(self.ledger_path).add(
            source_url="https://example.com", captured_at="2099-01-01T00:00:00Z"
        )
        code, out, _ = self.run_cli(
            ["check", self.ledger_path, "--until", "2030-01-01T00:00:00Z", "--json"]
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "MISSING")


class TestCheck(CliTestCase):
    def test_check_missing_exits_0_by_default(self):
        code, out, _ = self.run_cli(["check", self.ledger_path])
        self.assertEqual(code, 0)
        self.assertIn("UNKNOWN", out)  # no ledger file yet

    def test_check_missing_state_exits_0_without_enforce(self):
        Ledger(self.ledger_path).add(
            source_url="https://example.com", captured_at="2020-01-01T00:00:00Z"
        )
        code, out, _ = self.run_cli(
            ["check", self.ledger_path, "--since", "2030-01-01T00:00:00Z"]
        )
        self.assertEqual(code, 0)
        self.assertIn("MISSING", out)

    def test_check_missing_state_exits_2_with_enforce(self):
        Ledger(self.ledger_path).add(
            source_url="https://example.com", captured_at="2020-01-01T00:00:00Z"
        )
        code, _, _ = self.run_cli(
            ["check", self.ledger_path, "--since", "2030-01-01T00:00:00Z", "--enforce"]
        )
        self.assertEqual(code, 2)

    def test_check_unknown_never_exits_2_with_enforce(self):
        code, out, _ = self.run_cli(["check", self.ledger_path, "--enforce"])
        self.assertEqual(code, 0)
        self.assertIn("UNKNOWN", out)

    def test_check_json_output_parses(self):
        Ledger(self.ledger_path).add(source_url="https://example.com")
        code, out, _ = self.run_cli(["check", self.ledger_path, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "PRESENT")


if __name__ == "__main__":
    unittest.main()
