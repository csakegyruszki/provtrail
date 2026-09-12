"""Structural checks for .github/workflows/tests.yml.

These are plain text checks, not a YAML parse, so the test suite does
not need a YAML library as a test dependency (the package itself has no
runtime dependencies beyond the standard library; see pyproject.toml).
"""

import os
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKFLOW_PATH = os.path.join(_ROOT, ".github", "workflows", "tests.yml")


class TestCiWorkflow(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(_WORKFLOW_PATH), f"missing {_WORKFLOW_PATH}")
        with open(_WORKFLOW_PATH, "r", encoding="utf-8") as f:
            self.text = f.read()

    def test_triggers_on_push_and_pull_request(self):
        self.assertIn("push:", self.text)
        self.assertIn("pull_request:", self.text)

    def test_matrix_covers_all_three_operating_systems(self):
        for os_name in ("ubuntu-latest", "windows-latest", "macos-latest"):
            with self.subTest(os_name=os_name):
                self.assertIn(os_name, self.text)

    def test_matrix_covers_all_six_python_versions_on_linux(self):
        for py_version in ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14"):
            with self.subTest(py_version=py_version):
                self.assertIn(py_version, self.text)

    def test_windows_and_macos_are_pinned_to_specific_python_versions(self):
        # Windows: 3.9 and 3.14. macOS: 3.14. (Linux covers the full range,
        # checked above.)
        self.assertIn('os: windows-latest\n            python-version: "3.9"', self.text)
        self.assertIn('os: windows-latest\n            python-version: "3.14"', self.text)
        self.assertIn('os: macos-latest\n            python-version: "3.14"', self.text)

    def test_actions_are_pinned_to_major_version_tags(self):
        self.assertIn("actions/checkout@v4", self.text)
        self.assertIn("actions/setup-python@v5", self.text)

    def test_matrix_job_runs_unittest_discover(self):
        self.assertIn("python -m unittest discover -s tests", self.text)

    def test_separate_job_installs_mcp_extra_and_jsonschema_on_ubuntu_py313(self):
        self.assertIn('pip install ".[mcp]" jsonschema', self.text)
        # There should be two distinct job keys: the OS/version matrix
        # job and the mcp-extra job.
        self.assertGreaterEqual(self.text.count("runs-on:"), 2)

    def test_every_job_uses_bash_shell(self):
        # Needed so the same wheel-build-and-smoke steps work on
        # ubuntu/macos and windows-latest runners alike.
        self.assertGreaterEqual(self.text.count("shell: bash"), 2)

    def test_every_job_builds_and_smoke_tests_the_wheel(self):
        self.assertEqual(self.text.count("pip wheel . -w dist --no-deps"), 2)
        for smoke_command in (
            "provtrail --help", "provtrail add", "provtrail verify", "provtrail head",
        ):
            with self.subTest(smoke_command=smoke_command):
                self.assertEqual(self.text.count(smoke_command), 2)


if __name__ == "__main__":
    unittest.main()
