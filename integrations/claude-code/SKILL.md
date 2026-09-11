---
name: provtrail-capture
description: Use when doing research with Claude Code (or another LLM-assisted workflow) and a project has opted into provtrail source tracking (a `.provtrail.json` file, or `PROVTRAIL_LEDGER` set, exists in the project). Records each source consulted (URL, search result, scrape, or file) in the project's provtrail ledger so that it can be audited later. Do not use this for projects without a provtrail ledger configured.
---

# provtrail source capture

provtrail is an append-only, hash-chained ledger of captured sources.
It allows the sources behind a piece of work to be checked afterwards
from the ledger, rather than reconstructed from the conversation.

## When to capture

Capture a record whenever you:
- Fetch or read a URL that informs a claim you are about to make.
- Get a result from a search tool that you use in your answer.
- Scrape a page.
- Read a local file that stands in for a "source" (e.g. a downloaded
  report, a saved dataset).

You do not need to capture purely internal reasoning, code you write,
or files you create yourself. Capture only external material that
supports a factual claim.

## How to capture

Prefer the CLI (always available, no extra dependency):

```bash
provtrail add <ledger-path> \
  --url "https://example.com/article" \
  --tool "browser:manual" \
  --claim "The claim this source supports" \
  --title "Article title" \
  --snippet "The exact sentence or number you are relying on"
```

For content you have in hand (e.g. scraped text) instead of, or in
addition to, a URL:

```bash
provtrail add <ledger-path> --content-text "the captured text" --tool "scraper:custom"
```

The CLI records the session ID automatically from the
`CLAUDE_CODE_SESSION_ID` environment variable; you do not need to pass
`--session-id` yourself inside a Claude Code session.

If the provtrail MCP server is configured for this project, its
`provtrail_add` tool is an alternative to the CLI, not an identical
copy of it. It does not take a ledger path (it resolves the project's
ledger itself) and it accepts a narrower set of fields: `source_url`,
`content`, `content_path`, `tool`, `kind`, `claim`, `title`, `query`,
`snippet`, `archived_url`, and `session_id`. For a file already on
disk, use `content_path`: the file must resolve inside the ledger's
directory, is hashed from its raw bytes, and its relative path is
recorded. The project's Stop hook checks the ledger independently at
the end of each turn.

## What NOT to do

- Do not fabricate a `source_url` or a `content_hash` for something you
  did not actually fetch.
- Do not capture secrets, credentials, or private data as ledger
  content; the ledger is plain JSONL, not an encrypted store.
- Do not edit or delete existing ledger lines. The ledger is
  append-only; if a mistake was made, add a new corrective record.

## Where the ledger lives

The ledger path for the current project is either the value of the
`PROVTRAIL_LEDGER` environment variable, or the `"ledger"` field of
`.provtrail.json` in the project root. If neither exists, this project
has not opted into provtrail tracking. Do not create a ledger without
asking the user first.
