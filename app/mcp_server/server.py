from fastmcp import FastMCP

mcp = FastMCP("analytics")


# Phase 2 placeholder — proves the MCP round-trip works end to end. Remove
# once Phase 3 registers real tools (.claude/rules/mcp-tools.md).
@mcp.tool()
def ping(message: str) -> str:
    """Echoes the given message back, prefixed with 'pong: '."""
    return f"pong: {message}"
