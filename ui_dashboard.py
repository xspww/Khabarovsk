from __future__ import annotations

# Legacy shim — canonical module moved to desktop/ui_dashboard.py.
import sys as _sys

from desktop import ui_dashboard as _canonical

_sys.modules[__name__] = _canonical
