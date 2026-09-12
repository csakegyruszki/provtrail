"""Tests for the Claude Code Stop hook, run as a real subprocess."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_ROOT, "src")
_HOOK = os.path.join(_ROOT, "integrations", "claude-code", "stop_hook.py")


def write_transcript(path, timestamp=None):
    lines = []
    if timestamp is not None:
        lines.append(json.dumps({"role": "user", "timestamp": timestamp}))
    lines.append(json.dumps({"role": "assistant", "text": "hello"}))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_entries(path, entries):
    """Write a synthetic transcript from raw entry dicts, one per line."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def human_prompt(timestamp, text="hello"):
    """A real human-prompt transcript entry, per the measured shape."""
    return {"type": "user", "timestamp": timestamp, "message": {"role": "user", "content": text}}


def tool_result_entry(timestamp):
    """A tool-result entry: also type=='user', but must not open a turn."""
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        "toolUseResult": {"stdout": "ok"},
    }


def stop_hook_feedback_entry(timestamp, text="provtrail: source ledger state is MISSING"):
    """A Stop-hook feedback entry: also type=='user', must not open a turn."""
    return {
        "type": "user",
        "timestamp": timestamp,
        "isMeta": True,
        "message": {"role": "user", "content": text},
    }


def run_hook(payload, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, _HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=cwd,
    )
    return proc


class StopHookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.ledger_path = os.path.join(self.tmpdir, "ledger.jsonl")
        self.transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")

    def tearDown(self):
        self._tmpdir.cleanup()

    def add_config(self, enforce=False):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "enforce": enforce}, f)

    def add_record(self, captured_at):
        sys.path.insert(0, _SRC)
        from provtrail.ledger import Ledger

        Ledger(self.ledger_path).add(source_url="https://example.com", captured_at=captured_at)


class TestNoConfig(StopHookTestCase):
    def test_no_config_no_output(self):
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class TestReportOnly(StopHookTestCase):
    def test_missing_gives_system_message_exit_0(self):
        self.add_config(enforce=False)
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, "2030-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("systemMessage", data)
        self.assertIn("MISSING", data["systemMessage"])


class TestEnforce(StopHookTestCase):
    def test_enforce_missing_blocks(self):
        self.add_config(enforce=True)
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, "2030-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("decision"), "block")

    def test_enforce_missing_with_stop_hook_active_does_not_block(self):
        self.add_config(enforce=True)
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, "2030-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": True}
        )
        self.assertEqual(proc.returncode, 0)
        if proc.stdout.strip():
            data = json.loads(proc.stdout)
            self.assertNotEqual(data.get("decision"), "block")

    def test_enforce_present_does_not_block(self):
        self.add_config(enforce=True)
        self.add_record(captured_at="2025-06-01T00:00:00Z")
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        if proc.stdout.strip():
            data = json.loads(proc.stdout)
            self.assertNotEqual(data.get("decision"), "block")

    def test_unreadable_transcript_does_not_block(self):
        self.add_config(enforce=True)
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        missing_transcript = os.path.join(self.tmpdir, "does-not-exist.jsonl")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": missing_transcript, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertNotEqual(data.get("decision"), "block")
        self.assertIn("UNKNOWN", data["systemMessage"])

    def test_absent_ledger_in_existing_dir_blocks(self):
        self.add_config(enforce=True)
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")

    def test_absent_ledger_dir_is_unknown_and_does_not_block(self):
        self.ledger_path = os.path.join(self.tmpdir, "no-such-dir", "ledger.jsonl")
        self.add_config(enforce=True)
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        data = json.loads(proc.stdout)
        self.assertNotEqual(data.get("decision"), "block")
        self.assertIn("UNKNOWN", data["systemMessage"])

    def test_relative_ledger_path_resolves_against_project_cwd(self):
        # The hook process runs in a different directory than the project.
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        with open(os.path.join(self.tmpdir, ".provtrail.json"), "w", encoding="utf-8") as f:
            json.dump({"ledger": "./ledger.jsonl", "enforce": True}, f)
        write_transcript(self.transcript_path, "2030-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as elsewhere:
            proc = run_hook(
                {"cwd": self.tmpdir, "transcript_path": self.transcript_path,
                 "stop_hook_active": False},
                cwd=elsewhere,
            )
        # Blocking proves the ledger was found and read as MISSING-since;
        # an unresolved path would yield UNKNOWN and no block.
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")


class TestSessionScoping(StopHookTestCase):
    def add_config_mode(self, mode):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": mode}, f)

    def add_record_session(self, captured_at, session_id):
        sys.path.insert(0, _SRC)
        from provtrail.ledger import Ledger

        Ledger(self.ledger_path).add(
            source_url="https://example.com", captured_at=captured_at, session_id=session_id
        )

    def test_cross_session_record_is_missing_and_blocks_in_enforce(self):
        self.add_config_mode("enforce")
        self.add_record_session("2025-06-01T00:00:00Z", session_id="session-B")
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {
                "cwd": self.tmpdir,
                "transcript_path": self.transcript_path,
                "stop_hook_active": False,
                "session_id": "session-A",
            }
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")

    def test_matching_session_is_present_and_does_not_block(self):
        self.add_config_mode("enforce")
        self.add_record_session("2025-06-01T00:00:00Z", session_id="session-A")
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {
                "cwd": self.tmpdir,
                "transcript_path": self.transcript_path,
                "stop_hook_active": False,
                "session_id": "session-A",
            }
        )
        self.assertEqual(proc.returncode, 0)
        if proc.stdout.strip():
            self.assertNotEqual(json.loads(proc.stdout).get("decision"), "block")

    def test_future_dated_record_is_not_counted_and_blocks(self):
        self.add_config_mode("enforce")
        self.add_record_session("2099-01-01T00:00:00Z", session_id="session-A")
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {
                "cwd": self.tmpdir,
                "transcript_path": self.transcript_path,
                "stop_hook_active": False,
                "session_id": "session-A",
            }
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")


