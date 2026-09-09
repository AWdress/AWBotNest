"""Micro-benchmark for AWBotNest's latency-sensitive interaction path.

Run from the repository root:
    python benchmarks/interactive_latency.py --iterations 1000

The benchmark uses local fakes and never connects to Telegram.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awbotnest.config import Settings
from awbotnest.context import PluginContext
from awbotnest.delivery import TelegramDelivery
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler
from awbotnest.services import PlatformServices
from awbotnest.sessions import SessionManager


def summarize(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    percentile = lambda value: ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * value) - 1))]
    return {
        "mean_us": statistics.fmean(ordered) / 1_000,
        "median_us": statistics.median(ordered) / 1_000,
        "p95_us": percentile(0.95) / 1_000,
        "p99_us": percentile(0.99) / 1_000,
        "min_us": ordered[0] / 1_000,
        "max_us": ordered[-1] / 1_000,
        "samples": len(ordered),
    }


class FakeClient:
    def __init__(self) -> None:
        self.handlers = []

    def is_connected(self) -> bool:
        return True

    def add_event_handler(self, callback, builder) -> None:
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback, builder) -> None:
        self.handlers.remove((callback, builder))


async def handler_wrapper_samples(iterations: int, root: Path, *, profile: bool = False) -> tuple[list[int], list[int]]:
    client = FakeClient()
    accounts = SimpleNamespace(
        users={"bench": client}, bots={},
        clients_for_scope=lambda scope, bot_id: [client],
        choose_bot=lambda bot_id: None,
    )
    settings = Settings(api_id=1, api_hash="benchmark")
    scheduler = PluginScheduler()
    profile_env = {"AWBOTNEST_INTERACTIVE_PROFILE": "1"} if profile else {}
    with patch("awbotnest.context.DATA_DIR", root), patch("awbotnest.storage.DATA_DIR", root), \
            patch.dict(os.environ, profile_env, clear=False):
        if not profile:
            os.environ.pop("AWBOTNEST_INTERACTIVE_PROFILE", None)
        services = PlatformServices(settings)
        context = PluginContext(
            "latency_benchmark", "user", accounts, scheduler, settings, services,
            PluginRoutes(), SimpleNamespace(), plugin_name="延迟基准",
        )
        context.interactive_profile.slow_ms = float("inf")

        @context.on_callback()
        async def regular_callback(event):
            return event

        @context.on_callback(interactive=True)
        async def fast_callback(event):
            return event

        regular_wrapper = client.handlers[0][0]
        fast_wrapper = client.handlers[1][0]
        event = SimpleNamespace(id=1, chat_id=1)
        direct = []
        regular = []
        fast = []
        for _ in range(iterations):
            started = time.perf_counter_ns()
            await fast_callback(event)
            direct.append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            await regular_wrapper(event)
            regular.append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            await fast_wrapper(event)
            fast.append(time.perf_counter_ns() - started)
        await context.close()
        scheduler.stop()
    # Subtract the direct coroutine-call cost sample-by-sample.
    return (
        [max(0, regular[index] - direct[index]) for index in range(iterations)],
        [max(0, fast[index] - direct[index]) for index in range(iterations)],
    )


async def session_lock_wait_samples(iterations: int) -> list[int]:
    sessions = SessionManager("latency_benchmark", "latency_benchmark")
    session = await sessions.get("chat")
    samples = []
    for _ in range(iterations):
        await session.lock.acquire()
        started = time.perf_counter_ns()
        waiter = asyncio.create_task(session.lock.acquire())
        await asyncio.sleep(0)
        session.lock.release()
        await waiter
        samples.append(time.perf_counter_ns() - started)
        session.lock.release()
    await sessions.close()
    return samples


async def session_uncontended_samples(iterations: int) -> list[int]:
    sessions = SessionManager("latency_benchmark", "latency_benchmark")
    session = await sessions.get("chat")
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        async with session.lock:
            pass
        samples.append(time.perf_counter_ns() - started)
    await sessions.close()
    return samples


async def session_contention_samples(concurrency: int, *, slow_await: bool) -> tuple[list[int], list[int], list[int]]:
    sessions = SessionManager("latency_benchmark", "latency_benchmark")
    session = await sessions.get("chat", initial={"count": 0})
    ready = asyncio.Event()
    started_count = 0

    async def worker():
        nonlocal started_count
        started_count += 1
        if started_count == concurrency:
            ready.set()
        await ready.wait()
        total_started = time.perf_counter_ns()
        wait_started = time.perf_counter_ns()
        await session.lock.acquire()
        acquired = time.perf_counter_ns()
        try:
            session.data["count"] += 1
            if slow_await:
                await asyncio.sleep(0.02)
        finally:
            released = time.perf_counter_ns()
            session.lock.release()
        return acquired - wait_started, released - acquired, released - total_started

    values = await asyncio.gather(*(worker() for _ in range(concurrency)))
    await sessions.close()
    return tuple([item[index] for item in values] for index in range(3))


async def telegram_api_start_samples(iterations: int) -> tuple[list[int], list[int]]:
    delivery = TelegramDelivery("latency_benchmark", "latency_benchmark")
    starts = []

    class Client:
        async def send_message(self, chat, text, **kwargs):
            starts.append(time.perf_counter_ns())
            return text

    client = Client()
    direct = []
    delivery_samples = []
    for index in range(iterations):
        started = time.perf_counter_ns()
        await client.send_message(index, "benchmark")
        direct.append(starts[-1] - started)
        started = time.perf_counter_ns()
        await delivery.send(client, index, "benchmark")
        delivery_samples.append(starts[-1] - started)
    await delivery.close()
    return direct, delivery_samples


async def main(iterations: int) -> None:
    with tempfile.TemporaryDirectory() as folder:
        regular_wrapper, fast_wrapper = await handler_wrapper_samples(iterations, Path(folder))
        _, profiled_wrapper = await handler_wrapper_samples(iterations, Path(folder), profile=True)
        direct_start, delivery_start = await telegram_api_start_samples(iterations)
        results = {
            "handler wrapper overhead (regular)": summarize(regular_wrapper),
            "interactive dispatch (profiling disabled)": summarize(fast_wrapper),
            "interactive dispatch (profiling enabled)": summarize(profiled_wrapper),
            "Session lock uncontended": summarize(await session_uncontended_samples(iterations)),
            "Session lock one-yield wait": summarize(await session_lock_wait_samples(iterations)),
            "Telegram direct API call start": summarize(direct_start),
            "TelegramDelivery immediate API call start": summarize(delivery_start),
        }
        for concurrency in (2, 5, 10, 20, 50):
            for scenario, slow in (("memory-only", False), ("slow-await", True)):
                waits, holds, totals = await session_contention_samples(concurrency, slow_await=slow)
                prefix = f"Session contention {scenario} x{concurrency}"
                results[f"{prefix} wait"] = summarize(waits)
                results[f"{prefix} hold"] = summarize(holds)
                results[f"{prefix} total"] = summarize(totals)
    print(f"iterations: {iterations}")
    print("Metric | Mean(us) | Median(us) | P95(us) | P99(us) | Min(us) | Max(us) | Samples")
    for name, values in results.items():
        print(
            f"{name} | {values['mean_us']:.3f} | {values['median_us']:.3f} | "
            f"{values['p95_us']:.3f} | {values['p99_us']:.3f} | {values['min_us']:.3f} | "
            f"{values['max_us']:.3f} | {values['samples']}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    asyncio.run(main(args.iterations))
