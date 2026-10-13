from __future__ import annotations

# Legacy shim — canonical module moved to services/account_hybrid.py.
# Alias so `import account_hybrid` IS the canonical module object
# (single ACCOUNT_STORE singleton, monkeypatches propagate).
import sys as _sys

from services import account_hybrid as _canonical

_sys.modules[__name__] = _canonical
