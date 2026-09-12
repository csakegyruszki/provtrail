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
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .hashing import (
    canonical_json_bytes,
    is_valid_content_hash,
    sha256_of_bytes,
    sha256_of_file,
    sha256_of_text,
    record_hash as compute_record_hash,
)

SCHEMA_ID = "provtrail/v1"
ALLOWED_KINDS = {"url", "search", "scrape", "file", "manual"}
LOCK_SUFFIX = ".lock"

# Every field name defined in schema/provtrail-record.v1.json's
# "properties". Kept as a plain constant (rather than read from the
# schema file at import time) because the schema lives outside the
# installed package (see MANIFEST.in); this set must be kept in sync by
# hand whenever the schema gains or loses a top-level field.
FIELD_NAMES = frozenset(
    {
        "schema",
        "seq",
        "id",
        "captured_at",
        "source_url",
        "content_hash",
        "kind",
        "tool",
        "query",
        "title",
        "claim",
        "snippet",
        "archived_url",
        "path",
        "session_id",
        "extra",
        "prev_hash",
        "record_hash",
    }
)

# Fields that, when present, must be strings. `schema`, `kind`,
# `captured_at`, `content_hash`, `id`, `record_hash` and `seq` have their
# own dedicated checks (WRONG_SCHEMA, INVALID_KIND,
# MISSING/INVALID_CAPTURED_AT, INVALID_CONTENT_HASH, ID_MISMATCH,
# HASH_MISMATCH) and are not covered here to avoid duplicate violations.
_STRING_FIELDS = (
    "tool",
    "query",
    "title",
    "claim",
    "snippet",
    "archived_url",
    "path",
    "session_id",
    "source_url",
)


def _find_non_string_key(value: Any) -> Any:
    """Return the first non-string dict key anywhere in ``value``, else None."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    return key
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return None


def _validate_record_fields(rec: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Structural, per-record field checks shared by ``add`` and ``verify``.

    This is the ONE place both `Ledger.add` (before writing) and
    `Ledger.verify` (while reading) check field names and field types, so
    the two can never silently drift apart. It only reports the two
    structural codes ``UNKNOWN_FIELD`` and ``INVALID_FIELD_TYPE``; codes
    that depend on chain state (``BAD_SEQ``, ``CHAIN_BROKEN``, ...) or
    that already have a dedicated, more specific check (``INVALID_KIND``,
    ``INVALID_CONTENT_HASH``, ...) are left where they already are.

    Returns a list of ``{"code": ..., "message": ...}`` dicts, empty if
    the record has no structural violations.
    """
    violations: List[Dict[str, str]] = []

    for key in rec:
        if key not in FIELD_NAMES:
            violations.append(
                {
                    "code": "UNKNOWN_FIELD",
                    "message": f"unknown field: {key!r}",
                }
            )

    if "seq" in rec:
        seq_val = rec["seq"]
        if isinstance(seq_val, bool) or not isinstance(seq_val, int) or seq_val < 1:
            violations.append(
                {
                    "code": "INVALID_FIELD_TYPE",
                    "message": f"seq must be an integer >= 1, got {seq_val!r}",
                }
            )

    for field_name in _STRING_FIELDS:
        if field_name in rec and not isinstance(rec[field_name], str):
            violations.append(
                {
                    "code": "INVALID_FIELD_TYPE",
                    "message": f"{field_name} must be a string, got {rec[field_name]!r}",
                }
            )

    if "extra" in rec and not isinstance(rec["extra"], dict):
        violations.append(
            {
                "code": "INVALID_FIELD_TYPE",
                "message": f"extra must be an object, got {rec['extra']!r}",
            }
        )
    elif "extra" in rec and _find_non_string_key(rec["extra"]) is not None:
        # json.dumps would silently turn 1 into "1", so the stored record
        # would no longer be what the caller passed.
        violations.append(
            {
                "code": "INVALID_FIELD_TYPE",
                "message": (
                    "extra keys must be strings, got "
                    f"{_find_non_string_key(rec['extra'])!r}"
                ),
            }
        )

    if "prev_hash" in rec:
        prev_hash_val = rec["prev_hash"]
        if prev_hash_val is not None and not is_valid_content_hash(prev_hash_val):
            violations.append(
                {
                    "code": "INVALID_FIELD_TYPE",
                    "message": (
                        "prev_hash must be null or sha256:<64 lower-hex>, got "
                        f"{prev_hash_val!r}"
                    ),
                }
            )

    return violations


