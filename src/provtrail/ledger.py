"""Append-only, hash-chained JSONL ledger for source chain-of-custody.

See the package README for the full contract. In short: a record is
valid iff ``captured_at`` is a parseable RFC3339 UTC timestamp AND
(``source_url`` is a non-empty string OR ``content_hash`` is a
non-empty ``sha256:<64 hex>`` string).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from .hashing import (
    is_valid_content_hash,
    sha256_of_bytes,
    sha256_of_file,
    sha256_of_text,
    record_hash as compute_record_hash,
)

SCHEMA_ID = "provtrail/v1"
ALLOWED_KINDS = {"url", "search", "scrape", "file", "manual"}
LOCK_SUFFIX = ".lock"

STATE_PRESENT = "PRESENT"
STATE_MISSING = "MISSING"
STATE_UNKNOWN = "UNKNOWN"


class ContractError(ValueError):
    """Raised when a record would violate the provtrail contract."""


class LockTimeout(TimeoutError):
    """Raised when the ledger append lock cannot be acquired in time."""


class LedgerError(Exception):
    """Raised when the ledger file cannot be parsed as valid JSONL."""


def now_rfc3339() -> str:
    """Return the current UTC time as an RFC3339 timestamp, seconds precision."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?"
    r"(?:([Zz])|([+-])(\d{2}):(\d{2}))$"
)


def parse_rfc3339_utc(value: Any) -> datetime:
    """Parse an RFC 3339 ``date-time`` into an aware ``datetime``.

    Accepts exactly the RFC 3339 grammar: ``T`` or ``t`` as the
    separator, optional fractional seconds of any length, and ``Z``/``z``
    or a ``+HH:MM``/``-HH:MM`` offset. A leap second (``:60``) is accepted
    and treated as ``:59``, since ``datetime`` cannot represent it.
    Fractional digits beyond microseconds are truncated. Parsing does not
    depend on the Python version's ``fromisoformat``. Raises
    ``ValueError`` for anything else.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    m = _RFC3339_RE.match(value)
    if not m:
        raise ValueError(f"not an RFC 3339 timestamp: {value!r}")
    year, month, day, hour, minute, second = (int(g) for g in m.group(1, 2, 3, 4, 5, 6))
    frac = m.group(7)
    micro = int((frac[1:] + "000000")[:6]) if frac else 0
    if second == 60:
        second = 59
    if m.group(8):
        tz = timezone.utc
    else:
        off_h, off_m = int(m.group(10)), int(m.group(11))
        if off_h > 23 or off_m > 59:
            raise ValueError(f"invalid UTC offset in timestamp: {value!r}")
        delta = timedelta(hours=off_h, minutes=off_m)
        tz = timezone(-delta if m.group(9) == "-" else delta)
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
    except ValueError as e:
        raise ValueError(f"not a valid RFC 3339 timestamp: {value!r} ({e})") from e


def _path_escapes(rel_path: Any) -> bool:
    """Return True unless ``rel_path`` is a relative path that stays inside
    the ledger's directory (checked lexically, without touching the disk)."""
    if not isinstance(rel_path, str) or not rel_path:
        return True
    if (
        os.path.isabs(rel_path)
        or os.path.splitdrive(rel_path)[0]
        or re.match(r"^[A-Za-z]:", rel_path)
    ):
        return True
    if rel_path.startswith(("/", "\\")):
        return True
    parts = re.split(r"[\\/]+", os.path.normpath(rel_path))
    return parts[0] == ".."


def _resolves_inside(directory: str, candidate: str) -> bool:
    """Return True if ``candidate`` resolves (following symlinks) inside ``directory``."""
    real_dir = os.path.realpath(directory)
    real_candidate = os.path.realpath(candidate)
    return real_candidate.startswith(real_dir.rstrip(os.sep) + os.sep)


@dataclass
class VerifyReport:
    ok: bool
    violations: List[Dict[str, Any]] = field(default_factory=list)
    record_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "record_count": self.record_count,
            "violations": self.violations,
        }


@dataclass
class CheckResult:
    state: str
    reason: str = ""
    matched_seq: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "matched_seq": self.matched_seq,
        }


