from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path


_TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-pytest-user-root-")
os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)

_TESTS_DIR = str(Path(__file__).resolve().parent / "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
