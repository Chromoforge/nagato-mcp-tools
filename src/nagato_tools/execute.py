import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root
from nagato_tools.errors import _nagato_error as nagato_error


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config / current working directory."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    try:
        return get_workspace_root()
    except Exception:
        return Path.cwd()


def _resolve_python_executable(workspace_root: Optional[Path] = None) -> str:
    if workspace_root is None:
        workspace_root = _get_workspace_root()
    venv_python = workspace_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


DEFAULT_TIMEOUT_SECONDS = 30  # Allow reasonable execution time for imports and test snippets


async def nagato_execute_snippet(code: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, _ctx=None) -> str:
    """
    Execute Tool: Runs a Python code snippet in a subprocess with a hard timeout.

    NOTE: This is NOT a sandbox. The snippet executes with the current user's
    full permissions and PYTHONPATH set to the workspace root. Treat it like
    running a local script — do not point this tool at untrusted prompts.

    Args:
        code (str): Python code snippet to execute (required)
        timeout_seconds (int): Maximum execution time in seconds (default: 30)
        _ctx: Optional session context (injected by facade)
    Returns:
        str: Execution result (STDOUT, STDERR, exit code, or error)
    """
    if not code or not isinstance(code, str):
        return nagato_error("code must be a non-empty string", tool="nagato_execute_snippet")

    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    workspace_root = _get_workspace_root(_ctx)
    python_executable = _resolve_python_executable(workspace_root)

    try:
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(workspace_root),
        }

        process = await asyncio.create_subprocess_exec(
            python_executable,
            "-u",
            "-c",
            code,
            cwd=str(workspace_root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            from nagato_tools.errors import kill_subprocess_tree
            await kill_subprocess_tree(process)
            return nagato_error(
                f"Timeout: Code did not complete within {timeout_seconds} seconds. "
                f"Possible infinite loop or heavy blocking operation detected. "
                f"Please optimize your snippet or use smaller inputs.",
                tool="nagato_execute_snippet",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        output_parts = []
        if stdout:
            output_parts.append(f"STDOUT:\n{stdout.strip()}")
        if stderr:
            output_parts.append(f"STDERR:\n{stderr.strip()}")
        if process.returncode != 0 and not stderr and not stdout:
            output_parts.append(f"EXIT CODE: {process.returncode}")
        if not output_parts:
            output_parts.append("(Execution completed with no output)")

        return "\n\n".join(output_parts)

    except Exception as e:
        return nagato_error(str(e), tool="nagato_execute_snippet")


# Non-prefixed aliases for MCP server compatibility
execute_snippet = nagato_execute_snippet
__all__ = ['nagato_execute_snippet']
