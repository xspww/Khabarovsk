from __future__ import annotations

# Legacy shim — canonical module moved to runtime/farm.py.
import sys as _sys

from runtime import farm as _canonical

_sys.modules[__name__] = _canonical
