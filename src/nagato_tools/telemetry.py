"""
Telemetry models and JSONL event publisher for Nagato Standalone mode.

Provides self-contained telemetry tracking, audit event publishing/reading,
and live listener callbacks for the standalone WebUI and ToolFacade without
any dependencies on the FSM engine or session storage.
"""

import asyncio
import json
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import uuid4
from pydantic import BaseModel, Field

# Global workspace root for standalone mode (can be overridden by caller/server)
_WORKSPACE_ROOT: Optional[Path] = None
_LOCK = threading.RLock()

# Listener hooks for WebSockets or event streams
_SESSION_LISTENERS: Dict[str, List[Callable[["AuditEvent"], None]]] = {}
_GLOBAL_LISTENERS: List[Callable[["AuditEvent"], None]] = []


def set_workspace_root(root: Optional[Union[str, Path]]) -> None:
    """Set the active workspace root for standalone telemetry storage."""
    global _WORKSPACE_ROOT
    with _LOCK:
        _WORKSPACE_ROOT = Path(root).resolve() if root is not None else None


def get_workspace_root() -> Path:
    """Get the active workspace root or default to Path.cwd()."""
    with _LOCK:
        return _WORKSPACE_ROOT if _WORKSPACE_ROOT is not None else Path.cwd()


class TelemetryMode(str, Enum):
    FSM = "FSM"
    STANDALONE = "STANDALONE"


class EventType(str, Enum):
    TOOL_DISPATCH = "TOOL_DISPATCH"
    THEORY_PROOF = "THEORY_PROOF"
    STATE_TRANSITION = "STATE_TRANSITION"
    LLM_INTERACTION = "LLM_INTERACTION"
    UNDO_EVENT = "UNDO_EVENT"
    SUBGOAL_EVENT = "SUBGOAL_EVENT"
    GATE_REJECTION = "GATE_REJECTION"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    SESSION_LIFECYCLE = "SESSION_LIFECYCLE"


class AuditEvent(BaseModel):
    """Standardized event emitted for standalone audit log."""
    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex[:12]}")
    session_id: str = "standalone"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    turn: Optional[int] = None
    mode: TelemetryMode = TelemetryMode.STANDALONE
    event_type: EventType
    state: Optional[str] = None
    duration_ms: Optional[float] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class UndoStepSummary(BaseModel):
    step: int
    command: str
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    timestamp: str
    files_touched: List[str] = Field(default_factory=list)


class UndoStatus(BaseModel):
    session_id: str = "standalone"
    current_step: int = 0
    max_step: int = 0
    can_undo: bool = False
    can_redo: bool = False
    available_undo_steps: int = 0
    available_redo_steps: int = 0
    steps: List[UndoStepSummary] = Field(default_factory=list)


class TokenUsageSnapshot(BaseModel):
    session_id: str = "standalone"
    turn: Optional[int] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: Optional[str] = None
    provider: Optional[str] = None
    nagatoboot_ctx_tokens: int = 0
    system_prompt_tokens: int = 0
    handoff_tokens: int = 0
    agent_output_tokens: int = 0
    mcp_docstring_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    latency_ms: Optional[float] = None
    completion_source: Optional[str] = None


class SessionAuditSummary(BaseModel):
    session_id: str = "standalone"
    mode: TelemetryMode = TelemetryMode.STANDALONE
    created_at: str
    last_event_at: str
    state: Optional[str] = None
    current_turn: int = 0
    total_events: int = 0
    total_tool_calls: int = 0
    total_state_changes: int = 0
    total_llm_interactions: int = 0
    undo_status: Optional[UndoStatus] = None
    token_usage: Optional[TokenUsageSnapshot] = None


def get_audit_file(session_id: str = "standalone", workspace_root: Optional[Path] = None) -> Path:
    """Return the path to the audit.jsonl log for standalone mode."""
    root = Path(workspace_root).resolve() if workspace_root is not None else get_workspace_root()
    if session_id == "standalone":
        audit_dir = root / ".nagato" / "standalone"
    else:
        audit_dir = root / ".nagato" / "sessions" / session_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "audit.jsonl"