class Ledger:
    """A single append-only JSONL provenance ledger."""

    def __init__(self, path: str):
        self.path = str(path)

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    def _lock_path(self) -> str:
        return self.path + LOCK_SUFFIX

    def _acquire_lock(self, timeout: float = 10.0, poll_interval: float = 0.05) -> str:
        lock_path = self._lock_path()
        start = time.monotonic()
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                return lock_path
            except FileExistsError:
                if time.monotonic() - start >= timeout:
                    raise LockTimeout(
                        f"could not acquire lock {lock_path!r} within {timeout}s"
                    )
                time.sleep(poll_interval)

    def _release_lock(self, lock_path: str) -> None:
        # We only ever remove a lock file this process created via
        # O_CREAT|O_EXCL above, so this never steals or deletes a
        # foreign lock.
        try:
            os.remove(lock_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def records(self) -> Iterator[Dict[str, Any]]:
        """Yield each record dict from the ledger, in file order.

        Raises ``LedgerError`` (with the offending line number) if a
        line is not valid JSON.
        """
        if not os.path.isfile(self.path):
            return
        with open(self.path, "rb") as f:
            for line_no, raw_bytes in enumerate(f, start=1):
                try:
                    raw_line = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise LedgerError(f"line {line_no} is not valid UTF-8: {e}") from e
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise LedgerError(f"malformed JSON at line {line_no}: {e}") from e
                yield obj

    def _last_record(self) -> Optional[Dict[str, Any]]:
        last = None
        for rec in self.records():
            last = rec
        return last

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        source_url: Optional[str] = None,
        content: Optional[Any] = None,
        content_path: Optional[str] = None,
        kind: str = "url",
        tool: Optional[str] = None,
        query: Optional[str] = None,
        title: Optional[str] = None,
        claim: Optional[str] = None,
        snippet: Optional[str] = None,
        archived_url: Optional[str] = None,
        path: Optional[str] = None,
        session_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        captured_at: Optional[str] = None,
        lock_timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Append one record to the ledger and return it as a dict.

        Raises ``ContractError`` if the resulting record would violate
        the provtrail contract, and ``LockTimeout`` if the append lock
        cannot be acquired within ``lock_timeout`` seconds.
        """
        if content is not None and content_path is not None:
            raise ValueError("provide at most one of 'content' or 'content_path'")

        if kind not in ALLOWED_KINDS:
            raise ValueError(
                f"invalid kind {kind!r}; must be one of {sorted(ALLOWED_KINDS)}"
            )

        if path is not None and _path_escapes(path):
            raise ValueError(
                f"path must be relative and inside the ledger's directory: {path!r}"
            )

        content_hash: Optional[str] = None
        if content is not None:
            if isinstance(content, bytes):
                content_hash = sha256_of_bytes(content)
            else:
                content_hash = sha256_of_text(str(content))
        elif content_path is not None:
            content_hash = sha256_of_file(content_path)

        has_url = isinstance(source_url, str) and source_url != ""
        has_hash = bool(content_hash) and is_valid_content_hash(content_hash)
        if not has_url and not has_hash:
            raise ContractError(
                "record needs a non-empty source_url, or content/content_path "
                "to derive a content_hash"
            )

        if captured_at is None:
            captured_at = now_rfc3339()
        else:
            try:
                parse_rfc3339_utc(captured_at)
            except ValueError as e:
                raise ContractError(
                    f"captured_at is not a parseable RFC3339 UTC timestamp: "
                    f"{captured_at!r}"
                ) from e

        lock_path = self._acquire_lock(timeout=lock_timeout)
        try:
            report = self.verify()
            if not report.ok:
                raise LedgerError(
                    f"ledger failed verification ({len(report.violations)} violation(s))"
                )
            last = self._last_record()
            seq = (last["seq"] + 1) if last else 1
            prev_hash = last["record_hash"] if last else None

            body: Dict[str, Any] = {
                "schema": SCHEMA_ID,
                "seq": seq,
                "captured_at": captured_at,
                "kind": kind,
            }
            if has_url:
                body["source_url"] = source_url
            if has_hash:
                body["content_hash"] = content_hash

            optional_fields = {
                "tool": tool,
                "query": query,
                "title": title,
                "claim": claim,
                "snippet": snippet,
                "archived_url": archived_url,
                "path": path,
                "session_id": session_id,
                "extra": extra,
            }
            for key, value in optional_fields.items():
                if value is not None:
                    body[key] = value

            body["prev_hash"] = prev_hash

            rh = compute_record_hash(body)
            record_id = "ev_" + rh.split(":", 1)[1][:16]

            full_record = dict(body)
            full_record["id"] = record_id
            full_record["record_hash"] = rh

            line = json.dumps(full_record, ensure_ascii=False) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            return full_record
        finally:
            self._release_lock(lock_path)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, check_files: bool = False) -> VerifyReport:
        violations: List[Dict[str, Any]] = []
        count = 0

        if not os.path.isfile(self.path):
            return VerifyReport(ok=True, violations=[], record_count=0)

        running_expected_seq = 1
        running_prev_hash: Optional[str] = None
        ledger_dir = os.path.dirname(os.path.abspath(self.path))

        with open(self.path, "rb") as f:
            for line_no, raw_bytes in enumerate(f, start=1):
                try:
                    raw_line = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as e:
                    violations.append(
                        {
                            "seq_or_line": line_no,
                            "code": "INVALID_JSON",
                            "message": f"line is not valid UTF-8: {e}",
                        }
                    )
                    continue
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue

                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    violations.append(
                        {"seq_or_line": line_no, "code": "INVALID_JSON", "message": str(e)}
                    )
                    continue
                if not isinstance(rec, dict):
                    violations.append(
                        {
                            "seq_or_line": line_no,
                            "code": "INVALID_JSON",
                            "message": "record is not a JSON object",
                        }
                    )
                    continue

                count += 1
                seq_or_line = rec.get("seq", line_no)

                if rec.get("schema") != SCHEMA_ID:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "WRONG_SCHEMA",
                            "message": (
                                f"expected schema {SCHEMA_ID!r}, got "
                                f"{rec.get('schema')!r}"
                            ),
                        }
                    )

                captured_at = rec.get("captured_at")
                if not captured_at:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "MISSING_CAPTURED_AT",
                            "message": "captured_at is missing or empty",
                        }
                    )
                else:
                    try:
                        parse_rfc3339_utc(captured_at)
                    except ValueError as e:
                        violations.append(
                            {
                                "seq_or_line": seq_or_line,
                                "code": "INVALID_CAPTURED_AT",
                                "message": str(e),
                            }
                        )

                source_url = rec.get("source_url")
                content_hash = rec.get("content_hash")
                has_url = isinstance(source_url, str) and source_url != ""
                has_hash = is_valid_content_hash(content_hash)
                if not has_url and not has_hash:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "MISSING_SOURCE_LOCATOR",
                            "message": "neither source_url nor content_hash present",
                        }
                    )

                seq_val = rec.get("seq")
                seq_is_integer = isinstance(seq_val, int) and not isinstance(seq_val, bool)
                if not seq_is_integer or seq_val != running_expected_seq:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "BAD_SEQ",
                            "message": (
                                f"expected seq {running_expected_seq}, got {seq_val!r}"
                            ),
                        }
                    )

                prev_hash_val = rec.get("prev_hash")
                if prev_hash_val != running_prev_hash:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "CHAIN_BROKEN",
                            "message": (
                                "prev_hash does not match the previous record's "
                                "record_hash"
                            ),
                        }
                    )

                stored_hash = rec.get("record_hash")
                recomputed = compute_record_hash(rec)
                if stored_hash != recomputed:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "HASH_MISMATCH",
                            "message": "record_hash does not match the recomputed hash",
                        }
                    )

                expected_id = "ev_" + recomputed.split(":", 1)[1][:16]
                if rec.get("id") != expected_id:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "ID_MISMATCH",
                            "message": "id does not match the value derived from the record hash",
                        }
                    )

                rec_path = rec.get("path")
                artifact_path = None
                if rec_path is not None:
                    if _path_escapes(rec_path):
                        violations.append(
                            {
                                "seq_or_line": seq_or_line,
                                "code": "INVALID_PATH",
                                "message": (
                                    "path must be relative and inside the ledger's "
                                    f"directory: {rec_path!r}"
                                ),
                            }
                        )
                    elif check_files:
                        candidate = os.path.join(ledger_dir, rec_path)
                        if _resolves_inside(ledger_dir, candidate):
                            artifact_path = candidate
                        else:
                            violations.append(
                                {
                                    "seq_or_line": seq_or_line,
                                    "code": "INVALID_PATH",
                                    "message": (
                                        "path resolves outside the ledger's directory: "
                                        f"{rec_path!r}"
                                    ),
                                }
                            )

                if artifact_path is not None:
                    if not os.path.isfile(artifact_path):
                        violations.append(
                            {
                                "seq_or_line": seq_or_line,
                                "code": "FILE_MISSING",
                                "message": f"artifact file not found: {rec['path']}",
                            }
                        )
                    else:
                        try:
                            actual_hash = sha256_of_file(artifact_path)
                        except OSError as e:
                            violations.append(
                                {
                                    "seq_or_line": seq_or_line,
                                    "code": "FILE_MISSING",
                                    "message": f"could not read artifact file: {e}",
                                }
                            )
                        else:
                            if has_hash and actual_hash != content_hash:
                                violations.append(
                                    {
                                        "seq_or_line": seq_or_line,
                                        "code": "FILE_HASH_MISMATCH",
                                        "message": (
                                            "artifact file content does not match "
                                            "content_hash"
                                        ),
                                    }
                                )

                # Advance chain-tracking state using this record's own
                # (possibly invalid) values, so later records are still
                # compared against what is actually on disk.
                running_expected_seq = (
                    seq_val if seq_is_integer else running_expected_seq
                ) + 1
                running_prev_hash = (
                    stored_hash if isinstance(stored_hash, str) else running_prev_hash
                )

        return VerifyReport(ok=(len(violations) == 0), violations=violations, record_count=count)

    # ------------------------------------------------------------------
    # Presence check
    # ------------------------------------------------------------------

    def check(
        self,
        since: Optional[str] = None,
        session_id: Optional[str] = None,
        until: Optional[str] = None,
    ) -> CheckResult:
        """Report whether a matching record exists in the ledger.

        ``since``/``until`` bound ``captured_at`` (inclusive); ``session_id``,
        when given, restricts matches to records whose ``session_id`` field
        equals it. All three are optional and independent; passing none of
        them reproduces the original "does the ledger have any record at
        all" check.
        """
        since_dt = None
        if since is not None:
            try:
                since_dt = parse_rfc3339_utc(since)
            except ValueError:
                return CheckResult(
                    STATE_UNKNOWN,
                    reason=f"'since' is not a parseable RFC3339 timestamp: {since!r}",
                )

        until_dt = None
        if until is not None:
            try:
                until_dt = parse_rfc3339_utc(until)
            except ValueError:
                return CheckResult(
                    STATE_UNKNOWN,
                    reason=f"'until' is not a parseable RFC3339 timestamp: {until!r}",
                )

        if not os.path.isfile(self.path):
            return CheckResult(STATE_UNKNOWN, reason="ledger file does not exist")

        report = self.verify(check_files=False)
        if not report.ok:
            return CheckResult(
                STATE_UNKNOWN,
                reason=f"ledger failed verification ({len(report.violations)} violation(s))",
            )

        try:
            recs = list(self.records())
        except LedgerError as e:
            return CheckResult(STATE_UNKNOWN, reason=f"could not read ledger: {e}")

        if not recs:
            return CheckResult(STATE_MISSING, reason="ledger has no records")

        if session_id is not None:
            total = len(recs)
            recs = [rec for rec in recs if rec.get("session_id") == session_id]
            if not recs:
                return CheckResult(
                    STATE_MISSING,
                    reason=(
                        f"none of the ledger's {total} record(s) belongs to "
                        f"session {session_id!r}"
                    ),
                )

        if since_dt is None and until_dt is None:
            return CheckResult(
                STATE_PRESENT,
                reason="ledger has at least one matching record",
                matched_seq=recs[-1].get("seq"),
            )

        for rec in recs:
            try:
                dt = parse_rfc3339_utc(rec.get("captured_at"))
            except ValueError:
                continue
            if since_dt is not None and dt < since_dt:
                continue
            if until_dt is not None and dt > until_dt:
                continue
            return CheckResult(
                STATE_PRESENT,
                reason="found a valid record captured within the given window",
                matched_seq=rec.get("seq"),
            )

        return CheckResult(
            STATE_MISSING, reason="no valid matching record captured within the given window"
        )
