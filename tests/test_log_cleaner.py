import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


_MODULE_PATH = Path(__file__).parents[1] / "schedulers" / "universal" / "log_cleaner.py"
_SPEC = importlib.util.spec_from_file_location("log_cleaner_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
log_cleaner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(log_cleaner)


class LogCleanerTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_values_are_bounded(self) -> None:
        with patch.object(log_cleaner.state_manager, "set_section") as set_section:
            result = log_cleaner.save_log_cleaner_settings({
                "enabled": False,
                "keep_lines": 0,
                "hour": 30,
                "minute": -4,
            })

        self.assertEqual(result, {"enabled": False, "keep_lines": 1, "hour": 23, "minute": 0})
        set_section.assert_called_once_with("SYSTEM", {
            "log_cleaner_enabled": "off",
            "log_keep_lines": 1,
            "log_clean_hour": 23,
            "log_clean_minute": 0,
        })

    def test_keep_lines_cannot_exceed_history_limit(self) -> None:
        with patch.object(log_cleaner.state_manager, "set_section"):
            result = log_cleaner.save_log_cleaner_settings({"keep_lines": 5000})

        self.assertEqual(result["keep_lines"], 1000)

    async def test_cleaner_includes_plugin_history(self) -> None:
        settings = {"enabled": True, "keep_lines": 80, "hour": 3, "minute": 0}
        with (
            patch.object(log_cleaner, "get_log_cleaner_settings", return_value=settings),
            patch.object(log_cleaner, "clean_log_file", return_value=False),
            patch("webui.log_stream.trim_history", return_value=True) as trim_history,
        ):
            await log_cleaner.log_cleaner_action()

        trim_history.assert_called_once_with(80)


if __name__ == "__main__":
    unittest.main()