def list_standalone_sessions(workspace_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List all available standalone sessions by scanning the sessions directory.
    
    Returns a list of session info dicts with session_id, has_undo, has_redo, has_audit.
    """
    root = Path(workspace_root).resolve() if workspace_root is not None else get_workspace_root()
    sessions_dir = root / ".nagato" / "sessions"
    
    sessions = []
    if sessions_dir.exists():
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = session_dir.name
            undo_dir = session_dir / "undo_cache"
            redo_dir = session_dir / "redo_cache"
            audit_file = get_audit_file(session_id, workspace_root=root)
            
            sessions.append({
                "session_id": session_id,
                "has_undo": undo_dir.exists() and any(undo_dir.glob("snapshot_*.json")),
                "has_redo": redo_dir.exists() and any(redo_dir.glob("snapshot_*.json")),
                "has_audit": audit_file.exists(),
            })
    
    # Always include the default "standalone" session as the first entry
    default_audit = get_audit_file("standalone", workspace_root=root)
    default_undo = root / ".nagato" / "undo_cache"
    default_redo = root / ".nagato" / "redo_cache"
    
    standalone_session = {
        "session_id": "standalone",
        "has_undo": default_undo.exists() and any(default_undo.glob("snapshot_*.json")),
        "has_redo": default_redo.exists() and any(default_redo.glob("snapshot_*.json")),
        "has_audit": default_audit.exists(),
    }
    
    # Check if "standalone" is already in the list (from sessions_dir)
    if any(s["session_id"] == "standalone" for s in sessions):
        # Replace the existing entry with our computed one
        sessions = [standalone_session if s["session_id"] == "standalone" else s for s in sessions]
    else:
        sessions.insert(0, standalone_session)
    
    return sessions


def publish_event(event: AuditEvent, workspace_root: Optional[Path] = None) -> None:
    """
    Append an audit event to .nagato/standalone/audit.jsonl and notify listeners.
    Thread-safe and fails gracefully without breaking caller execution.
    """
    try:
        audit_file = get_audit_file(event.session_id, workspace_root=workspace_root)
        line = event.model_dump_json() + "\n"
        with _LOCK:
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(line)

        _notify_listeners(event)
    except Exception:
        # Telemetry must never crash the caller or tool dispatch
        pass


def _notify_listeners(event: AuditEvent) -> None:
    """Notify in-memory listeners of an emitted event."""
    with _LOCK:
        session_callbacks = list(_SESSION_LISTENERS.get(event.session_id, []))
        global_callbacks = list(_GLOBAL_LISTENERS)

    for cb in session_callbacks + global_callbacks:
        try:
            if asyncio.iscoroutinefunction(cb):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(cb(event))
                except RuntimeError:
                    asyncio.run(cb(event))
            else:
                cb(event)
        except Exception:
            pass


def add_listener(session_id: Optional[str], callback: Callable[[AuditEvent], None]) -> None:
    """Subscribe a listener to audit events for a session (or globally if session_id is None)."""
    with _LOCK:
        if session_id:
            if session_id not in _SESSION_LISTENERS:
                _SESSION_LISTENERS[session_id] = []
            _SESSION_LISTENERS[session_id].append(callback)
        else:
            _GLOBAL_LISTENERS.append(callback)


def remove_listener(session_id: Optional[str], callback: Callable[[AuditEvent], None]) -> None:
    """Unsubscribe a listener."""
    with _LOCK:
        if session_id and session_id in _SESSION_LISTENERS:
            if callback in _SESSION_LISTENERS[session_id]:
                _SESSION_LISTENERS[session_id].remove(callback)
        elif not session_id and callback in _GLOBAL_LISTENERS:
            _GLOBAL_LISTENERS.remove(callback)


def read_audit_events(
    session_id: str = "standalone",
    event_type: Optional[EventType] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    after_id: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> List[AuditEvent]:
    """Read audit events from disk for standalone mode."""
    audit_file = get_audit_file(session_id, workspace_root=workspace_root)
    if not audit_file.exists():
        return []

    events: List[AuditEvent] = []
    try:
        with open(audit_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    ev = AuditEvent(**data)
                    if event_type and ev.event_type != event_type:
                        continue
                    events.append(ev)
                except Exception:
                    continue
    except Exception:
        return []

    if after_id is not None:
        cursor_pos = next((i for i, ev in enumerate(events) if ev.id == after_id), None)
        events = events[cursor_pos + 1:] if cursor_pos is not None else events

    if offset > 0:
        events = events[offset:]
    if limit is not None:
        events = events[:limit]
    return events


__all__ = [
    "TelemetryMode",
    "EventType",
    "AuditEvent",
    "UndoStepSummary",
    "UndoStatus",
    "TokenUsageSnapshot",
    "SessionAuditSummary",
    "set_workspace_root",
    "get_workspace_root",
    "get_audit_file",
    "list_standalone_sessions",
    "publish_event",
    "read_audit_events",
    "add_listener",
    "remove_listener",
]
