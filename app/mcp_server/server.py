from fastmcp import FastMCP

from app.config import MCP_SERVER_NAME

mcp = FastMCP(MCP_SERVER_NAME)

# Importing each module runs its @mcp.tool() decorators, which is what
# registers the tools.
from app.mcp_server import dax_tool
from app.mcp_server import chart_tool
from app.mcp_server import code_search
from app.mcp_server import combine_tool
