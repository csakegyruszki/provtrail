# provtrail

Source provenance ledger for LLM-assisted research.

provtrail is an append-only, hash-chained JSONL ledger for recording which sources were captured
during LLM-assisted research, and when. It has no runtime dependencies beyond the Python standard
library. It includes a command-line interface, a Claude Code Stop hook, an agent skill, and an
optional MCP server. What a verified ledger does and does not prove is set out under "What the
ledger establishes, and what it does not".

## Installation

```bash
pip install provtrail            # from PyPI
pip install "provtrail[mcp]"     # with the optional MCP server dependency
pip install -e .                 # from a clone
```

The core package and the Stop hook require Python 3.9 or later. The `[mcp]` extra requires Python
3.10 or later, because the `mcp` package does not support 3.9.

## Usage

```bash
# append a record
provtrail add ./ledger.jsonl --url "https://example.com/report" \
  --title "Example report" --claim "X happened in 2024"

# append a record identified by content rather than URL
provtrail add ./ledger.jsonl --content-file ./report.pdf --kind file --path report.pdf

# verify the chain (add --check-files to re-hash referenced files)
provtrail verify ./ledger.jsonl

# report whether a valid record exists in a time window and/or for a session
provtrail check ./ledger.jsonl --since 2024-01-01T00:00:00Z
provtrail check ./ledger.jsonl --since 2024-01-01T00:00:00Z --until 2024-01-02T00:00:00Z
provtrail check ./ledger.jsonl --session-id abc123

# print the last record's seq:record_hash
provtrail head ./ledger.jsonl

# print the installed version
provtrail --version

# verify against one or more previously recorded anchors
provtrail verify ./ledger.jsonl --expect 42:sha256:<64-hex-digest>
provtrail verify ./ledger.jsonl --expect-file ./anchors.txt
```

Exit codes:

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `add` | record appended | contract violation, invalid `kind`, ledger fails verification, or lock timeout | usage error |
| `verify` | no violations | violations found (including a missing or mismatched `--expect` anchor) | malformed `--expect`/`--expect-file` entry |
| `check` | all states without `--enforce` | - | `MISSING` under `--enforce`; usage error |
| `head` | printed `seq:record_hash` | ledger has no records, or fails verification | usage error |

`check` returns one of three states:

- `PRESENT`: the ledger verifies and contains a matching record.
- `MISSING`: the ledger verifies and contains no matching record.
- `UNKNOWN`: the check could not be performed (ledger absent, chain fails verification, or a
  timestamp argument is unparseable).

`UNKNOWN` never produces a non-zero exit, including under `--enforce`: a failed check is reported,
not treated as a confirmed absence.

### Anchoring

`provtrail verify` alone cannot tell a ledger that lost its last few records from one that never
had them: a truncated file, or a whole file rewritten from a truncated copy, still verifies as a
valid, shorter chain. Detecting that requires a record of the last `record_hash` kept somewhere
the ledger itself cannot reach.

`provtrail head LEDGER` prints that anchor as `seq:record_hash` (`--json` for `{"seq": N,
"record_hash": "sha256:..."}`); it exits 1 if the ledger has no records or fails verification.
Store the anchor outside the ledger, for example in a signed git commit, a separate append-only log
or a timestamping service, and check it later with `provtrail verify --expect`.

Anchoring in a signed git commit:

```bash
anchor=$(provtrail head ./ledger.jsonl)
git commit --allow-empty -S -m "provtrail anchor: $anchor"
```

The commit's GPG/SSH signature and its position in the git history then vouch for when that anchor
was recorded; later, `git show --show-signature <commit>` recovers the anchor text to check with
`provtrail verify --expect`. Anchoring with a timestamping service (RFC 3161) instead:

```bash
anchor=$(provtrail head ./ledger.jsonl)
echo -n "$anchor" > anchor.txt
openssl ts -query -data anchor.txt -no_nonce -sha256 -out anchor.tsq
curl -s -H "Content-Type: application/timestamp-query" --data-binary @anchor.tsq \
  https://freetsa.org/tsr > anchor.tsr
```

