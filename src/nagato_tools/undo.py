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

        undo_dir = Path(undo_dir)

        undo_dir.mkdir(parents=True, exist_ok=True)

        return undo_dir

    # Fallback for contexts without undo_dir property

    workspace_root = getattr(ctx, 'workspace_root', Path.cwd())

    d = workspace_root / ".nagato" / "undo_cache"

    d.mkdir(parents=True, exist_ok=True)

    return d





def _get_standalone_redo_dir(ctx: Any) -> Path:

    """Get the redo directory for standalone mode."""

    redo_dir = getattr(ctx, 'redo_dir', None)

    if redo_dir is not None:

        redo_dir = Path(redo_dir)

        redo_dir.mkdir(parents=True, exist_ok=True)

        return redo_dir

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





async def _standalone_undo(_ctx: Any, mode: str = "step", value: Union[int, float, str] = 1) -> str:
    """
    Standalone undo implementation.
    
    Args:
        _ctx: MockFSMContext
        mode: "step" (default), "redo"
        value: Number of steps for "step" mode
        
    Returns:
        Status message
    """

    if mode not in ("step", "redo"):
        return f"ERROR[nagato_undo_standalone]: Unknown mode {mode!r}."
    try:
        steps = max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return "ERROR[nagato_undo_standalone]: value must be a positive number of steps."

    if mode == "redo" and steps > 1:
        results = []
        for _ in range(steps):
            result = await _standalone_undo(_ctx, mode="redo", value=1)
            if result.startswith(("ERROR", "No redo")):
                if not results or result.startswith("ERROR"):
                    results.append(result)
                break
            results.append(result)
        return "\n".join(results)

    undo_dir = _get_standalone_undo_dir(_ctx)
    redo_dir = _get_standalone_redo_dir(_ctx)
    
    snapshots = sorted(undo_dir.glob("snapshot_*.json"), key=_snapshot_sort_key)
    
    if mode == "redo":
        redo_snapshots = sorted(redo_dir.glob("snapshot_*.json"), key=_snapshot_sort_key)
        if not redo_snapshots:
            return "No redo history available."
        
        latest_redo = redo_snapshots[0]
        with open(latest_redo, 'r', encoding='utf-8') as f:
            snapshot = json.load(f)

        if snapshot.get("action_id"):
            return _move_journaled_snapshot(_ctx, snapshot, latest_redo, undo_dir, redo=True)
        
        file_path = Path(_ctx.workspace_root) / snapshot["file"]
        
        if snapshot.get("is_directory", False):
            if snapshot["is_new"]:
                file_path.mkdir(parents=True, exist_ok=False)
            elif snapshot["is_delete"]:
                file_path.rmdir()
        else:
            if snapshot["is_new"]:
                if snapshot.get("new_content") is None:
                    return f"Cannot redo creation of {snapshot['file']}: snapshot has no file content."
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'x', encoding='utf-8', newline='') as f:
                    f.write(snapshot["new_content"])
            elif snapshot["is_delete"]:
                file_path.unlink()
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8', newline='') as f:
                    f.write(snapshot["new_content"] or "")

        shutil.move(str(latest_redo), str(undo_dir / latest_redo.name))
        _publish_standalone_undo_event("redo", snapshot["file"], _ctx)
        return f"Redo: Applied change to {snapshot['file']}" + _mark_index_stale(_ctx, [snapshot["file"]])

    if not snapshots:
        return "No undo history available."
    
    # Default: undo step(s)
    steps = max(1, min(steps, len(snapshots)))
    
    results = []
    for _ in range(steps):
        if not snapshots:
            break
        latest = snapshots.pop()
        
        with open(latest, 'r', encoding='utf-8') as f:
            snapshot = json.load(f)

        if snapshot.get("action_id"):
            result = _move_journaled_snapshot(_ctx, snapshot, latest, redo_dir, redo=False)
            if result.startswith("ERROR"):
                results.append(result)
                break
            results.append(result)
            continue
        
        file_path = Path(_ctx.workspace_root) / snapshot["file"]
        
        if snapshot.get("is_directory", False):
            # Directory operations
            if snapshot["is_new"]:
                # Was a new directory, delete it (only if empty)
                if file_path.exists() and file_path.is_dir():
                    try:
                        file_path.rmdir()  # Only removes if empty
                        results.append(f"Undid creation of directory {snapshot['file']}")
                        _publish_standalone_undo_event("undo", snapshot["file"], _ctx)
                    except OSError:
                        results.append(f"Cannot undo directory creation: {snapshot['file']} not empty")
                        break
            elif snapshot["is_delete"]:
                # Was a directory deletion, restore it
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.mkdir(parents=True, exist_ok=True)
                results.append(f"Undid deletion of directory {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], _ctx)
        else:
            # File operations
            if snapshot["is_new"]:
                # Was a new file, delete it
                if file_path.exists():
                    file_path.unlink()
                    results.append(f"Undid creation of {snapshot['file']}")
                    _publish_standalone_undo_event("undo", snapshot["file"], _ctx)
            elif snapshot["is_delete"]:
                # Was a deletion, restore file
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid deletion of {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], _ctx)
            else:
                # Was a modification, restore old content
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(snapshot["old_content"] or "")
                results.append(f"Undid modification of {snapshot['file']}")
                _publish_standalone_undo_event("undo", snapshot["file"], _ctx)
        
        # Move to redo_dir
        shutil.move(str(latest), str(redo_dir / latest.name))
        warning = _mark_index_stale(_ctx, [snapshot["file"]])
        if warning:
            results.append(warning.strip())
    
    return "\n".join(results) if results else "Nothing to undo."


