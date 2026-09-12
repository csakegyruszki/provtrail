"""Black-box contract tests for provtrail 0.2.0 addendum items I, J, K, M."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from provtrail import Ledger
from provtrail.hashing import canonical_json_bytes

try:
    import jsonschema
except ImportError:
    jsonschema = None


def canonical(record):
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signed_record(**changes):
    record = {
        "schema": "provtrail/v1",
        "seq": 1,
        "captured_at": "2026-09-12T12:00:00Z",
        "kind": "url",
        "source_url": "https://example.test/source",
        "prev_hash": None,
    }
    record.update(changes)
    payload = {k: v for k, v in record.items() if k not in {"id", "record_hash"}}
    digest = hashlib.sha256(canonical(payload)).hexdigest()
    record["record_hash"] = "sha256:" + digest
    record["id"] = "ev_" + digest[:16]
    return record


def load_vector_fixtures():
    vectors_path = ROOT / "tests" / "vectors" / "invalid_records.json"
    if not vectors_path.is_file():
        raise AssertionError(str(vectors_path))
    fixtures = json.loads(vectors_path.read_text(encoding="utf-8"))
    if isinstance(fixtures, dict):
        invalid = fixtures.get("invalid_records", fixtures.get("invalid", []))
        valid = fixtures.get("valid_record", fixtures.get("valid"))
    else:
        invalid = [item for item in fixtures if not item.get("valid")]
        valid_items = [item for item in fixtures if item.get("valid")]
        valid = valid_items[0] if valid_items else None
    return invalid, valid


class AddendumBlackBoxContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.ledger = self.work / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, record):
        self.ledger.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "provtrail", "verify", str(self.ledger)],
            cwd=self.work, env=env, text=True, capture_output=True, check=False,
        )

    def test_i_record_validator_is_shared_by_add_and_verify(self):
        cases = [
            ("unknown_field", {"foo": "bar"}, "UNKNOWN_FIELD"),
            ("integer_title", {"title": 123}, "INVALID_FIELD_TYPE"),
            ("string_extra", {"extra": "x"}, "INVALID_FIELD_TYPE"),
            ("boolean_seq", {"seq": True}, "INVALID_FIELD_TYPE"),
            ("bad_prev_hash", {"prev_hash": "abc"}, "INVALID_FIELD_TYPE"),
            ("missing_kind", {"kind": None}, "INVALID_KIND"),
            ("unknown_kind", {"kind": "podcast"}, "INVALID_KIND"),
            ("upper_content_hash", {"content_hash": "sha256:" + "A" * 64}, "INVALID_CONTENT_HASH"),
        ]
        for name, changes, expected in cases:
            with self.subTest(name=name):
                record = signed_record(**changes)
                if name == "missing_kind":
                    record = {k: v for k, v in record.items() if k not in ("kind", "id", "record_hash")}
                    digest = hashlib.sha256(canonical(record)).hexdigest()
                    record["record_hash"] = "sha256:" + digest
                    record["id"] = "ev_" + digest[:16]
                result = self.verify(record)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout + result.stderr)

        for kwargs in ({"title": 123}, {"extra": "x"}):
            with self.subTest(add_kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    Ledger(self.work / "fresh.jsonl").add(source_url="https://example.test/source", **kwargs)

        invalid, valid = load_vector_fixtures()
        self.assertGreaterEqual(len(invalid), 8)
        self.assertIsNotNone(valid, "invalid_records.json needs a valid fixture")
        valid_record = valid.get("record", valid) if isinstance(valid, dict) else valid
        for entry in invalid:
            record = entry.get("record", entry)
            expected = entry.get("expected_code", entry.get("code"))
            self.assertIsInstance(expected, str, "invalid fixture needs expected_code or code")
            result = self.verify(record)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(expected, result.stdout + result.stderr)


    @unittest.skipUnless(jsonschema is not None, "jsonschema is not installed")
    def test_i_vector_fixtures_fail_published_schema(self):
        invalid, valid = load_vector_fixtures()
        self.assertGreaterEqual(len(invalid), 8)
        self.assertIsNotNone(valid, "invalid_records.json needs a valid fixture")
        valid_record = valid.get("record", valid) if isinstance(valid, dict) else valid
        schema = json.loads((ROOT / "schema" / "provtrail-record.v1.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        self.assertFalse(list(validator.iter_errors(valid_record)))
        for entry in invalid:
            self.assertTrue(list(validator.iter_errors(entry.get("record", entry))))

    def test_j_strict_json_values(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    Ledger(self.ledger).add(
                        source_url="https://example.test/source", extra={"x": value}
                    )
        raw_nan = signed_record(extra={"x": float("nan")})
        result = self.verify(raw_nan)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("INVALID_JSON", result.stdout + result.stderr)
        with self.assertRaises(ValueError):
            canonical_json_bytes({"a": float("nan")})
        normal = signed_record(title="normal")
        payload = {k: v for k, v in normal.items() if k not in {"id", "record_hash"}}
        self.assertEqual(canonical_json_bytes(payload), canonical(payload))

    def test_k_ci_matrix_and_install_smoke_are_declared(self):
        workflow = ROOT / ".github" / "workflows" / "tests.yml"
        self.assertTrue(workflow.is_file(), str(workflow))
        text = workflow.read_text(encoding="utf-8")
        for required in (
            "3.9", "3.10", "3.11", "3.12", "3.13", "3.14",
            "windows-latest", "macos-latest", "ubuntu-latest", ".[mcp]",
            "jsonschema", "pip wheel",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertTrue("provtrail --help" in text or "provtrail head" in text)

    def test_m_naming_and_stop_hook_completeness_disclosure(self):
        phrase = "source provenance ledger for llm-assisted research"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(phrase, "\n".join(readme.splitlines()[:5]).lower())
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        description = re.search(r"^description\s*=\s*[\"']([^\"']+)[\"']", pyproject, re.M)
        self.assertIsNotNone(description)
        self.assertIn(phrase, description.group(1).lower())
        self.assertRegex(
            readme,
            re.compile(r"stop hook[\s\S]{0,800}(every source[\s\S]{0,200}captured|captured[\s\S]{0,200}every source)", re.I),
        )


if __name__ == "__main__":
    unittest.main()