`anchor.tsr` is a timestamp token binding the anchor's hash to a time attested by the timestamping
authority; keep it alongside `anchor.txt`. provtrail itself does not sign, timestamp or transmit
anything; both examples are external to the ledger.

Checking stored anchors:

```bash
provtrail verify ./ledger.jsonl --expect 42:sha256:<64-hex-digest>
# or, for several anchors collected over time, one per line:
provtrail verify ./ledger.jsonl --expect-file ./anchors.txt
```

`--expect-file` lines are `seq:sha256:<64 hex>`; blank lines and lines starting with `#` are
ignored. Each anchor must match a record with that exact `seq` and `record_hash`, checked against
the recomputed hash (not merely the stored one), so a forged replacement record is still caught.
Violations: `ANCHOR_MISSING` (no record at that `seq`, e.g. the ledger was truncated) and
`ANCHOR_MISMATCH` (a record exists there, but its hash differs). A malformed `--expect` or
`--expect-file` entry is a usage error (exit 2), not a violation.

Library equivalents: `Ledger.head()` returns `(seq, record_hash)` or `None`; `Ledger.verify(...,
expect=[(seq, record_hash), ...])` checks the same anchors and folds `ANCHOR_MISSING`/
`ANCHOR_MISMATCH` into its usual violation list.

## What the ledger establishes, and what it does not

The ledger establishes:

- that the existing chain has not been edited, reordered or truncated in the middle by an
  accidental or partial change: `provtrail verify` recomputes every hash and walks the
  `prev_hash` chain, so a record changed without re-hashing everything after it is detected.
  Someone who can rewrite the affected record and every later one, recomputing their hashes,
  can produce another internally valid ledger. Detecting that requires an external anchor
  (see "Anchoring");
