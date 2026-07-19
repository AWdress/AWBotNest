import json
import logging
import tempfile
import unittest
from pathlib import Path

from webui import log_stream


class PersistentLogStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._old_history_file = log_stream._HISTORY_FILE
        self._old_loaded = log_stream._HISTORY_LOADED
        self._old_writes = log_stream._WRITES_SINCE_COMPACT
        self._old_buffer = list(log_stream._BUFFER)
        log_stream._HISTORY_FILE = Path(self._temp_dir.name) / "logs" / "webui_history.jsonl"
        log_stream._HISTORY_LOADED = False
        log_stream._WRITES_SINCE_COMPACT = 0
        log_stream._BUFFER.clear()

    def tearDown(self) -> None:
        log_stream._BUFFER.clear()
        log_stream._BUFFER.extend(self._old_buffer)
        log_stream._HISTORY_FILE = self._old_history_file
        log_stream._HISTORY_LOADED = self._old_loaded
        log_stream._WRITES_SINCE_COMPACT = self._old_writes
        self._temp_dir.cleanup()

    @staticmethod
    def _record(message: str, plugin: str = "demo") -> logging.LogRecord:
        record = logging.LogRecord(
            name="main",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        record.plugin = plugin
        return record

    def test_history_survives_memory_reset(self) -> None:
        log_stream._load_history()
        log_stream.LogStreamHandler().emit(self._record("persist this log"))

        log_stream._BUFFER.clear()
        log_stream._HISTORY_LOADED = False
        restored = log_stream.recent_logs()

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["source"], "demo")
        self.assertEqual(restored[0]["msg"], "persist this log")
        self.assertRegex(restored[0]["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_broken_history_lines_are_ignored(self) -> None:
        log_stream._HISTORY_FILE.parent.mkdir(parents=True)
        valid = {
            "date": "2026-07-19",
            "time": "12:00:00",
            "level": "INFO",
            "name": "main",
            "source": "demo",
            "msg": "valid log",
        }
        log_stream._HISTORY_FILE.write_text(
            "not-json\n" + json.dumps(valid, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        restored = log_stream.recent_logs()

        self.assertEqual(restored, [valid])

    def test_only_latest_history_is_restored(self) -> None:
        log_stream._HISTORY_FILE.parent.mkdir(parents=True)
        with log_stream._HISTORY_FILE.open("w", encoding="utf-8") as stream:
            for index in range(log_stream._BUFFER_LIMIT + 25):
                item = {
                    "date": "2026-07-19",
                    "time": "12:00:00",
                    "level": "INFO",
                    "name": "main",
                    "source": "demo",
                    "msg": f"entry {index}",
                }
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

        restored = log_stream.recent_logs()

        self.assertEqual(len(restored), log_stream._BUFFER_LIMIT)
        self.assertEqual(restored[0]["msg"], "entry 25")
        self.assertEqual(restored[-1]["msg"], f"entry {log_stream._BUFFER_LIMIT + 24}")

    def test_trim_history_updates_memory_and_disk(self) -> None:
        log_stream._load_history()
        handler = log_stream.LogStreamHandler()
        for index in range(5):
            handler.emit(self._record(f"entry {index}"))

        self.assertTrue(log_stream.trim_history(2))
        self.assertEqual([item["msg"] for item in log_stream.recent_logs()], ["entry 3", "entry 4"])

        log_stream._BUFFER.clear()
        log_stream._HISTORY_LOADED = False
        self.assertEqual(
            [item["msg"] for item in log_stream.recent_logs()],
            ["entry 3", "entry 4"],
        )


if __name__ == "__main__":
    unittest.main()
