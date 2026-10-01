"""Local deterministic stdio fixture: no network, npm or cloud key."""

import asyncio

from mcp.server import MCPServer

server = MCPServer("nexus-test")


@server.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers."""
    return {"sum": a + b}


@server.tool()
async def wait(seconds: float) -> str:
    """Wait to exercise client interruption and cleanup."""
    await asyncio.sleep(seconds)
    return "done"


if __name__ == "__main__":
    server.run()
