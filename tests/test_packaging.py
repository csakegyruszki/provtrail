"""The sdist must ship the files its own test suite reads."""

import os
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class TestManifest(unittest.TestCase):
    def test_sdist_includes_test_fixtures_and_ci_workflow(self):
        with open(os.path.join(_ROOT, "MANIFEST.in"), encoding="utf-8") as fh:
            lines = [line.strip() for line in fh]
        self.assertIn("recursive-include tests/vectors *.json", lines)
        self.assertIn("include .github/workflows/tests.yml", lines)


if __name__ == "__main__":
    unittest.main()
