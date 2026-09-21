"""
Shell command execution tool for NagatoFSM.

Provides nagato_shell() for executing shell commands with:
- Hard timeout (default 5 seconds)
- Output truncation via ctx.MaxContextSize
- Command logging (no-op in standalone)
- Security allowlist for permitted commands (configurable via .nagato/functions_config.json)
- Structured return with stdout, stderr, exit_code, truncated, timed_out
"""
import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.config import load_functions_config
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.read import _get_context_token_limit, _truncate_to_token_limit


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


# Default timeout in seconds
DEFAULT_TIMEOUT_SECONDS = 5

# ==============================================================================
# Command Security: Allowlist Model (configurable via .nagato/functions_config.json)
# ==============================================================================

# Default allowlist — can be overridden in .nagato/functions_config.json under "shell.allowed_commands"
DEFAULT_ALLOWED_COMMANDS = frozenset({
    'python', 'python3', 'python.exe',
    'pytest', 'ruff', 'mypy', 'black', 'isort',
    'uv', 'pip',
    # Windows native shells
    'cmd', 'cmd.exe', 'powershell', 'pwsh',
})

# Default allowed environment variable keys — can be overridden in config
DEFAULT_ALLOWED_ENV_KEYS = frozenset({
    'PYTHONPATH', 'PYTHONUTF8', 'PYTHONUNBUFFERED',
    'PATH', 'HOME', 'USERPROFILE',
})

# Test runners excluded from nagato_shell — agents must use nagato_run_test instead
EXCLUDED_TEST_COMMANDS = {
    'pytest',
    'py.test',
    'unittest',
    'nosetests',
    'tox',
    'nox',
}

# Substring / argument patterns that indicate test runner invocations
EXCLUDED_TEST_PATTERNS = [
    'pytest',
    'py.test',
    '-m pytest',
    '-m unittest',
    'run_tests.ps1',
    'run_tests.cmd',
]

# Defense-in-depth: dangerous patterns that are always blocked regardless of allowlist
DANGEROUS_PATTERNS = [
    'rm -rf', 'rm -r', 'rm -f',
    '> /dev/', '>/dev/',
    '| sh', '| bash', '| zsh', '| powershell',
    '&& rm', '; rm',
    'sudo ', 'su -',
    'chmod 777', 'chmod +x',
    'git ',  # Block git commands - use nagato_git / nagato_upload instead
]


def _load_shell_config(ctx: Optional[Any] = None) -> tuple[frozenset[str], frozenset[str]]:
    """Load shell allowlist configuration from .nagato/functions_config.json."""
    try:
        workspace_root = _get_workspace_root(ctx)
        config = load_functions_config(workspace_root / ".nagato" / "functions_config.json")
        shell_cfg = config.tool_filtering.get("shell", {}) if config.tool_filtering else {}
        
        allowed_commands = frozenset(shell_cfg.get("allowed_commands", DEFAULT_ALLOWED_COMMANDS))
        allowed_env_keys = frozenset(shell_cfg.get("allowed_env_keys", DEFAULT_ALLOWED_ENV_KEYS))
        return allowed_commands, allowed_env_keys
    except Exception:
        return DEFAULT_ALLOWED_COMMANDS, DEFAULT_ALLOWED_ENV_KEYS


