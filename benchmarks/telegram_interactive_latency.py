"""Manual real-Telegram RPC/network latency probe; never run as a CI test."""
from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import Button, events

from awbotnest.config import load_settings
from awbotnest.delivery import TelegramDelivery
from awbotnest.telegram import TelegramAccounts


def mask(value: object) -> str:
    text = str(value)
    return f"***{text[-4:]}" if len(text) > 4 else "***"


def summarize(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    at = lambda p: ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * p) - 1))]
    return {
        "mean": statistics.fmean(ordered) / 1_000_000,
        "median": statistics.median(ordered) / 1_000_000,
        "p90": at(.90) / 1_000_000,
        "p95": at(.95) / 1_000_000,
        "p99": at(.99) / 1_000_000,
        "min": ordered[0] / 1_000_000,
        "max": ordered[-1] / 1_000_000,
        "stddev": statistics.pstdev(ordered) / 1_000_000,
        "samples": len(ordered),
    }


def proxy_label(proxy_url: str) -> str:
    if not proxy_url:
        return "disabled (direct)"
    parsed = urlparse(proxy_url)
    return f"enabled ({parsed.scheme or 'unknown'}://{parsed.hostname or 'unknown'}:{parsed.port or 'default'})"


def dc_info(client) -> tuple[str, str, str]:
    session = getattr(client, "session", None)
    dc_id = getattr(session, "dc_id", None)
    address = getattr(session, "server_address", None)
    port = getattr(session, "port", None)
    return (
        str(dc_id) if dc_id is not None else "unavailable",
        f"{address}:{port}" if address and port else "unavailable",
        "unavailable (Telethon has no stable public transport property)",
    )


def flood_seconds(exc: BaseException) -> float | None:
    if type(exc).__name__ not in {"FloodWait", "FloodWaitError"}:
        return None
    try:
        return max(0.0, float(getattr(exc, "seconds", getattr(exc, "value", 0))))
    except (TypeError, ValueError):
        return 0.0


async def collect(operation, samples: list[int], *, count: int, interval: float,
                  floods: dict[str, int], name: str) -> None:
    while len(samples) < count:
        started = time.perf_counter_ns()
        try:
            await operation(len(samples))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            seconds = flood_seconds(exc)
            if seconds is None:
                raise
            floods[name] = floods.get(name, 0) + 1
            await asyncio.sleep(seconds + interval)
            continue
        samples.append(time.perf_counter_ns() - started)
        if interval:
            await asyncio.sleep(interval)


def show(args, account: str, proxy_url: str, client, results: dict[str, list[int]],
         floods: dict[str, int]) -> None:
    dc, endpoint, connection = dc_info(client)
    print("Environment")
    print(f"Account: {account.partition(':')[0]} {mask(account.partition(':')[2])}")
    print(f"Chat: {mask(args.chat)}")
    print(f"Proxy: {proxy_label(proxy_url)}")
    print(f"Telegram DC: {dc}")
    print(f"Remote endpoint: {endpoint}")
    print(f"Connection mode: {connection}")
    print(f"Iterations: {args.iterations}")
    print(f"Warmup: {args.warmup}")
    print(f"Interval: {args.interval:.3f}s")
    print("Operation | Mean(ms) | Median(ms) | P90(ms) | P95(ms) | P99(ms) | Min(ms) | Max(ms) | StdDev(ms) | Samples | FloodWait")
    for operation, values in results.items():
        if not values:
            print(f"{operation} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | 0 | {floods.get(operation, 0)}")
            continue
        item = summarize(values)
        print(
            f"{operation} | {item['mean']:.3f} | {item['median']:.3f} | {item['p90']:.3f} | "
            f"{item['p95']:.3f} | {item['p99']:.3f} | {item['min']:.3f} | "
            f"{item['max']:.3f} | {item['stddev']:.3f} | {item['samples']} | "
            f"{floods.get(operation, 0)}"
        )


