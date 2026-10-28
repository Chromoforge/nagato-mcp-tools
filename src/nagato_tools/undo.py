"""

Undo tool for standalone (session-free) mode.



Provides nagato_undo_standalone() for lightweight persistent version tracking.

The session-mode counterpart `nagato_undo_fsm` is provided by the host application and is

NOT mirrored into the standalone `nagato_tools` package — it requires the

session journal system and has no meaning without it.

"""



import json

import shutil

import time

import uuid

from pathlib import Path

from typing import Any, Dict, List, Optional, Union



# Snapshot schema version. Bump when the dict shape changes so that older

# readers (e.g. ctx_mock) can be migrated safely. Filename convention is

# `snapshot_<ms>_<uuid8>.json` — collision-safe with ctx_mock writes.

SNAPSHOT_SCHEMA_VERSION = 2





def _get_standalone_undo_dir(ctx: Any) -> Path:

    """Get the undo directory for standalone mode."""

    undo_dir = getattr(ctx, 'undo_dir', None)

    if undo_dir is not None:

        return Path(undo_dir)

    # Fallback for contexts without undo_dir property

    workspace_root = getattr(ctx, 'workspace_root', Path.cwd())

    d = workspace_root / ".nagato" / "undo_cache"

    d.mkdir(parents=True, exist_ok=True)

    return d





def _get_standalone_redo_dir(ctx: Any) -> Path:

    """Get the redo directory for standalone mode."""

    redo_dir = getattr(ctx, 'redo_dir', None)

    if redo_dir is not None:

        return Path(redo_dir)

    # Fallback for contexts without redo_dir property

    workspace_root = getattr(ctx, 'workspace_root', Path.cwd())

    d = workspace_root / ".nagato" / "redo_cache"

    d.mkdir(parents=True, exist_ok=True)

    return d





def _publish_standalone_undo_event(direction: str, file_path: str, ctx: Any = None) -> None:

    """Emit an UNDO_EVENT for a standalone undo/redo action (mirrors fsm/undo/journal.py::restore_step)."""

    try:

        from nagato_tools.telemetry import AuditEvent, EventType, TelemetryMode

        from nagato_tools.telemetry import publish_event

        ws_root = getattr(ctx, 'workspace_root', None) if ctx else None

        publish_event(

            AuditEvent(

                session_id="standalone",

                mode=TelemetryMode.STANDALONE,

                event_type=EventType.UNDO_EVENT,

                payload={"direction": direction, "file": file_path},

            ),

            workspace_root=ws_root,

        )

    except Exception:

        pass