def _check_command_blocked(args: List[str], ctx: Optional[Any] = None) -> tuple[bool, str]:
    """
    Check if command is blocked (not in allowlist, excluded test runner, or dangerous pattern).
    
    Returns:
        (is_blocked, reason)
    """
    if not args:
        return False, ""
    
    cmd = args[0].lower()
    cmd_basename = os.path.basename(cmd)
    cmd_stem = cmd_basename[:-4] if cmd_basename.endswith(".exe") else cmd_basename
    
    # Load allowlist config
    allowed_commands, _ = _load_shell_config(ctx)
    
    # 1. Test runner exclusion checks (direct command name or executable)
    if cmd_basename in EXCLUDED_TEST_COMMANDS or cmd_stem in EXCLUDED_TEST_COMMANDS:
        return True, (
            f"Test runner '{cmd_basename}' is excluded from nagato_shell. "
            "Always use 'nagato_run_test' instead (e.g. nagato_run_test(test_node_id='path/to/test.py::test_name')). "
            "nagato_shell has a hard 5-second timeout and is not intended for test executions."
        )

    # 2. Test runner exclusion checks (patterns anywhere in command args)
    full_cmd = ' '.join(args).lower()
    for pattern in EXCLUDED_TEST_PATTERNS:
        if pattern in full_cmd:
            return True, (
                f"Test execution is excluded from nagato_shell (matched '{pattern}'). "
                "Always use 'nagato_run_test' instead (e.g. nagato_run_test(test_node_id='path/to/test.py::test_name')). "
                "nagato_shell has a hard 5-second timeout and is not intended for test executions."
            )

    # 3. Allowlist check: command basename must be in allowed_commands
    if cmd_basename not in allowed_commands and cmd_stem not in allowed_commands:
        return True, (
            f"Command '{cmd_basename}' is not in the allowed commands list. "
            f"Allowed: {', '.join(sorted(allowed_commands))}. "
            f"Configure via .nagato/functions_config.json under 'shell.allowed_commands'."
        )

    # 4. Defense-in-depth: dangerous patterns in full command (always blocked)
    for pattern in DANGEROUS_PATTERNS:
        if pattern in full_cmd:
            return True, f"Dangerous pattern detected: '{pattern}'"
    
    return False, ""


def _validate_env(env: Optional[Dict[str, str]], ctx: Optional[Any] = None) -> Optional[str]:
    """Validate environment variables against allowlist. Returns error message or None."""
    if not env:
        return None
    
    _, allowed_env_keys = _load_shell_config(ctx)
    
    for key in env.keys():
        if key not in allowed_env_keys:
            return f"Environment variable '{key}' is not allowed. Allowed: {', '.join(sorted(allowed_env_keys))}. Configure via .nagato/functions_config.json under 'shell.allowed_env_keys'."
    
    # Additional sanitization for PATH
    if 'PATH' in env:
        path_val = env['PATH']
        # Basic sanity: no directory traversal, no absolute paths outside workspace
        for part in path_val.split(os.pathsep):
            if part.startswith('..') or (os.path.isabs(part) and not part.startswith(str(_get_workspace_root(ctx)))):
                return f"PATH contains disallowed entry: '{part}'"
    
    return None


