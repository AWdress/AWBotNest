from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..auth import admin_dependency
from ..config import APP_ROOT, Settings
from ..market import PluginMarket
from ..open_api import register_open_api
from ..resources import ResourceSampler
from .accounts import create_router as accounts_router
from .docs import register_api_docs
from .operations import create_router as operations_router
from .plugin_routes import create_router as plugin_routes_router
from .plugins import create_router as plugins_router
from .settings import create_router as settings_router
from .system import create_router as system_router


@dataclass(slots=True)
class ApiDependencies:
    settings: object
    accounts: object
    runtime: object
    scheduler: object
    routes: object
    restart_event: asyncio.Event | None
    market: object
    require_admin: object
    started_at: float
    resource_sampler: ResourceSampler


def create_app(settings: Settings, accounts, runtime, scheduler, routes,
               restart_event: asyncio.Event | None = None,
               market: PluginMarket | None = None) -> FastAPI:
    app = FastAPI(
        title="AWBotNest API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.platform_ready = False
    app.state.platform_startup_error = ""
    market = market or PluginMarket(settings)
    deps = ApiDependencies(settings, accounts, runtime, scheduler, routes, restart_event, market,
                           admin_dependency(settings), time.monotonic(), ResourceSampler())
    register_open_api(app, settings, accounts, runtime)
    app.include_router(system_router(deps))
    plugin_router, list_plugins = plugin_routes_router(deps)
    app.include_router(plugin_router)
    app.include_router(settings_router(deps))
    app.include_router(accounts_router(deps))
    app.include_router(plugins_router(deps))
    app.include_router(operations_router(deps, list_plugins))
    register_api_docs(app, __version__)
    static_dir = APP_ROOT / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="webui")
    return app
