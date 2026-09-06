import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root as _config_get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.lint import nagato_lint


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config / current working directory."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    try:
        return _config_get_workspace_root()
    except Exception:
        return Path.cwd()


# V1 Suite Provider Architecture (optional, for gradual migration)
try:
    try:
        from fsm.suite_dispatcher import run_suite_via_provider, get_latest_suite_result  # host-only
    except ImportError:
        run_suite_via_provider = None  # type: ignore[misc]  # fsm-only; standalone gets None
        get_latest_suite_result = None  # type: ignore[misc]  # fsm-only; standalone gets None
    try:
        from fsm.provider_loader import ProviderLoadError, load_provider_config  # host-only
    except ImportError:
        ProviderLoadError = None  # type: ignore[misc]  # fsm-only; standalone gets None
        load_provider_config = None  # type: ignore[misc]  # fsm-only; standalone gets None
    PROVIDER_AVAILABLE = True
except ImportError:
    PROVIDER_AVAILABLE = False

# Standalone mode: detect core session tooling availability and provide stubs when unavailable
try:
    # Check that all core dependencies are importable
    from nagato_tools.lint import nagato_lint  # noqa: F401
    FSM_MODE_AVAILABLE = True
except ImportError:
    FSM_MODE_AVAILABLE = False

# Standalone fallback stubs for when full host tooling is unavailable
if not FSM_MODE_AVAILABLE:
    async def nagato_run_test(test_node_id: str, timeout_seconds: int = 120, _ctx = None) -> str:
        """Standalone stub: host tools unavailable."""
        return (
            "STANDALONE MODE: nagato_run_test is unavailable — core host tooling not installed.\n"
            "Install NagatoFSM or host tooling to enable full test runner support."
        )

    async def nagato_run_gold_full(_ctx = None) -> str:
        """Standalone stub: host tools unavailable."""
        return (
            "STANDALONE MODE: nagato_run_gold_full is unavailable — core host tooling not installed.\n"
            "Install NagatoFSM or host tooling to enable full gold test suite support."
        )

    async def nagato_run_configured_suite(semantic_role: Optional[str] = None, _ctx = None) -> str:
        """Standalone stub: host tools unavailable."""
        return (
            "STANDALONE MODE: nagato_run_configured_suite is unavailable — core host tooling not installed.\n"
            "Install NagatoFSM or host tooling to enable configured suite support."
        )


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to mock."""
    if ctx is not None:
        return ctx
    return get_mock_context(_get_workspace_root(ctx))


def _read_gold_status(workspace_root: Optional[Path] = None) -> Optional[dict]:
    """Read the latest gold test status from the status file."""
    if workspace_root is None:
        workspace_root = _get_workspace_root()
    status_file = workspace_root / "debug_outputs" / "gold_full_latest.status.json"
    if not status_file.exists():
        return None
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_python_executable(workspace_root: Optional[Path] = None) -> str:
    if workspace_root is None:
        workspace_root = _get_workspace_root()
    venv_python = workspace_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _try_dispatch_suite_via_provider(
    semantic_role: str,
    session_id: str,
    fsm_state: str,
    target: str | None = None,
    baseline_reds: int | None = None,
    workspace_root: Optional[Path] = None,
) -> dict[str, object] | None:
    """
    Try to run a suite via the new provider architecture.
    
    If provider configuration exists and is valid, dispatch to the provider.
    Otherwise, return None to indicate fallback to legacy behavior.
    
    Returns:
        Normalized suite result dict (compat format), or None if provider unavailable.
    """
    if workspace_root is None:
        workspace_root = _get_workspace_root()
    
    if not PROVIDER_AVAILABLE:
        return None
    
    try:
        # Attempt to dispatch via new provider architecture
        result = run_suite_via_provider(
            workspace_root=workspace_root,
            semantic_role=semantic_role,
            session_id=session_id,
            fsm_state=fsm_state,
            target=target,
            baseline_reds=baseline_reds,
        )
        
        # Convert normalized result to legacy compat shape for host consumption
        try:
            from fsm.suite_contract import project_to_legacy_gold_status  # host-only
        except ImportError:
            project_to_legacy_gold_status = None  # type: ignore[misc]  # fsm-only; standalone gets None
        return project_to_legacy_gold_status(result)
        
    except ProviderLoadError:
        # Provider config missing or invalid; fall back to legacy behavior
        return None
    except Exception as e:
        # Log but don't crash; let the host fall back to legacy gold runner
        import traceback
        traceback.print_exc()
        return None


async def nagato_run_test(test_node_id: str, timeout_seconds: int = 120, _ctx: Optional[Any] = None) -> str:
    """
    Executes an isolated pytest and returns the full result directly.
    Waits for completion (not fire-and-forget).
    Args:
        test_node_id:     The pytest node identifier or test file path (e.g., TitanTest/test_xy.py::test_name).
        timeout_seconds:  Maximum wait time in seconds (default 120).
        _ctx:             Optional session context (injected by facade).
    """
    if not test_node_id or not isinstance(test_node_id, str):
        return nagato_error("test_node_id must be a non-empty string specifying a pytest node or test file path.", tool="nagato_run_test")

    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    python_executable = _resolve_python_executable(workspace_root)

    try:
        log_dir = workspace_root / "debug_outputs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "nagato_run_test_latest.log"

        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(workspace_root)}
        cmd = [python_executable, "-m", "pytest", test_node_id, "--tb=short", "-o", "log_cli=false", "-v"]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace_root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            from nagato_tools.errors import kill_subprocess_tree
            await kill_subprocess_tree(process)
            return nagato_error(
                f"Timeout after {timeout_seconds}s. Test aborted.",
                tool="nagato_run_test",
            )

        output = stdout_bytes.decode("utf-8", errors="replace")
        log_path.write_text(output, encoding="utf-8")

        status = "PASSED" if process.returncode == 0 else f"FAILED (exit {process.returncode})"
        # Output last 4000 characters — enough for pytest --tb=short output
        tail = output[-4000:] if len(output) > 4000 else output
        return f"=== {status} ===\n{tail}"

    except Exception as e:
        return nagato_error(str(e), tool="nagato_run_test")

async def nagato_run_gold_full(_ctx: Optional[Any] = None) -> str:
    """
    Executes the Gold Full acceptance run via configured suite provider.

    Returns a clean, machine-readable result.
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    
    try:
        # Try provider dispatch (if available and configured)
        if PROVIDER_AVAILABLE:
            provider_result = _try_dispatch_suite_via_provider(
                semantic_role="gold",
                session_id="unknown",  # Will be set by the host caller
                fsm_state="VERIFYING",  # Nominal state for gold suite
                target=None,
                baseline_reds=None,
                workspace_root=workspace_root,
            )
            if provider_result is not None:
                # Provider succeeded; extract result and format for the host
                state = provider_result.get("state", "unknown")
                reds = provider_result.get("reds", -1)
                passed = provider_result.get("passed", 0)
                exit_code = provider_result.get("exit_code", 1 if reds > 0 else 0)
                failed_files = provider_result.get("failed_files", [])
                xfailed = provider_result.get("xfailed", 0)
                xpassed = provider_result.get("xpassed", 0)
                skipped = provider_result.get("skipped", 0)
                
                summary = f"reds={reds}, passed={passed}, exit={exit_code}"
                header = "GOLD FULL DONE" if exit_code == 0 else f"GOLD FULL FAILED ({summary})"
                detail_msg = f"{header}\n{summary}\n"
                detail_msg += f"XFailed: {xfailed}, XPassed: {xpassed}, Skipped: {skipped}\n"
                if failed_files:
                    detail_msg += "Failed/Error Files:\n" + "\n".join(f"  - {f}" for f in failed_files) + "\n"
                detail_msg += f"[Executed via provider: {provider_result.get('suite_name', 'unknown')}]\n"
                return detail_msg

        return nagato_error(
            "Gold suite provider is not available or not configured in TitanTest/config.json.",
            tool="nagato_run_gold_full",
        )

    except Exception as e:
        return nagato_error(str(e), tool="nagato_run_gold_full")


