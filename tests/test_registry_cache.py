from __future__ import annotations

import os
from pathlib import Path

from kernel.registry import PluginRegistry


def _write_plugin(path: Path, version: str) -> None:
    path.write_text(
        "\n".join(
            [
                "__plugin__ = {",
                '    "name": "Test plugin",',
                '    "id": "demo",',
                f'    "version": "{version}",',
                '    "scope": "user",',
                '    "config_schema": {"limit": {"default": 3}},',
                "}",
                "",
                "async def setup(ctx):",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_get_meta_reuses_ast_result_until_source_changes(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin_file = plugins_dir / "demo.py"
    state_file = tmp_path / "data" / "plugins_state.json"
    _write_plugin(plugin_file, "1.0.0")
    registry = PluginRegistry(plugins_dir=plugins_dir, state_file=state_file)

    parse_calls = 0
    original_parse = registry.parse_meta

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(registry, "parse_meta", counted_parse)

    assert registry.get_meta("demo").version == "1.0.0"
    assert registry.get_meta("demo").version == "1.0.0"
    assert registry.get_config("demo")["limit"] == 3
    assert parse_calls == 1

    _write_plugin(plugin_file, "1.0.1")
    stat = plugin_file.stat()
    os.utime(plugin_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    assert registry.get_meta("demo").version == "1.0.1"
    assert parse_calls == 2


def test_invalidate_scan_cache_also_invalidates_single_meta_cache(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin_file = plugins_dir / "demo.py"
    _write_plugin(plugin_file, "1.0.0")
    registry = PluginRegistry(
        plugins_dir=plugins_dir,
        state_file=tmp_path / "data" / "plugins_state.json",
    )

    parse_calls = 0
    original_parse = registry.parse_meta

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(registry, "parse_meta", counted_parse)

    registry.get_meta("demo")
    registry.invalidate_scan_cache()
    registry.get_meta("demo")

    assert parse_calls == 2
