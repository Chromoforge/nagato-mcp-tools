"""
Nagato MCP Tools Server

Exposes nagato_tools modules as Model Context Protocol (MCP) tools for any MCP client.
Provides code search, editing, execution, linting, git, and static analysis capabilities.

Targets the official ``mcp`` Python SDK 2.1+ (2026-07-28 MCP specification) and uses
the high-level ``MCPServer`` interface (formerly ``FastMCP``) for dynamic tool
registration and stdio transport.

Supports multi-project isolation via optional --session-id flag or NAGATO_SESSION_ID env var.
Formatted session IDs like "proj:my-app" are sanitized for filesystem use.
"""

import argparse
import asyncio
import inspect
import logging
import traceback
import warnings
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from nagato_tools.ctx_mock import _resolve_session_id, _sanitize_session_id
from nagato_tools.facade import ToolFacade, get_tool_facade

_LOGGER = logging.getLogger("nagato_mcp_tools.server")

# Defense-in-depth: when this module is launched via ``python -m
# nagato_mcp_tools.server`` from inside a process (or test runner) that has
# already imported it as a package submodule, CPython's ``runpy`` emits a
# ``RuntimeWarning: ... found in sys.modules after import of package ...``.
# The behavior is functionally harmless but spooks clients like the MCP
# Inspector, which interpret any stderr warning as a startup failure. The
# preferred fix is launching via the ``nagato-mcp-tools`` console script
# entry point (see ``pyproject.toml``) so the module is loaded as
# ``__main__``; we filter here as a safety net for ``-m`` invocations.
warnings.filterwarnings(
    "ignore",
    message=r".*found in sys\.modules after import of package.*",
    category=RuntimeWarning,
)


def _get_first_doc_paragraph(docstring: str | None, default: str) -> str:
    """Extract the first summary sentence or paragraph from a docstring."""
    if not docstring:
        return default
    lines = [line.strip() for line in docstring.strip().split("\n")]
    summary_lines: list[str] = []
    for line in lines:
        if not line or line.startswith(
            ("Args:", "Parameters:", "Returns:", "Raises:", "Yields:", "Note:", "Attributes:")
        ):
            break
        summary_lines.append(line)
    return " ".join(summary_lines).strip() if summary_lines else default


def _public_parameters(func: Any) -> list[inspect.Parameter]:
    """Return the facade function's parameters minus the implicit MCP ``Context``."""
    params = list(inspect.signature(func).parameters.values())
    public: list[inspect.Parameter] = []
    for param in params:
        if param.name in ("ctx", "_ctx"):
            continue
        annotation_name = getattr(param.annotation, "__name__", "")
        if annotation_name == "Context":
            continue
        public.append(param)
    return public


class _DispatchTool:
    """Adapter exposing a ``ToolFacade`` function as an MCPServer tool.

    The SDK v2 ``MCPServer`` derives the tool's JSON schema directly from the
    callable's signature, so this wrapper exposes only the parameters the LLM
    is allowed to provide and forwards calls to ``ToolFacade.call`` /
    ``ToolFacade.acall`` based on whether the registered function is sync or
    async.
    """

    def __init__(
        self,
        func_name: str,
        facade: ToolFacade,
        description: str,
    ) -> None:
        self._func_name = func_name
        self._facade = facade
        self._description = description
        # Surface identity that inspect.signature / MCPServer will pick up.
        self.__name__ = func_name
        self.__qualname__ = func_name
        self.__doc__ = description
        self.__wrapped__ = facade._registry[func_name]  # type: ignore[attr-defined]

    @property
    def __signature__(self) -> inspect.Signature:  # type: ignore[override]
        return inspect.Signature(
            parameters=_public_parameters(self.__wrapped__),
            return_annotation=inspect.Signature.empty,
        )

    async def __call__(self, **kwargs: Any) -> Any:
        if self._facade.is_async(self._func_name):
            return await self._facade.acall(self._func_name, **kwargs)
        return self._facade.call(self._func_name, **kwargs)


class NagatoMCPServer:
    """MCP Server wrapping Nagato MCP Tools ToolFacade using MCP SDK v2."""

    SERVER_NAME = "nagato-mcp-tools"
    SERVER_DESCRIPTION = (
        "Agent-first MCP toolkit exposing nagato code search, editing, "
        "execution, linting, git, and static analysis tools."
    )

    def __init__(
        self,
        workspace_root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        resolved_session_id = _resolve_session_id(session_id)
        sanitized_session_id = _sanitize_session_id(resolved_session_id)
        self.facade: ToolFacade = get_tool_facade(
            workspace_root=self.workspace_root,
            session_id=sanitized_session_id,
        )
        self.server: MCPServer = MCPServer(
            name=self.SERVER_NAME,
            description=self.SERVER_DESCRIPTION,
        )
        self._register_tools()

    def _register_tools(self) -> None:
        """Register every ``nagato_*`` facade function with the MCPServer."""
        for func_name in self.facade.get_registered_functions():
            func = self.facade._registry[func_name]
            docstring = func.__doc__ or ""
            description = _get_first_doc_paragraph(
                docstring,
                default=f"Nagato MCP tool: {func_name}",
            )
            dispatch = _DispatchTool(func_name, self.facade, description)
            try:
                self.server.add_tool(
                    dispatch,
                    name=func_name,
                    description=description,
                )
            except Exception as exc:  # noqa: BLE001
                # A single bad tool must not abort server startup.
                _LOGGER.error(
                    "Failed to register tool %s: %s\n%s",
                    func_name,
                    exc,
                    traceback.format_exc(),
                )

    async def run(self) -> None:
        """Run the MCP server over stdio via the SDK v2 transport pipeline."""
        await self.server.run_stdio_async()


async def async_main(args: argparse.Namespace | None = None) -> None:
    """Async entry point for the MCP server.

    Accepts an optional pre-parsed ``argparse.Namespace`` to keep ``main()``
    testable without re-parsing argv. Errors during startup or transport are
    caught and logged once, so flaky clients (MCP Inspector, host reconnects)
    do not trigger cascading retries with repeated tracebacks.
    """
    parser = argparse.ArgumentParser(description="Nagato MCP Tools Server")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root directory (default: current working directory).",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help=(
            "Session ID for multi-project isolation (e.g. 'proj:my-app', 'standalone'). "
            "Defaults to NAGATO_SESSION_ID env var or 'standalone'."
        ),
    )
    parsed = args if args is not None else parser.parse_args()

    try:
        server = NagatoMCPServer(
            workspace_root=parsed.workspace,
            session_id=parsed.session_id,
        )
        await server.run()
    except KeyboardInterrupt:
        _LOGGER.info("Nagato MCP Tools server stopped by user (KeyboardInterrupt).")
    except Exception as exc:  # noqa: BLE001
        # Top-level crash containment: log a single descriptive error and exit
        # cleanly. We deliberately avoid re-raising so transport clients see a
        # graceful close instead of a cascade of retries.
        _LOGGER.error(
            "Nagato MCP Tools server crashed: %s\n%s",
            exc,
            traceback.format_exc(),
        )


def main() -> None:
    """Synchronous entry point for setuptools / CLI."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        _LOGGER.info("Nagato MCP Tools server stopped by user (KeyboardInterrupt).")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error(
            "Nagato MCP Tools server crashed in main(): %s\n%s",
            exc,
            traceback.format_exc(),
        )


if __name__ == "__main__":
    main()
