"""Regression tests for findings from an independent adversarial review:
strict RFC 3339 parsing, id verification, path confinement, invalid
UTF-8 handling, and the Stop hook's stop_hook_active parsing."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from provtrail.hashing import record_hash  # noqa: E402
from provtrail.ledger import STATE_UNKNOWN, Ledger, parse_rfc3339_utc  # noqa: E402

_HOOK = os.path.join(_ROOT, "integrations", "claude-code", "stop_hook.py")


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    for key in ("PROVTRAIL_LEDGER", "PROVTRAIL_MODE", "PROVTRAIL_ENFORCE"):
        env.pop(key, None)
    return env


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.dir)
        self.ledger_path = os.path.join(self.dir, "ledger.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def write_signed(self, **fields):
        """Append a record with a correct record_hash and id but arbitrary fields."""
        body = {"schema": "provtrail/v1", "seq": 1, "captured_at": "2025-01-01T00:00:00Z",
                "kind": "file", "prev_hash": None}
        body.update(fields)
        rh = record_hash(body)
        rec = dict(body, id="ev_" + rh.split(":", 1)[1][:16], record_hash=rh)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


class TestRfc3339(unittest.TestCase):
    def test_rejects_non_rfc3339_forms(self):
        for ts in ("2026-01-01 00:00:00Z", "2026-01-01T00:00:00+00:00:30",
                   "2026-01-01T00:00:00,5Z", "2026-01-01T00:00:00", " 2026-01-01T00:00:00Z",
                   "2026-01-01T00:00:00+24:00", "2026-02-30T00:00:00Z"):
            with self.subTest(ts=ts):
                with self.assertRaises(ValueError):
                    parse_rfc3339_utc(ts)

    def test_accepts_rfc3339_forms(self):
        for ts in ("2026-01-01T00:00:00Z", "2026-01-01t00:00:00z", "2026-01-01T00:00:00.1Z",
                   "2026-01-01T00:00:00.1234567+02:00", "2026-01-01T00:00:00-05:30"):
            with self.subTest(ts=ts):
                self.assertIsNotNone(parse_rfc3339_utc(ts).tzinfo)

    def test_leap_second_is_accepted_as_59(self):
        dt = parse_rfc3339_utc("2016-12-31T23:59:60Z")
        self.assertEqual((dt.minute, dt.second), (59, 59))

    def test_offset_is_applied(self):
        a = parse_rfc3339_utc("2026-01-01T02:00:00+02:00")
        b = parse_rfc3339_utc("2026-01-01T00:00:00Z")
        self.assertEqual(a, b)


class TestIdVerification(TmpCase):
    def test_forged_id_is_reported(self):
        Ledger(self.ledger_path).add(source_url="https://example.com")
        with open(self.ledger_path, encoding="utf-8") as f:
            rec = json.loads(f.read())
        rec["id"] = "ev_0000000000000000"
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        codes = [v["code"] for v in Ledger(self.ledger_path).verify().violations]
        self.assertEqual(codes, ["ID_MISMATCH"])


class TestPathConfinement(TmpCase):
    def test_add_rejects_escaping_and_absolute_paths(self):
        for bad in ("../outside.txt", "a/../../outside.txt", os.path.abspath(self.dir), ""):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    Ledger(self.ledger_path).add(source_url="https://example.com", path=bad)

    def test_add_accepts_nested_relative_path(self):
        rec = Ledger(self.ledger_path).add(source_url="https://example.com", path="sub/../file.txt")
        self.assertEqual(rec["path"], "sub/../file.txt")

    def test_verify_reports_escaping_path_and_does_not_read_it(self):
        outside = os.path.join(self._tmp.name, "outside.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("outside")
        from provtrail.hashing import sha256_of_file
        self.write_signed(content_hash=sha256_of_file(outside), path="../outside.txt")
        codes = [v["code"] for v in Ledger(self.ledger_path).verify(check_files=True).violations]
        self.assertEqual(codes, ["INVALID_PATH"])
        # Without --check-files the lexical check still reports it.
        codes = [v["code"] for v in Ledger(self.ledger_path).verify(check_files=False).violations]
        self.assertEqual(codes, ["INVALID_PATH"])

    def test_verify_rejects_symlink_escape_with_check_files(self):
        outside = os.path.join(self._tmp.name, "outside.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("outside")
        link = os.path.join(self.dir, "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available on this platform/account")
        from provtrail.hashing import sha256_of_file
        self.write_signed(content_hash=sha256_of_file(outside), path="link.txt")
        codes = [v["code"] for v in Ledger(self.ledger_path).verify(check_files=True).violations]
        self.assertEqual(codes, ["INVALID_PATH"])


class TestInvalidUtf8(TmpCase):
    def setUp(self):
        super().setUp()
        with open(self.ledger_path, "wb") as f:
            f.write(b'{"a": "\xff\xfe"}\n')

    def test_verify_reports_invalid_json(self):
        report = Ledger(self.ledger_path).verify()
        self.assertFalse(report.ok)
        self.assertEqual([v["code"] for v in report.violations], ["INVALID_JSON"])

    def test_check_is_unknown(self):
        self.assertEqual(Ledger(self.ledger_path).check().state, STATE_UNKNOWN)

    def test_cli_verify_and_add_fail_cleanly(self):
        for args in (["verify", self.ledger_path], ["add", self.ledger_path, "--url", "https://x"]):
            with self.subTest(cmd=args[0]):
                p = subprocess.run([sys.executable, "-m", "provtrail"] + args,
                                   capture_output=True, text=True, env=_env(), timeout=30)
                self.assertEqual(p.returncode, 1)
                self.assertNotIn("Traceback", p.stderr)


class TestStopHookActiveParsing(TmpCase):
    def run_hook(self, active):
        with open(os.path.join(self.dir, ".provtrail.json"), "w", encoding="utf-8") as f:
            json.dump({"ledger": "./ledger.jsonl", "mode": "enforce"}, f)
        transcript = os.path.join(self.dir, "t.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2020-01-01T00:00:00Z"}) + "\n")
        payload = {"session_id": "S", "transcript_path": transcript, "cwd": self.dir,
                   "stop_hook_active": active}
        p = subprocess.run([sys.executable, _HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=_env(), timeout=30)
        return json.loads(p.stdout)

    def test_string_false_does_not_suppress_block(self):
        self.assertEqual(self.run_hook("false").get("decision"), "block")

    def test_true_values_suppress_block(self):
        for active in (True, "true"):
            with self.subTest(active=active):
                self.assertNotEqual(self.run_hook(active).get("decision"), "block")


class TestPackagedStopHook(TmpCase):
    def test_module_entry_point_blocks_like_the_script(self):
        with open(os.path.join(self.dir, ".provtrail.json"), "w", encoding="utf-8") as f:
            json.dump({"ledger": "./ledger.jsonl", "mode": "enforce"}, f)
        transcript = os.path.join(self.dir, "t.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2020-01-01T00:00:00Z"}) + "\n")
        payload = {"session_id": "S", "transcript_path": transcript, "cwd": self.dir,
                   "stop_hook_active": False}
        p = subprocess.run([sys.executable, "-m", "provtrail.stop_hook"], input=json.dumps(payload),
                           capture_output=True, text=True, env=_env(), timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout).get("decision"), "block")


if __name__ == "__main__":
    unittest.main()
