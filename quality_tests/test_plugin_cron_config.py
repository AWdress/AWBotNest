import unittest

from awbotnest.plugins import PluginRuntime


class PluginCronConfigTests(unittest.TestCase):
    def test_declared_cron_format_accepts_five_and_six_fields(self):
        schema = {"schedule": {"type": "string", "format": "cron"}}

        PluginRuntime.validate_config(schema, {"schedule": "6 3 * * *"})
        PluginRuntime.validate_config(schema, {"schedule": "0 6 3 * * *"})

    def test_declared_cron_format_rejects_invalid_expression(self):
        schema = {"schedule": {"type": "string", "format": "cron"}}

        with self.assertRaisesRegex(ValueError, "5 位或 6 位 Cron"):
            PluginRuntime.validate_config(schema, {"schedule": "61 3 * * *"})

    def test_legacy_cron_type_is_still_supported(self):
        PluginRuntime.validate_config(
            {"schedule": {"type": "cron"}},
            {"schedule": "*/10 * * * *"},
        )


if __name__ == "__main__":
    unittest.main()
