"""mcp_test_support.py — the ONE place tests talk MCP to an engine server.

Both MCP server test suites (tools/test_knowledge_query.py and tools/memory/test_mcp_server.py) exercise
their server through the SDK's in-memory client via these helpers, so the suite proves what a real caller
sees — registration, schema generation, serialization — not what the server object's internals return. The
result-unwrapping lives here and nowhere else: the SDK has renamed its server class once and reshaped its
call-tool result once already, so the next churn touches this file alone, not every call site.

TWO FACTS THIS FILE ENCODES, both established against the real mcp 2.0.0 package:

  * A raising tool does NOT raise here. Over the protocol the SDK catches the exception and returns an
    error RESULT (`is_error=True` with the message as text content) — unlike a direct call on the server
    object, which propagates. `call_tool_json` therefore asserts not-error before parsing, so a tool that
    starts failing can never read as a green test; `call_tool_expect_error` is the deliberate path for
    asserting a refusal.
  * The JSON payload rides in the text content. A `-> dict` annotation does not populate
    `structured_content` in 2.0.0 (measured, not assumed), so parsing `content[0].text` is the honest
    unwrap, not a legacy habit.
"""
from __future__ import annotations
import json

from mcp import Client


async def call_tool_json(server, name: str, args: dict) -> dict:
    """Call tool `name` on `server` through an in-memory client and return its JSON payload.

    Raises AssertionError when the tool errored — the protocol reports that as an error result, not an
    exception, and an unchecked result would let a broken tool pass green."""
    async with Client(server) as client:
        res = await client.call_tool(name, args)
        if res.is_error:
            text = res.content[0].text if res.content else "<no content>"
            raise AssertionError(f"tool {name!r} errored: {text}")
        return json.loads(res.content[0].text)


async def call_tool_expect_error(server, name: str, args: dict) -> str:
    """Call tool `name` expecting a tool-level refusal; return the error text.

    The counterpart to `call_tool_json`: a test asserting that bad arguments are refused uses this, because
    over the protocol the refusal arrives as `is_error=True`, never as a raised exception."""
    async with Client(server) as client:
        res = await client.call_tool(name, args)
        if not res.is_error:
            raise AssertionError(f"tool {name!r} unexpectedly succeeded with {args!r}")
        return res.content[0].text if res.content else ""


async def list_tool_objects(server) -> list:
    """The server's tool list as seen by a real client — objects with `.name` and `.description`."""
    async with Client(server) as client:
        return list((await client.list_tools()).tools)


async def stdio_health(engine_dir: str, script_rel: str, timeout_s: float = 120.0) -> dict:
    """Launch a server exactly as .mcp.json does — a real `uv run --frozen` subprocess over stdio — complete
    the protocol handshake, call `health`, and return its payload.

    This is the launch-seam smoke test the in-memory client cannot provide: it is the only coverage of
    `server.run()`, the frozen-environment resolution, and the stdio handshake — the seam where a dead
    server fails SILENTLY in a deployment (it simply never appears in the model's tool list). The timeout
    bounds the first-launch case where uv must still materialize the environment."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", engine_dir, "--frozen", "--", "python", script_rel],
    )

    async def _go() -> dict:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("health", {})
                return json.loads(res.content[0].text)

    return await asyncio.wait_for(_go(), timeout=timeout_s)
