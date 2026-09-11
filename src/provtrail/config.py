"""Shared configuration resolution for provtrail integrations.

Used by both the Claude Code Stop hook (``integrations/claude-code/
stop_hook.py``) and the MCP server (``integrations/mcp/provtrail_mcp.py``)
so the two agree on where the ledger lives and which mode applies.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

MODE_REPORT = "report"
MODE_ENFORCE = "enforce"
MODE_STRICT = "strict"
VALID_MODES = (MODE_REPORT, MODE_ENFORCE, MODE_STRICT)

CONFIG_FILENAME = ".provtrail.json"


def _load_config_file(cwd: str) -> Dict[str, Any]:
    config_path = os.path.join(cwd, CONFIG_FILENAME)
    if not os.path.isfile(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{config_path}: expected a JSON object")
    return cfg


def _validate_mode(value: Any) -> str:
    if value not in VALID_MODES:
        raise ValueError(
            f"invalid provtrail mode {value!r}; must be one of {list(VALID_MODES)}"
        )
    return value


def _resolve_mode(cfg: Dict[str, Any]) -> str:
    env_mode = os.environ.get("PROVTRAIL_MODE")
    if env_mode:
        return _validate_mode(env_mode)

    if os.environ.get("PROVTRAIL_ENFORCE") == "1":
        return MODE_ENFORCE

    cfg_mode = cfg.get("mode")
    if cfg_mode:
        return _validate_mode(cfg_mode)

    if bool(cfg.get("enforce", False)):
        return MODE_ENFORCE

    return MODE_REPORT


def resolve_config(cwd: str) -> Tuple[Optional[str], str]:
    """Resolve the ledger path and mode for the project at ``cwd``.

    Ledger path: the ``PROVTRAIL_LEDGER`` environment variable if set,
    else the ``"ledger"`` field of ``<cwd>/.provtrail.json``. A relative
    path is joined to ``cwd``. Returns ``None`` if neither is configured.

    Mode is resolved independently of the ledger path, so setting only
    ``PROVTRAIL_LEDGER`` does not reset a mode configured in the file.
    Precedence, highest first:
        1. ``PROVTRAIL_MODE`` environment variable.
        2. legacy ``PROVTRAIL_ENFORCE=1`` environment variable -> "enforce".
        3. the config file's ``"mode"`` field.
        4. legacy config ``"enforce": true`` -> "enforce".
        5. default: "report".

    Raises ``ValueError`` if the config file is not valid JSON, or if an
    explicit mode value (env or config) is not one of "report",
    "enforce", "strict".
    """
    cfg = _load_config_file(cwd)

    env_ledger = os.environ.get("PROVTRAIL_LEDGER")
    if env_ledger:
        ledger_path: Optional[str] = os.path.join(cwd, env_ledger)
    else:
        cfg_ledger = cfg.get("ledger")
        ledger_path = os.path.join(cwd, cfg_ledger) if cfg_ledger else None

    mode = _resolve_mode(cfg)
    return ledger_path, mode