- that, for a record with a `content_hash`, the referenced bytes are the ones that were hashed
  (`verify --check-files` re-hashes files stored under the ledger's directory).

The ledger does not establish:

- that a source was actually read or consulted. A record can be written without reading anything,
  and the tool cannot tell a genuine capture from a fabricated one;
- that a URL was reachable, or that supplied content came from that URL. provtrail never makes
  network requests;
- that a source supports the `claim` stored next to it;
- when a capture happened, beyond the caller's own clock. `captured_at` is written by the process
  that appends the record; it is not a trusted timestamp;
- that nothing was removed from the end of the file, or that the whole file was not rewritten,
  unless an anchor was recorded outside the ledger beforehand. `provtrail head` prints the last
  record's `seq:record_hash`; checking a stored anchor later with `provtrail verify --expect`
  detects truncation and whole-file rewrites (see "Anchoring"). Without a stored anchor, neither
  is detectable.

The Claude Code Stop hook checks that at least one valid record exists for the current session or
turn. It does not know how many sources the session drew on, so one record satisfies it as fully
as ten. Proving that every source was captured would require matching each tool event (each web
fetch, each file read) to a ledger record, which provtrail does not do. The hook is a reminder to
capture sources, not an audit control.

## Record contract

`provtrail verify` accepts a record when all of the following hold (each failure has a code, listed
under "Violation codes"):

- `schema` is `provtrail/v1`, and `seq`, `prev_hash`, `record_hash` and `id` are consistent with
  the chain;
- `captured_at` is an RFC 3339 `date-time`: `T` separator, optional fractional seconds, and `Z` or
  a `±HH:MM` offset (a leap second `:60` is accepted and treated as `:59`);
- `source_url` is a non-empty string, or `content_hash` is `sha256:` followed by 64 lower-case hex
  digits, or both;
- `kind` is one of `url`, `search`, `scrape`, `file`, `manual` (`provtrail add` defaults it to
  `url`);
- every key is a v1 field, and every field has its documented type.

`tool`, `query`, `title`, `claim`, `snippet`, `archived_url`, `path`, `session_id` and `extra` are
optional. `provtrail add` applies the same field checks before writing, so it never appends a
record that `verify` would reject.

## Record schema (v1)

The JSON Schema (draft 2020-12) is in `schema/provtrail-record.v1.json`. It is a structural check
of types, required fields and patterns. `provtrail verify` is the normative check: it parses
`captured_at` with the ledger's own RFC 3339 rules, recomputes every hash and walks the chain,
which a schema validator does not do.

| Field | Type | Notes |
|---|---|---|
| `schema` | string | Always `"provtrail/v1"`. |
| `seq` | integer | 1-based, contiguous within a ledger. |
| `id` | string | `"ev_"` followed by the first 16 hex characters of the `record_hash` digest. |
| `captured_at` | string | RFC 3339 timestamp, set by the appending process. Required. |
| `source_url` | string | Required unless `content_hash` is set. |
| `content_hash` | string | `sha256:<64 hex>`. Required unless `source_url` is set. |
| `kind` | string | `url`, `search`, `scrape`, `file`, or `manual`. Default `url`. |
| `tool` | string | Capturing tool or backend, e.g. `web-search:api`. |
| `query` | string | Search query, if any. |
| `title` | string | Source title. |
| `claim` | string | The statement the source is recorded for. Not checked. |
| `snippet` | string | Short excerpt. |
| `archived_url` | string | URL of an archived copy. |
| `path` | string | Artifact path relative to the ledger's directory. Absolute paths and paths that leave the directory are rejected. |
| `session_id` | string | Identifier of the capturing session. |
| `extra` | object | Free-form metadata. |
| `prev_hash` | string or null | `record_hash` of the previous record; `null` for `seq` 1. |
| `record_hash` | string | `sha256:` digest of the canonical JSON of the record, excluding `record_hash` and `id`. |

Canonical JSON is
`json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`
encoded as UTF-8. `NaN` and `Infinity` are rejected. `extra` may hold only JSON values:
objects with string keys, arrays, strings, numbers, booleans and `null`. Anything JSON would
rewrite or cannot represent, such as a `None` or integer key, a tuple, a set or bytes, is rejected.
Subclasses of the built-in types (an `OrderedDict`, an `IntEnum` member) are accepted and read
back as the plain type, with an equal value.

### Violation codes

| Code | Meaning |
|---|---|
| `INVALID_JSON` | Line is not valid UTF-8, not valid JSON, or not a JSON object. |
| `WRONG_SCHEMA` | `schema` is not `provtrail/v1`. |
| `MISSING_CAPTURED_AT` | `captured_at` is absent or empty. |
| `INVALID_CAPTURED_AT` | `captured_at` is present but not parseable. |
| `MISSING_SOURCE_LOCATOR` | Neither `source_url` nor a valid `content_hash` is present. |
| `INVALID_KIND` | `kind` is missing or not one of `url`, `search`, `scrape`, `file`, `manual`. |
| `INVALID_CONTENT_HASH` | `content_hash` is present but not a well-formed `sha256:<64 lower-case hex>` string. |
| `UNKNOWN_FIELD` | The record has a key that is not a field of the v1 schema. |
| `INVALID_FIELD_TYPE` | A field has the wrong type: `seq` not an integer >= 1, a string field holding a non-string, `extra` not an object or holding a non-JSON value, `prev_hash` neither `null` nor `sha256:<64 lower-case hex>`. |
| `BAD_SEQ` | `seq` does not follow the previous record. |
| `CHAIN_BROKEN` | `prev_hash` does not match the previous record's `record_hash`. |
| `HASH_MISMATCH` | `record_hash` does not match the recomputed value. |
| `ID_MISMATCH` | `id` does not match the value derived from the recomputed hash. |
| `INVALID_PATH` | `path` is absolute or leaves the ledger's directory (with `--check-files`, also after following symlinks). |
| `FILE_MISSING` | `--check-files`: referenced file is absent or unreadable. |
| `FILE_HASH_MISMATCH` | `--check-files`: file content does not match `content_hash`. |
| `ANCHOR_MISSING` | `verify --expect`/`--expect-file`: no record exists at the expected `seq`. |
| `ANCHOR_MISMATCH` | `verify --expect`/`--expect-file`: a record exists at that `seq`, but its hash differs. |

## Claude Code integration

### Stop hook

The Stop hook runs at the end of every turn and reports, or optionally blocks, when the ledger has
no valid record for the current session. It runs regardless of which tools the model called.

1. Add the `Stop` entry from `integrations/claude-code/settings.example.json` to the project's
   `.claude/settings.json`. It runs the `provtrail-stop-hook` command installed with the package;
   if that command is not on the `PATH` Claude Code uses, give its absolute path, or use
   `python -m provtrail.stop_hook` with the interpreter where provtrail is installed. From a source
   checkout, point the command at `integrations/claude-code/stop_hook.py`.
2. Create `.provtrail.json` in the project root:
   ```json
   { "ledger": "./ledger.jsonl", "mode": "report" }
   ```
   Alternatively set `PROVTRAIL_LEDGER` and, optionally, `PROVTRAIL_MODE`.

Without a configured ledger the hook exits silently.

Modes:

| Mode | MISSING | UNKNOWN |
|---|---|---|
| `report` (default) | `systemMessage`, never blocks | `systemMessage`, never blocks |
| `enforce` | blocks | `systemMessage`, never blocks |
| `strict` | blocks | blocks |

`PRESENT` never blocks. A hook re-invocation (`stop_hook_active` true) never blocks either, which
prevents Stop-hook loops. `report` is the recommended mode; see "What the ledger establishes" for
why `enforce` and `strict` are not audit controls.

Scope of the check:

- If the hook payload includes `session_id`, only records with the same `session_id` count.
- Records dated more than 300 seconds after the hook runs do not count (a margin for clock
  differences between the writer and the hook).
- The window opens at the first `timestamp` found in the session transcript, when there is one
  (`scope: "session"`, the default; see "Turn scope" below for `scope: "turn"`). This relies on
  the transcript's JSONL format, which Claude Code does not document; if the format changes, the
  hook falls back to `session_id` alone, or to `UNKNOWN` when neither is available.
- An absent ledger file in an existing directory counts as `MISSING`; an absent directory as
  `UNKNOWN` (probably a misconfigured path).
- Unexpected errors, such as a malformed `.provtrail.json` or an invalid mode or scope, are
  reported as a `systemMessage`; in `strict` mode they block, unless `stop_hook_active` is true.
  The hook always exits 0.

Configuration precedence (shared by the hook and the MCP server through
`provtrail.config.resolve_config` and `provtrail.config.resolve_scope`):

- Ledger path: `PROVTRAIL_LEDGER`, then `"ledger"` in `<project>/.provtrail.json`. Relative paths
  resolve against the project directory.
- Mode, resolved independently of the ledger path: `PROVTRAIL_MODE`, then the legacy
  `PROVTRAIL_ENFORCE=1`, then `"mode"` in `.provtrail.json`, then the legacy `"enforce": true`,
  then `report`. An invalid mode value is an error, not a silent fallback.
- Scope, resolved the same way (minus the legacy aliases): `PROVTRAIL_SCOPE`, then `"scope"` in
  `.provtrail.json`, then `session`. An invalid scope value is likewise an error.

#### Turn scope

`scope: "turn"` narrows the check from "was something captured this session" to "was something
captured during the turn that is ending". Instead of opening the window at the transcript's first
timestamp, it opens at the LAST transcript entry that is a human prompt: `type == "user"`,
`isMeta` not `true`, `isSidechain` not `true`, and `message.content` either a plain string or a
list containing no `tool_result` block. Two other kinds of transcript entry also have `type ==
"user"` and must not be mistaken for a prompt: Stop-hook feedback (`isMeta: true`, injected by a
previous hook run) and tool-result entries. If no qualifying entry exists, or the transcript
cannot be read, the state is `UNKNOWN`; turn scope never falls back to session scope's
first-timestamp behaviour. The `session_id` filter and the 300-second future bound still apply on
top of the turn window.

This transcript shape is observed, not documented by Claude Code, and may change without notice.
It was measured in two Claude Code 2.1.269 `claude -p` transcripts: a human prompt is a plain
string `message.content` with no `isMeta`; a tool result is a list `message.content` containing a
`tool_result` block, plus a top-level `toolUseResult` field on the entry; Stop-hook feedback is a
string `message.content` with `isMeta: true`.

### Agent skill

`integrations/claude-code/SKILL.md` describes when and how the model should call `provtrail add`.
Install it as a project or user skill.

### MCP server

`provtrail-mcp` (module `provtrail.mcp_server`) is an MCP server over stdio that exposes
`provtrail_add` and `provtrail_verify`. It requires the `[mcp]` extra and works with mcp 1.x and
2.x.

Neither tool takes a ledger path; the server resolves it from its working directory with the same
configuration rules as the hook. `provtrail_add` accepts `source_url`, `content`, `content_path`,
`tool`, `kind`, `claim`, `title`, `query`, `snippet`, `archived_url` and `session_id`. A
`content_path` must resolve, after following symlinks, inside the ledger's directory; the file is
hashed from its raw bytes and its relative path is stored in `path`. The server also passes the
ledger's directory to `Ledger.add` as `content_root` (see below), so the library enforces the same
rule a second time.
`session_id` defaults to the `CLAUDE_CODE_SESSION_ID` environment variable, which Claude Code
sets for Bash tool subprocesses and stdio MCP server processes.

### `Ledger.add(..., content_root=...)`

When `content_root` is given together with `content_path`, the realpath of `content_path` must
resolve inside `content_root`, else `Ledger.add` raises `ValueError` before writing anything. With
the default `None`, any readable `content_path` is hashed.
This is a path-containment check, not a security sandbox: it resolves `content_path` once, at
check time, so it does not stop a file being replaced by a symlink pointing outside
`content_root` between the check and the read (a time-of-check/time-of-use race), and it does not
defend against hardlinks or other filesystem-level aliasing. Use it against accidental misuse
(a caller-supplied path landing outside the intended directory), not as a defense against an
adversarial filesystem.

## Limitations

- **Tamper-evident, not tamper-proof, without a stored anchor.** Editing, reordering or removing a
  record in the middle of the ledger is always detected. Removing records from the end leaves a
  valid, shorter chain, and anyone who can rewrite the whole file can build a new consistent one;
  `provtrail verify` alone cannot tell that apart from a ledger that legitimately never had those
  records. Detecting it requires recording an anchor outside the ledger first and checking it
  later (see "Anchoring"); without a stored anchor, it is undetectable.
- **A ledger that fails verification is not extended.** `add` verifies the ledger before
  appending and refuses if any violation is found. Repair the file or start a new ledger.
- **Cost of appending.** Every `add` verifies the whole ledger, so appending is linear in the
  ledger's length. Measured on 2026-09-12 on a Windows 11 laptop (Intel Core i5-13420H,
  32 GB RAM, NVMe SSD; Python 3.13.14; `Ledger.add` in a loop, timing the last 100 appends at each
  size): at 1,000 records, append latency p50 = 36.3 ms,
  p95 = 45.9 ms, and `verify` on the full ledger takes 0.04 s; at 10,000 records, append p50 =
  176.8 ms, p95 = 205.5 ms, and `verify` takes 0.18 s. This is fine for research sessions of
  hundreds or thousands of records, not for very large ledgers.
- **Advisory locking.** `<ledger>.lock` serialises concurrent `add` calls on one machine. It does
  not stop a process that ignores it and is not reliable on network file systems. The lock file
  records an ownership token, the owner's PID and the acquisition time; a process removes the
  lock on release only if it still holds its own token. This reduces, but does not eliminate,
  the race that follows when a lock is deleted while its owner is still running. A lock left by
  a crashed process is not cleared automatically: remove it manually only after confirming that
  no process with the recorded PID is still running `provtrail`.
- **Canonical JSON is Python's.** The hash input is Python's `json.dumps` output as specified
  above, not RFC 8785 (JCS). A verifier in another language must reproduce it exactly; see
  `tests/vectors/canonical_v1.json` for worked input/output pairs covering ASCII, non-ASCII,
  nested objects, integers, a float, and `prev_hash: null`.
- **The library API's containment is opt-in, not a sandbox.** `Ledger.add(content_path=...)`
  hashes any file the caller can read unless `content_root` is passed (see above), and even then
  it is a containment check, not a defense against a hostile filesystem.
- **Plain-text storage.** The ledger is unencrypted JSONL. Do not store credentials, tokens or
  personal data in any field.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT. See `LICENSE`.
