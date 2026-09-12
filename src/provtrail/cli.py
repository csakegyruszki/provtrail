"""Command-line interface for provtrail.

Usage:
    provtrail add LEDGER (--url U | --content-file F | --content-text S) ...
    provtrail verify LEDGER [--check-files] [--expect SEQ:HASH ...] [--expect-file FILE] [--json]
    provtrail check LEDGER [--since ISO] [--until ISO] [--session-id ID] [--enforce] [--json]
    provtrail head LEDGER [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import List, Optional, Tuple

from .ledger import (
    STATE_MISSING,
    CheckResult,
    ContractError,
    Ledger,
    LedgerError,
    LockTimeout,
    VerifyReport,
)

_EXPECT_RE = re.compile(r"^(\d+):(sha256:[0-9a-f]{64})$")


def _build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="provtrail", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="append one record to a ledger")
    add_p.add_argument("ledger", help="path to the ledger JSONL file")
    add_p.add_argument("--url", dest="source_url", default=None, help="source URL")
    add_p.add_argument(
        "--content-file", dest="content_file", default=None,
        help="path to a file whose content is hashed for content_hash",
    )
    add_p.add_argument(
        "--content-text", dest="content_text", default=None,
        help="literal text content hashed for content_hash",
    )
    add_p.add_argument("--kind", default="url", help="record kind (default: url)")
    add_p.add_argument("--tool", default=None, help="capturing tool/backend name")
    add_p.add_argument("--query", default=None, help="search query, if applicable")
    add_p.add_argument("--title", default=None, help="source title")
    add_p.add_argument("--claim", default=None, help="the claim this source supports")
    add_p.add_argument("--snippet", default=None, help="short supporting excerpt")
    add_p.add_argument("--archived-url", default=None, help="archived copy URL")
    add_p.add_argument(
        "--path", default=None,
        help="artifact path, relative to the ledger's directory",
    )
    add_p.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        help="session identifier (default: $CLAUDE_CODE_SESSION_ID)",
    )

    verify_p = sub.add_parser("verify", help="verify a ledger's integrity")
    verify_p.add_argument("ledger", help="path to the ledger JSONL file")
    verify_p.add_argument(
        "--check-files", action="store_true",
        help="also re-hash referenced artifact files and compare",
    )
    verify_p.add_argument(
        "--expect", action="append", default=[], metavar="SEQ:HASH",
        help="require a record at SEQ with this record_hash (repeatable)",
    )
    verify_p.add_argument(
        "--expect-file", default=None, metavar="FILE",
        help="file with one SEQ:HASH anchor per line (blank lines and "
        "lines starting with '#' are ignored)",
    )
    verify_p.add_argument("--json", action="store_true", help="print JSON output")

    head_p = sub.add_parser("head", help="print the seq:record_hash of the last record")
    head_p.add_argument("ledger", help="path to the ledger JSONL file")
    head_p.add_argument("--json", action="store_true", help="print JSON output")

    check_p = sub.add_parser("check", help="report whether a source was captured")
    check_p.add_argument("ledger", help="path to the ledger JSONL file")
    check_p.add_argument(
        "--since", default=None,
        help="only count records captured at/after this RFC3339 timestamp",
    )
    check_p.add_argument(
        "--until", default=None,
        help="only count records captured at/before this RFC3339 timestamp",
    )
    check_p.add_argument(
        "--session-id", default=None,
        help="only count records with this exact session_id",
    )
    check_p.add_argument(
        "--enforce", action="store_true",
        help="exit 2 if state is MISSING (UNKNOWN never blocks)",
    )
    check_p.add_argument("--json", action="store_true", help="print JSON output")

    return parser


def _cmd_add(args: argparse.Namespace) -> int:
    content = args.content_text
    content_path = args.content_file
    ledger = Ledger(args.ledger)
    try:
        record = ledger.add(
            source_url=args.source_url,
            content=content,
            content_path=content_path,
            kind=args.kind,
            tool=args.tool,
            query=args.query,
            title=args.title,
            claim=args.claim,
            snippet=args.snippet,
            archived_url=args.archived_url,
            path=args.path,
            session_id=args.session_id,
        )
    except LockTimeout as e:
        print(f"provtrail add: {e}", file=sys.stderr)
        return 1
    except (ContractError, ValueError, LedgerError, OSError) as e:
        print(f"provtrail add: rejected: {e}", file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


class _UsageError(ValueError):
    """A malformed CLI argument that must exit 2, not 1."""


def _parse_expect_spec(spec: str) -> Tuple[int, str]:
    m = _EXPECT_RE.match(spec.strip())
    if not m:
        raise _UsageError(
            f"invalid --expect value {spec!r}; expected SEQ:sha256:<64 hex>"
        )
    return int(m.group(1)), m.group(2)


def _load_expect_file(path: str) -> List[Tuple[int, str]]:
    expectations: List[Tuple[int, str]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    expectations.append(_parse_expect_spec(line))
                except _UsageError as e:
                    raise _UsageError(f"{path}:{line_no}: {e}") from e
    except OSError as e:
        raise _UsageError(f"could not read --expect-file {path!r}: {e}") from e
    return expectations


def _collect_expectations(args: argparse.Namespace) -> List[Tuple[int, str]]:
    expectations = [_parse_expect_spec(spec) for spec in args.expect]
    if args.expect_file:
        expectations.extend(_load_expect_file(args.expect_file))
    return expectations


def _print_verify_report(report: VerifyReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return
    if report.ok:
        print(f"OK: {report.record_count} record(s) verified, no violations.")
        return
    print(
        f"FAILED: {report.record_count} record(s) checked, "
        f"{len(report.violations)} violation(s):"
    )
    for v in report.violations:
        print(f"  seq/line {v['seq_or_line']}: {v['code']}: {v['message']}")


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        expectations = _collect_expectations(args)
    except _UsageError as e:
        print(f"provtrail verify: {e}", file=sys.stderr)
        return 2

    ledger = Ledger(args.ledger)
    try:
        report = ledger.verify(check_files=args.check_files, expect=expectations)
    except LedgerError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"provtrail verify: {e}", file=sys.stderr)
        return 1
    _print_verify_report(report, args.json)
    return 0 if report.ok else 1


def _cmd_head(args: argparse.Namespace) -> int:
    ledger = Ledger(args.ledger)
    try:
        report = ledger.verify()
    except LedgerError as e:
        print(f"provtrail head: {e}", file=sys.stderr)
        return 1
    if not report.ok:
        print(
            f"provtrail head: ledger failed verification "
            f"({len(report.violations)} violation(s))",
            file=sys.stderr,
        )
        return 1

    head = ledger.head()
    if head is None:
        print("provtrail head: ledger has no records", file=sys.stderr)
        return 1

    seq, record_hash = head
    if args.json:
        print(json.dumps({"seq": seq, "record_hash": record_hash}, ensure_ascii=False))
    else:
        print(f"{seq}:{record_hash}")
    return 0


def _print_check_result(result: CheckResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    print(f"{result.state}: {result.reason}")


def _cmd_check(args: argparse.Namespace) -> int:
    ledger = Ledger(args.ledger)
    result = ledger.check(since=args.since, session_id=args.session_id, until=args.until)
    _print_check_result(result, args.json)
    if args.enforce and result.state == STATE_MISSING:
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "add":
        return _cmd_add(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "head":
        return _cmd_head(args)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
