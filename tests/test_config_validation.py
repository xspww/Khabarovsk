"""Config validation: pure clamping/coercion, no I/O."""
import unittest

from config.validation import (
    CONFIG_SCHEMA_VERSION,
    validate_config_payload,
)


class TestValidateConfigPayload(unittest.TestCase):
    def test_bool_coercion(self) -> None:
        defaults = {"a": True, "b": False}
        out = validate_config_payload({"a": "off", "b": "on"}, defaults)
        self.assertFalse(out["a"])
        self.assertTrue(out["b"])
        self.assertEqual(out["schema_version"], CONFIG_SCHEMA_VERSION)

    def test_int_clamp(self) -> None:
        defaults = {"max_concurrent_accounts": 5}
        out = validate_config_payload({"max_concurrent_accounts": 9999}, defaults)
        self.assertEqual(out["max_concurrent_accounts"], 200)
        out = validate_config_payload({"max_concurrent_accounts": -5}, defaults)
        self.assertEqual(out["max_concurrent_accounts"], 1)
        out = validate_config_payload({"max_concurrent_accounts": "not-a-number"}, defaults)
        self.assertEqual(out["max_concurrent_accounts"], 5)

    def test_float_clamp(self) -> None:
        defaults = {"recovery_confidence_threshold": 50.0}
        out = validate_config_payload({"recovery_confidence_threshold": 999.0}, defaults)
        self.assertEqual(out["recovery_confidence_threshold"], 100.0)
        out = validate_config_payload({"recovery_confidence_threshold": "bad"}, defaults)
        self.assertEqual(out["recovery_confidence_threshold"], 50.0)

    def test_str_list_dict_passthrough(self) -> None:
        defaults = {"name": "", "tags": [1], "meta": {"x": 1}}
        out = validate_config_payload({"name": 0, "tags": "nope", "meta": "nope"}, defaults)
        self.assertEqual(out["name"], "")
        self.assertEqual(out["tags"], [1])
        self.assertEqual(out["meta"], {"x": 1})

    def test_removed_keys_dropped_and_extra_kept(self) -> None:
        defaults = {"keep": 1}
        out = validate_config_payload(
            {"keep": 1, "home_rejoin_enabled": True, "custom_extra": "yes"},
            defaults,
        )
        self.assertNotIn("home_rejoin_enabled", out)
        self.assertEqual(out["custom_extra"], "yes")

    def test_non_dict_input(self) -> None:
        out = validate_config_payload(None, {"a": 1})
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["schema_version"], CONFIG_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