def _snapshot_sort_key(path: Path) -> tuple[float, str]:
    with path.open(encoding="utf-8") as file:
        snapshot = json.load(file)
    return float(snapshot.get("timestamp", 0)), path.name


def _move_journaled_snapshot(ctx: Any, snapshot: dict, source: Path, destination: Path, redo: bool) -> str:
    result = _apply_journaled_snapshot(ctx, snapshot, redo=redo)
    if result.startswith("ERROR"):
        return result
    try:
        shutil.move(str(source), str(destination / source.name))
    except OSError as exc:
        rollback = _apply_journaled_snapshot(ctx, snapshot, redo=not redo)
        if rollback.startswith("ERROR"):
            return f"ERROR[nagato_undo_standalone]: History move failed: {exc}; rollback failed: {rollback}"
        return f"ERROR[nagato_undo_standalone]: History move failed: {exc}; action rolled back."
    from nagato_tools.action_journal import ActionJournal, FileEffect

    session_id = getattr(ctx, "session_id", "standalone")
    workspace_root = Path(ctx.workspace_root)
    journal = ActionJournal(workspace_root / ".nagato" / "sessions" / session_id / "action_journal",
                            workspace_root, session_id=session_id)
    manifest = journal.load_manifest(snapshot["action_step"])
    if manifest is None:
        return result + "\nWARNING: Could not mark the semantic index stale: action history is missing."
    paths = [effect.file_path for effect in manifest.effects
             if isinstance(effect, FileEffect) and effect.namespace == "workspace" and not effect.is_directory]
    return result + _mark_index_stale(ctx, paths)


def _mark_index_stale(ctx: Any, paths: list[str]) -> str:
    try:
        from nagato_tools.semanticindex import mark_semantic_files_stale
        mark_semantic_files_stale(paths, ctx)
    except Exception as exc:
        return f"\nWARNING: Could not mark the semantic index stale: {exc}"
    return ""


def _apply_journaled_snapshot(ctx: Any, snapshot: dict, redo: bool) -> str:
    from nagato_tools.action_journal import ActionJournal, UndoConflictError

    session_id = getattr(ctx, "session_id", "standalone")
    workspace_root = Path(ctx.workspace_root)
    journal_root = workspace_root / ".nagato" / "sessions" / session_id / "action_journal"
    journal = ActionJournal(journal_root, workspace_root, session_id=session_id)
    step = snapshot["action_step"]
    manifest = journal.load_manifest(step)
    pointer = journal._read_pointer()
    expected_step = step - 1 if redo else step
    if manifest is None or manifest.action_id != snapshot["action_id"]:
        return "ERROR: Undo action history is missing or does not match the snapshot."
    if pointer["current_step"] != expected_step:
        return "ERROR: Undo action history does not match the current snapshot."
    try:
        _validate_journaled_snapshot(manifest, workspace_root, session_id, redo)
        if redo:
            journal.redo_to_step(step)
            return f"Redo: Applied change to {snapshot['file']}"
        journal.undo_to_step(step - 1)
        return f"Undid action affecting {snapshot['file']}"
    except (UndoConflictError, OSError) as exc:
        return f"ERROR[nagato_undo_standalone]: {exc}"