async def nagato_run_configured_suite(semantic_role: Optional[str] = None, _ctx: Optional[Any] = None) -> str:
    """
    Runs whichever suite is configured as 'primary_suite' in TitanTest/config.json
    (default: "gold"), or an explicit semantic_role override.

    Single entry point so projects without a Gold Suite can configure "targeted"
    (or any other provider-defined role) as their acceptance suite, without needing
    a dedicated tool/state per suite.
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)

    role = semantic_role
    if role is None:
        role = "gold"
        if PROVIDER_AVAILABLE:
            try:
                config = load_provider_config(workspace_root)
                role = config.primary_suite
            except ProviderLoadError:
                pass

    if role == "gold":
        return await nagato_run_gold_full(_ctx=ctx)

    if not PROVIDER_AVAILABLE:
        return nagato_error(
            f"Cannot run suite '{role}': provider architecture unavailable and no legacy runner "
            f"exists for non-gold suites. Configure TitanTest/config.json with a provider_module.",
            tool="nagato_run_configured_suite",
        )

    provider_result = _try_dispatch_suite_via_provider(
        semantic_role=role,
        session_id="unknown",
        fsm_state="VERIFYING",
        target=None,
        baseline_reds=None,
    )
    if provider_result is None:
        return nagato_error(
            f"Suite '{role}' could not be dispatched (missing/invalid provider config, "
            f"or '{role}' not mapped in suite_roles).",
            tool="nagato_run_configured_suite",
        )

    reds = provider_result.get("reds", -1)
    passed = provider_result.get("passed", 0)
    exit_code = provider_result.get("exit_code", 1 if reds > 0 else 0)
    failed_files = provider_result.get("failed_files", [])
    xfailed = provider_result.get("xfailed", 0)
    xpassed = provider_result.get("xpassed", 0)
    skipped = provider_result.get("skipped", 0)

    summary = f"reds={reds}, passed={passed}, exit={exit_code}"
    header = f"SUITE '{role}' DONE" if exit_code == 0 else f"SUITE '{role}' FAILED ({summary})"
    detail_msg = f"{header}\n{summary}\n"
    detail_msg += f"XFailed: {xfailed}, XPassed: {xpassed}, Skipped: {skipped}\n"
    if failed_files:
        detail_msg += "Failed/Error Files:\n" + "\n".join(f"  - {f}" for f in failed_files) + "\n"
    detail_msg += f"[Executed via provider: {provider_result.get('suite_name', role)}]\n"
    return detail_msg


# Non-prefixed aliases for MCP server compatibility
run_test = nagato_run_test
run_gold_full = nagato_run_gold_full
run_configured_suite = nagato_run_configured_suite
__all__ = ['nagato_run_test', 'nagato_run_gold_full', 'nagato_run_configured_suite']
