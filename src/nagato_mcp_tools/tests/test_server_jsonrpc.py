"""End-to-end JSON-RPC lifecycle tests for the MCP SDK v2 standalone server.

These tests spawn the standalone ``nagato_mcp_tools.server`` module as a real
stdio subprocess and drive it through the full MCP initialize / list_tools /
call_tool handshake using the official ``mcp`` Python client. They guard
against silent regressions in the SDK v2 migration (Plan: standalone MCP SDK
v2 refactor, Phase 3, step 9).

Skipped automatically if no ``mcp>=2.0.0`` SDK is importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[3]
NAGATO_MCP_PKG_PARENT = Path(__file__).resolve().parents[3]


def _server_module_path() -> Path:
    import nagato_mcp_tools.server as srv_mod

    return Path(srv_mod.__file__).resolve()


def _mcp_version() -> str:
    import importlib.metadata as md

    return md.version("mcp")


pytestmark = pytest.mark.skipif(
    not _mcp_version().startswith("2."),
    reason=f"MCP SDK v2 required for this test (found {_mcp_version()})",
)


def _spawn_params() -> StdioServerParameters:
    script = _server_module_path()
    # Launch via ``python -m nagato_mcp_tools`` (the package's ``__main__``)
    # rather than ``python -m nagato_mcp_tools.server``. The package-level
    # entry point avoids CPython's ``runpy`` ``RuntimeWarning`` that fires
    # when the same module is already in ``sys.modules`` (a common situation
    # under test runners that import the package before launching a subprocess
    # copy of the same module).
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "nagato_mcp_tools"],
        cwd=str(script.parent.parent.parent.parent),
    )


async def _open_session() -> tuple[ClientSession, object, object]:
    ctx = stdio_client(_spawn_params())
    read, write = await ctx.__aenter__()
    session_ctx = ClientSession(read, write)
    session = await session_ctx.__aenter__()
    await session.initialize()
    return session, session_ctx, ctx


@pytest.mark.asyncio
async def test_initialize_handshake_reports_server_name() -> None:
    session, session_ctx, ctx = await _open_session()
    try:
        info = session.initialize_result
        assert info is not None
        assert info.server_info.name == "nagato-mcp-tools"
    finally:
        await session_ctx.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_list_tools_returns_at_least_one_nagato_tool() -> None:
    session, session_ctx, ctx = await _open_session()
    try:
        result = await session.list_tools()
        names = {t.name for t in result.tools}
        assert "nagato_generate_uuid4" in names, names
        # Every nagato_* tool must carry a description (used by MCP hosts).
        for t in result.tools:
            assert t.name.startswith("nagato_")
            assert t.description, f"Tool {t.name} missing description"
    finally:
        await session_ctx.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_call_uuid4_tool_round_trip() -> None:
    session, session_ctx, ctx = await _open_session()
    try:
        result = await session.call_tool("nagato_generate_uuid4", {"count": 2})
        assert not result.is_error, result.content[0].text
        text = " ".join(c.text for c in result.content if hasattr(c, "text"))
        # Two UUID4 strings, comma- or whitespace-separated.
        uuids = [t.strip() for t in text.replace(",", " ").split() if t.strip()]
        assert len(uuids) == 2
        for u in uuids:
            assert len(u) == 36 and u.count("-") == 4, f"bad uuid: {u!r}"
    finally:
        await session_ctx.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_unknown_tool_returns_is_error_true() -> None:
    session, session_ctx, ctx = await _open_session()
    try:
        result = await session.call_tool("nagato_does_not_exist", {})
        assert result.is_error, "Unknown tool must be reported as is_error"
        assert any("Unknown tool" in getattr(c, "text", "") for c in result.content)
    finally:
        await session_ctx.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_invalid_arguments_return_is_error_true() -> None:
    session, session_ctx, ctx = await _open_session()
    try:
        # `count` must be an integer per the tool's schema.
        result = await session.call_tool("nagato_generate_uuid4", {"count": "not_an_int"})
        assert result.is_error, "Pydantic validation error must be is_error"
        assert any(
            "validation" in getattr(c, "text", "").lower()
            or "Error executing" in getattr(c, "text", "")
            for c in result.content
        )
    finally:
        await session_ctx.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)