def _validate_journaled_snapshot(manifest: Any, workspace_root: Path, session_id: str, redo: bool) -> None:
    from nagato_tools.action_journal import FileEffect, SqliteRowEffect, UndoConflictError, _compute_sha256

    file_effects = [effect for effect in manifest.effects if isinstance(effect, FileEffect)]
    removed_paths = {
        effect._resolve_target(workspace_root, session_id)
        for effect in file_effects
        if (effect.after_exists if redo else effect.before_exists) is False
    }
    for effect in file_effects:
        target = effect._resolve_target(workspace_root, session_id)
        expected_exists = effect.before_exists if redo else effect.after_exists
        expected_hash = effect.before_hash if redo else effect.after_hash
        if target.exists() != expected_exists:
            raise UndoConflictError(f"{effect.file_path} existence changed since the action.")
        if expected_exists:
            if target.is_dir() != effect.is_directory:
                raise UndoConflictError(f"{effect.file_path} changed type since the action.")
            if effect.is_directory and target in removed_paths:
                if any(child not in removed_paths for child in target.rglob("*")):
                    raise UndoConflictError(f"{effect.file_path} contains changes outside this action.")
            elif not effect.is_directory and expected_hash:
                with target.open("r", encoding="utf-8", newline="") as file:
                    if _compute_sha256(file.read()) != expected_hash:
                        raise UndoConflictError(f"{effect.file_path} content changed since the action.")

    row_effects = [effect for effect in manifest.effects if isinstance(effect, SqliteRowEffect)]
    row_changes = {}
    for effect in row_effects:
        key = (effect.db_path, effect.table, tuple(effect.pk_values.items()))
        if key not in row_changes:
            row_changes[key] = [effect.before_row, effect.after_row]
        else:
            row_changes[key][1] = effect.after_row
    removed_rows = {key for key, (before, after) in row_changes.items()
                    if (before if redo else after) is not None and (after if redo else before) is None}
    checked = set()
    for effect in row_effects if redo else reversed(row_effects):
        key = (effect.db_path, effect.table, tuple(effect.pk_values.items()))
        if key in checked:
            continue
        checked.add(key)
        connection = effect._get_connection(workspace_root)
        try:
            current = effect._fetch_current_row(connection)
            expected = effect.before_row if redo else effect.after_row
            if not effect._rows_match(current, expected):
                raise UndoConflictError(f"Graph row {effect.table} {effect.pk_values} changed since the action.")
            if effect.table == "memory_nodes" and key in removed_rows:
                recorded_edges = {
                    dict(row_key[2])["id"] for row_key in removed_rows
                    if row_key[0] == effect.db_path and row_key[1] == "memory_edges"
                }
                node_id = effect.pk_values["id"]
                edges = connection.execute(
                    "SELECT id FROM memory_edges WHERE source_id = ? OR target_id = ?", (node_id, node_id)
                ).fetchall()
                if any(edge["id"] not in recorded_edges for edge in edges):
                    raise UndoConflictError(f"Graph node {node_id} has links outside this action.")
            if effect.table == "blocks" and key in removed_rows:
                for table in ("block_nodes", "block_edges"):
                    recorded = {dict(row_key[2])["rowid"] for row_key in removed_rows
                                if row_key[0] == effect.db_path and row_key[1] == table and "rowid" in dict(row_key[2])}
                    rows = connection.execute(f"SELECT rowid FROM {table} WHERE block_id = ?", (effect.pk_values["id"],)).fetchall()
                    if any(row["rowid"] not in recorded for row in rows):
                        raise UndoConflictError(f"Block {effect.pk_values['id']} has changes outside this action.")
        finally:
            connection.close()


async def nagato_undo_standalone(
    _ctx: Any,
    mode: str = "step",
    value: Union[int, float, str] = 1,
) -> str:
    """
    Public entry point for standalone undo/redo.
    
    Args:
        _ctx: MockFSMContext or similar
        mode: "step" (default), "redo"
        value: Number of steps for "step" mode
        
    Returns:
        Status message
    """
    return await _standalone_undo(_ctx, mode, value)


def get_standalone_undo_status(_ctx: Any) -> "UndoStatus":
    """
    Get the current undo/redo status for standalone mode.
    
    Args:
        _ctx: MockFSMContext or similar
        
    Returns:
        UndoStatus object with current undo/redo state
    """
    from nagato_tools.telemetry import UndoStatus, UndoStepSummary
    import json
    
    undo_dir = _get_standalone_undo_dir(_ctx)
    redo_dir = _get_standalone_redo_dir(_ctx)
    
    snapshots = sorted(undo_dir.glob("snapshot_*.json"), key=_snapshot_sort_key)
    redo_snapshots = sorted(redo_dir.glob("snapshot_*.json"), key=_snapshot_sort_key)
    
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
        session_id=getattr(_ctx, "session_id", "standalone"),
        current_step=len(snapshots),
        max_step=len(snapshots),
        can_undo=len(snapshots) > 0,
        can_redo=len(redo_snapshots) > 0,
        available_undo_steps=len(snapshots),
        available_redo_steps=len(redo_snapshots),
        steps=steps,
    )
__all__ = ['nagato_undo_standalone']