def choose(accounts: TelegramAccounts, requested: str):
    if requested:
        kind, _, name = requested.partition(":")
        values = accounts.bots if kind == "bot" else accounts.users if kind == "user" else {}
        if name not in values:
            raise RuntimeError("指定账号不存在或未连接")
        return requested, values[name]
    if accounts.users:
        name, client = next(iter(accounts.users.items()))
        return f"user:{name}", client
    if accounts.bots:
        name, client = next(iter(accounts.bots.items()))
        return f"bot:{name}", client
    raise RuntimeError("没有已连接的 Telegram 账号")


def limit_accounts(settings, requested: str) -> str:
    """Connect only the selected benchmark account without saving modified settings."""
    if not requested:
        if settings.user_sessions:
            requested = f"user:{settings.user_sessions[0]}"
        else:
            specs = [item for item in settings.bot_specs() if item.token]
            if not specs:
                raise RuntimeError("没有已配置的 Telegram 账号")
            requested = f"bot:{specs[0].id}"
    kind, _, name = requested.partition(":")
    if kind == "user":
        if name not in settings.user_sessions:
            raise RuntimeError("指定用户 Session 不存在")
        settings.user_sessions = [name]
        settings.bot_token = ""
        settings.bots = []
    elif kind == "bot":
        settings.user_sessions = []
        if name == "default":
            settings.bots = []
        else:
            selected = next((item for item in settings.bots if item.id == name), None)
            if selected is None:
                raise RuntimeError("指定 Bot 不存在")
            settings.bot_token = ""
            settings.bots = [selected]
    else:
        raise ValueError("--account 必须为 bot:<id> 或 user:<session>")
    return requested


async def probe_callback(client, chat, args, floods) -> dict[str, list[int]]:
    names = ("callback answer RPC", "callback answer + edit RPC")
    results = {name: [] for name in names}
    done = asyncio.Event()
    data = b"awbotnest-latency-probe"
    button = [[Button.inline("运行延迟测试", data=data)]]
    message = await client.send_message(chat, "AWBotNest 延迟探针：请点击按钮", buttons=button)
    seen = 0

    async def handle(event):
        nonlocal seen
        try:
            started = time.perf_counter_ns()
            await event.answer("ok", cache_time=0)
            answer_elapsed = time.perf_counter_ns() - started
            started = time.perf_counter_ns()
            await event.edit(f"AWBotNest 延迟探针：已接收 {seen + 1}", buttons=button)
            edit_elapsed = time.perf_counter_ns() - started
        except Exception as exc:
            seconds = flood_seconds(exc)
            if seconds is None:
                raise
            floods[names[0]] = floods.get(names[0], 0) + 1
            return
        seen += 1
        if seen > args.warmup:
            results[names[0]].append(answer_elapsed)
            results[names[1]].append(answer_elapsed + edit_elapsed)
        if len(results[names[0]]) >= args.iterations:
            done.set()
        elif args.interval:
            await asyncio.sleep(args.interval)

    builder = events.CallbackQuery(chats=chat, data=data)
    client.add_event_handler(handle, builder)
    try:
        print(f"请在 Telegram 中点击测试按钮 {args.warmup + args.iterations} 次。")
        await done.wait()
    finally:
        client.remove_event_handler(handle, builder)
        await message.edit("AWBotNest 延迟探针完成", buttons=None)
    return results


async def probe_edit(client, chat, args, floods) -> dict[str, list[int]]:
    names = ("direct message.edit RPC", "delivery edit coalesce=False RPC")
    results = {name: [] for name in names}
    message = await client.send_message(chat, "AWBotNest edit latency probe")
    delivery = TelegramDelivery("latency_probe", "latency_probe", retries=0, coalesce_window=0)
    try:
        for warmup in (True, False):
            count = args.warmup if warmup else args.iterations
            direct = [] if warmup else results[names[0]]
            governed = [] if warmup else results[names[1]]
            await collect(lambda i: message.edit(f"AWBotNest direct edit {warmup}:{i}"), direct,
                          count=count, interval=args.interval, floods=floods, name=names[0])
            await collect(lambda i: delivery.edit(message, f"AWBotNest delivery edit {warmup}:{i}", coalesce=False),
                          governed, count=count, interval=args.interval, floods=floods, name=names[1])
    finally:
        await delivery.close()
    return results


