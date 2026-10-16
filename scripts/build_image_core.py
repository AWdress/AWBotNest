"""Compile the Docker core and reject incomplete or incompatible artifacts.

Only the compiled core and a transparent launcher enter the runtime image.
Core source snapshots, the compiler and its generated files stay in the
disposable build stage.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.machinery
import importlib.metadata
import inspect
import json
import multiprocessing
import runpy
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace


def api_app():
    from awbotnest.api import create_app
    from awbotnest.config import Settings
    from awbotnest.routing import PluginRoutes
    from awbotnest.scheduler import PluginScheduler

    return create_app(
        Settings(), SimpleNamespace(), SimpleNamespace(), PluginScheduler(),
        PluginRoutes(), market=SimpleNamespace(clear_cache=lambda: None),
    )


def verify_interfaces() -> None:
    from fastapi.testclient import TestClient

    from awbotnest.activity import install_client_hooks
    from awbotnest.auth import admin_dependency
    from awbotnest.config import Settings
    from awbotnest.plugin_runtime.loader import PluginLoader
    from awbotnest.services.ocr import _worker_call

    class ThirdPartySender:
        __annotations__ = None
        __type_params__ = None

        async def __call__(self):
            return "ok"

    sender = install_client_hooks(SimpleNamespace(send_message=ThirdPartySender()))
    if asyncio.run(sender.send_message()) != "ok":
        raise RuntimeError("Compiled wrappers cannot call third-party senders")

    # Exercise async dependency dispatch, not only schema generation. No real
    # account, credentials, config file or external service is used here.
    if not inspect.iscoroutinefunction(admin_dependency(Settings())):
        raise RuntimeError("Compiled async dependencies lost coroutine metadata")
    with TestClient(api_app()) as client:
        if client.get("/api/accounts").status_code != 401:
            raise RuntimeError("Compiled API did not enforce authentication")
        if client.post("/api/auth/login", json={}).status_code != 422:
            raise RuntimeError("Compiled API did not validate request bodies")

    # A source plugin must still import the compiled public interfaces and load
    # through the ordinary plugin loader. Keep the disposable entry out of both
    # the published repository and the runtime image.
    with tempfile.TemporaryDirectory(prefix="awbotnest-build-") as directory:
        root = Path(directory)
        entry = root / "image_core_check.py"
        entry.write_text(
            "from awbotnest.context import PluginContext\n"
            "async def setup(ctx):\n"
            "    return PluginContext.__name__\n", encoding="utf-8",
        )
        loader = PluginLoader(root)
        try:
            plugin = loader.load(entry, entry.stem)
            if asyncio.run(plugin.setup(SimpleNamespace())) != "PluginContext":
                raise RuntimeError("Source plugins cannot use the compiled core")
        finally:
            loader.unload(entry.stem)

    # Validate serialization and re-import inside a spawn worker without
    # importing an OCR model or downloading optional dependencies.
    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        future = pool.submit(_worker_call, "default", "__image_build_check__", (), {})
        try:
            future.result(timeout=30)
        except ValueError as exc:
            if "__image_build_check__" not in str(exc):
                raise
        else:
            raise RuntimeError("Compiled OCR worker did not dispatch correctly")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def verify(output: Path, manifest_path: Path) -> None:
    sys.dont_write_bytecode = True
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Run in a fresh, isolated interpreter: never accept imports from the source
    # checkout or an installed plaintext copy instead of the compiled core.
    sys.path.insert(0, str(output))
    for name in manifest["modules"]:
        module = importlib.import_module(name)
        if not hasattr(module, "__compiled__"):
            raise RuntimeError(f"Core module was not compiled: {name}")
        if not Path(module.__file__).resolve().is_relative_to(output):
            raise RuntimeError(f"Core module loaded outside the artifact: {name}")

    # Old container configurations retain `python -m awbotnest.main`. A small
    # source launcher provides get_code() without exposing the compiled core.
    from awbotnest import _image_main

    entry = output / "awbotnest" / "main.py"
    if entry.read_bytes() != Path(__file__).with_name("image_module_entrypoint.py").read_bytes():
        raise RuntimeError("Image module launcher does not match its transparent source")
    if sorted(path.relative_to(output / "awbotnest").as_posix()
              for path in (output / "awbotnest").rglob("*") if path.is_file()) != ["main.py"]:
        raise RuntimeError("Image entry directory contains unexpected files")
    calls = []
    original_main = _image_main.main
    _image_main.main = lambda: calls.append("started")
    try:
        runpy.run_module("awbotnest.main", run_name="__main__")
        runpy.run_module("awbotnest.main", run_name="__mp_main__")
    finally:
        _image_main.main = original_main
    if calls != ["started"]:
        raise RuntimeError("Legacy module entry did not start exactly once")
    if importlib.import_module("awbotnest.main") is not _image_main:
        raise RuntimeError("Image entry changed the public main module's globals")

    from awbotnest.config import APP_ROOT
    from awbotnest.main import main

    if APP_ROOT != output:
        raise RuntimeError(f"Compiled core uses the wrong application root: {APP_ROOT}")
    if not callable(main):
        raise RuntimeError("Compiled core has no callable entry point")
    if api_app().openapi() != manifest["openapi"]:
        raise RuntimeError("Compiled core changed the OpenAPI contract")
    verify_interfaces()
    print(f"Compiled core verified: {len(manifest['modules'])} modules; "
          "legacy entry compatible; OpenAPI unchanged")


def compile_core(source: Path, output: Path) -> None:
    if not source.is_dir() or not (source / "__init__.py").is_file():
        raise RuntimeError(f"Missing core package: {source}")
    output.mkdir(parents=True, exist_ok=False)
    modules = set()
    for path in source.rglob("*.py"):
        relative = path.relative_to(source.parent).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        name = ".".join(parts)
        modules.add("awbotnest._image_main" if name == "awbotnest.main" else name)
    sys.path.insert(0, str(source.parent))
    manifest = output.parent / f"{output.name}-contract.json"
    manifest.write_text(json.dumps({
        "modules": sorted(modules), "openapi": api_app().openapi(),
    }, ensure_ascii=False), encoding="utf-8")

    # Keep the source deployment unchanged. Only the disposable build copy uses
    # a private compiled implementation so Python -m can load a source launcher.
    with tempfile.TemporaryDirectory(prefix="awbotnest-core-") as directory:
        staged = Path(directory) / "awbotnest"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        if (staged / "_image_main.py").exists():
            raise RuntimeError("Image entry implementation name is already in use")
        (staged / "main.py").rename(staged / "_image_main.py")
        subprocess.run([
            sys.executable, "-m", "nuitka", "--mode=package", str(staged),
            "--include-package=awbotnest", f"--output-dir={output}",
            "--file-reference-choice=runtime", "--no-pyi-file", "--remove-output",
            "--jobs=2", "--lto=no",
        ], cwd=staged.parent, check=True)
    artifacts = list(output.iterdir())
    if (len(artifacts) != 1 or not artifacts[0].is_file()
            or not any(artifacts[0].name == f"awbotnest{suffix}"
                       for suffix in importlib.machinery.EXTENSION_SUFFIXES)):
        raise RuntimeError("Compiler did not produce exactly one core extension")
    entry_dir = output / "awbotnest"
    entry_dir.mkdir()
    shutil.copyfile(Path(__file__).with_name("image_module_entrypoint.py"), entry_dir / "main.py")
    subprocess.run([
        sys.executable, "-I", str(Path(__file__).resolve()),
        "--verify", str(output), "--manifest", str(manifest),
    ], check=True, timeout=120)
    notices = output.parent / "nuitka-licenses"
    notices.mkdir(exist_ok=True)
    distribution = importlib.metadata.distribution("Nuitka")
    for name in ("LICENSE.txt", "LICENSE-RUNTIME.txt", "NOTICE.txt"):
        file = next(item for item in distribution.files or () if item.name == name)
        shutil.copyfile(distribution.locate_file(file), notices / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("awbotnest"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        if args.manifest is None or not sys.flags.isolated:
            parser.error("--verify requires --manifest and Python -I")
        verify(args.verify.resolve(), args.manifest.resolve())
    else:
        if args.output is None:
            parser.error("--output is required")
        compile_core(args.source.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
