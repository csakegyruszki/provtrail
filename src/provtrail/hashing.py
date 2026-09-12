"""Canonical JSON encoding and hashing helpers for provtrail.

The chain-of-custody guarantee rests on one rule: every record hash is
computed from a deterministic byte representation of the record. This
module is the single place that defines that representation, so ledger
writing and ledger verification can never silently drift apart.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

SHA256_PREFIX = "sha256:"
_HEX_DIGITS = frozenset("0123456789abcdef")


def canonical_json_bytes(obj: Mapping[str, Any]) -> bytes:
    """Return the canonical JSON byte encoding used for hashing.

    Canonical JSON = ``json.dumps(obj, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False, allow_nan=False)`` encoded
    as UTF-8. ``allow_nan=False`` makes ``NaN``, ``Infinity`` and
    ``-Infinity`` anywhere in ``obj`` (including nested values, such as
    inside ``extra``) raise ``ValueError`` instead of being silently
    serialized as invalid JSON tokens.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex sha256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_prefixed(data: bytes) -> str:
    """Return ``sha256:<hex>`` for ``data``."""
    return SHA256_PREFIX + sha256_hex(data)


def sha256_of_text(text: str) -> str:
    """Return ``sha256:<hex>`` for a text payload (UTF-8 encoded)."""
    return sha256_prefixed(text.encode("utf-8"))


def sha256_of_bytes(data: bytes) -> str:
    """Return ``sha256:<hex>`` for a raw bytes payload."""
    return sha256_prefixed(data)


def sha256_of_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    """Stream-hash a file on disk and return ``sha256:<hex>``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return SHA256_PREFIX + h.hexdigest()


def is_valid_content_hash(value: Any) -> bool:
    """Check whether ``value`` is a well-formed ``sha256:<64 hex>`` string.

    Only lower-case hex digits are accepted. ``int(digest, 16)`` alone
    would also accept upper-case hex (and a leading ``+``/``-``/``0x``),
    so the digest is checked character-by-character instead.
    """
    if not isinstance(value, str):
        return False
    if not value.startswith(SHA256_PREFIX):
        return False
    digest = value[len(SHA256_PREFIX):]
    if len(digest) != 64:
        return False
    return all(c in _HEX_DIGITS for c in digest)


def record_hash(record: Mapping[str, Any]) -> str:
    """Compute the ``record_hash`` for a record dict.

    The hash is computed over the canonical JSON of the record WITHOUT
    the ``record_hash`` key itself (a record cannot include its own hash
    inside the hashed payload).

    The ``id`` key is excluded for the same reason: ``id`` is defined as
    ``"ev_" + record_hash[:16]`` (after the ``sha256:`` prefix), so it is
    derived FROM ``record_hash`` and cannot also be an input to it without
    circularity. ``record_hash`` is computed once, before ``id`` is
    assigned, over a payload that contains neither key; excluding both
    during verification recomputes the same payload.
    """
    payload = {k: v for k, v in record.items() if k not in ("record_hash", "id")}
    return sha256_prefixed(canonical_json_bytes(payload))
