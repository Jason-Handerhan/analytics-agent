"""Layer 1 test for app/main.py's shutdown-propagation logic (docs/testing.md).

Exercises _propagate_shutdown's mirroring logic with plain fakes standing in
for uvicorn.Server.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.main import _propagate_shutdown


@pytest.mark.asyncio
async def test_propagate_shutdown_mirrors_should_exit():
    servers = [SimpleNamespace(should_exit=False) for _ in range(3)]

    async def trigger_one():
        await asyncio.sleep(0.05)
        servers[1].should_exit = True

    await asyncio.wait_for(
        asyncio.gather(_propagate_shutdown(servers, interval=0.01), trigger_one()),
        timeout=1,
    )

    assert all(s.should_exit for s in servers)
