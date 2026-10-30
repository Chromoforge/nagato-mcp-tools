"""
Plan writing tool for the PLANNING state.

This tool is the ONLY write-capable tool available in PLANNING state.
It validates the plan content against the strict grammar before writing.
"""

from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root
from nagato_tools.plan_parser import PlanParseError, parse_plan_steps
from nagato_tools.edit import _get_context


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config_paths."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def nagato_write_plan(path: str, content: str, _ctx: Optional[Any] = None) -> str:
    """
    Write a plan file to .github/plans/ after validating its ## Steps section.
    
    Args:
        path: Relative path under .github/plans/ (e.g., "my-plan.md")
        content: Full markdown content of the plan
        _ctx: Optional session context (injected by facade).
        
    Returns:
        Confirmation message with parsed phase/step counts, or error message
    """
    # Resolve workspace root
    workspace_root = _get_workspace_root(_ctx)
    plans_dir = workspace_root / ".github" / "plans"

    # Normalize path if already prefixed with .github/plans
    clean_path = path.replace("\\", "/").lstrip("/")
    if clean_path.startswith(".github/plans/"):
        path = clean_path[len(".github/plans/"):]
    
    # Validate path is under .github/plans/ (OWASP path traversal guard)
    try:
        target_path = (plans_dir / path).resolve()
        # Ensure it's actually under plans_dir
        target_path.relative_to(plans_dir.resolve())
    except (ValueError, OSError):
        return f"ERROR[nagato_write_plan]: Path '{path}' resolves outside .github/plans/ directory"
    
    # Ensure plans directory exists
    plans_dir.mkdir(parents=True, exist_ok=True)
    
    # Parse and validate the ## Steps section BEFORE writing
    try:
        phases = parse_plan_steps(content)
    except PlanParseError as e:
        return f"ERROR[nagato_write_plan]: Plan validation failed - {e}"
    
    # Read existing content for undo tracking
    old_content = None
    is_new = False
    if target_path.exists():
        old_content = target_path.read_text(encoding="utf-8")
    else:
        is_new = True
    
    # Write the file
    try:
        target_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"ERROR[nagato_write_plan]: Failed to write file - {e}"
    
    # Track for standalone undo
    ctx = _get_context(_ctx)
    if hasattr(ctx, 'track_edit_for_undo'):
        ctx.track_edit_for_undo(str(target_path.relative_to(workspace_root)), old_content=old_content, new_content=content, is_new=is_new)
    action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
    if action_context is not None and hasattr(action_context, "record_file_change"):
        action_context.record_file_change(str(target_path.relative_to(workspace_root)), before_content=old_content, after_content=content, is_new=is_new)
    
    # Count phases and steps for confirmation
    phase_count = len(phases)
    step_count = sum(len(p.steps) for p in phases)
    
    return (
        f"Plan written to {target_path.relative_to(workspace_root)}\n"
        f"Parsed: {phase_count} phase(s), {step_count} step(s) across all phases.\n"
        f"Ready for PLANNING -> IMPLEMENTING transition via 'report_fix'."
    )


# Non-prefixed aliases for MCP server compatibility
write_plan = nagato_write_plan
__all__ = ['nagato_write_plan']
