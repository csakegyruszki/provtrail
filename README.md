# provtrail

provtrail is an append-only, hash-chained JSONL ledger for recording the
sources used in LLM-assisted research. It has no runtime dependencies
beyond the Python standard library and ships with a Claude Code Stop hook,
an agent skill, and an optional MCP server.

## Record contract

A record is valid if and only if:

- `captured_at` is an RFC 3339 `date-time`: `T` separator, optional
  fractional seconds, and `Z` or a `±HH:MM` offset (a leap second `:60`
  is accepted and treated as `:59`), and
- `source_url` is a non-empty string, or `content_hash` is a
  `sha256:<64 hex>` string (or both).

All other fields (`kind`, `tool`, `query`, `title`, `claim`, `snippet`,
`archived_url`, `path`, `session_id`, `extra`) are optional metadata.

Each record stores the `record_hash` of its predecessor in `prev_hash`.
`provtrail verify` walks the whole file and reports every violation with
a stable error code.

## Scope

provtrail records *which sources were captured and when*. It does not:

- extract or track claims from a finished manuscript;
- log agent actions or tool calls in general;
- capture, render, or archive web pages. It hashes content you supply
  and chains the records; preserving the page itself is left to a
  dedicated capture tool, whose output can be logged as a provtrail record.

## Installation

```bash
pip install -e .            # from a clone
pip install -e ".[mcp]"     # with the optional MCP server dependency
```

The core package and the Stop hook require Python 3.9 or later. The
`[mcp]` extra requires Python 3.10 or later, because the `mcp` package
does not support 3.9.

## Usage

```bash
# append a record
provtrail add ./ledger.jsonl --url "https://example.com/report" \
  --title "Example report" --claim "X happened in 2024"

# append a record identified by content rather than URL
provtrail add ./ledger.jsonl --content-file ./report.pdf --kind file --path report.pdf

# verify the chain (add --check-files to re-hash referenced files)
provtrail verify ./ledger.jsonl

# report whether any valid record exists at or after a timestamp
provtrail check ./ledger.jsonl --since 2024-01-01T00:00:00Z

# narrow further to a time window and/or a specific session
provtrail check ./ledger.jsonl --since 2024-01-01T00:00:00Z --until 2024-01-02T00:00:00Z
provtrail check ./ledger.jsonl --session-id abc123
```

Exit codes:

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `add` | record appended | contract violation, invalid `kind`, or lock timeout | usage error |
| `verify` | no violations | violations found | usage error |
| `check` | all states without `--enforce` | — | `MISSING` under `--enforce`; usage error |

`check` returns one of three states:

- `PRESENT`: the ledger verifies and contains a matching record.
- `MISSING`: the ledger verifies and contains no matching record.
- `UNKNOWN`: the check could not be performed (ledger absent, chain
  fails verification, or `--since` is unparseable).

`UNKNOWN` never produces a non-zero exit, including under `--enforce`.
A failed measurement is reported, not treated as a confirmed absence.

## Record schema (v1)

The JSON Schema (draft 2020-12) is in `schema/provtrail-record.v1.json`.

### Validation layers

The JSON Schema is a structural check: it constrains types, required
fields, and patterns, and annotates `captured_at` with `"format":
"date-time"`. Whether that `format` annotation is actually enforced
depends on the JSON Schema validator; draft 2020-12 treats `format` as
advisory unless the validator opts into assertion behaviour. `provtrail
verify` is the normative check: it parses `captured_at` with the same
RFC 3339 rules the ledger itself uses, recomputes every hash, and
walks the `prev_hash` chain, none of which a schema validator does. Use
the schema for editor/IDE hints and quick structural checks; use
`provtrail verify` to decide whether a ledger is actually trustworthy.

