from fastmcp import FastMCP

from app.config import MCP_SERVER_NAME

mcp = FastMCP(MCP_SERVER_NAME)

# Importing each module runs its @mcp.tool() decorators, which is what
# registers the tools. Import for side effect only -- nothing is called here.
from app.mcp_server import dax_tool  # noqa: F401,E402 -- run_dax_query
