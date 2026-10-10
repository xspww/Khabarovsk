"""Tests for the shared window-enumeration rules (pure, no Win32)."""
import unittest

from services.roblox_windows import (
    _dedupe_largest_per_pid,
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


if __name__ == "__main__":
    unittest.main()