def _track_edit_for_undo(ctx: Any, file_path: str, old_content: Optional[str] = None, new_content: Optional[str] = None, is_new: bool = False, is_delete: bool = False, is_directory: bool = False, created_dirs: Optional[list] = None, graph_effects: Optional[dict] = None) -> None:
    """
    Track an edit for standalone undo.
    
    Args:
        ctx: MockFSMContext or similar
        file_path: Path to the file (relative to workspace)
        old_content: Content before edit (None for new files)
        new_content: Content after edit (None for deletions)
        is_new: True if this is a new file creation
        is_delete: True if this is a file deletion
        is_directory: True if this is a directory creation
        created_dirs: List of (dir_path, node_id) tuples for implicitly created parent directories
        graph_effects: Dict with graph changes (nodes_created, edges_created, nodes_deleted, edges_deleted)
    """
    undo_dir = _get_standalone_undo_dir(ctx)

    # Create snapshot
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "file": file_path,
        "timestamp": time.time(),
        "is_new": is_new,
        "is_delete": is_delete,
        "is_directory": is_directory,
        "old_content": old_content,
        "new_content": new_content,
        "created_dirs": created_dirs or [],
        "graph_effects": graph_effects or {},
    }

    # Save snapshot with collision-safe filename (matches ctx_mock convention)
    snapshot_file = undo_dir / f"snapshot_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
    with open(snapshot_file, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    
    # Prune old snapshots (keep max 50)
    snapshots = sorted(undo_dir.glob("snapshot_*.json"))
    if len(snapshots) > 50:
        for old_snap in snapshots[:-50]:
            old_snap.unlink()





async def _standalone_undo(ctx: Any, mode: str = "step", value: Union[int, float, str] = 1) -> str:

    """
    Standalone undo implementation.
    
    Args:
        ctx: MockFSMContext
        mode: "step" (default), "redo"
        value: Number of steps for "step" mode
        
    Returns:
        Status message
    """

    undo_dir = _get_standalone_undo_dir(ctx)
    redo_dir = _get_standalone_redo_dir(ctx)
    
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
        
        file_path = Path(ctx.workspace_root) / snapshot["file"]
        
        if snapshot.get("is_directory", False):
            # Was a directory creation
            if snapshot["is_new"]:
                # Was a new directory, delete it (only if empty)
                if file_path.exists() and file_path.is_dir():
                    try:
                        file_path.rmdir()  # Only removes if empty
                        _publish_standalone_undo_event("redo", snapshot["file"], ctx)
                        return f"Redo: Deleted newly created directory {snapshot['file']}"
                    except OSError:
                        return f"Redo: Cannot delete directory {snapshot['file']} (not empty)"
            elif snapshot["is_delete"]:
                # Was a directory deletion, restore it
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.mkdir(parents=True, exist_ok=True)
                _publish_standalone_undo_event("redo", snapshot["file"], ctx)
                return f"Redo: Restored deleted directory {snapshot['file']}"
        else:
            # File operations
            if snapshot["is_new"]:
                # Was a new file, delete it
                if file_path.exists():
                    file_path.unlink()
                    _publish_standalone_undo_event("redo", snapshot["file"], ctx)
                    return f"Redo: Deleted newly created file {snapshot['file']}"
            elif snapshot["is_delete"]:
                # Was a deletion, restore file
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                _publish_standalone_undo_event("redo", snapshot["file"], ctx)
                return f"Redo: Restored deleted file {snapshot['file']}"
            else:
                # Was a modification, restore new content (redo)
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["new_content"] or "")
                _publish_standalone_undo_event("redo", snapshot["file"], ctx)
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
        
        file_path = Path(ctx.workspace_root) / snapshot["file"]
        
        if snapshot.get("is_directory", False):
            # Directory operations
            if snapshot["is_new"]:
                # Was a new directory, delete it (only if empty)
                if file_path.exists() and file_path.is_dir():
                    try:
                        file_path.rmdir()  # Only removes if empty
                        results.append(f"Undid creation of directory {snapshot['file']}")
                        _publish_standalone_undo_event("undo", snapshot["file"], ctx)
                    except OSError:
                        results.append(f"Cannot undo directory creation: {snapshot['file']} not empty")
            elif snapshot["is_delete"]:
                # Was a directory deletion, restore it
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.mkdir(parents=True, exist_ok=True)
                results.append(f"Undid deletion of directory {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], ctx)
        else:
            # File operations
            if snapshot["is_new"]:
                # Was a new file, delete it
                if file_path.exists():
                    file_path.unlink()
                    results.append(f"Undid creation of {snapshot['file']}")
                    _publish_standalone_undo_event("undo", snapshot["file"], ctx)
            elif snapshot["is_delete"]:
                # Was a deletion, restore file
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid deletion of {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], ctx)
            else:
                # Was a modification, restore old content
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid modification of {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], ctx)
        
        # Move to redo_dir
        shutil.move(str(latest), str(redo_dir / latest.name))
    
    return "\n".join(results) if results else "Nothing to undo."


async def nagato_undo_standalone(
    ctx: Any,
    mode: str = "step",
    value: Union[int, float, str] = 1,
) -> str:
    """
    Public entry point for standalone undo/redo.
    
    Args:
        ctx: MockFSMContext or similar
        mode: "step" (default), "redo"
        value: Number of steps for "step" mode
        
    Returns:
        Status message
    """
    return await _standalone_undo(ctx, mode, value)


def get_standalone_undo_status(ctx: Any) -> "UndoStatus":
    """
    Get the current undo/redo status for standalone mode.
    
    Args:
        ctx: MockFSMContext or similar
        
    Returns:
        UndoStatus object with current undo/redo state
    """
    from nagato_tools.telemetry import UndoStatus, UndoStepSummary
    import json
    
    undo_dir = _get_standalone_undo_dir(ctx)
    redo_dir = _get_standalone_redo_dir(ctx)
    
    snapshots = sorted(undo_dir.glob("snapshot_*.json"))
    redo_snapshots = sorted(redo_dir.glob("snapshot_*.json"))
    
    steps = []
    for i, snap_file in enumerate(snapshots):
        try:
            with open(snap_file, 'r', encoding='utf-8') as f:
                snapshot = json.load(f)
            steps.append(UndoStepSummary(
                step=i + 1,
                command=snapshot.get("file", "unknown"),
                state_before=None,
                state_after=None,
                timestamp=str(snapshot.get("timestamp", 0)),
                files_touched=[snapshot.get("file", "unknown")],
                has_messages=False,
            ))
        except Exception:
            pass
    
    return UndoStatus(
        session_id=getattr(ctx, "session_id", "standalone"),
        current_step=len(snapshots),
        max_step=len(snapshots),
        can_undo=len(snapshots) > 0,
        can_redo=len(redo_snapshots) > 0,
        available_undo_steps=len(snapshots),
        available_redo_steps=len(redo_snapshots),
        steps=steps,
    )
__all__ = ['nagato_undo_standalone']
