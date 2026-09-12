# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-09-12

### Added
- One shared record validator (`_validate_record_fields`) used by both `Ledger.add` (before
  writing) and `Ledger.verify` (while reading), so the two can never check different rules. New
  violation codes `UNKNOWN_FIELD` (a key not in the v1 schema's `properties`) and
  `INVALID_FIELD_TYPE` (`seq` not an integer >= 1 or a bool; a string field holding a non-string;
  `extra` not an object; `prev_hash` neither `null` nor `sha256:<64 lower-hex>`). `add` now raises
  `ValueError` for a badly typed field before acquiring the append lock or touching the ledger
  file. `tests/vectors/invalid_records.json` adds eight signed invalid-record fixtures (missing
  kind, unknown kind, unknown field, upper-case content hash, integer title, non-object `extra`,
  boolean `seq`, malformed `prev_hash`) plus one valid record, each checked against both `verify`
  and, when `jsonschema` is installed, the published schema.
- `canonical_json_bytes` now passes `allow_nan=False` to `json.dumps`, so `NaN`, `Infinity` and
  `-Infinity` anywhere in a record (including nested inside `extra`) raise `ValueError` instead of
  being silently written as invalid JSON. `Ledger.add` rejects them before acquiring the lock;
  `Ledger.verify` and `Ledger.records` parse each line with a `parse_constant` that turns those
  tokens into `INVALID_JSON` instead of a Python float. Existing `record_hash` values are
  unaffected (`tests/vectors/canonical_v1.json` is unchanged).
- CI matrix (`.github/workflows/tests.yml`) widened to Python 3.9-3.14 on Linux, 3.9 and 3.14 on
  Windows, 3.14 on macOS, plus the existing separate `[mcp]`-extra job (now also installing
  `jsonschema`). Every job additionally builds a wheel (`pip wheel . -w dist --no-deps`), installs
  it into a fresh venv, and smoke-tests `provtrail --help`, `add`, `verify` and `head` from that
  venv, on both bash and Windows runners (`shell: bash` throughout).
- README: measured append/verify performance numbers in Limitations (item L), the package's
  one-line description ("Source provenance ledger for LLM-assisted research") at the top and in
  `pyproject.toml`, an explicit note that the Stop hook shows capture happened rather than that
  every source used was captured, and a signed-git-commit / RFC 3161 timestamping example for
  anchoring.
- Head anchoring: `provtrail head LEDGER [--json]` prints the `seq:record_hash`
  of the last record; `Ledger.head()` returns it as a tuple. `provtrail verify
  --expect SEQ:HASH` (repeatable) and `--expect-file FILE` check one or more
  external anchors against the ledger, reporting `ANCHOR_MISSING` (no record
  at that `seq`, e.g. truncation) or `ANCHOR_MISMATCH` (record exists, hash
  differs). `Ledger.verify` takes a matching `expect` parameter. A malformed
  `--expect`/`--expect-file` entry is a usage error (exit 2).
- Stricter `verify`: new `INVALID_KIND` (missing or unrecognized `kind`) and
  `INVALID_CONTENT_HASH` (present but malformed `content_hash`) violation
  codes. `is_valid_content_hash` now rejects upper-case hex, closing the gap
  noted in the 0.1.x Limitations section.
- Stop hook `scope` setting (`"session"`, default, or `"turn"`), resolved the
  same way as `mode` (`PROVTRAIL_SCOPE` env var, then the config file's
  `"scope"` field, then `"session"`). In `turn` scope the window opens at the
  last human-prompt entry in the transcript rather than the first, so the
  check narrows to the turn that is ending. Stop-hook feedback and tool-result
  transcript entries are excluded explicitly, since both share `type ==
  "user"` with a real prompt. No qualifying entry, or an unreadable
  transcript, resolves to `UNKNOWN` and never falls back to session scope.
- `Ledger.add(..., content_root=...)`: when set together with `content_path`,
  the realpath of `content_path` must resolve inside `content_root`, else
  `ValueError`. Default `None` preserves prior behaviour. This is a
  path-containment check, not a security sandbox (no defense against a
  time-of-check/time-of-use symlink swap, hardlinks, or other aliasing). The
  MCP server now passes the ledger's directory as `content_root`.
- `tests/vectors/canonical_v1.json`: canonicalization + `record_hash` test
  vectors (ASCII, non-ASCII, nested `extra`, integers, a float, `prev_hash:
  null`) for anyone implementing a provtrail verifier in another language.
- GitHub Actions workflow (`.github/workflows/tests.yml`): a 3 OS x 2 Python
  version matrix, plus a separate job installing the `[mcp]` extra.

### Changed
- `Ledger.add` now reads the ledger file once instead of twice: the
  verification pass it already runs before appending returns the last record,
  so the second, redundant full scan is gone. Behaviour is unchanged.
- README: documented the anchor workflow (A), the new violation codes (B),
  turn scope (C), and `content_root` (D); moved the now-fixed anchoring and
  `kind`/hash validation gaps out of Limitations.

## [0.1.2] - 2026-09-11

### Changed
- Package metadata: project URLs (repository, issues, changelog), so the
  PyPI page links to the source repository. No code changes.

## [0.1.1] - 2026-09-11

### Changed
- Documentation: Claude Code passes `CLAUDE_CODE_SESSION_ID` to stdio
  MCP server processes as well as to Bash tool subprocesses (measured),
  so records written through `provtrail-mcp` match the Stop hook's
  session check without an explicit `session_id`. The README and the
  MCP server docstring previously described this as unverified.

## [0.1.0] - 2026-09-11

### Added
- Hash-chained JSONL ledger: `Ledger.add`, `Ledger.records`,
  `Ledger.verify`, and `Ledger.check` (optionally scoped by `session_id`
  and bounded by `since`/`until`). `add` refuses to append to a ledger
  that fails verification.
- JSON Schema (draft 2020-12) for record format v1. `provtrail verify`
  is the normative check; the schema is structural.
- CLI: `provtrail add`, `provtrail verify`, `provtrail check`. `add`
  takes `--session-id` from `$CLAUDE_CODE_SESSION_ID` by default.
- `provtrail.config.resolve_config`: ledger path and mode resolution
  shared by the Stop hook and the MCP server.
- Claude Code Stop hook (`provtrail-stop-hook` command, module
  `provtrail.stop_hook`) with `report` (default), `enforce`, and `strict`
  modes, matched to the payload's `session_id`. It reports `UNKNOWN`
  when it cannot scope the check to the session, and reports unexpected
  errors via `systemMessage`.
- Claude Code agent skill describing when and how to capture sources.
- MCP server (`provtrail-mcp` command, module `provtrail.mcp_server`)
  for mcp 1.x and 2.x. The ledger is resolved from configuration, not passed by
  the caller; files are accepted via `content_path` only inside the
  ledger's directory.
