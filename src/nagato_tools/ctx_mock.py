"""
Standalone MockContext for nagato-mcp-functions package.

Auto-generated from the host project mock context by sync_tools.py with all host-only
branches removed. Do not edit directly.
"""


import copy
import logging
import os
import re
import yaml
from typing import Optional, Dict, Any, List, Union
from pathlib import Path
from nagato_tools.config import get_token_limits


logger = logging.getLogger(__name__)


def _should_log_session_sanitize() -> bool:
    """Return True if the user opted into session-id sanitization logging.

    Opt-in via either ``NAGATO_LOG_SESSION_SANITIZE=1`` or ``NAGATO_DEBUG=1``.
    Default is silent so existing users are not surprised by new stderr output.
    """
    return os.environ.get("NAGATO_LOG_SESSION_SANITIZE") == "1" or os.environ.get("NAGATO_DEBUG") == "1"


def _sanitize_session_id(session_id: str) -> str:
    """
    Sanitize a session ID for safe filesystem usage.

    Replaces characters that are problematic in filesystem paths (colons, slashes, etc.)
    with safe alternatives while preserving readability.

    Args:
        session_id: Raw session ID (e.g., "proj:my-app", "standalone:dev", UUID4)

    Returns:
        Filesystem-safe session ID (e.g., "proj_my-app", "standalone_dev")
    """
    # Replace colons and slashes with underscores
    sanitized = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
    # Remove any other problematic characters
    sanitized = re.sub(r'[<>:"|?*\x00-\x1f]', "_", sanitized)
    # Collapse multiple underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Trim leading/trailing underscores
    sanitized = sanitized.strip("_")

    # Opt-in visibility: log when the input was rewritten. Default is silent to
    # avoid surprising existing users. Toggle with NAGATO_LOG_SESSION_SANITIZE=1
    # (or NAGATO_DEBUG=1).
    if sanitized != session_id and _should_log_session_sanitize():
        logger.info(
            "[nagato] session-id sanitized: %r -> %r", session_id, sanitized
        )
    return sanitized


def _resolve_session_id(explicit_session_id: Optional[str] = None) -> str:
    """
    Resolve the session ID from explicit parameter, environment variable, or default.
    
    Resolution order:
    1. Explicit session_id parameter
    2. NAGATO_SESSION_ID environment variable
    3. Default "standalone"
    
    Args:
        explicit_session_id: Optional explicitly provided session ID
        
    Returns:
        Resolved session ID string
    """
    if explicit_session_id:
        return explicit_session_id
    env_session_id = os.environ.get("NAGATO_SESSION_ID")
    if env_session_id:
        return env_session_id
    return "standalone"


