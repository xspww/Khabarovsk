"""Browser tracker helpers: pure string rules, no browser needed."""
import unittest

from services.browser_tracker import (
    extract_browser_tracker_id,
    tracker_label,
    tracker_matches,
)


class TestBrowserTracker(unittest.TestCase):
    def test_extract_variants(self) -> None:
        self.assertEqual(extract_browser_tracker_id("browsertrackerid:ABC123"), "ABC123")
        self.assertEqual(extract_browser_tracker_id("..browserTrackerId=XYZ-9_8.."), "XYZ-9_8")
        self.assertEqual(extract_browser_tracker_id(""), "")
        self.assertEqual(extract_browser_tracker_id(None), "")
        self.assertEqual(extract_browser_tracker_id("no tracker here"), "")

    def test_matches(self) -> None:
        self.assertTrue(tracker_matches("A", "A"))
        self.assertFalse(tracker_matches("A", "B"))
        self.assertFalse(tracker_matches("", "B"))
        self.assertFalse(tracker_matches("A", ""))
        # strips before compare
        self.assertTrue(tracker_matches(" A ", "A"))

    def test_label(self) -> None:
        self.assertEqual(tracker_label(""), "")
        self.assertEqual(tracker_label("ABC"), "ABC")
        self.assertEqual(tracker_label("12345678"), "12345678")
        self.assertEqual(tracker_label("TRACKER123456789"), "...23456789")
        self.assertTrue(tracker_label("TRACKER123456789").startswith("..."))


if __name__ == "__main__":
    unittest.main()
