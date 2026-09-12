"""Tests for provtrail.config: ledger path and mode resolution precedence."""

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from provtrail.config import (  # noqa: E402
    MODE_ENFORCE,
    MODE_REPORT,
    MODE_STRICT,
    SCOPE_SESSION,
    SCOPE_TURN,
    resolve_config,
    resolve_scope,
)

_ENV_KEYS = ("PROVTRAIL_LEDGER", "PROVTRAIL_MODE", "PROVTRAIL_ENFORCE", "PROVTRAIL_SCOPE")


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = self._tmpdir.name
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self._tmpdir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_config(self, cfg):
        with open(os.path.join(self.cwd, ".provtrail.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)


class TestLedgerPathResolution(ConfigTestCase):
    def test_no_config_no_env_returns_none(self):
        ledger_path, mode = resolve_config(self.cwd)
        self.assertIsNone(ledger_path)
        self.assertEqual(mode, MODE_REPORT)

    def test_env_ledger_overrides_config_ledger(self):
        self.write_config({"ledger": "./from-config.jsonl"})
        os.environ["PROVTRAIL_LEDGER"] = "./from-env.jsonl"
        ledger_path, _mode = resolve_config(self.cwd)
        self.assertEqual(ledger_path, os.path.join(self.cwd, "./from-env.jsonl"))

    def test_config_ledger_used_when_no_env(self):
        self.write_config({"ledger": "./ledger.jsonl"})
        ledger_path, _mode = resolve_config(self.cwd)
        self.assertEqual(ledger_path, os.path.join(self.cwd, "./ledger.jsonl"))

    def test_env_ledger_alone_does_not_reset_config_mode(self):
        # Regression: setting only PROVTRAIL_LEDGER used to reset the
        # mode to report because the old hook code short-circuited on
        # the env ledger before reading the config file at all.
        self.write_config({"ledger": "./ledger.jsonl", "mode": "enforce"})
        os.environ["PROVTRAIL_LEDGER"] = "./other.jsonl"
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_ENFORCE)


class TestModeResolution(ConfigTestCase):
    def test_default_is_report(self):
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_REPORT)

    def test_legacy_enforce_env_wins_without_config(self):
        os.environ["PROVTRAIL_ENFORCE"] = "1"
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_ENFORCE)

    def test_legacy_enforce_true_in_config(self):
        self.write_config({"enforce": True})
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_ENFORCE)

    def test_config_mode_field(self):
        self.write_config({"mode": "strict"})
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_STRICT)

    def test_env_mode_overrides_config_mode(self):
        self.write_config({"mode": "strict"})
        os.environ["PROVTRAIL_MODE"] = "report"
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_REPORT)

    def test_env_mode_overrides_legacy_enforce_env(self):
        os.environ["PROVTRAIL_ENFORCE"] = "1"
        os.environ["PROVTRAIL_MODE"] = "strict"
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_STRICT)

    def test_config_mode_overrides_legacy_config_enforce(self):
        self.write_config({"mode": "report", "enforce": True})
        _ledger_path, mode = resolve_config(self.cwd)
        self.assertEqual(mode, MODE_REPORT)

    def test_invalid_env_mode_raises(self):
        os.environ["PROVTRAIL_MODE"] = "bogus"
        with self.assertRaises(ValueError):
            resolve_config(self.cwd)

    def test_invalid_config_mode_raises(self):
        self.write_config({"mode": "bogus"})
        with self.assertRaises(ValueError):
            resolve_config(self.cwd)

    def test_invalid_json_config_raises(self):
        with open(os.path.join(self.cwd, ".provtrail.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            resolve_config(self.cwd)


class TestScopeResolution(ConfigTestCase):
    def test_default_is_session(self):
        self.assertEqual(resolve_scope(self.cwd), SCOPE_SESSION)

    def test_config_scope_field(self):
        self.write_config({"scope": "turn"})
        self.assertEqual(resolve_scope(self.cwd), SCOPE_TURN)

    def test_env_scope_overrides_config_scope(self):
        self.write_config({"scope": "turn"})
        os.environ["PROVTRAIL_SCOPE"] = "session"
        self.assertEqual(resolve_scope(self.cwd), SCOPE_SESSION)

    def test_invalid_env_scope_raises(self):
        os.environ["PROVTRAIL_SCOPE"] = "bogus"
        with self.assertRaises(ValueError):
            resolve_scope(self.cwd)

    def test_invalid_config_scope_raises(self):
        self.write_config({"scope": "bogus"})
        with self.assertRaises(ValueError):
            resolve_scope(self.cwd)

    def test_scope_resolution_independent_of_mode(self):
        self.write_config({"mode": "strict", "scope": "turn"})
        _ledger_path, mode = resolve_config(self.cwd)
        scope = resolve_scope(self.cwd)
        self.assertEqual(mode, MODE_STRICT)
        self.assertEqual(scope, SCOPE_TURN)


if __name__ == "__main__":
    unittest.main()
