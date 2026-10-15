"""Tests for old-version cleanup (filesystem, no Roblox needed)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.roblox_install_manager import RobloxInstallManager


def _mgr():
    return RobloxInstallManager(
        guard_running=lambda: False,
        roblox_running=lambda: False,
        logger=lambda _msg: None,
    )


class TestRemoveOldVersions(unittest.TestCase):
    def test_keeps_current_removes_old(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "Roblox"
            versions = root / "Versions"
            (versions / "version-new").mkdir(parents=True)
            (versions / "version-old").mkdir(parents=True)
            (versions / "not-a-version").mkdir(parents=True)
            (versions / "version-old" / "file.txt").write_text("x")
            mgr = _mgr()
            mgr._safe_roblox_root = lambda _p: True  # type: ignore[method-assign]
            removed = mgr.remove_old_versions("version-new", roots=[root])
            self.assertEqual(removed, 1)
            self.assertTrue((versions / "version-new").is_dir())
            self.assertFalse((versions / "version-old").exists())
            self.assertTrue((versions / "not-a-version").is_dir())

    def test_missing_versions_dir(self):
        with TemporaryDirectory() as tmp:
            mgr = _mgr()
            self.assertEqual(mgr.remove_old_versions("version-new", roots=[Path(tmp) / "Roblox"]), 0)

    def test_case_insensitive_keep(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "Roblox"
            versions = root / "Versions"
            (versions / "Version-New").mkdir(parents=True)
            (versions / "version-old").mkdir(parents=True)
            mgr = _mgr()
            mgr._safe_roblox_root = lambda _p: True  # type: ignore[method-assign]
            self.assertEqual(mgr.remove_old_versions("version-new", roots=[root]), 1)
            self.assertTrue((versions / "Version-New").is_dir())


class TestManagerSurface(unittest.TestCase):
    """Regression: every public entry must exist on the class.

    A mis-indented helper once swallowed the methods below it, so
    `status()` blew up at runtime while compileall stayed green.
    """

    def test_public_surface(self):
        for name in (
            "status",
            "detect_installed",
            "list_installed",
            "remove_old_versions",
            "roblox_roots",
            "find_install_blockers",
        ):
            self.assertTrue(callable(getattr(RobloxInstallManager, name, None)), name)

    def test_status_runs(self):
        self.assertTrue(_mgr().status()["ok"])


if __name__ == "__main__":
    unittest.main()
