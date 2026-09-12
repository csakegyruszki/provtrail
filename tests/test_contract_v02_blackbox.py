"""Black-box contract tests for provtrail 0.2.0 (stdlib, Python 3.9+)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
import provtrail  # noqa: E402


def iso(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_record(seq, url, captured_at, prev_hash=None, **extra):
    record = {
        "schema": "provtrail/v1", "seq": seq, "captured_at": captured_at,
        "source_url": url, "prev_hash": prev_hash,
    }
    record.update(extra)
    record.setdefault("kind", "url")
    if record["kind"] is None:
        del record["kind"]  # explicit request for a record without kind
    body = canonical(record).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    record["record_hash"] = "sha256:" + digest
    record["id"] = "ev_" + digest[:16]
    return record


def write_ledger(path, records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


class ContractV02BlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, cwd=None, env=None):
        run_env = os.environ.copy()
        run_env["PYTHONPATH"] = str(SRC) + os.pathsep + run_env.get("PYTHONPATH", "")
        if env:
            run_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "provtrail", *map(str, args)],
            cwd=str(cwd or self.root), env=run_env, text=True, input=None,
            capture_output=True, check=False,
        )

    def add_three(self, path=None, base="https://example.test/"):
        path = path or self.ledger
        for n in range(3):
            result = self.cli("add", path, "--url", "%s%d" % (base, n))
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)

    def hook(self, transcript, scope=None, env_scope=None):
        config = {"ledger": "./ledger.jsonl", "mode": "enforce"}
        if scope is not None:
            config["scope"] = scope
        (self.root / ".provtrail.json").write_text(json.dumps(config), encoding="utf-8")
        transcript_path = self.root / "transcript.jsonl"
        transcript_path.write_text("\n".join(json.dumps(x) for x in transcript) + "\n", encoding="utf-8")
        payload = {"session_id": "s-1", "transcript_path": str(transcript_path),
                   "cwd": str(self.root), "stop_hook_active": False}
        run_env = os.environ.copy()
        run_env["PYTHONPATH"] = str(SRC) + os.pathsep + run_env.get("PYTHONPATH", "")
        if env_scope is not None:
            run_env["PROVTRAIL_SCOPE"] = env_scope
        result = subprocess.run(
            [sys.executable, "-m", "provtrail.stop_hook"], cwd=str(self.root),
            env=run_env, input=json.dumps(payload), text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return (json.loads(result.stdout) if result.stdout.strip() else {}), result

    def assert_blocked(self, response):
        self.assertEqual("block", response.get("decision"), response)

    def assert_not_blocked(self, response):
        self.assertNotEqual("block", response.get("decision"), response)

    # A: public head and expectation anchors.
    def test_a_head_json_and_expectations_detect_truncation_and_rewrite(self):
        self.add_three()
        head = provtrail.Ledger(self.ledger).head()
        self.assertIsNotNone(head)
        seq, record_hash = head
        self.assertEqual(3, seq)
        self.assertTrue(provtrail.Ledger(self.ledger).verify(expect=((seq, record_hash),)).ok)
        plain = self.cli("head", self.ledger)
        self.assertEqual(0, plain.returncode, plain.stderr)
        self.assertEqual("%d:%s" % (seq, record_hash), plain.stdout.strip())
        as_json = self.cli("head", self.ledger, "--json")
        self.assertEqual(0, as_json.returncode, as_json.stderr)
        self.assertEqual({"seq": seq, "record_hash": record_hash}, json.loads(as_json.stdout))
        verified = self.cli("verify", self.ledger, "--expect", "%d:%s" % (seq, record_hash))
        self.assertEqual(0, verified.returncode, verified.stderr + verified.stdout)
        self.assertIn("OK", verified.stdout)
        expect_file = self.root / "anchors.txt"
        expect_file.write_text("# retained anchor\n\n%d:%s\n" % (seq, record_hash), encoding="utf-8")
        self.assertEqual(0, self.cli("verify", self.ledger, "--expect-file", expect_file).returncode)
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        original_seq2_hash = json.loads(lines[1])["record_hash"]
        self.ledger.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        missing = self.cli("verify", self.ledger, "--expect", "%d:%s" % (seq, record_hash))
        self.assertEqual(1, missing.returncode)
        self.assertIn("ANCHOR_MISSING", missing.stdout + missing.stderr)
        # Distinct URLs: identical records within one second would hash identically.
        self.add_three(self.root / "replacement.jsonl", base="https://replacement.example.test/")
        self.ledger.write_text((self.root / "replacement.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        mismatch = self.cli("verify", self.ledger, "--expect", "2:%s" % original_seq2_hash)
        self.assertEqual(1, mismatch.returncode)
        self.assertIn("ANCHOR_MISMATCH", mismatch.stdout + mismatch.stderr)
        malformed = self.cli("verify", self.ledger, "--expect", "not-an-anchor")
        self.assertEqual(2, malformed.returncode)

    def test_a_empty_head_is_exit_one(self):
        self.ledger.touch()
        result = self.cli("head", self.ledger)
        self.assertEqual(1, result.returncode)
        self.assertIn("ledger has no records", result.stdout + result.stderr)

    # B: target one validly hashed violation at a time.
    def test_b_uppercase_content_hash_is_invalid(self):
        value = "sha256:" + "A" * 64
        from provtrail.hashing import is_valid_content_hash
        self.assertFalse(is_valid_content_hash(value))
        write_ledger(self.ledger, [make_record(1, "https://example.test", iso(), kind="url", content_hash=value)])
        result = self.cli("verify", self.ledger)
        self.assertEqual(1, result.returncode)
        self.assertIn("INVALID_CONTENT_HASH", result.stdout + result.stderr)

    def test_b_missing_or_unknown_kind_is_invalid(self):
        for bad_kind in ("podcast", None):
            with self.subTest(kind=bad_kind):
                fields = {"kind": bad_kind}  # None means: no kind field at all
                write_ledger(self.ledger, [make_record(1, "https://example.test", iso(), **fields)])
                result = self.cli("verify", self.ledger)
                self.assertEqual(1, result.returncode)
                self.assertIn("INVALID_KIND", result.stdout + result.stderr)

    # C: Stop hook scope is evaluated from JSONL transcript events.
    def test_c_turn_scope_blocks_old_turn_record_but_session_scope_does_not(self):
        start, captured, prompt = iso(-30), iso(-20), iso(-10)
        write_ledger(self.ledger, [make_record(1, "https://example.test", captured, session_id="s-1")])
        transcript = [
            {"type": "user", "timestamp": start, "message": {"content": "first prompt"}},
            {"type": "user", "timestamp": iso(-15), "toolUseResult": {}, "message": {"content": [{"type": "tool_result"}]}},
            {"type": "user", "isMeta": True, "timestamp": iso(-14), "message": {"content": "Stop hook feedback: x"}},
            {"type": "user", "timestamp": prompt, "message": {"content": "last prompt"}},
        ]
        turn, _ = self.hook(transcript, env_scope="turn")
        self.assert_blocked(turn)
        session, _ = self.hook(transcript, scope="session")
        self.assert_not_blocked(session)
        write_ledger(self.ledger, [make_record(1, "https://example.test", iso(-5), session_id="s-1")])
        response, _ = self.hook(transcript, scope="turn")
        self.assert_not_blocked(response)

    def test_c_turn_scope_unknown_without_human_prompt_does_not_block(self):
        transcript = [
            {"type": "user", "timestamp": iso(-20), "toolUseResult": {}, "message": {"content": [{"type": "tool_result"}]}},
            {"type": "user", "isMeta": True, "timestamp": iso(-10), "message": {"content": "Stop hook feedback: x"}},
        ]
        response, _ = self.hook(transcript, scope="turn")
        self.assert_not_blocked(response)
        self.assertTrue(response, "UNKNOWN must be reported, not silent")
        self.assertIn("UNKNOWN", json.dumps(response))

    def test_c_invalid_scope_is_a_system_message_not_silence(self):
        response, _ = self.hook([], scope="between")
        self.assertTrue(response, "invalid scope was silently ignored")
        self.assertIn("systemMessage", response)
        self.assertIn("scope", response["systemMessage"].lower())

    # D: library content-root realpath confinement; None remains unconstrained.
    def test_d_content_root_confines_add_but_none_does_not(self):
        root = self.root / "root"
        root.mkdir()
        inside, outside = root / "inside.txt", self.root / "outside.txt"
        inside.write_text("inside", encoding="utf-8")
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(ValueError):
            provtrail.Ledger(self.ledger).add(content_path=outside, content_root=root)
        provtrail.Ledger(self.ledger).add(content_path=inside, content_root=root)
        provtrail.Ledger(self.root / "unconfined.jsonl").add(content_path=outside)

    # F/G: repository artifacts are independently checked without implementation imports.
    def test_f_canonical_vectors_are_self_consistent(self):
        vectors_path = REPO / "tests" / "vectors" / "canonical_v1.json"
        self.assertTrue(vectors_path.is_file(), vectors_path)
        vectors = json.loads(vectors_path.read_text(encoding="utf-8"))
        self.assertIsInstance(vectors, list)
        self.assertTrue(vectors)
        for vector in vectors:
            with self.subTest(vector=vector.get("record_hash")):
                hashed_record = {k: v for k, v in vector["record"].items()
                                 if k not in ("id", "record_hash")}
                actual = canonical(hashed_record)
                self.assertEqual(vector["canonical"], actual)
                self.assertEqual("sha256:" + hashlib.sha256(actual.encode("utf-8")).hexdigest(), vector["record_hash"])

    def test_g_ci_matrix_and_mcp_job_are_declared(self):
        workflow = REPO / ".github" / "workflows" / "tests.yml"
        self.assertTrue(workflow.is_file(), workflow)
        text = workflow.read_text(encoding="utf-8")
        for token in ("3.9", "3.13", "windows-latest", "macos-latest", "ubuntu-latest", ".[mcp]"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
