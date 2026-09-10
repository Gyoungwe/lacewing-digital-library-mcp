import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ldl_mcp"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {"health_check", "resolve_taxon", "type_info", "distribution", "batch_resolve", "bibliography"}
            missing = required - names
            assert not missing, f"missing tools: {sorted(missing)}"
            result = await session.call_tool("resolve_taxon", {"scientific_name": "Mantispa styriaca", "include_history": False})
            assert not result.isError, result
            print("MCP_SMOKE_OK", len(names), sorted(required))


if __name__ == "__main__":
    asyncio.run(run())
