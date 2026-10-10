"""Tests for the single-owner resource-signal scorer."""
import unittest

from services.process_proof_policy import score_resource_signals


def _legacy(windows, rss_mb, cpu):
    return (
        min(15.0, float(windows) * 7.0)
        + min(12.0, float(rss_mb) / 120.0)
        + min(10.0, float(cpu) * 2.0)
    )


class TestScoreResourceSignals(unittest.TestCase):
    def test_matches_legacy_formula(self):
        cases = [
            (0, 0.0, 0.0),
            (1, 60.0, 2.5),
            (3, 500.0, 12.0),
            (10, 4000.0, 100.0),
        ]
        for windows, rss, cpu in cases:
            self.assertAlmostEqual(
                score_resource_signals(windows, rss, cpu),
                _legacy(windows, rss, cpu),
                places=9,
            )

    def test_caps(self):
        self.assertAlmostEqual(score_resource_signals(99, 1e6, 1e6), 37.0)

    def test_garbage_returns_zero(self):
        self.assertEqual(score_resource_signals("x", None, object()), 0.0)


if __name__ == "__main__":
    unittest.main()
