"""Tests for provtrail.ledger: contract, chain integrity, files, locking, check."""

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from provtrail.ledger import (  # noqa: E402
    STATE_MISSING,
    STATE_PRESENT,
    STATE_UNKNOWN,
    ContractError,
    Ledger,
    LedgerError,
    LockTimeout,
    now_rfc3339,
)


class TempLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.ledger_path = os.path.join(self.tmpdir, "ledger.jsonl")

    def tearDown(self):
        self._tmpdir.cleanup()

    def append_raw(self, obj):
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def read_lines(self):
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            return f.readlines()

    def write_lines(self, lines):
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.writelines(lines)


class TestContract(TempLedgerTestCase):
    def test_add_rejects_windows_absolute_path_on_posix(self):
        with self.assertRaises(ValueError):
            Ledger(self.ledger_path).add(
                source_url="https://example.com", path="C:\\outside.txt"
            )

    def test_add_with_url_only_is_valid(self):
        ledger = Ledger(self.ledger_path)
        rec = ledger.add(source_url="https://example.com/a")
        self.assertEqual(rec["source_url"], "https://example.com/a")
        self.assertNotIn("content_hash", rec)
        report = ledger.verify()
        self.assertTrue(report.ok, report.violations)

    def test_add_with_content_hash_only_is_valid(self):
        ledger = Ledger(self.ledger_path)
        rec = ledger.add(content="hello world")
        self.assertTrue(rec["content_hash"].startswith("sha256:"))
        self.assertNotIn("source_url", rec)
        report = ledger.verify()
        self.assertTrue(report.ok, report.violations)

    def test_add_with_neither_raises_contract_error(self):
        ledger = Ledger(self.ledger_path)
        with self.assertRaises(ContractError):
            ledger.add()
        # No partial record should have been written.
        self.assertFalse(os.path.isfile(self.ledger_path))

    def test_handcrafted_missing_captured_at(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "source_url": "https://example.com",
                "kind": "url",
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        report = Ledger(self.ledger_path).verify()
        codes = {v["code"] for v in report.violations}
        self.assertIn("MISSING_CAPTURED_AT", codes)
        self.assertNotIn("INVALID_CAPTURED_AT", codes)

    def test_handcrafted_invalid_captured_at(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "captured_at": "not-a-date",
                "source_url": "https://example.com",
                "kind": "url",
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        report = Ledger(self.ledger_path).verify()
        codes = {v["code"] for v in report.violations}
        self.assertIn("INVALID_CAPTURED_AT", codes)
        self.assertNotIn("MISSING_CAPTURED_AT", codes)

    def test_handcrafted_missing_source_locator(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "captured_at": now_rfc3339(),
                "kind": "url",
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        report = Ledger(self.ledger_path).verify()
        codes = {v["code"] for v in report.violations}
        self.assertIn("MISSING_SOURCE_LOCATOR", codes)


class TestChain(TempLedgerTestCase):
    def _add_five(self, ledger):
        for i in range(5):
            ledger.add(source_url=f"https://example.com/{i}")

    def test_five_records_verify_ok(self):
        ledger = Ledger(self.ledger_path)
        self._add_five(ledger)
        report = ledger.verify()
        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.record_count, 5)

    def test_edit_field_in_line_three_gives_hash_mismatch(self):
        ledger = Ledger(self.ledger_path)
        self._add_five(ledger)
        lines = self.read_lines()
        rec = json.loads(lines[2])
        rec["title"] = "tampered"
        lines[2] = json.dumps(rec, ensure_ascii=False) + "\n"
        self.write_lines(lines)

        report = ledger.verify()
        self.assertFalse(report.ok)
        hash_violations = [v for v in report.violations if v["code"] == "HASH_MISMATCH"]
        self.assertTrue(hash_violations)
        self.assertEqual(hash_violations[0]["seq_or_line"], 3)

    def test_delete_line_three_breaks_chain(self):
        ledger = Ledger(self.ledger_path)
        self._add_five(ledger)
        lines = self.read_lines()
        del lines[2]
        self.write_lines(lines)

        report = ledger.verify()
        self.assertFalse(report.ok)
        codes = {v["code"] for v in report.violations}
        self.assertTrue({"BAD_SEQ", "CHAIN_BROKEN"} & codes)

    def test_swap_lines_two_and_three_is_a_violation(self):
        ledger = Ledger(self.ledger_path)
        self._add_five(ledger)
        lines = self.read_lines()
        lines[1], lines[2] = lines[2], lines[1]
        self.write_lines(lines)

        report = ledger.verify()
        self.assertFalse(report.ok)
        self.assertTrue(report.violations)

    def test_wrong_schema(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        lines = self.read_lines()
        rec = json.loads(lines[0])
        rec["schema"] = "not-provtrail"
        # record_hash won't match once schema changes, but we still
        # expect WRONG_SCHEMA to be reported specifically.
        self.write_lines([json.dumps(rec, ensure_ascii=False) + "\n"])

        report = ledger.verify()
        codes = {v["code"] for v in report.violations}
        self.assertIn("WRONG_SCHEMA", codes)

    def test_boolean_seq_is_a_bad_sequence(self):
        ledger = Ledger(self.ledger_path)
        rec = ledger.add(source_url="https://example.com")
        rec["seq"] = True
        from provtrail.hashing import record_hash

        rec["record_hash"] = record_hash(rec)
        rec["id"] = "ev_" + rec["record_hash"].split(":", 1)[1][:16]
        self.write_lines([json.dumps(rec) + "\n"])

        codes = {v["code"] for v in ledger.verify().violations}
        self.assertIn("BAD_SEQ", codes)

    def test_empty_ledger_verifies_ok(self):
        report = Ledger(self.ledger_path).verify()
        self.assertTrue(report.ok)
        self.assertEqual(report.record_count, 0)

    def test_add_rejects_existing_tampered_ledger_without_appending(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        lines = self.read_lines()
        rec = json.loads(lines[0])
        rec["title"] = "tampered"
        self.write_lines([json.dumps(rec) + "\n"])

        with self.assertRaises(LedgerError):
            ledger.add(source_url="https://example.com/1")

        self.assertEqual(len(self.read_lines()), 1)


class TestFiles(TempLedgerTestCase):
    def test_content_path_round_trip_ok(self):
        artifact_path = os.path.join(self.tmpdir, "artifact.txt")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("artifact content")

        ledger = Ledger(self.ledger_path)
        ledger.add(content_path=artifact_path, path="artifact.txt", kind="file")

        report = ledger.verify(check_files=True)
        self.assertTrue(report.ok, report.violations)

    def test_modified_artifact_gives_file_hash_mismatch(self):
        artifact_path = os.path.join(self.tmpdir, "artifact.txt")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("artifact content")

        ledger = Ledger(self.ledger_path)
        ledger.add(content_path=artifact_path, path="artifact.txt", kind="file")

        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("tampered content")

        report = ledger.verify(check_files=True)
        codes = {v["code"] for v in report.violations}
        self.assertIn("FILE_HASH_MISMATCH", codes)

    def test_deleted_artifact_gives_file_missing(self):
        artifact_path = os.path.join(self.tmpdir, "artifact.txt")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("artifact content")

        ledger = Ledger(self.ledger_path)
        ledger.add(content_path=artifact_path, path="artifact.txt", kind="file")

        os.remove(artifact_path)

        report = ledger.verify(check_files=True)
        codes = {v["code"] for v in report.violations}
        self.assertIn("FILE_MISSING", codes)

    def test_check_files_false_ignores_missing_artifact(self):
        artifact_path = os.path.join(self.tmpdir, "artifact.txt")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("artifact content")

        ledger = Ledger(self.ledger_path)
        ledger.add(content_path=artifact_path, path="artifact.txt", kind="file")
        os.remove(artifact_path)

        report = ledger.verify(check_files=False)
        self.assertTrue(report.ok, report.violations)


class TestLock(TempLedgerTestCase):
    def test_foreign_lock_causes_timeout_and_is_preserved(self):
        lock_path = self.ledger_path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, b"99999")
        os.close(fd)

        ledger = Ledger(self.ledger_path)
        with self.assertRaises(LockTimeout):
            ledger.add(source_url="https://example.com", lock_timeout=0.3)

        self.assertTrue(os.path.isfile(lock_path), "foreign lock file must survive")
        with open(lock_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "99999")

        os.remove(lock_path)

    def test_lock_released_after_successful_add(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com")
        self.assertFalse(os.path.isfile(self.ledger_path + ".lock"))


class TestCheck(TempLedgerTestCase):
    def test_present_with_any_record(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com")
        result = ledger.check()
        self.assertEqual(result.state, STATE_PRESENT)

    def test_unknown_when_file_does_not_exist(self):
        result = Ledger(self.ledger_path).check()
        self.assertEqual(result.state, STATE_UNKNOWN)

    def test_missing_when_ledger_readable_but_no_record_since(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", captured_at="2020-01-01T00:00:00Z")
        result = ledger.check(since="2030-01-01T00:00:00Z")
        self.assertEqual(result.state, STATE_MISSING)

    def test_unknown_when_ledger_corrupted(self):
        self.write_lines(["not json at all\n"])
        result = Ledger(self.ledger_path).check()
        self.assertEqual(result.state, STATE_UNKNOWN)

    def test_unknown_when_since_unparseable(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com")
        result = ledger.check(since="not-a-date")
        self.assertEqual(result.state, STATE_UNKNOWN)

    def test_present_with_matching_since(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", captured_at="2025-06-01T00:00:00Z")
        result = ledger.check(since="2025-01-01T00:00:00Z")
        self.assertEqual(result.state, STATE_PRESENT)


class TestCheckSessionAndUntil(TempLedgerTestCase):
    def test_session_id_mismatch_is_missing(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", session_id="session-B")
        result = ledger.check(session_id="session-A")
        self.assertEqual(result.state, STATE_MISSING)

    def test_session_id_match_is_present(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", session_id="session-A")
        result = ledger.check(session_id="session-A")
        self.assertEqual(result.state, STATE_PRESENT)

    def test_until_excludes_future_dated_record(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", captured_at="2099-01-01T00:00:00Z")
        result = ledger.check(until="2030-01-01T00:00:00Z")
        self.assertEqual(result.state, STATE_MISSING)

    def test_until_includes_record_within_bound(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com", captured_at="2025-06-01T00:00:00Z")
        result = ledger.check(until="2030-01-01T00:00:00Z")
        self.assertEqual(result.state, STATE_PRESENT)

    def test_unparseable_until_is_unknown(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com")
        result = ledger.check(until="not-a-date")
        self.assertEqual(result.state, STATE_UNKNOWN)

    def test_session_id_and_until_combine(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(
            source_url="https://example.com",
            session_id="session-A",
            captured_at="2099-01-01T00:00:00Z",
        )
        result = ledger.check(session_id="session-A", until="2030-01-01T00:00:00Z")
        self.assertEqual(result.state, STATE_MISSING)


class TestRecordsIterator(TempLedgerTestCase):
    def test_records_raises_ledger_error_with_line_number(self):
        self.write_lines(["{\"schema\": \"provtrail/v1\"}\n", "not valid json\n"])
        ledger = Ledger(self.ledger_path)
        with self.assertRaises(LedgerError) as ctx:
            list(ledger.records())
        self.assertIn("line 2", str(ctx.exception))


class TestHeadAndAnchors(TempLedgerTestCase):
    def test_head_is_none_for_empty_ledger(self):
        self.assertIsNone(Ledger(self.ledger_path).head())

    def test_head_returns_last_seq_and_hash(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        rec2 = ledger.add(source_url="https://example.com/1")
        seq, record_hash = ledger.head()
        self.assertEqual(seq, 2)
        self.assertEqual(record_hash, rec2["record_hash"])

    def test_verify_expect_matching_anchor_is_ok(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        rec2 = ledger.add(source_url="https://example.com/1")
        report = ledger.verify(expect=[(2, rec2["record_hash"])])
        self.assertTrue(report.ok, report.violations)

    def test_verify_expect_truncated_ledger_gives_anchor_missing(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        rec2 = ledger.add(source_url="https://example.com/1")
        # Simulate truncation: drop the last line and re-verify against
        # the anchor recorded before the drop.
        lines = self.read_lines()
        self.write_lines(lines[:-1])
        report = ledger.verify(expect=[(2, rec2["record_hash"])])
        self.assertFalse(report.ok)
        codes = {v["code"] for v in report.violations}
        self.assertIn("ANCHOR_MISSING", codes)

    def test_verify_expect_wrong_hash_gives_anchor_mismatch(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        ledger.add(source_url="https://example.com/1")
        report = ledger.verify(expect=[(2, "sha256:" + "0" * 64)])
        self.assertFalse(report.ok)
        codes = {v["code"] for v in report.violations}
        self.assertIn("ANCHOR_MISMATCH", codes)


class TestKindAndContentHashValidation(TempLedgerTestCase):
    def test_missing_kind_is_invalid_kind(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "captured_at": now_rfc3339(),
                "source_url": "https://example.com",
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        codes = {v["code"] for v in Ledger(self.ledger_path).verify().violations}
        self.assertIn("INVALID_KIND", codes)

    def test_unknown_kind_is_invalid_kind(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "captured_at": now_rfc3339(),
                "kind": "not-a-real-kind",
                "source_url": "https://example.com",
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        codes = {v["code"] for v in Ledger(self.ledger_path).verify().violations}
        self.assertIn("INVALID_KIND", codes)

    def test_uppercase_content_hash_is_invalid(self):
        self.append_raw(
            {
                "schema": "provtrail/v1",
                "seq": 1,
                "id": "ev_0000000000000000",
                "captured_at": now_rfc3339(),
                "kind": "file",
                "content_hash": "sha256:" + "AB" * 32,
                "prev_hash": None,
                "record_hash": "sha256:" + "0" * 64,
            }
        )
        codes = {v["code"] for v in Ledger(self.ledger_path).verify().violations}
        self.assertIn("INVALID_CONTENT_HASH", codes)

    def test_valid_kind_and_hash_do_not_trigger_new_codes(self):
        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")
        report = ledger.verify()
        codes = {v["code"] for v in report.violations}
        self.assertNotIn("INVALID_KIND", codes)
        self.assertNotIn("INVALID_CONTENT_HASH", codes)


class TestContentRoot(TempLedgerTestCase):
    def test_content_path_inside_root_succeeds(self):
        artifact_path = os.path.join(self.tmpdir, "artifact.txt")
        with open(artifact_path, "w", encoding="utf-8") as f:
            f.write("artifact content")
        rec = Ledger(self.ledger_path).add(
            content_path=artifact_path, kind="file", content_root=self.tmpdir
        )
        self.assertTrue(rec["content_hash"].startswith("sha256:"))

    def test_content_path_outside_root_is_rejected(self):
        outside_dir = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(outside_dir, "secret.txt")
            with open(outside_file, "w", encoding="utf-8") as f:
                f.write("outside content")
            with self.assertRaises(ValueError):
                Ledger(self.ledger_path).add(
                    content_path=outside_file, kind="file", content_root=self.tmpdir
                )
        finally:
            import shutil

            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_no_content_root_preserves_previous_unrestricted_behavior(self):
        outside_dir = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(outside_dir, "secret.txt")
            with open(outside_file, "w", encoding="utf-8") as f:
                f.write("outside content")
            rec = Ledger(self.ledger_path).add(content_path=outside_file, kind="file")
            self.assertTrue(rec["content_hash"].startswith("sha256:"))
        finally:
            import shutil

            shutil.rmtree(outside_dir, ignore_errors=True)


class TestAddSingleRead(TempLedgerTestCase):
    def test_add_reads_ledger_once(self):
        """`add` must read the ledger file only once (via `verify`'s own
        pass), not walk it again afterwards to find the last record."""
        import builtins
        from unittest import mock

        ledger = Ledger(self.ledger_path)
        ledger.add(source_url="https://example.com/0")

        real_open = builtins.open
        abs_ledger_path = os.path.abspath(self.ledger_path)
        read_opens = []

        def spy_open(file, mode="r", *args, **kwargs):
            if (
                isinstance(file, str)
                and os.path.abspath(file) == abs_ledger_path
                and "r" in mode
            ):
                read_opens.append(mode)
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=spy_open):
            ledger.add(source_url="https://example.com/1")

        self.assertEqual(
            len(read_opens), 1,
            f"expected exactly one read of the ledger file, got {read_opens!r}",
        )


if __name__ == "__main__":
    unittest.main()
