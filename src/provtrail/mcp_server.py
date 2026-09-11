"""MCP server exposing provtrail as tools.

Requires the ``mcp`` package (the ``provtrail[mcp]`` extra):
``pip install "provtrail[mcp]"``. Installed as the ``provtrail-mcp``
command; also runnable as ``python -m provtrail.mcp_server``.

The tools only run when the model calls them. For coverage that does
not depend on the model, use the Stop hook (``provtrail-stop-hook``).

The ledger is not a tool parameter. This server resolves it the same
way the Stop hook does, via ``provtrail.config.resolve_config``, using
its own working directory and the ``PROVTRAIL_LEDGER`` / config file
(``.provtrail.json``). Start this process from the project directory
(or set ``PROVTRAIL_LEDGER``) for it to find the right ledger.

Whether Claude Code passes ``CLAUDE_CODE_SESSION_ID`` to an MCP server
process is not verified (it is verified for Bash tool subprocesses).
``provtrail_add`` falls back to that environment variable for
``session_id`` when the caller does not supply one, but if the Stop
hook's session matching needs to line up with what this tool records,
pass ``session_id`` explicitly with the value from the current
conversation rather than relying on the fallback.
"""

from __future__ import annotations

import os

try:
    import mcp  # noqa: F401
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "The 'mcp' package is required for the provtrail MCP server. "
        "Install it with: pip install \"provtrail[mcp]\""
    ) from e

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .config import resolve_config
from .ledger import ContractError, Ledger, LedgerError, LockTimeout


server = _Server("provtrail")


def _resolve_ledger_path():
    ledger_path, _mode = resolve_config(os.getcwd())
    return ledger_path


def _resolve_content_path(content_path, ledger_dir):
    """Resolve content_path and reject it if it escapes ledger_dir.

    Returns the resolved path, or ``None`` if it escapes.
    """
    candidate = content_path
    if not os.path.isabs(candidate):
        candidate = os.path.join(ledger_dir, candidate)
    real_candidate = os.path.realpath(candidate)
    real_ledger_dir = os.path.realpath(ledger_dir)
    if real_candidate != real_ledger_dir and not real_candidate.startswith(
        real_ledger_dir + os.sep
    ):
        return None
    return real_candidate


@server.tool()
def provtrail_add(
    source_url: str = "",
    content: str = "",
    content_path: str = "",
    tool: str = "",
    kind: str = "url",
    claim: str = "",
    title: str = "",
    query: str = "",
    snippet: str = "",
    archived_url: str = "",
    session_id: str = "",
) -> dict:
    """Append one captured source to the project's provtrail ledger.

    The ledger is resolved from this server's working directory (see
    the module docstring); it is not a parameter here. Provide at least
    one of ``source_url``, ``content``, or ``content_path``, and at
    most one of ``content``/``content_path``. ``content`` is hashed as
    text. ``content_path`` names a file, resolved against the ledger's
    directory if relative, that must resolve (after following symlinks)
    inside that directory; it is rejected otherwise and hashed from its
    raw bytes when accepted. ``session_id`` defaults to the
    ``CLAUDE_CODE_SESSION_ID`` environment variable when not supplied.
    Returns the appended record, or an error dict on rejection.
    """
    ledger_path = _resolve_ledger_path()
    if not ledger_path:
        return {"ok": False, "error": "no provtrail ledger configured for this directory"}

    if content and content_path:
        return {"ok": False, "error": "provide at most one of 'content' or 'content_path'"}

    resolved_content_path = None
    artifact_rel_path = None
    if content_path:
        ledger_dir = os.path.realpath(os.path.dirname(os.path.abspath(ledger_path)))
        resolved_content_path = _resolve_content_path(content_path, ledger_dir)
        if resolved_content_path is None:
            return {
                "ok": False,
                "error": "content_path must resolve inside the ledger's directory",
            }
        # Stored so that `provtrail verify --check-files` can re-hash the file.
        artifact_rel_path = os.path.relpath(resolved_content_path, ledger_dir).replace(os.sep, "/")

    effective_session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID") or None

    try:
        record = Ledger(ledger_path).add(
            source_url=source_url or None,
            content=content or None,
            content_path=resolved_content_path,
            kind=kind or "url",
            tool=tool or None,
            claim=claim or None,
            title=title or None,
            query=query or None,
            snippet=snippet or None,
            archived_url=archived_url or None,
            path=artifact_rel_path,
            session_id=effective_session_id,
        )
        return {"ok": True, "record": record}
    except (ContractError, ValueError, LedgerError) as e:
        return {"ok": False, "error": str(e)}
    except LockTimeout as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def provtrail_verify() -> dict:
    """Verify the project's provtrail ledger integrity and return the report.

    The ledger is resolved the same way as ``provtrail_add`` (see the
    module docstring); it is not a parameter here.
    """
    ledger_path = _resolve_ledger_path()
    if not ledger_path:
        return {"ok": False, "error": "no provtrail ledger configured for this directory"}
    report = Ledger(ledger_path).verify(check_files=False)
    return report.to_dict()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
