"""
Calculator MCP Server — a learning project.

Shows the three MCP building blocks:
  1. Tools     -> functions Claude can CALL   (add, divide, evaluate, ...)
  2. Resources -> data Claude can READ        (calculation history)
  3. Prompts   -> reusable prompt templates   (explain a calculation)

Written for the MCP Python SDK v2 (`pip install "mcp[cli]"`).
In SDK v1 the class was called FastMCP:  from mcp.server.fastmcp import FastMCP
"""

import ast
import hmac
import logging
import math
import operator
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# IMPORTANT: with the stdio transport, stdout carries the MCP protocol itself.
# Never print() to stdout — log to stderr instead, or Claude will see garbage.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("calculator")

mcp = MCPServer(
    name="calculator",
    instructions="A calculator. Use these tools for any arithmetic instead of computing in your head.",
)

# Simple in-memory history (resets whenever the server restarts).
history: list[str] = []


def _record(entry: str) -> None:
    history.append(entry)
    log.info(entry)


# ---------------------------------------------------------------------------
# 1. TOOLS — the type hints + docstring become the tool's schema/description
#    that Claude sees, so write them carefully.
# ---------------------------------------------------------------------------

@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    result = a + b
    _record(f"{a} + {b} = {result}")
    return result


@mcp.tool()
def subtract(a: float, b: float) -> float:
    """Subtract b from a."""
    result = a - b
    _record(f"{a} - {b} = {result}")
    return result


@mcp.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    result = a * b
    _record(f"{a} * {b} = {result}")
    return result


@mcp.tool()
def divide(a: float, b: float) -> float:
    """Divide a by b. Fails if b is zero."""
    if b == 0:
        # Raise ToolError for errors you expect: Claude gets your message.
        # Any other exception is treated as a crash and the message is hidden.
        raise ToolError("Cannot divide by zero.")
    result = a / b
    _record(f"{a} / {b} = {result}")
    return result


@mcp.tool()
def power(base: float, exponent: float) -> float:
    """Raise base to the power of exponent."""
    result = base ** exponent
    _record(f"{base} ^ {exponent} = {result}")
    return result


@mcp.tool()
def square_root(x: float) -> float:
    """Square root of a non-negative number."""
    if x < 0:
        raise ToolError("Cannot take the square root of a negative number.")
    result = math.sqrt(x)
    _record(f"sqrt({x}) = {result}")
    return result


@mcp.tool()
def percentage(value: float, percent: float) -> float:
    """Return `percent`% of `value` (e.g. percentage(200, 15) -> 30)."""
    result = value * percent / 100
    _record(f"{percent}% of {value} = {result}")
    return result


# Safe expression evaluator: walks the syntax tree and allows ONLY numbers and
# arithmetic operators. Never use eval() on input that comes from an LLM.
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolError("Exponent too large.")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ToolError(f"Unsupported element in expression: {ast.dump(node)[:60]}")


@mcp.tool()
def evaluate(expression: str) -> float:
    """Evaluate an arithmetic expression, e.g. '(12 + 8) * 3 / 4'.
    Supports + - * / // % ** and parentheses."""
    if len(expression) > 200:
        raise ToolError("Expression too long (max 200 characters).")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ToolError(f"Invalid expression: {expression!r}")
    try:
        result = _safe_eval(tree)
    except ZeroDivisionError:
        raise ToolError("Cannot divide by zero.")
    _record(f"{expression} = {result}")
    return result


# ---------------------------------------------------------------------------
# 2. RESOURCES — read-only data, addressed by a URI
# ---------------------------------------------------------------------------

@mcp.resource("calculator://history", mime_type="text/plain")
def get_history() -> str:
    """All calculations done in this session."""
    return "\n".join(history) if history else "No calculations yet."


# ---------------------------------------------------------------------------
# 3. PROMPTS — templates the user can pick from Claude's prompt menu
# ---------------------------------------------------------------------------

@mcp.prompt()
def explain_calculation(expression: str) -> str:
    """Ask Claude to solve an expression step by step using the tools."""
    return (
        f"Solve {expression} step by step. Use the calculator tools for every "
        "arithmetic step and explain each step in simple words."
    )


# ---------------------------------------------------------------------------
# HTTP hosting + optional API key
# ---------------------------------------------------------------------------

class ApiKeyMiddleware:
    """If MCP_API_KEY is set, every HTTP request must send
    `Authorization: Bearer <MCP_API_KEY>`, otherwise it gets 401."""

    def __init__(self, app, api_key: str):
        self.app = app
        self.expected = f"Bearer {api_key}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error": "unauthorized"}'})
                return
        await self.app(scope, receive, send)


def run_http() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")       # 0.0.0.0 = reachable from outside
    port = int(os.getenv("PORT", "8000"))     # hosts like Render/Railway set PORT
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,   # no per-client session state: survives restarts/scaling
        json_response=True,    # plain JSON replies instead of an SSE stream
        host=host,
    )
    api_key = os.getenv("MCP_API_KEY")
    if api_key:
        app = ApiKeyMiddleware(app, api_key)
        log.info("API key required (Authorization: Bearer ...)")
    else:
        log.warning("No MCP_API_KEY set: anyone with the URL can use this server")
    log.info("Serving MCP over HTTP at http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    # Two ways to run:
    #   python calculator_server.py                       -> stdio (local Claude Desktop/Code)
    #   MCP_TRANSPORT=http python calculator_server.py    -> HTTP at http://HOST:PORT/mcp
    if os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http"):
        run_http()
    else:
        mcp.run()  # stdio transport
