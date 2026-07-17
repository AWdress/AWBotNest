"""Web console package with lazy API exports for early startup helpers."""

__all__ = ["app", "start_web_ui"]


def __getattr__(name: str):
    if name in __all__:
        from webui.api import app, start_web_ui

        return {"app": app, "start_web_ui": start_web_ui}[name]
    raise AttributeError(name)
