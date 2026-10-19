"""Fixed docker-exec bridge. No user arguments are interpreted as commands."""
import asyncio
import sys

SOCKET = "/tmp/awbotnest-sandbox.sock"
MAX_WIRE = 2 * 1024 * 1024


async def main():
    data = sys.stdin.buffer.read(MAX_WIRE + 1)
    if len(data) > MAX_WIRE or b"\n" in data:
        raise ValueError("invalid request framing")
    async with asyncio.timeout(29):
        reader, writer = await asyncio.open_unix_connection(SOCKET, limit=MAX_WIRE + 1)
        writer.write(data + b"\n")
        await writer.drain()
        response = await reader.readuntil(b"\n")
        if len(response) > MAX_WIRE:
            raise ValueError("response too large")
        sys.stdout.buffer.write(response)
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
