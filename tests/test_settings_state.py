"""Tests for the settings adapter helpers (dict-in/dict-out, no HTTP layer)."""
import unittest
from types import SimpleNamespace

from api_routes.settings_state import (
    _float_setting,
    _int_setting,
    _normalize_window_size_settings,
    persist_config,
)


class _FakeCfg:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


def _ctx(values=None):
    return SimpleNamespace(cfg_mgr=_FakeCfg(values or {}))


class TestScalarHelpers(unittest.TestCase):
    def test_int_clamps_silently(self):
        self.assertEqual(_int_setting("9999", 200, 80, 1920), 1920)
        self.assertEqual(_int_setting("abc", 200, 80, 1920), 200)
        self.assertEqual(_int_setting("512.9", 200, 80, 1920), 512)

    def test_float_clamps_silently(self):
        self.assertEqual(_float_setting("1e6", 300.0, 10.0, 86400.0), 86400.0)
        self.assertEqual(_float_setting("bad", 300.0, 10.0, 86400.0), 300.0)


class TestWindowSizeSettings(unittest.TestCase):
    def test_preset_resolves_dimensions(self):
        out = _normalize_window_size_settings(_ctx(), {"preset": "800x600"})
        self.assertEqual((out["width"], out["height"]), (800, 600))

    def test_bad_preset_rejected(self):
        with self.assertRaises(ValueError):
            _normalize_window_size_settings(_ctx(), {"preset": "nope"})

    def test_custom_clamped(self):
        out = _normalize_window_size_settings(
            _ctx(), {"preset": "custom", "width": 99999, "height": -5}
        )
        self.assertEqual(out["width"], 1920)
        self.assertEqual(out["height"], 60)

    def test_body_overrides_config(self):
        ctx = _ctx({"roblox_window_resize_enabled": False})
        out = _normalize_window_size_settings(ctx, {"enabled": True})
        self.assertTrue(out["enabled"])


class _FakeCfgMgr:
    def __init__(self):
        self.updated = []
        self.saved = 0

    def update(self, updates):
        self.updated.append(dict(updates))

    def save(self):
        self.saved += 1


class _FakeFarm:
    def __init__(self, fail=False):
        self.snapshots = 0
        self.fail = fail

    def apply_config_snapshot(self):
        self.snapshots += 1
        if self.fail:
            raise RuntimeError("fan-out boom")


class TestPersistConfig(unittest.TestCase):
    def test_update_save_only_by_default(self):
        cfg, farm = _FakeCfgMgr(), _FakeFarm()
        ctx = SimpleNamespace(cfg_mgr=cfg, farm=farm)
        persist_config(ctx, {"a": 1})
        self.assertEqual(cfg.updated, [{"a": 1}])
        self.assertEqual(cfg.saved, 1)
        self.assertEqual(farm.snapshots, 0)

    def test_guarded_snapshot_swallows(self):
        cfg, farm = _FakeCfgMgr(), _FakeFarm(fail=True)
        ctx = SimpleNamespace(cfg_mgr=cfg, farm=farm)
        persist_config(ctx, {"a": 1}, apply_snapshot=True)
        self.assertEqual(farm.snapshots, 1)

    def test_strict_snapshot_propagates(self):
        cfg, farm = _FakeCfgMgr(), _FakeFarm(fail=True)
        ctx = SimpleNamespace(cfg_mgr=cfg, farm=farm)
        with self.assertRaises(RuntimeError):
            persist_config(ctx, {"a": 1}, apply_snapshot=True, strict_snapshot=True)
        self.assertEqual(cfg.saved, 1)

    def test_missing_farm_ok(self):
        cfg = _FakeCfgMgr()
        persist_config(SimpleNamespace(cfg_mgr=cfg, farm=None), {"a": 1}, apply_snapshot=True)
        self.assertEqual(cfg.saved, 1)

    def test_farm_without_method_ok(self):
        cfg = _FakeCfgMgr()
        persist_config(SimpleNamespace(cfg_mgr=cfg, farm=object()), {"a": 1}, apply_snapshot=True)
        self.assertEqual(cfg.saved, 1)


if __name__ == "__main__":
    unittest.main()
