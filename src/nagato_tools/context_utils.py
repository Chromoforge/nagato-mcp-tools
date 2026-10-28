"""Shared context utilities to avoid circular imports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root

# Bound by NagatoFSM on startup.
fsm_instance = None


def set_active_fsm(instance) -> None:
    """Registers the active session instance for edit tracking hooks."""
    global fsm_instance
    fsm_instance = instance


def _get_fsm_instance():
    if fsm_instance is None:
        raise RuntimeError("Session instance not bound. Call set_active_session() during host initialization.")
    return fsm_instance


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to the mock context."""
    if ctx is not None:
        return ctx
    try:
        fsm = _get_fsm_instance()
        if fsm is not None:
            return fsm.Context
    except Exception:
        pass
    from nagato_tools.ctx_mock import get_mock_context
    return get_mock_context(_get_workspace_root(ctx))
__all__ = []
