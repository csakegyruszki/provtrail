#!/usr/bin/env python3
"""Claude Code Stop hook: check that sources were captured this session.

Installed as the ``provtrail-stop-hook`` command; also runnable as
``python -m provtrail.stop_hook``.

Reads the Stop-hook JSON payload from stdin (``session_id``,
``transcript_path``, ``cwd``, ``stop_hook_active``) and reports on, or
optionally enforces, whether the project's provtrail ledger has a
record captured during this session.

Session scoping:
    When the payload includes ``session_id``, only ledger records whose
    own ``session_id`` field matches count, and the window closes 300
    seconds after the hook runs (so a record from a later, unrelated
    session cannot satisfy an earlier one). The window still opens at
    the first timestamp in the transcript, if one is found.

    When ``session_id`` is absent, the hook falls back to the
    transcript's first timestamp as the window start (the original
    behaviour), still bounded by the same 300-second ``until``.

    If ``session_id`` is absent AND the transcript has no timestamp
    (missing, unreadable, or simply empty of them), the check cannot be
    scoped to this session at all: the state is ``UNKNOWN`` rather than
    treating an unrelated old record as ``PRESENT``.

Configuration is resolved by ``provtrail.config.resolve_config``, shared
with the MCP server:
    - ledger path: ``PROVTRAIL_LEDGER`` env var, else the ``"ledger"``
      field of ``<cwd>/.provtrail.json``.
    - mode: ``PROVTRAIL_MODE`` env var, else legacy ``PROVTRAIL_ENFORCE=1``,
      else the config file's ``"mode"`` field, else legacy
      ``"enforce": true``, else ``"report"``.

If no ledger is configured, the project has not opted in: exit 0, no
output.

Modes:
    report (default): if the ledger state is not PRESENT, print
        ``{"systemMessage": "..."}`` and exit 0. Never blocks.
    enforce: blocks (prints ``{"decision": "block", "reason": "..."}``)
        only when the state is MISSING. UNKNOWN never blocks in this
        mode.
    strict: blocks on MISSING or UNKNOWN.

In every mode, a hook re-invocation (``stop_hook_active`` true) never
blocks, to avoid Stop-hook loops.

Error handling: this script always exits 0, but it is not silent on
unexpected errors (a malformed ``.provtrail.json``, an invalid mode
value, an unreadable ledger, etc.). It prints a ``systemMessage`` naming
the exception type and message. If the mode had already been resolved
to ``strict`` before the error occurred, it blocks instead of just
reporting, unless ``stop_hook_active`` is true.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _import_provtrail():
    # Imported lazily so that an import error is reported by run()'s
    # error handler instead of escaping as a traceback.
    from .ledger import Ledger, STATE_MISSING, STATE_PRESENT, STATE_UNKNOWN
    return Ledger, STATE_MISSING, STATE_PRESENT, STATE_UNKNOWN


def _import_config():
    from .config import MODE_ENFORCE, MODE_REPORT, MODE_STRICT, resolve_config
    return resolve_config, MODE_REPORT, MODE_ENFORCE, MODE_STRICT


def _find_since(transcript_path):
    """Return (first_timestamp_or_None, transcript_readable_bool)."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None, False
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("timestamp"):
                    return obj["timestamp"], True
        return None, True
    except OSError:
        return None, False


def _until_str(seconds: int = 300) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(payload: dict) -> int:
    cwd = payload.get("cwd") or os.getcwd()
    # Claude Code sends a JSON boolean. Only true (or the string "true")
    # counts, so that a stray value such as "false" cannot suppress a block.
    active = payload.get("stop_hook_active", False)
    stop_hook_active = active is True or (isinstance(active, str) and active.lower() == "true")
    mode = None

    try:
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id") or None

        resolve_config, MODE_REPORT, MODE_ENFORCE, MODE_STRICT = _import_config()
        ledger_path, mode = resolve_config(cwd)
        if not ledger_path:
            return 0

        Ledger, STATE_MISSING, STATE_PRESENT, STATE_UNKNOWN = _import_provtrail()

        transcript_ts, transcript_readable = _find_since(transcript_path)
        no_session_scope = not session_id and transcript_ts is None
        ledger_dir = os.path.dirname(os.path.abspath(ledger_path))

        if no_session_scope:
            state = STATE_UNKNOWN
            if not transcript_readable:
                reason = (
                    "transcript was missing or unreadable, and the payload had "
                    "no session_id to scope the check to"
                )
            else:
                reason = (
                    "transcript has no timestamp, and the payload had no "
                    "session_id to scope the check to"
                )
        elif not os.path.exists(ledger_path) and os.path.isdir(ledger_dir):
            # The project opted in and the ledger's directory exists, so an
            # absent file means no record has been written yet. A missing
            # directory falls through to Ledger.check(), which reports
            # UNKNOWN for a nonexistent file (likely a misconfigured path).
            state = STATE_MISSING
            reason = "configured ledger file has not been created yet"
        else:
            ledger = Ledger(ledger_path)
            until = _until_str()
            result = ledger.check(since=transcript_ts, session_id=session_id, until=until)
            state = result.state
            reason = result.reason

        def system_message():
            if state != STATE_PRESENT:
                msg = (
                    f"provtrail: source ledger state is {state} ({reason}). "
                    f"Capture sources with `provtrail add {ledger_path} --url ...` "
                    f"or the provtrail MCP tool."
                )
                print(json.dumps({"systemMessage": msg}))

        if mode == MODE_REPORT:
            system_message()
            return 0

        should_block = (mode == MODE_ENFORCE and state == STATE_MISSING) or (
            mode == MODE_STRICT and state in (STATE_MISSING, STATE_UNKNOWN)
        )
        if should_block and not stop_hook_active:
            reason_msg = (
                f"No source has satisfied provtrail for this session ({state}: {reason}). "
                "Capture the sources used with `provtrail add <ledger> --url <url>` "
                "(or the provtrail MCP tool) before stopping."
            )
            print(json.dumps({"decision": "block", "reason": reason_msg}))
            return 0

        # state is PRESENT, or a non-blocking MISSING/UNKNOWN, or a
        # re-invocation (stop_hook_active) -- fall back to report-only.
        system_message()
        return 0
    except Exception as e:
        msg = f"provtrail: stop hook error: {type(e).__name__}: {e}"
        if mode == "strict" and not stop_hook_active:
            print(json.dumps({"decision": "block", "reason": msg}))
            return 0
        print(json.dumps({"systemMessage": msg}))
        return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        msg = f"provtrail: stop hook error: {type(e).__name__}: {e}"
        print(json.dumps({"systemMessage": msg}))
        return 0
    return run(payload)


if __name__ == "__main__":
    raise SystemExit(main())
