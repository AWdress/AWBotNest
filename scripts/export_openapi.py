"""Export the FastAPI contract without starting Telegram or the web server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awbotnest.api import create_app
from awbotnest.config import Settings
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scheduler = PluginScheduler()
    runtime = SimpleNamespace()
    accounts = SimpleNamespace()
    market = SimpleNamespace(clear_cache=lambda: None)
    app = create_app(Settings(), accounts, runtime, scheduler, PluginRoutes(), market=market)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
