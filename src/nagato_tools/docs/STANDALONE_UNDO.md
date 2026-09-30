# Undo & Redo System

The Nagato Tools suite provides a dedicated, lightweight, persistent version-tracking system (`nagato_undo_standalone` and `_track_edit_for_undo`) designed to operate reliably across editing workflows.

---

## ⏪ Session & Workspace Isolation

The undo system maintains **isolated undo/redo caches per `(workspace, session_id)` pair**. This enables safe concurrent usage by multiple agents:

| Configuration | Session ID | Undo Cache Location | Redo Cache Location |
|---------------|------------|---------------------|---------------------|
| Default (no session_id) | Auto-generated from workspace hash (e.g., `ws_a1b2c3d4`) | `.nagato/sessions/ws_a1b2c3d4/undo_cache/` | `.nagato/sessions/ws_a1b2c3d4/redo_cache/` |
| `NAGATO_SESSION_ID=proj:my-app` | `proj_my-app` (sanitized) | `.nagato/sessions/proj_my-app/undo_cache/` | `.nagato/sessions/proj_my-app/redo_cache/` |
| Explicit `session_id="standalone"` | `standalone` | `.nagato/undo_cache/` | `.nagato/redo_cache/` |

### Concurrency Model

- **Different workspaces**: Naturally isolated by different workspace roots
- **Same workspace, different session_ids**: Isolated via `.nagato/sessions/<session_id>/` — each agent gets its own undo stream
- **Same workspace, same session_id**: Share undo cache (intentional for collaborative editing)

---

## 1. What It Can Do

The Undo system tracks file modifications, file creations, file renames, and file deletions:
- **Reverts modifications**: Restores previous file content after `nagato_edit` or `nagato_edit_lines`.
- **Reverts creations**: Deletes newly created files if the creation step was undone.
- **Reverts deletions**: Restores deleted files and their contents after `nagato_delete`.
- **Multi-step Undo**: Roll back $N$ steps at once.
- **Redo Support**: Re-apply undone changes.
- **Cross-process Persistence**: Snapshot and action journal history is written directly to disk under `.nagato/undo_cache/`, `.nagato/redo_cache/`, and `.nagato/sessions/<session_id>/action_journal/`.

---

## 2. Architecture & Storage

The system operates via a disk-backed file-snapshot stack, scoped to the session:

```
Workspace Root/
  └── .nagato/
        ├── sessions/
        │     └── <session_id>/
        │           ├── undo_cache/
        │           │     ├── snapshot_1700000001000.json
        │           │     └── snapshot_1700000002000.json  <-- Latest edit
        │           └── redo_cache/
        │                 └── snapshot_1700000003000.json  <-- Moved here upon undo
        └── (legacy standalone session)
              ├── undo_cache/
              └── redo_cache/
```

For the default `standalone` session (or when explicitly set), the legacy flat structure is used:
```
Workspace Root/
  └── .nagato/
        ├── undo_cache/
        └── redo_cache/
```

### Snapshot Structure
Each change creates a JSON snapshot containing:
```json
{
  "file": "src/module/example.py",
  "timestamp": 1700000002.5,
  "is_new": false,
  "is_delete": false,
  "old_content": "... content before edit ...",
  "new_content": "... content after edit ..."
}
```

### Snapshot Retention Limit
To prevent uncontrolled disk growth, `_track_edit_for_undo()` automatically maintains a rolling window of the **last 50 snapshots**, deleting older snapshots automatically.

---

## 3. How It Works

### Automatic Tracking on Edit Operations
When modifying operations execute:
1. Before applying an edit or deletion, the tool captures the current file content.
2. After writing the change, `_track_edit_for_undo(ctx, file_path, old_content, new_content, is_new, is_delete)` writes a timestamped snapshot into `.nagato/undo_cache/`.

### Undo Execution (`mode="step"`, `value=N`)
1. Reads the latest $N$ snapshots from `.nagato/undo_cache/` in reverse chronological order.
2. For each snapshot:
   - If `is_new == True`: Deletes the created file from disk.
   - If `is_delete == True`: Writes `old_content` back to recreate the deleted file.
   - If modification: Writes `old_content` back to the file.
3. Moves processed snapshots to `.nagato/redo_cache/` so they can be re-applied if needed.

### Redo Execution (`mode="redo"`)
1. Reads the latest snapshot from `.nagato/redo_cache/`.
2. Restores the modified version or re-applies the deletion/creation:
   - If `is_new == True`: Deletes file.
   - If `is_delete == True`: Restores file.
   - If modification: Writes `new_content` back to disk.
3. Moves the snapshot back into `.nagato/undo_cache/`.

---

## 4. Usage Examples

### Python API Call
```python
import asyncio
from nagato_tools import nagato_undo_standalone, nagato_edit

# 1. Edit a file
asyncio.run(nagato_edit("hello.py", "", "print('Hello world!')"))

# 2. Undo the last change (1 step)
result = asyncio.run(nagato_undo_standalone(mode="step", value=1))
# Result: "Undid creation of hello.py"

# 3. Redo the change
result = asyncio.run(nagato_undo_standalone(mode="redo"))
# Result: "Redo: Restored hello.py to modified version"
```

### Multiple Steps Undo
```python
# Undo 3 consecutive modifications
result = asyncio.run(nagato_undo_standalone(mode="step", value=3))
```
