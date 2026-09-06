"""Shared helper for canonically formatted nagato_* tool error returns.

All nagato_* tool functions (registered in NagatoFSM.tool_registry) should return
errors via `_nagato_error(message, tool="nagato_<name>")` instead of ad-hoc
prefixes like "ERROR:", "CRITICAL ERROR in X:", or "[NFSM ERROR]". This keeps
every tool error in the single, parseable shape `ERROR[<tool_name>]: <message>`,
which is what the planned Context.LastErrors fields will extract from.

NOTE: The leading underscore signals that this is an internal helper, not a
user-facing tool. It must NOT be exported in nagato_tools/__init__.py.
"""
import sys
from typing import Any, Optional


def _nagato_error(message: str, tool: str | None = None, _ctx: Optional[Any] = None) -> str:
    """
    Builds the canonical `ERROR[<tool>]: <message>` string returned by nagato_* tools.

    Args:
        message: The error detail, without any "ERROR"/"CRITICAL ERROR" prefix.
        tool: Name of the tool function raising the error (e.g. "nagato_edit").
            If omitted, the call stack is walked to find the nearest enclosing
            nagato_* function as a compatibility fallback.
        _ctx: Optional session context (injected by facade). Ignored but accepted for compatibility.

    Returns:
        Canonical error string: `ERROR[<tool>]: <message>`.
    """
    tool_name = tool
    if tool_name is None:
        depth = 1
        while True:
            try:
                frame = sys._getframe(depth)
                code_name = frame.f_code.co_name
                if code_name.startswith("nagato_"):
                    tool_name = code_name
                    break
                depth += 1
            except ValueError:
                break
    if tool_name is None:
        tool_name = "unknown"
    return f"ERROR[{tool_name}]: {message}"


# Internal callers in functions_internal modules should import this as
# `from nagato_tools.errors import _nagato_error as nagato_error`
# so that the leading underscore correctly hides it from the facade's
# tool-discovery scan (which only picks up names starting with `nagato_`).
__all__ = ['_nagato_error', 'kill_subprocess_tree']


async def kill_subprocess_tree(process: Any) -> None:
    """
    Best-effort process kill that propagates to child processes on Windows.

    `asyncio.subprocess.Process.kill()` calls TerminateProcess on the main
    process only. On Windows, child processes started via `cmd /c` or shell
    pipelines leak past the timeout. We follow up with `taskkill /F /T /PID`
    to terminate the whole tree, then await the process to clean up.

    Args:
        process: An asyncio.subprocess.Process instance.
    """
    try:
        process.kill()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import subprocess  # local import to avoid hard dep on sync side
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    try:
        await process.wait()
    except Exception:
        pass
