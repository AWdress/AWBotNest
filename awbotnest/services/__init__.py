"""Platform service implementations and compatibility exports."""

from ..config import DATA_DIR
from .ai import AIService, PluginAI
from .browser import BrowserService
from .container import PlatformServices
from .cookies import CookieService
from .http import HttpService

__all__ = [
    "AIService", "BrowserService", "CookieService", "HttpService",
    "PlatformServices", "PluginAI", "DATA_DIR",
]