class TestNoScopeIsUnknown(StopHookTestCase):
    def add_config_mode(self, mode):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": mode}, f)

    def test_no_timestamp_no_session_id_is_unknown_and_does_not_block_in_enforce(self):
        # Regression: the old hook called check(since=None), which
        # reports PRESENT for any old record when there is no way to
        # scope the check to this session at all.
        self.add_config_mode("enforce")
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, timestamp=None)
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertNotEqual(data.get("decision"), "block")
        self.assertIn("UNKNOWN", data["systemMessage"])

    def test_no_timestamp_no_session_id_blocks_in_strict(self):
        self.add_config_mode("strict")
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, timestamp=None)
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")

    def test_strict_does_not_block_when_stop_hook_active(self):
        self.add_config_mode("strict")
        write_transcript(self.transcript_path, timestamp=None)
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": True}
        )
        self.assertEqual(proc.returncode, 0)
        if proc.stdout.strip():
            self.assertNotEqual(json.loads(proc.stdout).get("decision"), "block")


class TestEnvLedgerKeepsConfigMode(StopHookTestCase):
    def test_env_ledger_alone_keeps_config_mode_enforce(self):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": "enforce"}, f)
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        # No record exists -> MISSING; PROVTRAIL_LEDGER alone must not
        # silently drop the configured mode back to report.
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False},
            env_extra={"PROVTRAIL_LEDGER": self.ledger_path},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout).get("decision"), "block")


class TestInvalidConfig(StopHookTestCase):
    def test_invalid_json_config_gives_system_message_not_silence(self):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.strip(), "expected a systemMessage, got silent output")
        data = json.loads(proc.stdout)
        self.assertIn("systemMessage", data)
        self.assertNotEqual(data.get("decision"), "block")


