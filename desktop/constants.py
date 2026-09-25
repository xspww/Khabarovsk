from __future__ import annotations

# Single source of truth for desktop-loopback constants.
# Replaces stringly-typed duplicates that drifted across
# desktop_host.py / desktop/* / api_routes/*.

APP_USER_AGENT = "CronusLauncher/RT"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 7777
INSTANCE_LOCK_PORT = 7711
PORT_SCAN_COUNT = 20
