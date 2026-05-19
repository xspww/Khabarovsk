"""Unittest discovery bootstrap.

Pytest loads conftest.py before app imports, but plain unittest discovery does
not. Keep unittest runs off the operator runtime by setting the same isolated
user root before any test module imports app code.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile


_TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-unittest-user-root-")
if "CRONUS_USER_ROOT" not in os.environ:
    os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)
