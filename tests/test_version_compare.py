"""Version helpers: pure, no I/O. Guards release tag <-> APP_VERSION contract."""
import unittest

from version import (
    compare_versions,
    is_newer_version,
    parse_version_tuple,
    strip_tag_prefix,
    tag_for_version,
)


class TestVersionHelpers(unittest.TestCase):
    def test_tag_roundtrip(self) -> None:
        self.assertEqual(tag_for_version("3.2.4"), "v3.2.4")
        self.assertEqual(tag_for_version("v3.2.4"), "v3.2.4")
        self.assertEqual(strip_tag_prefix("v3.2.4"), "3.2.4")
        self.assertEqual(strip_tag_prefix("3.2.4"), "3.2.4")
        self.assertEqual(strip_tag_prefix(""), "")

    def test_parse_tuple(self) -> None:
        self.assertEqual(parse_version_tuple("3.2.4"), (3, 2, 4))
        self.assertEqual(parse_version_tuple("v3.2"), (3, 2))
        self.assertEqual(parse_version_tuple(""), (0,))

    def test_compare_release(self) -> None:
        self.assertEqual(compare_versions("3.2.4", "3.2.4"), 0)
        self.assertEqual(compare_versions("3.2.5", "3.2.4"), 1)
        self.assertEqual(compare_versions("3.2.3", "3.2.4"), -1)
        self.assertEqual(compare_versions("v3.2.4", "3.2.4"), 0)
        self.assertEqual(compare_versions("10.0.0", "9.9.9"), 1)

    def test_compare_prerelease(self) -> None:
        self.assertEqual(compare_versions("1.0.0-beta.1", "1.0.0"), -1)
        self.assertEqual(compare_versions("1.0.0", "1.0.0-beta.1"), 1)
        self.assertLess(compare_versions("1.0.0-beta.1", "1.0.0-beta.2"), 0)
        self.assertEqual(compare_versions("1.0.0-beta.1", "1.0.0-beta.1"), 0)

    def test_is_newer(self) -> None:
        self.assertTrue(is_newer_version("3.2.5", "3.2.4"))
        self.assertFalse(is_newer_version("3.2.4", "3.2.4"))
        self.assertFalse(is_newer_version("3.2.3", "3.2.4"))


if __name__ == "__main__":
    unittest.main()
