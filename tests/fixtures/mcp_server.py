"""Local deterministic stdio fixture: no network, npm or cloud key."""

from mcp.server import MCPServer

server = MCPServer("nexus-test")


@server.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers."""
    return {"sum": a + b}


if __name__ == "__main__":
    server.run()
