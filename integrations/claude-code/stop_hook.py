#!/usr/bin/env python3
"""provtrail Stop hook, entry point for a source checkout.

The implementation is ``provtrail.stop_hook``. When provtrail is
installed with pip, configure the ``provtrail-stop-hook`` command
instead of this file.
"""

import os
import sys

try:
    from provtrail.stop_hook import main
except ImportError:
    sys.path.insert(
        0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
    )
    from provtrail.stop_hook import main

if __name__ == "__main__":
    raise SystemExit(main())