def _reject_json_constant(name: str) -> None:
    # json.loads calls this for the tokens NaN / Infinity / -Infinity
    # instead of returning a float, when they are not wanted in ledger
    # data (see Ledger.verify below). Raising here turns them into the
    # same "this line is not valid JSON" outcome as any other malformed
    # line, reported as INVALID_JSON.
    raise ValueError(f"disallowed JSON constant: {name}")

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
    # The last record read from the file (raw, as parsed from its JSON
    # line), or None for an empty/absent ledger. Not part of the public
    # JSON output (see to_dict); it exists so that `Ledger.add` can reuse
    # the single pass `verify` already makes over the file instead of
    # scanning it a second time to find the last record.
    last_record: Optional[Dict[str, Any]] = None

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
                    obj = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as e:
                    raise LedgerError(f"malformed JSON at line {line_no}: {e}") from e
                yield obj

    def _last_record(self) -> Optional[Dict[str, Any]]:
        last = None
        for rec in self.records():
            last = rec
        return last

    def head(self) -> Optional[Tuple[int, str]]:
        """Return ``(seq, record_hash)`` of the last record, or ``None``.

        This reads the last record's own ``seq`` and ``record_hash``
        fields as stored; it does not itself verify the ledger. Callers
        that need the anchor to be trustworthy should call :meth:`verify`
        first (as the ``provtrail head`` command does).
        """
        last = self._last_record()
        if last is None:
            return None
        return last.get("seq"), last.get("record_hash")

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
        content_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append one record to the ledger and return it as a dict.

        Raises ``ContractError`` if the resulting record would violate
        the provtrail contract, and ``LockTimeout`` if the append lock
        cannot be acquired within ``lock_timeout`` seconds.

        ``content_root``, when given together with ``content_path``,
        requires the realpath of ``content_path`` to resolve inside
        ``content_root`` (following symlinks); otherwise ``ValueError``
        is raised and nothing is written. The default ``None`` preserves
        the previous behaviour of hashing any readable ``content_path``.
        This is a path-containment check, not a security sandbox. It
        resolves ``content_path`` once, at check time; it does not stop
        a file that is replaced by a symlink pointing outside
        ``content_root`` between the check and the read (a
        time-of-check/time-of-use race), and it does not defend against
        hardlinks or other filesystem-level aliasing. Treat it as a
        guard against accidental misuse, not against an adversarial
        filesystem.
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
            if content_root is not None and not _resolves_inside(content_root, content_path):
                raise ValueError(
                    f"content_path must resolve inside content_root: {content_path!r}"
                )
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

        # Validate the fields the caller controls (everything except
        # `seq` and `prev_hash`, which this method derives itself and
        # which are therefore always well-formed) with the same
        # `_validate_record_fields` that `verify` uses on every record
        # read back from disk. This runs before the lock is acquired and
        # before the ledger file is opened for writing, so a badly typed
        # field never touches the ledger.
        prevalidation: Dict[str, Any] = {
            "schema": SCHEMA_ID,
            "captured_at": captured_at,
            "kind": kind,
        }
        if has_url:
            prevalidation["source_url"] = source_url
        if has_hash:
            prevalidation["content_hash"] = content_hash
        for key, value in optional_fields.items():
            if value is not None:
                prevalidation[key] = value

        field_violations = _validate_record_fields(prevalidation)
        if field_violations:
            summary = "; ".join(
                f"{v['code']}: {v['message']}" for v in field_violations
            )
            raise ValueError(f"record would fail validation: {summary}")

        # NaN/Infinity/-Infinity anywhere in the record (including nested
        # inside `extra`) are rejected outright: they are not valid JSON
        # tokens, so a record containing one could never be read back
        # without a lenient (non-standard) JSON parser. This uses the
        # same `allow_nan=False` canonical encoding `record_hash` would
        # otherwise fail on, just before the lock is taken instead of
        # after, so nothing about this add touches the ledger or its lock
        # file.
        try:
            canonical_json_bytes(prevalidation)
        except ValueError as e:
            raise ValueError(f"record contains a non-finite float: {e}") from e
        except TypeError as e:
            raise ValueError(f"record contains a value that is not JSON: {e}") from e

        lock_path = self._acquire_lock(timeout=lock_timeout)
        try:
            report = self.verify()
            if not report.ok:
                raise LedgerError(
                    f"ledger failed verification ({len(report.violations)} violation(s))"
                )
            # `verify` above already walked the whole file once and kept
            # track of the last record it saw; reuse that instead of a
            # second full scan via `_last_record()`.
            last = report.last_record
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

    def verify(
        self,
        check_files: bool = False,
        expect: Iterable[Tuple[int, str]] = (),
    ) -> VerifyReport:
        """Verify the ledger and return a :class:`VerifyReport`.

        ``expect`` is an iterable of ``(seq, record_hash)`` anchor pairs,
        typically obtained earlier from :meth:`head` and stored outside
        the ledger. Each anchor is checked against the record actually
        found at that ``seq`` (using its recomputed, not merely stored,
        hash): ``ANCHOR_MISSING`` if no record has that ``seq`` (for
        example because the ledger was truncated), ``ANCHOR_MISMATCH``
        if a record exists there but its hash differs.
        """
        violations: List[Dict[str, Any]] = []
        count = 0
        expect = list(expect)

        if not os.path.isfile(self.path):
            for exp_seq, exp_hash in expect:
                violations.append(
                    {
                        "seq_or_line": exp_seq,
                        "code": "ANCHOR_MISSING",
                        "message": f"no record at seq {exp_seq!r}: ledger has no records",
                    }
                )
            return VerifyReport(
                ok=(len(violations) == 0), violations=violations, record_count=0
            )

        running_expected_seq = 1
        running_prev_hash: Optional[str] = None
        ledger_dir = os.path.dirname(os.path.abspath(self.path))
        last_record: Optional[Dict[str, Any]] = None
        seq_to_hash: Dict[Any, str] = {}

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
                    rec = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as e:
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
                last_record = rec
                seq_or_line = rec.get("seq", line_no)

                for field_violation in _validate_record_fields(rec):
                    violations.append({"seq_or_line": seq_or_line, **field_violation})

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
                if content_hash is not None and not has_hash:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "INVALID_CONTENT_HASH",
                            "message": (
                                f"content_hash is not a well-formed sha256:<64 lower-case "
                                f"hex> string: {content_hash!r}"
                            ),
                        }
                    )

                rec_kind = rec.get("kind")
                if rec_kind not in ALLOWED_KINDS:
                    violations.append(
                        {
                            "seq_or_line": seq_or_line,
                            "code": "INVALID_KIND",
                            "message": (
                                f"kind must be one of {sorted(ALLOWED_KINDS)}, got {rec_kind!r}"
                            ),
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
                if seq_is_integer:
                    seq_to_hash[seq_val] = recomputed
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

        for exp_seq, exp_hash in expect:
            actual_hash = seq_to_hash.get(exp_seq)
            if actual_hash is None:
                violations.append(
                    {
                        "seq_or_line": exp_seq,
                        "code": "ANCHOR_MISSING",
                        "message": f"no record found at seq {exp_seq!r}",
                    }
                )
            elif actual_hash != exp_hash:
                violations.append(
                    {
                        "seq_or_line": exp_seq,
                        "code": "ANCHOR_MISMATCH",
                        "message": (
                            f"record at seq {exp_seq!r} has hash {actual_hash!r}, "
                            f"expected {exp_hash!r}"
                        ),
                    }
                )

        return VerifyReport(
            ok=(len(violations) == 0),
            violations=violations,
            record_count=count,
            last_record=last_record,
        )

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
