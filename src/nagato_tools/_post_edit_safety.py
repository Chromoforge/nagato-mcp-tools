"""
Shared helper to enforce edit+insight atomicity in the standalone path.

After every mutating tool (nagato_edit / nagato_edit_lines / nagato_delete /
nagato_rename / nagato_create), if trigger_insight_sync_bg returned False
(i.e. the Insight graph will NOT catch up automatically) AND the tool is
configured as a forced-insight trigger AND force-insight is enabled, this
helper auto-rolls back the on-disk file change so file state and Insight
graph stay consistent.

Used by:
- All mutating tools in fsm/functions_internal/edit.py (standalone-safe path)
- fsm/dispatch/pipeline.py via the _last_insight_sync_rolled_back flag
  (dedup guard against double-roll)

Why this lives here (not in pipeline.py):
- Pipeline hooks only run inside the FSM dispatch path. Standalone scripts
  (sync_tools.py, fsm.standalone wrappers, external callers via FunctionFacade)
  bypass the pipeline entirely. Putting the rollback at the tool boundary
  means one helper covers both paths.
- The pipeline-side _rollback_and_refute remains the source of truth for
  FSM-side telemetry and LastErrors; the flag-based dedup in pipeline.py
  prevents double-roll when the tool self-heals before the pipeline checks.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Rollback reason categories for better error handling
class RollbackReason:
    """Categorized rollback reasons for intelligent retry logic."""
    NO_EVENT_LOOP = "no_event_loop"
    INSIGHT_DISABLED = "insight_disabled"
    FORCE_INSIGHT_DISABLED = "force_insight_disabled"
    TOOL_NOT_FORCED = "tool_not_forced"
    SYNC_QUEUED_SUCCESS = "sync_queued_success"
    CONTEXT_MISSING = "context_missing"
    STANDALONE_UNDO_MISSING = "standalone_undo_missing"
    STANDALONE_SNAPSHOT_MISSING = "standalone_snapshot_missing"
    JOURNAL_REVERT_FAILED = "journal_revert_failed"
    UNKNOWN = "unknown"

# Track failed operations to prevent retry loops
_failed_operations: Dict[str, Dict[str, Any]] = {}
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds


def _get_operation_key(file: str, tool_name: str) -> str:
    """Generate a unique key for tracking operation state."""
    return f"{tool_name}:{file}"


def _record_failed_operation(file: str, tool_name: str, reason: str) -> int:
    """Record a failed operation and return the attempt count."""
    key = _get_operation_key(file, tool_name)
    now = time.time()
    
    if key not in _failed_operations:
        _failed_operations[key] = {
            "attempts": 0,
            "first_failure": now,
            "last_failure": now,
            "reasons": []
        }
    
    op = _failed_operations[key]
    op["attempts"] += 1
    op["last_failure"] = now
    op["reasons"].append(reason)
    
    # Clean up old entries (older than 1 hour)
    cutoff = now - 3600
    keys_to_remove = [k for k, v in _failed_operations.items() if v["last_failure"] < cutoff]
    for k in keys_to_remove:
        del _failed_operations[k]
    
    return op["attempts"]


def _should_retry_operation(file: str, tool_name: str, reason: str) -> bool:
    """Determine if an operation should be retried based on failure history and reason."""
    key = _get_operation_key(file, tool_name)
    
    if key not in _failed_operations:
        return True
    
    op = _failed_operations[key]
    
    # Don't retry if max attempts reached
    if op["attempts"] >= _MAX_RETRY_ATTEMPTS:
        return False
    
    # Don't retry permanent failures
    permanent_reasons = {
        RollbackReason.INSIGHT_DISABLED,
        RollbackReason.FORCE_INSIGHT_DISABLED,
        RollbackReason.TOOL_NOT_FORCED,
        RollbackReason.CONTEXT_MISSING,
    }
    if reason in permanent_reasons:
        return False
    
    # For transient failures, check if enough time has passed (exponential backoff)
    # Only apply backoff on subsequent attempts (attempts > 1)
    if reason in {RollbackReason.NO_EVENT_LOOP, RollbackReason.STANDALONE_UNDO_MISSING}:
        if op["attempts"] > 1:
            min_wait = _RETRY_BACKOFF_BASE ** (op["attempts"] - 1)
            if time.time() - op["last_failure"] < min_wait:
                return False
    
    return True


def _get_retry_guidance(reason: str, attempt: int) -> str:
    """Get user-friendly guidance for retrying after a rollback."""
    guidance = {
        RollbackReason.NO_EVENT_LOOP: (
            f"Attempt {attempt}/{_MAX_RETRY_ATTEMPTS}: No async event loop was running. "
            f"Ensure you're running in an async context or start an event loop. "
            f"Wait {_RETRY_BACKOFF_BASE ** attempt:.0f}s before retrying."
        ),
        RollbackReason.INSIGHT_DISABLED: (
            "Insight is disabled in configuration (.nagato/config.yaml: insight.enabled=false). "
            "Enable Insight or disable force_insight (handoff.bForceInsight=false) to allow edits without sync."
        ),
        RollbackReason.FORCE_INSIGHT_DISABLED: (
            "Force insight gating is disabled (handoff.bForceInsight=false). "
            "This should not cause rollbacks - check configuration."
        ),
        RollbackReason.TOOL_NOT_FORCED: (
            f"The tool is not in the forced_insight_tools list. "
            f"Add it to handoff.forced_insight_tools in config or disable bForceInsight."
        ),
        RollbackReason.STANDALONE_UNDO_MISSING: (
            f"Attempt {attempt}/{_MAX_RETRY_ATTEMPTS}: Standalone undo directory or snapshots missing. "
            f"This may indicate a context initialization issue. Wait {_RETRY_BACKOFF_BASE ** attempt:.0f}s before retrying."
        ),
        RollbackReason.STANDALONE_SNAPSHOT_MISSING: (
            "No snapshot found for rollback. The edit may have been applied outside the tracking system."
        ),
        RollbackReason.JOURNAL_REVERT_FAILED: (
            "Journal-based revert failed. Check FSM undo service availability."
        ),
    }
    return guidance.get(reason, f"Unknown rollback reason: {reason}. Check logs for details.")


def _categorize_sync_failure(ctx: Any) -> str:
    """Categorize why insight sync was not queued."""
    # Check if there's an event loop
    try:
        import asyncio
        asyncio.get_running_loop()
        has_event_loop = True
    except RuntimeError:
        has_event_loop = False
    
    if not has_event_loop:
        return RollbackReason.NO_EVENT_LOOP
    
    # Check if insight is enabled
    try:
        from nagato_tools.config import get_insight_config
        if not get_insight_config().get("enabled", False):
            return RollbackReason.INSIGHT_DISABLED
    except Exception:
        return RollbackReason.INSIGHT_DISABLED
    
    # Check if force insight is enabled
    try:
        try:
            from fsm import config as config_module  # host-only
        except (ImportError, Exception):
            config_module = None  # type: ignore[misc]  # fsm-only; standalone gets None
        is_force_fn = getattr(config_module, "is_force_insight_enabled", lambda: False)
        if not is_force_fn():
            return RollbackReason.FORCE_INSIGHT_DISABLED
    except Exception:
        return RollbackReason.FORCE_INSIGHT_DISABLED
    
    return RollbackReason.UNKNOWN


def _maybe_rollback_after_edit(file: str, ctx: Any, tool_name: str) -> Dict[str, Any]:
    """Inspect _last_insight_sync_queued; roll back the edit if appropriate.

    Gate (ALL must be true):
    1. ctx is not None
    2. ctx._last_insight_sync_queued is False (sync was structurally skipped)
    3. config.is_force_insight_enabled() is True
    4. config.is_forced_insight_trigger(tool_name) is True

    Then:
    - FSM-context path: call fsm.undo.service.revert_file (preferred; journal-aware)
    - Standalone-context path: reapply the most recent snapshot_*.json written
      by MockFSMContext.track_edit_for_undo for this file

    Returns:
        {"rolled_back": bool, "reason": str, "file": str, "can_retry": bool, "guidance": str}
        Never raises.
    """
    out = {"rolled_back": False, "reason": "n/a", "file": file, "can_retry": False, "guidance": ""}

    if ctx is None:
        out["reason"] = RollbackReason.CONTEXT_MISSING
        out["guidance"] = _get_retry_guidance(out["reason"], 0)
        return out

    sync_queued = getattr(ctx, "_last_insight_sync_queued", None)
    if sync_queued is not False:
        out["reason"] = RollbackReason.SYNC_QUEUED_SUCCESS
        return out  # Sync either succeeded or wasn't attempted

    try:
        try:
            from fsm import config as config_module  # host-only
        except (ImportError, Exception):
            config_module = None  # type: ignore[misc]  # fsm-only; standalone gets None
    except ImportError:
        try:
            from nagato_tools import config as config_module  # type: ignore[no-redef]
        except ImportError:
            out["reason"] = RollbackReason.UNKNOWN
            out["guidance"] = _get_retry_guidance(out["reason"], 0)
            return out

    # Insight must be globally enabled — otherwise sync_queued=False is the
    # EXPECTED return value (no insight module loaded) and rolling back the
    # edit on that signal would break normal standalone-mode edits.
    insight_cfg = {}
    try:
        get_insight_cfg_fn = getattr(config_module, "get_insight_config", None)
        if get_insight_cfg_fn is not None:
            insight_cfg = get_insight_cfg_fn()
    except Exception:
        insight_cfg = {}
    if not insight_cfg.get("enabled", False):
        out["reason"] = RollbackReason.INSIGHT_DISABLED
        out["guidance"] = _get_retry_guidance(out["reason"], 0)
        return out

    is_force_fn = getattr(config_module, "is_force_insight_enabled", lambda: False)
    if not is_force_fn():
        out["reason"] = RollbackReason.FORCE_INSIGHT_DISABLED
        out["guidance"] = _get_retry_guidance(out["reason"], 0)
        return out
    is_trigger_fn = getattr(config_module, "is_forced_insight_trigger", lambda _t: False)
    if not is_trigger_fn(tool_name):
        out["reason"] = RollbackReason.TOOL_NOT_FORCED
        out["guidance"] = _get_retry_guidance(out["reason"], 0)
        return out

    # Categorize the sync failure for better error reporting
    sync_failure_reason = _categorize_sync_failure(ctx)

    # Detect context type: MockFSMContext has undo_enabled=False (or None).
    # NagatoFSMContext / FSM test contexts have undo_enabled=True or ctx.FSM set.
    is_fsm_ctx = bool(getattr(ctx, "undo_enabled", False)) or bool(getattr(ctx, "FSM", None)) or (type(ctx).__name__ in ("NagatoFSMContext", "FSMContext"))

    if is_fsm_ctx:
        rollback = _rollback_via_journal(file, ctx)
    else:
        rollback = _rollback_via_standalone_snapshot(file, ctx)

    out.update(rollback)

    if out["rolled_back"]:
        # Record the failure for loop prevention
        attempt = _record_failed_operation(file, tool_name, sync_failure_reason)
        out["reason"] = sync_failure_reason
        out["can_retry"] = _should_retry_operation(file, tool_name, sync_failure_reason)
        out["guidance"] = _get_retry_guidance(sync_failure_reason, attempt)
        
        try:
            ctx._last_insight_sync_rolled_back = True
        except Exception:
            pass
        # Clear pending-insight gate (matches pipeline._maybe_auto_refute Side-effect 5)
        try:
            if getattr(ctx, "pending_insight_required", False):
                ctx.pending_insight_required = False
            if getattr(ctx, "last_forced_insight_trigger", None):
                ctx.last_forced_insight_trigger = None
        except Exception:
            pass

        logger.warning(
            "Auto-rollback: %s on %s succeeded on disk but Insight sync returned False. "
            "Reason: %s. File has been restored to pre-edit state. Can retry: %s. Guidance: %s",
            tool_name, file, out["reason"], out["can_retry"], out["guidance"],
        )

        # NOTE: LastErrors + telemetry emission are intentionally NOT done here.
        # The FSM dispatch path's _rollback_and_refute owns these side-effects
        # (source='auto_rollback_refute') so the audit log stays consistent.
        # In standalone mode there is no LastErrors/telemetry on the caller, so
        # nothing further is needed — the logger.warning above is the signal.

    return out


def _rollback_via_journal(file: str, ctx: Any) -> Dict[str, Any]:
    """Roll back via fsm.undo.revert_file. Mirrors pipeline._rollback_and_refute."""
    try:
        try:
            try:
                from fsm import undo  # host-only
            except (ImportError, Exception):
                undo = None  # type: ignore[misc]  # fsm-only; standalone gets None
        except ImportError:
            return {"rolled_back": False, "reason": "FSM undo service not available in this environment"}
        restored, failed = undo.revert_file(ctx, file, version=0, namespace="workspace")
        if restored > 0:
            return {"rolled_back": True, "reason": "Insight sync queued=False; journal revert succeeded"}
        return {"rolled_back": False, "reason": "Journal revert reported 0 restorations"}
    except NotImplementedError:
        return {"rolled_back": False, "reason": "Undo not enabled in this context"}
    except Exception as e:
        return {"rolled_back": False, "reason": f"Journal revert raised {type(e).__name__}: {e}"}


def _rollback_via_standalone_snapshot(file: str, ctx: Any) -> Dict[str, Any]:
    """Roll back via the standalone track_edit_for_undo snapshot files.

    Reads MockFSMContext.undo_dir for snapshot_*.json files matching `file`,
    picks the most recent, and reapplies old_content (or unlinks if is_new,
    or re-creates if is_delete). Prunes the consumed snapshot.
    """
    undo_dir = getattr(ctx, "undo_dir", None)
    if undo_dir is None:
        return {"rolled_back": False, "reason": "Standalone undo_dir missing on ctx"}
    if not Path(undo_dir).exists():
        return {"rolled_back": False, "reason": "Standalone undo_dir does not exist on disk"}

    try:
        candidates = []
        for snap_path in Path(undo_dir).glob("snapshot_*.json"):
            try:
                with open(snap_path, "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if snap.get("file") != file:
                continue
            candidates.append((snap_path, snap))

        if not candidates:
            return {"rolled_back": False, "reason": "No matching standalone snapshot found"}

        # Pick most recent by timestamp
        candidates.sort(key=lambda pair: pair[1].get("timestamp", 0), reverse=True)
        snap_path, snap = candidates[0]

        workspace_root = getattr(ctx, "workspace_root", None)
        if workspace_root is None:
            return {"rolled_back": False, "reason": "ctx.workspace_root missing"}

        target = Path(workspace_root) / file

        if snap.get("is_delete"):
            # Edit was a delete; restore the old content
            old = snap.get("old_content")
            if old is None:
                return {"rolled_back": False, "reason": "Delete snapshot missing old_content"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(old, encoding="utf-8", newline="")
        elif snap.get("is_new"):
            # Edit was a create; unlink the file
            if target.exists():
                target.unlink()
        else:
            # Regular edit; restore old_content
            old = snap.get("old_content")
            if old is None:
                return {"rolled_back": False, "reason": "Edit snapshot missing old_content"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(old, encoding="utf-8", newline="")

        # Prune consumed snapshot so standalone_undo doesn't rewind further than expected
        try:
            snap_path.unlink()
        except OSError:
            pass

        return {"rolled_back": True, "reason": "Insight sync queued=False; standalone snapshot reverted"}

    except Exception as e:
        return {"rolled_back": False, "reason": f"Standalone revert raised {type(e).__name__}: {e}"}


def _emit_telemetry(ctx: Any, tool_name: str, file: str, reason: str) -> None:
    """Best-effort telemetry emit. Mirrors pipeline._rollback_and_refute Side-effect 5."""
    try:
        from nagato_tools.telemetry import AuditEvent, EventType, TelemetryMode
        from nagato_tools.telemetry import publish_event
        publish_event(
            AuditEvent(
                session_id=getattr(ctx, "session_id", None),
                turn=getattr(ctx, "Turn", None),
                mode=TelemetryMode.FSM,
                event_type=EventType.THEORY_PROOF,
                state="AUTO_ROLLBACK_STANDALONE",
                payload={
                    "action": "auto_rollback_standalone",
                    "tool": tool_name,
                    "file": file,
                    "reason": reason,
                    "status": "REFUTED_AND_ROLLED_BACK",
                },
            )
        )
    except Exception:
        pass
__all__ = []
