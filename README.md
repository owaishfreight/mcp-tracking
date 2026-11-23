# MCP Calculator — learning project

A small MCP (Model Context Protocol) server that gives Claude calculator tools.
It covers all three MCP building blocks:

| Type | What it is | In this project |
|---|---|---|
| **Tool** | A function Claude can call | `add`, `subtract`, `multiply`, `divide`, `power`, `square_root`, `percentage`, `evaluate` |
| **Resource** | Read-only data with a URI | `calculator://history` |
| **Prompt** | A reusable prompt template | `explain_calculation` |

```
mcp-calculator/
├── calculator_server.py   # the MCP server
├── test_client.py         # a tiny client to test it without Claude
├── requirements.txt
├── Dockerfile             # for hosting anywhere with Docker
├── render.yaml            # one-click deploy on Render
└── README.md
```

## 1. Set up

Needs Python 3.10+.

```bash
cd mcp-calculator
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Test it without Claude

```bash
python test_client.py
```

You should see the tool list, results like `add(5, 7) -> 12.0`, two error
cases (divide by zero, a blocked `__import__`), and the history resource.

For a visual debugger, run the MCP Inspector (needs Node.js):

```bash
mcp dev calculator_server.py
```

## 3. Add it to Claude

You need the **full path** to the Python inside your venv and to the server file.

### Claude Desktop

Open *Settings → Developer → Edit Config*, which opens `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add this (Windows example — note the double backslashes):

```json
{
  "mcpServers": {
    "calculator": {
      "command": "C:\\Users\\you\\mcp-calculator\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\mcp-calculator\\calculator_server.py"]
    }
  }
}
```

macOS/Linux example:

```json
{
  "mcpServers": {
    "calculator": {
      "command": "/Users/you/mcp-calculator/.venv/bin/python",
      "args": ["/Users/you/mcp-calculator/calculator_server.py"]
    }
  }
}
```

Fully quit Claude Desktop (from the system tray / menu bar, not just the window)
and reopen it. The calculator tools appear under the tools/connectors menu in the chat box.

Shortcut: `mcp install calculator_server.py` writes this config for you.

### Claude Code (terminal)

```bash
claude mcp add calculator -- /full/path/to/.venv/bin/python /full/path/to/calculator_server.py
claude mcp list        # check it's connected
```

Inside Claude Code, type `/mcp` to see its status.

## 4. Host it at a URL (remote MCP server)

The same file runs as an HTTP server when `MCP_TRANSPORT=http` is set.
The MCP endpoint is always at **`/mcp`**.

### Step 1 — Run it locally over HTTP

```bash
# macOS/Linux
MCP_TRANSPORT=http MCP_API_KEY=my-secret python calculator_server.py

# Windows PowerShell
$env:MCP_TRANSPORT="http"; $env:MCP_API_KEY="my-secret"; python calculator_server.py
```

Test it from another terminal:

```bash
MCP_API_KEY=my-secret python test_client.py http://localhost:8000/mcp
```

| Env variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Set to `http` to serve over a URL |
| `PORT` | `8000` | Port (hosting platforms set this for you) |
| `HOST` | `0.0.0.0` | Listen address |
| `MCP_API_KEY` | *(none)* | If set, requests must send `Authorization: Bearer <key>` |

> **Always set `MCP_API_KEY` on a public server.** Without it, anyone who finds
> the URL can call your tools.

### Step 2 — Deploy it (free option: Render)

1. Push this folder to a GitHub repo.
2. On render.com: **New → Blueprint** and pick the repo. `render.yaml` sets
   everything up, including a random `MCP_API_KEY`.
3. After deploy, your URL is `https://<your-app>.onrender.com/mcp`.
   Copy `MCP_API_KEY` from the service's **Environment** tab.
4. Check it: `MCP_API_KEY=<key> python test_client.py https://<your-app>.onrender.com/mcp`

Free Render services go to sleep when idle, so the first request after a
break can take a while to respond.

**Other options:** the `Dockerfile` works on Railway, Fly.io, Google Cloud Run,
or any VPS (`docker build -t mcp-calc . && docker run -p 8000:8000 -e MCP_API_KEY=... mcp-calc`).
For a quick temporary public URL without deploying, run locally and use
`ngrok http 8000`, then use the `https://….ngrok-free.app/mcp` URL.

### Step 3 — Connect Claude to the URL

Claude only connects to **https** URLs for remote servers (Render and ngrok give you https).

**Claude (web / desktop / mobile) — custom connector**

1. Go to **Customize → Connectors** (Team/Enterprise admins: *Organization settings → Connectors*).
2. Click **Add** → hover **Custom** → **Web**.
3. Name: `Calculator`. URL: `https://<your-app>.onrender.com/mcp`. Continue.
4. In the auth step, under **Request headers**, add
   `Authorization` = `Bearer <your MCP_API_KEY>`.
5. Enable the connector in a chat and ask it to calculate something.

**Claude Code**

```bash
claude mcp add --transport http calculator https://<your-app>.onrender.com/mcp \
  --header "Authorization: Bearer <your MCP_API_KEY>"
```

### How the HTTP mode is set up (in the code)

- `stateless_http=True`: each request stands alone, so restarts or multiple
  instances don't break clients. (Side effect: the history resource is
  per-process and resets on restart.)
- `json_response=True`: replies are plain JSON, so it's easy to inspect with `curl`.
- `ApiKeyMiddleware`: a ~15-line check of the `Authorization` header, using
  `hmac.compare_digest` to avoid timing attacks. For real multi-user products
  you'd use OAuth instead; the SDK supports it through `auth=` / `token_verifier=`.

Raw request with curl (handy for security testing):

```bash
curl -X POST https://<your-app>.onrender.com/mcp \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"multiply","arguments":{"a":6,"b":7}}}'
```

## 5. Try it

Ask Claude:

- "What is 18% GST on ₹2,499? Use the calculator."
- "Evaluate (125 * 4 + 300) / 8"
- "Divide 10 by 0" — see how a tool error comes back
- Pick the `explain_calculation` prompt from the prompt menu (the + / attach menu in Claude Desktop)

## Key things to learn from the code

1. **The docstring and type hints are the tool's interface.** Claude reads
   them to decide when and how to call a tool, so write them clearly.
2. **Never print to stdout.** With the stdio transport, stdout carries the
   protocol. Log to stderr (see `logging.basicConfig(stream=sys.stderr)`).
3. **Raise `ToolError` for expected failures.** Claude gets your message.
   Any other exception counts as a crash and Claude sees only
   "Error executing tool <name>".
4. **Never `eval()` input from an LLM.** `evaluate` walks the syntax tree and
   only allows numbers and arithmetic operators. Good thing to test as a QA
   engineer: try to break it.

## SDK version note

This uses the MCP Python SDK **v2**, where the server class is `MCPServer`:

```python
from mcp.server.mcpserver import MCPServer
```

Most tutorials online still show v1, where it was called `FastMCP`
(`from mcp.server.fastmcp import FastMCP`). The decorators (`@mcp.tool()`,
`@mcp.resource()`, `@mcp.prompt()`) work the same way. If you follow a v1
tutorial, either rename the import or install `"mcp[cli]<2"`.

## Ideas to extend it

- Add a `unit_convert` tool (km↔miles, °C↔°F)
- Add a `currency_convert` tool that calls a real exchange-rate API
- Save history to a SQLite file so it survives restarts
- Replace the API key with OAuth so each user signs in
