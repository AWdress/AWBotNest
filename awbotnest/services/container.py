from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..config import DATA_DIR, Settings

from .ai import AIService
from .browser import BrowserService
from .cookies import CookieService
from .http import HttpService

class PlatformServices:
    def __init__(self, settings: Settings) -> None:
        from ..governance import PluginGovernor
        # Resolve through the public compatibility module so existing platform tests and
        # embedders that override awbotnest.services.DATA_DIR retain their old behavior.
        from . import DATA_DIR as services_data_dir
        self.governor = PluginGovernor(services_data_dir / "plugin_events.jsonl")
        self.http = HttpService(settings)
        self.cookies = CookieService()
        self.browser = BrowserService(settings)
        self.ai = AIService(settings, self.http)
