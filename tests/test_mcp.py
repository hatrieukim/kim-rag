"""MCP server: agent kết nối được qua stdio và thấy đúng các tool."""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_agent_ket_noi_duoc_va_thay_cac_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # không cần .env; liệt kê tool không đụng tới database

    async def talk():
        params = StdioServerParameters(command=sys.executable, args=["-m", "lifelog.cli", "mcp"])
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, tools

    init, tools = asyncio.run(talk())
    assert init.server_info.name == "lifelog" and "search" in init.instructions
    assert [t.name for t in tools.tools] == ["search", "summary", "people", "timeline"]
    assert all(t.description and all(p.get("description") for p in t.input_schema["properties"].values())
               for t in tools.tools)
    schemas = {t.name: t.input_schema for t in tools.tools}
    assert set(schemas["summary"]["properties"]) == {"date", "period", "detail"} and not schemas["summary"].get("required")
    assert schemas["people"]["required"] == ["name"]
    assert set(schemas["timeline"]["properties"]) == {"topic", "since", "until", "limit"}

    tool = tools.tools[0]
    props = tool.input_schema["properties"]
    assert set(props) == {"query", "since", "until", "speakers", "limit"}
    assert tool.input_schema["required"] == ["query"]
    assert "Nguyên văn" in tool.description and "Fact" in tool.description
    assert all(p.get("description") for p in props.values())
