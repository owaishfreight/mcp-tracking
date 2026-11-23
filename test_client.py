"""
Tiny MCP client that calls the tracking server's tools.

  python test_client.py                              # local, over stdio
  python test_client.py http://localhost:8000/mcp    # running HTTP server
  MCP_API_KEY=secret python test_client.py https://your-app.onrender.com/mcp
"""


import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

SERVER = str(Path(__file__).with_name("tracking_server.py"))
CONTAINER = "MSKU1712406"


def show(title: str, result) -> None:
    print(f"\n=== {title} {'(ERROR)' if result.is_error else ''}")
    # Prefer structured output (whole list/dict); fall back to text content.
    if result.structured_content is not None and not result.is_error:
        data = result.structured_content
        print(json.dumps(data.get("result", data), indent=2))
    else:
        print("\n".join(c.text for c in result.content if hasattr(c, "text")))


async def main() -> None:
    if len(sys.argv) > 1:
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

        show("list_containers", await client.call_tool("list_containers", {}))
        show("get_container_status", await client.call_tool("get_container_status", {"container_number": CONTAINER}))
        show("get_milestones", await client.call_tool("get_milestones", {"container_number": CONTAINER}))
        show("get_vessel_legs", await client.call_tool("get_vessel_legs", {"container_number": CONTAINER}))

        # Error cases
        show("bad format", await client.call_tool("get_container_status", {"container_number": "ABC123"}))
        show("bad check digit", await client.call_tool("get_container_status", {"container_number": "MSKU1712407"}))
        show("unknown container", await client.call_tool("get_container_status", {"container_number": "MSCU1234566"}))


if __name__ == "__main__":
    asyncio.run(main())
