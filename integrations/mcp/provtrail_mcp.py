#!/usr/bin/env python3
"""provtrail MCP server, entry point for a source checkout.

The implementation is ``provtrail.mcp_server``. When provtrail is
installed with ``pip install "provtrail[mcp]"``, run the
``provtrail-mcp`` command instead of this file.
"""

import os
import sys

try:
    from provtrail.mcp_server import main, provtrail_add, provtrail_verify, server  # noqa: F401
except ImportError:
    sys.path.insert(
        0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
    )
    from provtrail.mcp_server import main, provtrail_add, provtrail_verify, server  # noqa: F401

if __name__ == "__main__":
    main()
