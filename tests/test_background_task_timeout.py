import asyncio

import pytest

from kernel.plugin_governance import PluginGovernor, ResourcePolicy


@pytest.mark.asyncio
async def test_execute_uses_policy_timeout_by_default():
    governor = PluginGovernor()
    governor.configure("short", {})
    governor._policies["short"] = ResourcePolicy(timeout_seconds=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await governor.execute("short", "request", lambda: asyncio.sleep(0.05))


@pytest.mark.asyncio
async def test_execute_can_disable_timeout_for_background_work():
    governor = PluginGovernor()
    governor.configure("worker", {})
    governor._policies["worker"] = ResourcePolicy(timeout_seconds=0.01)

    result = await governor.execute(
        "worker", "background", lambda: asyncio.sleep(0.05, result="finished"), timeout=0
    )

    assert result == "finished"
