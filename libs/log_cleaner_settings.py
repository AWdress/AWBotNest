"""Lightweight storage helpers for log-cleaner settings."""

from libs.state import state_manager


def _bounded_int(value, default, minimum, maximum):
    try:
        return min(max(int(value), minimum), maximum)
    except (ValueError, TypeError):
        return default


def _enabled(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "1", "yes"}
    return default


def get_log_cleaner_settings():
    """Return normalized log-cleaner settings."""
    return {
        "enabled": _enabled(state_manager.get_item("SYSTEM", "log_cleaner_enabled", "on")),
        "keep_lines": _bounded_int(
            state_manager.get_item("SYSTEM", "log_keep_lines", 100), 100, 1, 1000
        ),
        "hour": _bounded_int(
            state_manager.get_item("SYSTEM", "log_clean_hour", 3), 3, 0, 23
        ),
        "minute": _bounded_int(
            state_manager.get_item("SYSTEM", "log_clean_minute", 0), 0, 0, 59
        ),
    }


def save_log_cleaner_settings(value):
    """Validate and save settings submitted from the maintenance page."""
    value = value if isinstance(value, dict) else {}
    settings = {
        "enabled": _enabled(value.get("enabled", True)),
        "keep_lines": _bounded_int(value.get("keep_lines"), 100, 1, 1000),
        "hour": _bounded_int(value.get("hour"), 3, 0, 23),
        "minute": _bounded_int(value.get("minute"), 0, 0, 59),
    }
    state_manager.set_section("SYSTEM", {
        "log_cleaner_enabled": "on" if settings["enabled"] else "off",
        "log_keep_lines": settings["keep_lines"],
        "log_clean_hour": settings["hour"],
        "log_clean_minute": settings["minute"],
    })
    return settings
