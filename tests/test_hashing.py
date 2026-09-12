"""Tests for provtrail.hashing: canonical JSON, content-hash validation,
and the cross-language canonicalization vector file."""

import json
import os
import sys
import unittest

_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from provtrail.hashing import (  # noqa: E402
    canonical_json_bytes,
    is_valid_content_hash,
    record_hash,
)

_VECTORS_PATH = os.path.join(os.path.dirname(__file__), "vectors", "canonical_v1.json")


class TestContentHashValidation(unittest.TestCase):
    def test_lowercase_hex_is_valid(self):
        self.assertTrue(is_valid_content_hash("sha256:" + "a1" * 32))

    def test_uppercase_hex_is_rejected(self):
        self.assertFalse(is_valid_content_hash("sha256:" + "A1" * 32))

    def test_mixed_case_hex_is_rejected(self):
        self.assertFalse(is_valid_content_hash("sha256:" + "aB" * 32))

    def test_wrong_length_is_rejected(self):
        self.assertFalse(is_valid_content_hash("sha256:" + "a1" * 31))

    def test_missing_prefix_is_rejected(self):
        self.assertFalse(is_valid_content_hash("a1" * 32))

    def test_non_string_is_rejected(self):
        self.assertFalse(is_valid_content_hash(None))
        self.assertFalse(is_valid_content_hash(12345))


class TestCanonicalVectors(unittest.TestCase):
    """Cross-language canonicalization vectors: see tests/vectors/canonical_v1.json.

    Anyone implementing a provtrail verifier in another language can use
    this file to check their canonical-JSON and record_hash output
    against this implementation's, without reading Python source.
    """

    def test_vector_file_exists(self):
        self.assertTrue(
            os.path.isfile(_VECTORS_PATH),
            f"missing {_VECTORS_PATH}",
        )

    def test_vectors_reproduce_canonical_and_record_hash(self):
        with open(_VECTORS_PATH, "r", encoding="utf-8") as f:
            vectors = json.load(f)
        self.assertGreaterEqual(len(vectors), 5, "expected at least 5 vectors")
        for i, vector in enumerate(vectors):
            with self.subTest(index=i, record_hash=vector["record_hash"]):
                record = vector["record"]
                actual_canonical = canonical_json_bytes(record).decode("utf-8")
                self.assertEqual(actual_canonical, vector["canonical"])
                self.assertEqual(record_hash(record), vector["record_hash"])


if __name__ == "__main__":
    unittest.main()
