"""Local-first configuration: validation + JSON store.

Layer: foundation over infra/ + domain/states.
- validation.py: schema version + range checks (pure, no I/O).
- store.py: ConfigManager + account text files (uses infra.app_paths).
Public entry: `from config.store import ConfigManager`
or `from core import ConfigManager` (facade).
"""
from config.store import (
    ACCOUNTS_TEXT_FILE,
    CONFIG_FILE,
    DEFAULTS,
    RUNTIME_TEXT_FILE,
    ConfigManager,
)

__all__ = [
    "ACCOUNTS_TEXT_FILE",
    "CONFIG_FILE",
    "DEFAULTS",
    "RUNTIME_TEXT_FILE",
    "ConfigManager",
]
