"""Version consistency between the package and pyproject.toml."""

import os
import re
import sys
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import provtrail  # noqa: E402

_PYPROJECT_PATH = os.path.join(_ROOT, "pyproject.toml")


class TestVersion(unittest.TestCase):
    def test_package_version_is_0_2_0(self):
        self.assertEqual(provtrail.__version__, "0.2.2")

    def test_pyproject_version_matches_package_version(self):
        # Read as text rather than parsing TOML, so the test does not
        # need a TOML library on Python 3.9 (tomllib is 3.11+, and the
        # package itself declares no runtime dependencies).
        with open(_PYPROJECT_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(m, "could not find a version field in pyproject.toml")
        self.assertEqual(m.group(1), provtrail.__version__)


if __name__ == "__main__":
    unittest.main()
