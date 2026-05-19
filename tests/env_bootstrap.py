"""Shared test environment bootstrap for unittest and direct test runs."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile


_TEST_USER_ROOT = ""


def ensure_test_user_root() -> None:
    global _TEST_USER_ROOT
    if "CRONUS_USER_ROOT" in os.environ:
        return
    _TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-unittest-user-root-")
    os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
