import asyncio
import os

import uvicorn

from app.config import MCP_HOST, MCP_PORT
from app.gateway.gateway import app as gateway_app
from app.mcp_server.server import mcp


async def _propagate_shutdown(servers: list[uvicorn.Server], interval: float = 0.1) -> None:
    """Whichever server's real signal handler fires, mirror should_exit onto
    the others — each server still owns its own actual shutdown sequence."""
    while not any(s.should_exit for s in servers):
        await asyncio.sleep(interval)
    for s in servers:
        s.should_exit = True


async def main() -> None:
    gateway_port = int(os.environ["PORT"])
    mcp_app = mcp.http_app()

    # Server.serve() is a plain coroutine -- unlike uvicorn.run()/mcp.run(),
    # which each call asyncio.run() internally and BLOCK the whole process
    # until that one server stops. .serve() lets both servers run as
    # ordinary coroutines in ONE shared event loop (asyncio.gather below)
    
    gateway_server = uvicorn.Server(uvicorn.Config(gateway_app, host="0.0.0.0", port=gateway_port))
    mcp_server = uvicorn.Server(uvicorn.Config(mcp_app, host=MCP_HOST, port=MCP_PORT))
    servers = [gateway_server, mcp_server]

    await asyncio.gather(
        gateway_server.serve(),
        mcp_server.serve(),
        _propagate_shutdown(servers),
    )


if __name__ == "__main__":
    asyncio.run(main())