| Field | Type | Notes |
|---|---|---|
| `schema` | string | Always `"provtrail/v1"`. |
| `seq` | integer | 1-based, contiguous within a ledger. |
| `id` | string | `"ev_"` followed by the first 16 hex characters of the `record_hash` digest. |
| `captured_at` | string | RFC 3339 timestamp. Required. |
| `source_url` | string | Required unless `content_hash` is set. |
| `content_hash` | string | `sha256:<64 hex>`. Required unless `source_url` is set. |
| `kind` | string | `url`, `search`, `scrape`, `file`, or `manual`. Default `url`. |
| `tool` | string | Capturing tool or backend, e.g. `web-search:api`. |
| `query` | string | Search query, if any. |
| `title` | string | Source title. |
| `claim` | string | The statement this source supports. |
| `snippet` | string | Short supporting excerpt. |
| `archived_url` | string | URL of an archived copy. |
| `path` | string | Artifact path relative to the ledger's directory. Absolute paths and paths that leave the directory are rejected. |
| `session_id` | string | Identifier of the capturing session. |
| `extra` | object | Free-form metadata. |
| `prev_hash` | string or null | `record_hash` of the previous record; `null` for `seq` 1. |
| `record_hash` | string | `sha256:` digest of the canonical JSON of the record, excluding `record_hash` and `id`. |

Canonical JSON is `json.dumps(record, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` encoded as UTF-8.

### Violation codes

| Code | Meaning |
|---|---|
| `INVALID_JSON` | Line is not valid UTF-8, not valid JSON, or not a JSON object. |
| `WRONG_SCHEMA` | `schema` is not `provtrail/v1`. |
| `MISSING_CAPTURED_AT` | `captured_at` is absent or empty. |
| `INVALID_CAPTURED_AT` | `captured_at` is present but not parseable. |
| `MISSING_SOURCE_LOCATOR` | Neither `source_url` nor a valid `content_hash` is present. |
| `BAD_SEQ` | `seq` does not follow the previous record. |
| `CHAIN_BROKEN` | `prev_hash` does not match the previous record's `record_hash`. |
| `HASH_MISMATCH` | `record_hash` does not match the recomputed value. |
| `ID_MISMATCH` | `id` does not match the value derived from the recomputed hash. |
| `INVALID_PATH` | `path` is absolute or leaves the ledger's directory (with `--check-files`, also after following symlinks). |
| `FILE_MISSING` | `--check-files`: referenced file is absent or unreadable. |
| `FILE_HASH_MISMATCH` | `--check-files`: file content does not match `content_hash`. |

## Claude Code integration

### Stop hook

A capture tool that the model has to call explicitly is easy to skip,
especially late in a long session. The Stop hook checks the ledger
state at the end of every turn regardless of what the model called, so
it is the recommended integration.

1. Add the `Stop` entry from `integrations/claude-code/settings.example.json`
   to your project's `.claude/settings.json`. It runs the
   `provtrail-stop-hook` command installed with the package. If that
   command is not on the `PATH` Claude Code uses, give its absolute
   path, or use `python -m provtrail.stop_hook` with the interpreter of
   the environment where provtrail is installed. From a source checkout
   without installing, point the command at
   `integrations/claude-code/stop_hook.py`.
2. Create `.provtrail.json` in the project root:
   ```json
   { "ledger": "./ledger.jsonl", "mode": "report" }
   ```
   Alternatively set `PROVTRAIL_LEDGER` and, optionally, `PROVTRAIL_MODE`.

Without a configured ledger, the hook exits silently; projects opt in
explicitly.

#### Configuration precedence

The Stop hook and the MCP server both resolve configuration through
`provtrail.config.resolve_config`, so they always agree.

Ledger path:

1. `PROVTRAIL_LEDGER` environment variable.
2. the `"ledger"` field of `<project>/.provtrail.json`.

A relative path is resolved against the project directory (the hook
payload's `cwd` for the hook; the server's own working directory for
the MCP server), not the process's own working directory.

Mode, resolved **independently** of the ledger path (setting only
`PROVTRAIL_LEDGER` does not reset a mode configured in the file):

1. `PROVTRAIL_MODE` environment variable.
2. legacy `PROVTRAIL_ENFORCE=1` environment variable, equivalent to `enforce`.
3. the `"mode"` field of `.provtrail.json`.
4. legacy `"enforce": true` in `.provtrail.json`, equivalent to `enforce`.
5. default: `report`.

An invalid mode value (from either source) raises an error rather than
silently falling back.

#### Modes

| Mode | MISSING | UNKNOWN |
|---|---|---|
| `report` (default) | `systemMessage`, never blocks | `systemMessage`, never blocks |
| `enforce` | blocks | `systemMessage`, never blocks |
| `strict` | blocks | blocks |