_is_dangerous_command = _check_command_blocked


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to the mock context."""
    if ctx is not None:
        return ctx
    # Try to get context from global session instance
    try:
        from nagato_tools import edit as edit_module
        fsm = edit_module._get_fsm_instance()
        if fsm is not None:
            return fsm.Context
    except Exception:
        pass
    from nagato_tools.ctx_mock import get_mock_context
    return get_mock_context(_get_workspace_root(ctx))


def _log_command(session_id: str, command: List[str], cwd: str, result: Dict[str, Any]) -> None:
    """Log command execution to the event log."""
    try:
        try:
            import importlib
            events_mod = None  # fsm-only importlib.load removed; standalone returns None
            event_log_mod = None  # fsm-only importlib.load removed; standalone returns None
            Event = events_mod.Event
            append_event = event_log_mod.append_event
        except ImportError:
            return  # Standalone mode: no event-log integration
        
        event = Event(
            name="shell_command_executed",
            source="nagato_shell",
            message=f"Executed: {' '.join(shlex.quote(arg) for arg in command)}",
            data={
                "command": command,
                "cwd": cwd,
                "exit_code": result.get("exit_code"),
                "timed_out": result.get("timed_out", False),
                "truncated": result.get("truncated", False),
                "stdout_length": len(result.get("stdout", "")),
                "stderr_length": len(result.get("stderr", "")),
            }
        )
        append_event(session_id, event)
    except Exception:
        # Logging failure should not break the tool
        pass


async def _nagato_shell_raw(
    command: Union[str, List[str]],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    _ctx: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Internal raw executor for shell commands with timeout and output truncation.
    
    Args:
        command: List of command arguments or a command string.
        timeout_seconds: Hard timeout in seconds (default: 5). Max 30.
        cwd: Working directory (default: workspace root).
        env: Environment variables to add/override (merged with current env).
        _ctx: Optional session context (injected by facade).
    
    Returns:
        Dict with keys:
        - stdout: Truncated stdout string
        - stderr: Truncated stderr string  
        - exit_code: Process exit code (None if timed out)
        - truncated: True if output was truncated
        - timed_out: True if command timed out
        - command: The command list that was executed
        - cwd: Working directory used
    """
    ctx = _get_context(_ctx)
    session_id = getattr(ctx, "session_id", "unknown")
    workspace_root = _get_workspace_root(_ctx)
    
    # Validate timeout
    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if timeout_seconds > 30:
        timeout_seconds = 30  # Cap at 30 seconds
    
    # Normalize command to list of strings
    if isinstance(command, str):
        try:
            cmd_list = shlex.split(command, posix=os.name != "nt")
        except Exception:
            cmd_list = command.split()
    elif isinstance(command, list):
        if len(command) == 1 and isinstance(command[0], str) and (" " in command[0] or "\t" in command[0]):
            try:
                cmd_list = shlex.split(command[0], posix=os.name != "nt")
            except Exception:
                cmd_list = command[0].split()
        else:
            cmd_list = [str(arg) for arg in command]
    else:
        return {
            "stdout": "",
            "stderr": nagato_error("Command must be a string or list of strings", tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": [],
            "cwd": cwd or str(workspace_root),
        }

    if not cmd_list:
        return {
            "stdout": "",
            "stderr": nagato_error("Command cannot be empty", tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": [],
            "cwd": cwd or str(workspace_root),
        }
    
    # Exclusion and security check
    is_blocked, reason = _check_command_blocked(cmd_list, _ctx)
    if is_blocked:
        _log_command(session_id, cmd_list, cwd or str(workspace_root), {
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": reason,
        })
        return {
            "stdout": "",
            "stderr": nagato_error(reason, tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": cmd_list,
            "cwd": cwd or str(workspace_root),
        }
    
    # Validate environment variables
    env_error = _validate_env(env, _ctx)
    if env_error:
        _log_command(session_id, cmd_list, cwd or str(workspace_root), {
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": env_error,
        })
        return {
            "stdout": "",
            "stderr": nagato_error(env_error, tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": cmd_list,
            "cwd": cwd or str(workspace_root),
        }
    
    # Resolve working directory
    if cwd is None:
        cwd = str(workspace_root)
    else:
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            cwd_path = workspace_root / cwd_path
        # Security: ensure cwd is within workspace
        try:
            cwd_path.resolve().relative_to(workspace_root.resolve())
        except ValueError:
            error_msg = f"Working directory '{cwd}' is outside workspace root"
            _log_command(session_id, cmd_list, cwd, {
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "stdout": "",
                "stderr": error_msg,
            })
            return {
                "stdout": "",
                "stderr": nagato_error(error_msg, tool="nagato_shell"),
                "exit_code": None,
                "truncated": False,
                "timed_out": False,
                "command": cmd_list,
                "cwd": cwd,
            }
        cwd = str(cwd_path)

    # Resolve executable path if relative with path separators (e.g. .venv/Scripts/python.exe)
    if cmd_list[0] and ("/" in cmd_list[0] or "\\" in cmd_list[0]):
        candidate = Path(cmd_list[0])
        if not candidate.is_absolute():
            if (Path(cwd) / candidate).exists():
                cmd_list[0] = str((Path(cwd) / candidate).resolve())
            elif (workspace_root / candidate).exists():
                cmd_list[0] = str((workspace_root / candidate).resolve())
    
    # Prepare environment
    process_env = os.environ.copy()
    if env:
        # env has already been validated by _validate_env
        process_env.update(env)
    
    # Execute command with timeout
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=process_env,
        )
        
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )
            exit_code = process.returncode
            timed_out = False
        except asyncio.TimeoutError:
            # Kill the process (and child tree on Windows)
            from nagato_tools.errors import kill_subprocess_tree
            await kill_subprocess_tree(process)
            stdout_bytes = b""
            stderr_bytes = f"Command timed out after {timeout_seconds} seconds".encode()
            exit_code = None
            timed_out = True
        
        # Decode output
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        
        # Truncate output based on context token limit
        max_tokens = _get_context_token_limit(ctx, default=get_tool_token_limit("shell"))
        stdout_truncated = _truncate_to_token_limit(stdout, max_tokens, ctx)
        stderr_truncated = _truncate_to_token_limit(stderr, max_tokens, ctx)
        
        truncated = (stdout_truncated != stdout) or (stderr_truncated != stderr)
        
        result = {
            "stdout": stdout_truncated,
            "stderr": stderr_truncated,
            "exit_code": exit_code,
            "truncated": truncated,
            "timed_out": timed_out,
            "command": cmd_list,
            "cwd": cwd,
        }
        
        # Log command execution
        _log_command(session_id, cmd_list, cwd, result)
        
        return result
        
    except FileNotFoundError:
        error_msg = f"Command not found: {cmd_list[0]}"
        _log_command(session_id, cmd_list, cwd, {
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": error_msg,
        })
        return {
            "stdout": "",
            "stderr": nagato_error(error_msg, tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": cmd_list,
            "cwd": cwd,
        }
    except PermissionError:
        error_msg = f"Permission denied: {cmd_list[0]}"
        _log_command(session_id, cmd_list, cwd, {
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": error_msg,
        })
        return {
            "stdout": "",
            "stderr": nagato_error(error_msg, tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": cmd_list,
            "cwd": cwd,
        }
    except Exception as e:
        error_msg = f"Execution error: {str(e)}"
        _log_command(session_id, cmd_list, cwd, {
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": error_msg,
        })
        return {
            "stdout": "",
            "stderr": nagato_error(error_msg, tool="nagato_shell"),
            "exit_code": None,
            "truncated": False,
            "timed_out": False,
            "command": cmd_list,
            "cwd": cwd,
        }


async def nagato_shell(
    command: Union[str, List[str]],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    _ctx: Optional[Any] = None
) -> str:
    """
    Execute a shell command with timeout and output truncation.
    
    Args:
        command: List of command arguments or a command string.
        timeout_seconds: Hard timeout in seconds (default: 5). Max 30.
        cwd: Working directory (default: workspace root).
        env: Environment variables to add/override (merged with current env).
        _ctx: Optional session context (injected by facade).
    
    Returns:
        Formatted execution output (STDOUT, STDERR, exit code, or error message).
    """
    result = await _nagato_shell_raw(command, timeout_seconds, cwd, env, _ctx)
    
    # If pure error without process launch
    if result.get("stderr") and not result.get("stdout") and result.get("exit_code") is None and not result.get("timed_out"):
        return result["stderr"]
    
    parts = []
    if result.get("stdout"):
        parts.append(f"STDOUT:\n{result['stdout'].strip()}")
    if result.get("stderr"):
        parts.append(f"STDERR:\n{result['stderr'].strip()}")
    if result.get("timed_out"):
        parts.append(f"TIMED OUT after {timeout_seconds}s")
    if result.get("exit_code") is not None:
        parts.append(f"EXIT CODE: {result['exit_code']}")
    if result.get("truncated"):
        parts.append("[OUTPUT TRUNCATED]")
    
    return "\n\n".join(parts) if parts else "(Execution completed with no output)"


# For backward compatibility / display purposes
async def nagato_shell_str(
    command: Union[str, List[str]],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    _ctx: Optional[Any] = None
) -> str:
    """String-formatted version of nagato_shell (alias)."""
    return await nagato_shell(command, timeout_seconds, cwd, env, _ctx)


# Non-prefixed aliases for MCP server compatibility
shell = nagato_shell
__all__ = ['nagato_shell', 'nagato_shell_str']