async def probe_send(client, chat, args, floods) -> dict[str, list[int]]:
    names = ("direct send_message RPC", "delivery send RPC")
    results = {name: [] for name in names}
    delivery = TelegramDelivery("latency_probe", "latency_probe", retries=0)
    try:
        for warmup in (True, False):
            count = args.warmup if warmup else args.iterations
            direct = [] if warmup else results[names[0]]
            governed = [] if warmup else results[names[1]]
            await collect(lambda i: client.send_message(chat, f"AWBotNest direct send {warmup}:{i}"), direct,
                          count=count, interval=args.interval, floods=floods, name=names[0])
            await collect(lambda i: delivery.send(client, chat, f"AWBotNest delivery send {warmup}:{i}"),
                          governed, count=count, interval=args.interval, floods=floods, name=names[1])
    finally:
        await delivery.close()
    return results


async def probe_reply(client, chat, args, floods) -> dict[str, list[int]]:
    name = "event.reply RPC"
    results = {name: []}
    done = asyncio.Event()
    seen = 0

    async def handle(event):
        nonlocal seen
        try:
            started = time.perf_counter_ns()
            await event.reply(f"AWBotNest reply probe {seen}")
        except Exception as exc:
            seconds = flood_seconds(exc)
            if seconds is None:
                raise
            floods[name] = floods.get(name, 0) + 1
            return
        elapsed = time.perf_counter_ns() - started
        seen += 1
        if seen > args.warmup:
            results[name].append(elapsed)
        if len(results[name]) >= args.iterations:
            done.set()

    builder = events.NewMessage(chats=chat, incoming=True)
    client.add_event_handler(handle, builder)
    try:
        print(f"请在测试 chat 中发送 {args.warmup + args.iterations} 条消息；探针将逐条 reply。")
        await done.wait()
    finally:
        client.remove_event_handler(handle, builder)
    return results


async def main(args) -> None:
    settings = load_settings(persist_defaults=False)
    args.account = limit_accounts(settings, args.account)
    if args.proxy == "none":
        settings.proxy_url = ""
    elif args.proxy != "current":
        parsed = urlparse(args.proxy)
        if parsed.scheme not in {"http", "socks4", "socks5"} or not parsed.hostname or not parsed.port:
            raise ValueError("--proxy 必须为 current、none 或有效的 http/socks4/socks5 URL")
        settings.proxy_url = args.proxy
    accounts = TelegramAccounts(settings)
    await accounts.start()
    try:
        account, client = choose(accounts, args.account)
        original_threshold = client.flood_sleep_threshold
        client.flood_sleep_threshold = 0
        floods: dict[str, int] = {}
        try:
            probes = {"callback": probe_callback, "edit": probe_edit,
                      "send": probe_send, "reply": probe_reply}
            results = await probes[args.mode](client, args.chat, args, floods)
            show(args, account, settings.proxy_url, client, results, floods)
        finally:
            client.flood_sleep_threshold = original_threshold
    finally:
        await accounts.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AWBotNest real Telegram latency probe")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--chat", required=True, help="Telegram username or numeric chat id")
    parser.add_argument("--mode", choices=("callback", "edit", "send", "reply"), required=True)
    parser.add_argument("--account", default="", help="bot:<id> or user:<session>; defaults to a connected user/bot")
    parser.add_argument("--proxy", default="current", help="current, none, or temporary proxy URL (never saved)")
    options = parser.parse_args()
    if options.iterations < 1 or options.iterations > 1000:
        parser.error("--iterations must be between 1 and 1000")
    if options.warmup < 0 or options.warmup > 20:
        parser.error("--warmup must be between 0 and 20")
    if options.interval < 0:
        parser.error("--interval cannot be negative")
    try:
        options.chat = int(options.chat)
    except ValueError:
        pass
    asyncio.run(main(options))