`PRESENT` never blocks in any mode. A hook re-invocation
(`stop_hook_active` true) never blocks in any mode either, which
prevents Stop-hook loops.

#### Session matching and the `until` bound

- If the Stop-hook payload includes `session_id`, only ledger records
  whose own `session_id` field equals it count towards `PRESENT`. This
  stops a record captured in an unrelated session from satisfying the
  check.
- The check window closes 300 seconds after the hook runs (an `until`
  bound), so a record dated more than 300 seconds in the future does
  not count. The margin allows for clock differences between the
  process that writes the record and the hook.
- The window still opens at the first timestamp found in the session
  transcript, when there is one, so the hook asks whether a source was
  captured **during the current session**, not during the latest turn.
- If `session_id` is absent from the payload AND the transcript has no
  timestamp (missing, unreadable, or simply empty of them), the check
  cannot be scoped to this session at all: the state is `UNKNOWN`
  rather than treating an unrelated old record as `PRESENT`.
- If the ledger file does not exist but its directory does, the hook
  treats the state as `MISSING` (nothing captured yet). If the
  directory is also missing, the state is `UNKNOWN`, since the path is
  probably misconfigured.
- Any unexpected error (a malformed `.provtrail.json`, an invalid mode
  value, etc.) is reported as a `systemMessage` naming the exception,
  never silently swallowed. If the mode had already resolved to
  `strict` before the error, the hook blocks instead of just
  reporting, unless `stop_hook_active` is true. The hook always exits 0.

### Agent skill

`integrations/claude-code/SKILL.md` describes when and how the model
should call `provtrail add`. Install it as a project or user skill.

### MCP server

`provtrail-mcp` (module `provtrail.mcp_server`; source-checkout entry
point `integrations/mcp/provtrail_mcp.py`) is an MCP server over stdio,
built on `MCPServer` with mcp 2.x and `FastMCP` with mcp 1.x, that
exposes `provtrail_add` and `provtrail_verify`. It requires the `[mcp]`
extra. It is a convenience for models that call tools explicitly and
does not replace the Stop hook.

Neither tool takes a ledger path; the server resolves it itself (the
same `resolve_config` used by the Stop hook, applied to the server's
own working directory). `provtrail_add` accepts a narrower set of
fields than the CLI: `source_url`, `content`, `content_path`, `tool`,
`kind`, `claim`, `title`, `query`, `snippet`, `archived_url`, and
`session_id`. A `content_path` is resolved against the ledger's
directory when relative and must resolve (after following symlinks)
inside that directory; it is rejected otherwise. The file is hashed
from its raw bytes, and its path relative to the ledger is stored in
`path`, so `provtrail verify --check-files` can re-hash it. `session_id` falls
back to the `CLAUDE_CODE_SESSION_ID` environment variable when not
supplied, but whether Claude Code actually sets that variable for an
MCP server process is not verified (it is verified for Bash tool
subprocesses), so pass `session_id` explicitly if session matching
against the Stop hook matters for your workflow.

## Security model and limitations

- **Tamper-evident, not tamper-proof.** Editing, reordering, or removing
  a record in the middle of the ledger is detected by `verify`.
  Truncation is not: dropping the last N records leaves a valid, shorter
  chain, and anyone able to rewrite the whole file can build a new
  consistent chain. To detect either, record the latest `record_hash`
  outside the ledger periodically (a commit message, a separate log, or
  a timestamping service).
- **A ledger that fails verification is not extended.** `add` verifies
  the ledger before appending and refuses (exit 1 from the CLI) if any
  violation is found, so an edited file, or one left with a partial
  last line by a crash, never gets new records chained onto it. Repair
  the file or start a new ledger before capturing again.
- **Advisory locking.** `<ledger>.lock` serialises concurrent
  `provtrail add` calls. It does not stop a process that ignores the
  lock or writes to the file directly. A lock left behind by a crashed
  process causes `add` to time out and must be removed manually.
- **No network access.** provtrail never fetches a URL and does not check
  that a URL is reachable or that supplied content came from it.
- **Plain-text storage.** The ledger is unencrypted JSONL. Do not store
  credentials, tokens, or personal data in any field.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT. See `LICENSE`.
