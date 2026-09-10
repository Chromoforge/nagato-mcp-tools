"""
Shell command execution tool for NagatoFSM.

Provides nagato_shell() for executing shell commands with:
- Hard timeout (default 5 seconds)
- Output truncation via ctx.MaxContextSize
- Command logging (no-op in standalone)
- Security denylist for dangerous commands
- Structured return with stdout, stderr, exit_code, truncated, timed_out
"""
import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from nagato_tools.config import get_tool_token_limit, get_workspace_root
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
# Command Exclusion & Security Lists
# ==============================================================================

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

# Denylist of dangerous commands/patterns
DANGEROUS_COMMANDS = {
    'rm', 'rmdir', 'del', 'erase', 'format', 'fdisk', 'mkfs',
    'sudo', 'su', 'doas', 'runas', 'pkexec',
    'chmod', 'chown', 'chgrp', 'attrib', 'icacls',
    'shutdown', 'reboot', 'halt', 'poweroff', 'init',
    'dd', 'fsck', 'mount', 'umount',
    'kill', 'killall', 'pkill', 'taskkill',
    'passwd', 'usermod', 'userdel', 'groupmod', 'groupdel',
    'iptables', 'ufw', 'firewall-cmd', 'netsh',
    'systemctl', 'service', 'sc', 'net',
    'crontab', 'at', 'schtasks',
    'ssh', 'scp', 'rsync', 'sftp',
    'curl', 'wget', 'nc', 'netcat', 'telnet',
    'perl', 'ruby', 'node', 'php', 'bash', 'sh', 'zsh', 'fish',
    # Note: powershell, pwsh, cmd are allowed as they are native shells on Windows
}

# Dangerous patterns in full command line
DANGEROUS_PATTERNS = [
    'rm -rf', 'rm -r', 'rm -f',
    '> /dev/', '>/dev/',
    '| sh', '| bash', '| zsh', '| powershell',
    '&& rm', '; rm',
    'sudo ', 'su -',
    'chmod 777', 'chmod +x',
]


def _check_command_blocked(args: List[str]) -> tuple[bool, str]:
    """
    Check if command is blocked (dangerous command or excluded test runner).
    
    Returns:
        (is_blocked, reason)
    """
    if not args:
        return False, ""
    
    cmd = args[0].lower()
    cmd_basename = os.path.basename(cmd)
    cmd_stem = cmd_basename[:-4] if cmd_basename.endswith(".exe") else cmd_basename
    
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

    # 3. Dangerous system commands
    if cmd_basename in DANGEROUS_COMMANDS or cmd_stem in DANGEROUS_COMMANDS:
        return True, f"Command '{cmd_basename}' is blocked (dangerous operation)"
    
    # 4. Dangerous patterns in full command
    for pattern in DANGEROUS_PATTERNS:
        if pattern in full_cmd:
            return True, f"Dangerous pattern detected: '{pattern}'"
    
    return False, ""


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
    is_blocked, reason = _check_command_blocked(cmd_list)
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
