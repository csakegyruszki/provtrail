# Changelog

All notable changes to this project are documented in this file.

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