class TestEnvConfig(StopHookTestCase):
    def test_env_ledger_variable_is_used(self):
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_transcript(self.transcript_path, "2030-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False},
            env_extra={"PROVTRAIL_LEDGER": self.ledger_path},
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("systemMessage", data)


class TestTurnScope(StopHookTestCase):
    def add_config_scope(self, scope, mode="report"):
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": mode, "scope": scope}, f)

    def test_turn_scope_opens_at_last_prompt_not_first(self):
        # Session scope would open the window at the FIRST prompt (T1),
        # which would include the record captured between the two
        # prompts. Turn scope must open at the LAST prompt (T2) instead,
        # excluding that same record.
        self.add_config_scope("turn")
        self.add_record(captured_at="2025-01-01T00:00:30Z")
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                human_prompt("2025-01-01T00:01:00Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("systemMessage", data)
        self.assertIn("MISSING", data["systemMessage"])

    def test_session_scope_same_transcript_is_present(self):
        # Control for the test above: under the default session scope
        # (no "scope" key at all), the same transcript and record must
        # report PRESENT rather than MISSING.
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": "report"}, f)
        self.add_record(captured_at="2025-01-01T00:00:30Z")
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                human_prompt("2025-01-01T00:01:00Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")  # PRESENT -> no systemMessage

    def test_turn_scope_present_when_record_after_last_prompt(self):
        self.add_config_scope("turn")
        self.add_record(captured_at="2025-01-01T00:01:10Z")
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                human_prompt("2025-01-01T00:01:00Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")  # PRESENT -> no systemMessage

    def test_stop_hook_feedback_entry_is_not_mistaken_for_the_prompt(self):
        # The feedback entry is timestamped AFTER the real prompt and
        # after the record. If it were mistaken for a human prompt, the
        # window would open too late and the record would be excluded.
        self.add_config_scope("turn")
        self.add_record(captured_at="2025-01-01T00:00:10Z")
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                stop_hook_feedback_entry("2025-01-01T00:00:20Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")  # PRESENT -> no systemMessage

    def test_tool_result_entry_is_not_mistaken_for_the_prompt(self):
        # Same idea with a tool-result entry, which also has type=="user".
        self.add_config_scope("turn")
        self.add_record(captured_at="2025-01-01T00:00:10Z")
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                tool_result_entry("2025-01-01T00:00:20Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")  # PRESENT -> no systemMessage

    def test_turn_scope_no_qualifying_entry_is_unknown_even_with_session_id(self):
        # Turn scope must never fall back to session scope's behaviour:
        # even with a session_id present, no qualifying prompt means
        # UNKNOWN, not a session-id-only check.
        self.add_config_scope("turn", mode="enforce")
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        write_entries(
            self.transcript_path,
            [
                tool_result_entry("2025-01-01T00:00:00Z"),
                stop_hook_feedback_entry("2025-01-01T00:00:10Z"),
            ],
        )
        proc = run_hook(
            {
                "cwd": self.tmpdir,
                "transcript_path": self.transcript_path,
                "stop_hook_active": False,
                "session_id": "session-A",
            }
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertNotEqual(data.get("decision"), "block")
        self.assertIn("UNKNOWN", data["systemMessage"])

    def test_turn_scope_missing_transcript_is_unknown(self):
        self.add_config_scope("turn", mode="enforce")
        self.add_record(captured_at="2020-01-01T00:00:00Z")
        missing_transcript = os.path.join(self.tmpdir, "does-not-exist.jsonl")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": missing_transcript, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertNotEqual(data.get("decision"), "block")
        self.assertIn("UNKNOWN", data["systemMessage"])

    def test_env_scope_turn_overrides_config_session(self):
        self.add_record(captured_at="2025-01-01T00:00:30Z")
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "mode": "report", "scope": "session"}, f)
        write_entries(
            self.transcript_path,
            [
                human_prompt("2025-01-01T00:00:00Z"),
                human_prompt("2025-01-01T00:01:00Z"),
            ],
        )
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False},
            env_extra={"PROVTRAIL_SCOPE": "turn"},
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("MISSING", data["systemMessage"])

    def test_invalid_scope_value_is_reported_not_silent(self):
        self.add_record(captured_at="2025-01-01T00:00:00Z")
        config_path = os.path.join(self.tmpdir, ".provtrail.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger_path, "scope": "bogus"}, f)
        write_transcript(self.transcript_path, "2025-01-01T00:00:00Z")
        proc = run_hook(
            {"cwd": self.tmpdir, "transcript_path": self.transcript_path, "stop_hook_active": False}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.strip(), "expected a systemMessage, got silent output")
        data = json.loads(proc.stdout)
        self.assertIn("systemMessage", data)
        self.assertNotEqual(data.get("decision"), "block")


if __name__ == "__main__":
    unittest.main()


class TestFutureBound(unittest.TestCase):
    def test_until_is_not_shorter_than_the_stated_window(self):
        from datetime import datetime, timedelta, timezone

        src = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
        if src not in sys.path:
            sys.path.insert(0, src)
        from provtrail.stop_hook import _until_str

        before = datetime.now(timezone.utc)
        until = datetime.fromisoformat(_until_str(300).replace("Z", "+00:00"))
        self.assertGreaterEqual(until, before + timedelta(seconds=300))
