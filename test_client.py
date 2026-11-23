"""
Tiny MCP client that launches calculator_server.py over stdio (exactly how
Claude does it) and calls its tools.

  python test_client.py                              # local, over stdio
  python test_client.py http://localhost:8000/mcp    # running HTTP server / hosted URL
  MCP_API_KEY=secret python test_client.py https://your-app.onrender.com/mcp
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

SERVER = str(Path(__file__).with_name("calculator_server.py"))


async def main() -> None:
    if len(sys.argv) > 1:
        # A URL -> connect over Streamable HTTP (with the API key if one is set)
        key = os.getenv("MCP_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else None
        target = streamable_http_client(
            sys.argv[1], http_client=httpx2.AsyncClient(headers=headers, timeout=60)
        )
    else:
        target = StdioServerParameters(command=sys.executable, args=[SERVER])
    async with Client(target) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools.tools])

        for name, args in [
            ("add", {"a": 5, "b": 7}),
            ("divide", {"a": 22, "b": 7}),
            ("percentage", {"value": 2500, "percent": 18}),
            ("evaluate", {"expression": "(12 + 8) * 3 / 4"}),
            ("divide", {"a": 1, "b": 0}),  # error case
            ("evaluate", {"expression": "__import__('os')"}),  # blocked
        ]:
            r = await client.call_tool(name, args)
            text = r.content[0].text if r.content else r
            print(f"{name}({args}) -> {'ERROR: ' if r.is_error else ''}{text}")

        prompts = await client.list_prompts()
        print("Prompts:", [p.name for p in prompts.prompts])

        res = await client.read_resource("calculator://history")
        print("History resource:\n" + res.contents[0].text)


if __name__ == "__main__":
    asyncio.run(main())
