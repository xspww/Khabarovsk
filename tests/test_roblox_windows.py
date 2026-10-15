"""Tests for the shared window-enumeration rules (pure, no Win32)."""
import unittest

from services.roblox_windows import (
    _EnumRect,
    _collect_roblox_proc_meta,
    _dedupe_largest_per_pid,
    _measure_window_rect,
    _passes_window_size_filter,
)


def _win(pid, area, created=0.0):
    return {"pid": pid, "hwnd": pid * 10 + area, "area": area, "created": created}


class TestSizeFilter(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(_passes_window_size_filter(60, 45))
        self.assertFalse(_passes_window_size_filter(59, 45))
        self.assertFalse(_passes_window_size_filter(60, 44))
        self.assertFalse(_passes_window_size_filter(0, 0))

    def test_matches_legacy_expression(self):
        cases = [(0, 0), (59, 100), (60, 44), (60, 45), (800, 600), (200, 150)]
        for width, height in cases:
            area = max(0, width) * max(0, height)
            legacy_skip = width < 60 or height < 45 or area <= 0
            self.assertEqual(_passes_window_size_filter(width, height), not legacy_skip)

    def test_garbage_rejected(self):
        self.assertFalse(_passes_window_size_filter("x", None))
        self.assertFalse(_passes_window_size_filter(None, None))


class TestDedupeLargestPerPid(unittest.TestCase):
    def test_largest_wins(self):
        out = _dedupe_largest_per_pid([_win(1, 10), _win(1, 50), _win(1, 30)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["area"], 50)

    def test_stable_order_by_created_then_pid(self):
        out = _dedupe_largest_per_pid([_win(2, 5, 9.0), _win(1, 5, 1.0), _win(3, 5, 1.0)])
        self.assertEqual([item["pid"] for item in out], [1, 3, 2])

    def test_empty(self):
        self.assertEqual(_dedupe_largest_per_pid([]), [])


class _FakeProc:
    def __init__(self, pid, created=0.0, name="RobloxPlayerBeta.exe", broken=False):
        self.pid = pid
        self._created = created
        self._name = name
        self._broken = broken

    def create_time(self):
        if self._broken:
            raise OSError("gone")
        return self._created

    def name(self):
        if self._broken:
            raise OSError("gone")
        return self._name


class TestCollectProcMeta(unittest.TestCase):
    def test_snapshot(self):
        meta = _collect_roblox_proc_meta(
            lambda game_only=True: [_FakeProc(11, 7.5), _FakeProc(12, 8.5, "other.exe")]
        )
        self.assertEqual(meta[11], {"created": 7.5, "name": "RobloxPlayerBeta.exe"})
        self.assertEqual(meta[12]["name"], "other.exe")

    def test_broken_proc_skipped(self):
        meta = _collect_roblox_proc_meta(
            lambda game_only=True: [_FakeProc(11), _FakeProc("x", broken=True)]
        )
        self.assertEqual(list(meta), [11])

    def test_empty(self):
        self.assertEqual(_collect_roblox_proc_meta(lambda game_only=True: []), {})


class _FakeUser32:
    def __init__(self, ok=True, fail=False):
        self._ok = ok
        self._fail = fail

    def GetWindowRect(self, hwnd, rect):
        if self._fail:
            raise OSError("win32 gone")
        return self._ok


def _rect(left=10, top=20, right=810, bottom=620):
    rect = _EnumRect()
    rect.left, rect.top, rect.right, rect.bottom = left, top, right, bottom
    return rect


class TestMeasureWindowRect(unittest.TestCase):
    def test_measures(self):
        out = _measure_window_rect(_FakeUser32(ok=True), _rect(), 5, False)
        self.assertEqual(out, (10, 20, 810, 620, 800, 600))

    def test_false_skipped(self):
        self.assertIsNone(_measure_window_rect(_FakeUser32(ok=False), _rect(), 5, False))

    def test_exception_skipped(self):
        self.assertIsNone(_measure_window_rect(_FakeUser32(fail=True), _rect(), 5, False))


if __name__ == "__main__":
    unittest.main()