class MockFSMContext:
    """Minimal session context for standalone function execution.
    
    Implements the same interface as NagatoSessionContext but with no-op
    behavior and sensible defaults. Used when functions are called
    outside of a session (CLI, scripts, tests).
    """
    
    # State tracking (class attributes for defaults)
    State = "STANDALONE"
    CurrentState = "STANDALONE"
    
    # Behavior flags
    bHardHandOff = False
    hard_handoff_post_toolcall = True
    hard_handoff_post_state_transition = True
    hard_handoff_post_subgoal_push = False
    bStepTrace = False
    
    # Other fields accessed by functions (class attributes for defaults)
    fix_type: Optional[str] = None
    red_count: int = 0
    anchor: str = "standalone"
    expected_regression_reds: int = 0
    expected_regression_reason: str = ""
    expected_regression_expiry_runs: int = 0
    last_seen_run_id: str = ""
    WorkflowDescription: str = ""
    
    # Context block fields
    ContextBlock: str = ""
    last_context_tokens: int = 0
    last_hard_handoff_tokens: Optional[int] = None
    NewLine: str = "\n"
    Header: str = "[NFSM CTX]\n"
    Turn: int = 0
    
    # Header variants kept for backward-compatible value stripping
    _HEADER_VARIANTS: dict[str, tuple[str, ...]] = {
        "current_datetime": ("Datetime: ", "Current Datetime: ", "Date & Time: "),
        "current_state": ("Session State: ", "Current State: ", "State: "),
        "target_info": ("Target: ", "Current Target: ", "Working on: "),
        "main_objective": ("Main Objective: ", "Overall Problem: ", "System Issue: "),
        "main_goal": ("Main Goal: ", "Overall Solution: ", "System Target: "),
        "objective": ("Objective: ", "Current Objective: ", "Goal: ", "Current Goal: ", "Task: ", "Current Task: "),
        "exit_criterion": ("Exit Criterion: ", "Success Criterion: ", "Done When: "),
        "last_action": ("Last Action: ", "Last Toolcall: ", "Last Action/Toolcall: "),
        "action_result": ("Action Result: ", "Toolcall Result: ", "Last Action/Toolcall Result: "),
        "current_code_chunk": ("Current Chunk:\n", "Current Code Chunk:\n", "Current Code:\n"),
    }
    # Canonical prefixes (single per field)
    hdrCurrentDateTime = "Current DateTime:\n"
    hdrCurrentState = "Session State: "
    hdrTargetInfo = "Target: "
    hdrMainObjective = "Main Objective: "
    hdrMainGoal = "Main Goal: "
    hdrObjective = "Objective: "
    hdrExitCriterion = "Exit Criterion: "
    hdrLastAction = "Last Action: "
    hdrActionResult = "Action Result: "
    hdrInstructionPointer = "InstrPointer: "
    hdrAvailableTools = "  \u4e16\u754c\n"
    
    # --- Target file being worked on ---
    Target = ""
    candidates = []
    
    Footer = "[/NFSM CTX]"
    
    BugfixInstruction = ""
    RefactorInstruction = ""
    NewCorpusInstruction = ""
    NewSubsystemInstruction = ""
    XFailFixInstruction = ""
    FixtureFixInstruction = ""
    CreateInstruction = ""
    PlanningInstruction = ""
    ImplementingInstruction = ""
    
    # Session reference (set by facade if needed)
    FSM = None
    session_id: str = "standalone"
    undo_enabled: bool = False  # Disable undo system in standalone mode
    
    # Force Insight Gating
    pending_insight_required: bool = False
    last_forced_insight_trigger: Optional[str] = None

    # Set by fsm.functions_internal._post_edit_safety._maybe_rollback_after_edit
    # so the FSM pipeline can skip its own rollback_and_refute (dedup guard).
    _last_insight_sync_rolled_back: bool = False

    # Standalone undo tracking
    _undo_dir: Optional[Path] = None
    _redo_dir: Optional[Path] = None
    MainGoal: str = ""
    MainObjective: str = ""
    Objective: str = ""
    ExitCriterion: str = ""
    CurrentDateTime: str = ""
    CurrentState: str = "STANDALONE"
    TargetInfo: str = ""
    LastAction: str = ""
    ActionResult: str = ""
    InstructionPointer: str = ""
    AvailableTools: str = ""
    
    def __init__(
        self, 
        workspace_root: Optional[Path] = None,
        session_id: Optional[str] = None,
    ):
        """Initialize mock context.
        
        Args:
            workspace_root: Optional workspace root for file operations.
            session_id: Optional session ID. If not provided, resolves from
                       NAGATO_SESSION_ID env var or defaults to "standalone".
                       Formatted IDs like "proj:my-app" are sanitized for filesystem use.
        """
        self._workspace_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        
        # Resolve and sanitize session ID
        raw_session_id = _resolve_session_id(session_id)
        self.session_id = _sanitize_session_id(raw_session_id)
        
        # Token limits loaded from config (linked: max_context_size = 80% of max_context_tokens by default)
        self.MaxContextSize, self.MaxContextTokens = get_token_limits()
        
        # Instance-specific mutable state (reset per instance)
        self.subgoal_stack: List[Dict[str, Any]] = []
        self.files_modified_this_step: set[tuple[str, str]] = set()
        self.action_counts: Dict[str, int] = {}
        self.action_sequence: int = 0
        self.action_loop_meta: Dict[str, Any] = {}
        self._last_tool_signature: Optional[tuple] = None
        self._request_stack: List[str] = []
        self._action_stack: List[str] = []
        self._state_stack: List[str] = ["STANDALONE"]

        # Standalone undo and redo cache directories (scoped to session_id)
        if self.session_id == "standalone":
            self._undo_dir = self._workspace_root / ".nagato" / "undo_cache"
            self._redo_dir = self._workspace_root / ".nagato" / "redo_cache"
        else:
            self._undo_dir = self._workspace_root / ".nagato" / "sessions" / self.session_id / "undo_cache"
            self._redo_dir = self._workspace_root / ".nagato" / "sessions" / self.session_id / "redo_cache"
        self._undo_dir.mkdir(parents=True, exist_ok=True)
        self._redo_dir.mkdir(parents=True, exist_ok=True)

        self._init_system_info()
    
    @property
    def workspace_root(self) -> Path:
        """Get workspace root for file operations."""
        return self._workspace_root
    
    @workspace_root.setter
    def workspace_root(self, value: Union[str, Path]) -> None:
        """Set workspace root and update undo/redo directories."""
        self._workspace_root = Path(value)
        self._undo_dir = self._workspace_root / ".nagato" / "undo_cache"
        self._redo_dir = self._workspace_root / ".nagato" / "redo_cache"
        self._undo_dir.mkdir(parents=True, exist_ok=True)
        self._redo_dir.mkdir(parents=True, exist_ok=True)
    
    @property
    def undo_dir(self) -> Path:
        """Directory for standalone undo snapshots."""
        return self._undo_dir
    
    @property
    def redo_dir(self) -> Path:
        """Directory for standalone redo snapshots."""
        return self._redo_dir
    
    @property
    def semantic_search_root(self) -> Optional[str]:
        """Get semantic search root override (always None in standalone mode).
        
        Standalone mode has no session concept, so this always returns None.
        Resolution falls through to the config-file base per design.
        """
        return None
    
    def _init_system_info(self):
        """Initialize system info (lazy)."""
        try:
            from fsm.functions_internal.system import get_system_info
            self.SystemInfo = get_system_info()
        except Exception:
            self.SystemInfo = "System info unavailable (standalone mode)"
    
    # --- Stack operations (no-op but track for compatibility) ---
    
    def push_request(self, request: str) -> str:
        """Push a request onto the request stack."""
        self._request_stack.append(request)
        return request
    
    def push_action(self, action: str, payload: Optional[Dict[str, Any]] = None) -> str:
        """Push an action onto the action stack."""
        self._action_stack.append(action)
        return action
    
    def push_state(self, state: str) -> str:
        """Push a state onto the state stack."""
        self._state_stack.append(state)
        self.CurrentState = state
        return state
    
    def GetRequest(self) -> str:
        """Get current request."""
        return self._request_stack[-1] if self._request_stack else ""
    
    def GetLastAction(self) -> str:
        """Get last action."""
        return self._action_stack[-1] if self._action_stack else ""
    
    def GetState(self) -> str:
        """Get current state."""
        return self.CurrentState
    
    # --- File tracking (no-op) ---
    
    def mark_file_modified(self, file_path: str, namespace: str = "workspace") -> None:
        """Register a file as modified in this dispatch step."""
        self.files_modified_this_step.add((file_path, namespace))

    def mark_file_dirty(self, file: str) -> None:
        """Mark a file as dirty (no-op in standalone)."""
        pass
    
    def track_file_edit(self, file: str, is_post_edit: bool = False) -> None:
        """Track a file edit (no-op in standalone)."""
        pass
    
    def register_file(self, file: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Register a file for tracking (no-op in standalone)."""
        pass
    
    def track_edit_for_undo(self, file_path: str, old_content: Optional[str] = None, new_content: Optional[str] = None, is_new: bool = False, is_delete: bool = False) -> None:
        """
        Track an edit for standalone undo.
        
        Args:
            file_path: Path to the file (relative to workspace)
            old_content: Content before edit (None for new files)
            new_content: Content after edit (None for deletions)
            is_new: True if this is a new file creation
            is_delete: True if this is a file deletion
        """
        import json
        import time
        import shutil
        import uuid
        
        undo_dir = self.undo_dir

        # Create snapshot. schema_version mirrors nagato_tools.undo so the
        # filename convention and payload shape stay in lockstep across both
        # writers that share the same .nagato/undo_cache/ directory.
        snapshot = {
            "schema_version": 1,
            "file": file_path,
            "timestamp": time.time(),
            "is_new": is_new,
            "is_delete": is_delete,
            "old_content": old_content,
            "new_content": new_content,
        }

        # Save snapshot with unique filename (timestamp + UUID to avoid collisions)
        snapshot_file = undo_dir / f"snapshot_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        
        # Prune old snapshots (keep max 50)
        snapshots = sorted(undo_dir.glob("snapshot_*.json"))
        if len(snapshots) > 50:
            for old_snap in snapshots[:-50]:
                old_snap.unlink()
    
    def standalone_undo(self, mode: str = "step", value: Union[int, float, str] = 1) -> str:
        """
        Standalone undo implementation.
        
        Args:
            mode: "step" (default), "redo"
            value: Number of steps for "step" mode
            
        Returns:
            Status message
        """
        import json
        import shutil
        
        undo_dir = self.undo_dir
        redo_dir = self.redo_dir
        
        snapshots = sorted(undo_dir.glob("snapshot_*.json"))
        
        if not snapshots:
            return "No undo history available."
        
        if mode == "redo":
            # Redo: restore from redo_dir
            redo_snapshots = sorted(redo_dir.glob("snapshot_*.json"))
            if not redo_snapshots:
                return "No redo history available."
            
            latest_redo = redo_snapshots[-1]
            with open(latest_redo, 'r', encoding='utf-8') as f:
                snapshot = json.load(f)
            
            file_path = self._workspace_root / snapshot["file"]
            
            if snapshot["is_new"]:
                # Was a new file, delete it
                if file_path.exists():
                    file_path.unlink()
                    return f"Redo: Deleted newly created file {snapshot['file']}"
            elif snapshot["is_delete"]:
                # Was a deletion, restore file
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                return f"Redo: Restored deleted file {snapshot['file']}"
            else:
                # Was a modification, restore new content (redo)
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["new_content"] or "")
                return f"Redo: Restored {snapshot['file']} to modified version"
            
            # Move snapshot back to undo_dir
            shutil.move(str(latest_redo), str(undo_dir / latest_redo.name))
            return "Redo completed."
        
        # Default: undo step(s)
        steps = int(value) if isinstance(value, (int, float, str)) else 1
        steps = max(1, min(steps, len(snapshots)))
        
        results = []
        for _ in range(steps):
            if not snapshots:
                break
            latest = snapshots.pop()
            
            with open(latest, 'r', encoding='utf-8') as f:
                snapshot = json.load(f)
            
            file_path = self._workspace_root / snapshot["file"]
            
            if snapshot["is_new"]:
                # Was a new file, delete it
                if file_path.exists():
                    file_path.unlink()
                    results.append(f"Undid creation of {snapshot['file']}")
            elif snapshot["is_delete"]:
                # Was a deletion, restore file
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid deletion of {snapshot['file']}")
            else:
                # Was a modification, restore old content
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid modification of {snapshot['file']}")
            
            # Move to redo_dir
            shutil.move(str(latest), str(redo_dir / latest.name))
        
        return "\n".join(results) if results else "Nothing to undo."
    
    # --- Subgoal management ---
    
    def get_subgoal_stack(self) -> List[Dict[str, Any]]:
        """Get the current subgoal stack."""
        return self.subgoal_stack
    
    def push_subgoal(self, objective: str, goal: str) -> Dict[str, Any]:
        """Push a subgoal onto the stack."""
        subgoal = {"objective": objective, "goal": goal}
        self.subgoal_stack.append(subgoal)
        return subgoal
    
    def pop_subgoal(self, result: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pop the current subgoal."""
        if self.subgoal_stack:
            subgoal = self.subgoal_stack.pop()
            if result is not None:
                subgoal["result"] = result
            return subgoal
        return None
    
    # --- Action counting (no-op) ---
    
    def IncreaseActionCount(self, action_type: str, signature: Optional[tuple] = None) -> int:
        """Increment action count (no-op in standalone)."""
        self.action_sequence += 1
        return self.action_sequence
    
    # --- Context rendering (minimal) ---
    
    def generateNFSMContext(self, gold_state: str = "unknown", reds: int = -1, anchor: str = "", run_id: str = "", hard_handoff_tokens: Optional[int] = None) -> str:
        """Generate context block matching the new YAML format."""
        context_format = get_context_format()
        
        # Build a minimal snapshot for standalone mode
        snapshot = {
            "session_id": self.session_id,
            "turn": self.Turn,
            "state": self.GetState(),
            "target": self.Target or "",
            # workflow_guide intentionally omitted (see note in fsm/context/renderer.py::get_handover_snapshot)
            "main_objective": self.MainObjective or "",
            "main_goal": self.MainGoal or "",
            "active_subgoal_id": None,
            "subgoals": [],
            "last_action": self.LastAction or "",
            "last_action_result": self.LastActionResult or "",
            "last_errors": [],
            "pending_questions": [],
            "answered_questions": [],
            "tools_available": [],
            "off_grid_mode": False,
            "gold_state": gold_state,
            "reds": reds,
            "anchor": anchor or "",
            "run_id": run_id or "",
            "pending_files": [],
        }
        
        if context_format == "legacy":
            return self._generate_legacy_context(snapshot, hard_handoff_tokens)
        elif context_format == "json":
            return self._generate_json_context(snapshot, hard_handoff_tokens)
        else:  # yaml (default)
            return self._generate_yaml_context(snapshot, hard_handoff_tokens)
    
    def _generate_yaml_context(self, snapshot: dict, hard_handoff_tokens: Optional[int] = None) -> str:
        """Generate YAML-formatted [NAGATO_BOOT] context for standalone mode."""
        limits = get_context_limits()
        
        # Build structured dict for YAML output
        structured = {
            "session_id": snapshot["session_id"],
            "turn": snapshot["turn"],
            "state": snapshot["state"],
            "target": snapshot["target"],
        }
        # workflow_guide intentionally omitted (see note in fsm/context/renderer.py::get_handover_snapshot)
        structured.update({
            "main_objective": snapshot["main_objective"],
            "main_goal": snapshot["main_goal"],
            "active_subgoal_id": snapshot["active_subgoal_id"],
            "subgoals": snapshot["subgoals"],
            "last_action": snapshot["last_action"],
            "last_action_result": snapshot["last_action_result"],
            "last_errors": snapshot["last_errors"],
            "pending_questions": snapshot["pending_questions"],
            "answered_questions": snapshot["answered_questions"],
            "tools_available": snapshot["tools_available"],
            "off_grid_mode": snapshot["off_grid_mode"],
            "gold_state": snapshot["gold_state"],
            "reds": snapshot["reds"],
            "anchor": snapshot["anchor"],
            "run_id": snapshot["run_id"],
            "pending_files": snapshot["pending_files"],
        })
        
        # SAFE YAML DUMP
        yaml_str = yaml.safe_dump(structured, sort_keys=False, allow_unicode=True)
        
        # SANITIZE - escape block delimiters in all string values
        def sanitize(obj):
            if isinstance(obj, str):
                return obj.replace("[/NAGATO_BOOT]", "\\[/NAGATO_BOOT]").replace("[NAGATO_BOOT]", "\\[NAGATO_BOOT]")
            elif isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize(v) for v in obj]
            return obj
        
        sanitized = sanitize(structured)
        yaml_str = yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True)
        
        lines = ["[NAGATO_BOOT]"]
        
        if self.bStepTrace:
            lines.append("🔴 STEPTRACE MODE - ALL TOOL CALLS BLOCKED")
            lines.append("⚠️  Tool execution is paused for interactive debugging.")
            lines.append("👤 HUMAN: Approve the next tool execution to proceed.")
            lines.append("---")
        
        lines.append(yaml_str.rstrip())
        
        # Token count
        soft_tokens = estimate("\n".join(lines))
        if get_show_token_budget_to_llm():
            lines.append(f"tokens={soft_tokens}/{self.MaxContextTokens} (ctx:{soft_tokens} sys:0 handoff:0 last_result:0 tool_docs:0)")
        if soft_tokens > self.MaxContextTokens:
            lines.append(
                f"⚠️ TOKEN_BUDGET_WARNING: NFSM Context is {soft_tokens} tokens "
                f"(limit {self.MaxContextTokens}). Consider wrapping up or requesting a chat rotation."
            )
        
        if hard_handoff_tokens is not None:
            lines.append(f"hard_handoff_tokens={hard_handoff_tokens} (est. cost if chat rotated now)")
        
        lines.append("[/NAGATO_BOOT]")
        result = "\n".join(lines)
        
        self.ContextBlock = result
        self.last_context_tokens = soft_tokens
        self.last_hard_handoff_tokens = hard_handoff_tokens
        return result
    
    def _generate_legacy_context(self, snapshot: dict, hard_handoff_tokens: Optional[int] = None) -> str:
        """Generate legacy flat key=value format for backward compatibility."""
        lines = ["[NAGATO_BOOT]"]
        
        if self.bStepTrace:
            lines.append("🔴 STEPTRACE MODE - ALL TOOL CALLS BLOCKED")
            lines.append("---")
        
        lines.append(f"sid={snapshot['session_id']}")
        lines.append(f"state={snapshot['state']}")
        
        if snapshot["target"]:
            lines.append(f"target={snapshot['target']}")
        if snapshot["main_objective"]:
            lines.append(f"main_objective={snapshot['main_objective']}")
        if snapshot["main_goal"]:
            lines.append(f"main_goal={snapshot['main_goal']}")
        if snapshot["last_action"]:
            lines.append(f"last_call={snapshot['last_action']}")
        if snapshot["last_action_result"]:
            lines.append(f"last_result={snapshot['last_action_result']}")
        
        soft_tokens = estimate("\n".join(lines))
        if get_show_token_budget_to_llm():
            lines.append(f"tokens={soft_tokens}/{self.MaxContextTokens}")
        if soft_tokens > self.MaxContextTokens:
            lines.append(f"⚠️ TOKEN_BUDGET_WARNING: {soft_tokens} tokens (limit {self.MaxContextTokens})")
        
        if hard_handoff_tokens is not None:
            lines.append(f"hard_handoff_tokens={hard_handoff_tokens}")
        
        lines.append("[/NAGATO_BOOT]")
        result = "\n".join(lines)
        
        self.ContextBlock = result
        self.last_context_tokens = soft_tokens
        self.last_hard_handoff_tokens = hard_handoff_tokens
        return result
    
    def _generate_json_context(self, snapshot: dict, hard_handoff_tokens: Optional[int] = None) -> str:
        """Generate compact JSON format for token efficiency."""
        import json
        
        structured = {
            "sid": snapshot["session_id"],
            "turn": snapshot["turn"],
            "state": snapshot["state"],
            "target": snapshot["target"],
            "main_obj": snapshot["main_objective"],
            "main_goal": snapshot["main_goal"],
            "active_sg": snapshot["active_subgoal_id"],
            "sgs": snapshot["subgoals"],
            "last_act": snapshot["last_action"],
            "last_res": snapshot["last_action_result"],
            "errs": snapshot["last_errors"],
            "pq": snapshot["pending_questions"],
            "aq": snapshot["answered_questions"],
            "tools": snapshot["tools_available"],
            "off_grid": snapshot["off_grid_mode"],
            "gold": snapshot["gold_state"],
            "reds": snapshot["reds"],
            "anchor": snapshot["anchor"],
            "run": snapshot["run_id"],
            "mod": snapshot["pending_files"],
        }
        
        def sanitize(obj):
            if isinstance(obj, str):
                return obj.replace("[/NAGATO_BOOT]", "\\[/NAGATO_BOOT]").replace("[NAGATO_BOOT]", "\\[NAGATO_BOOT]")
            elif isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize(v) for v in obj]
            return obj
        
        sanitized = sanitize(structured)
        json_str = json.dumps(sanitized, separators=(',', ':'), ensure_ascii=False)
        
        lines = ["[NAGATO_BOOT]"]
        
        if self.bStepTrace:
            lines.append("🔴 STEPTRACE MODE - ALL TOOL CALLS BLOCKED")
            lines.append("---")
        
        lines.append(json_str)
        
        soft_tokens = estimate("\n".join(lines))
        if get_show_token_budget_to_llm():
            lines.append(f"tokens={soft_tokens}/{self.MaxContextTokens}")
        if soft_tokens > self.MaxContextTokens:
            lines.append(f"⚠️ TOKEN_BUDGET_WARNING: {soft_tokens} tokens (limit {self.MaxContextTokens})")
        
        if hard_handoff_tokens is not None:
            lines.append(f"hard_handoff_tokens={hard_handoff_tokens}")
        
        lines.append("[/NAGATO_BOOT]")
        result = "\n".join(lines)
        
        self.ContextBlock = result
        self.last_context_tokens = soft_tokens
        self.last_hard_handoff_tokens = hard_handoff_tokens
        return result
    
    # --- Compatibility properties ---
    
    @property
    def LastActionResult(self) -> str:
        """Last action result (empty in standalone)."""
        return getattr(self, '_last_action_result', "")
    
    @LastActionResult.setter
    def LastActionResult(self, value: str) -> None:
        self._last_action_result = value
    
    @property
    def Context(self):
        """Self-reference for compatibility."""
        return self


# Global mock instance for simple access
_mock_instance: Optional[MockFSMContext] = None


def get_mock_context(
    workspace_root: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> MockFSMContext:
    """Get or create the global mock context instance.
    
    Args:
        workspace_root: Optional workspace root for file operations.
        session_id: Optional session ID. If not provided, resolves from
                   NAGATO_SESSION_ID env var or defaults to "standalone".
    """
    global _mock_instance
    if _mock_instance is None:
        _mock_instance = MockFSMContext(workspace_root, session_id)
    return _mock_instance


def set_mock_context(context: MockFSMContext) -> None:
    """Set a custom mock context instance."""
    global _mock_instance
    _mock_instance = context