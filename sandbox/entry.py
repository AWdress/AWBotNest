"""Root-owned runner entry; isolated Python ignores user-controlled cwd/env."""
import asyncio
import sys

sys.path.insert(0, "/opt/runner")
from sandbox.runner import serve

asyncio.run(serve())
