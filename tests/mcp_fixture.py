"""A real MCP server shared by the HTTP and stdio integration tests."""
import json
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

ERROR = {"issues": [{"code": "invalid_value", "path": ["sort"],
                     "message": "expected created_at", "values": ["created_at"]}]}
server = FastMCP("repair-fixture", stateless_http=True, json_response=True)


@server.tool()
def list_issues(sort: str, token: str) -> CallToolResult:
    if sort != "created_at":
        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(ERROR))])
    if token != "local-secret":
        return CallToolResult(isError=True, content=[TextContent(type="text", text="missing credential")])
    return CallToolResult(content=[TextContent(type="text", text="repaired with original credential")])


if __name__ == "__main__":
    server.run(transport="stdio")
