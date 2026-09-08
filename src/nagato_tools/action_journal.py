"""
Portable Action Journal and Reversible Effects Core.

Provides unified, deterministic, cross-domain forward/backward execution for
files, FSM state, and SQLite knowledge graph / block rows.

Usable in both FSM mode and Standalone mode (portable to MCP package).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class ActionState(str, Enum):
    PREPARING = "PREPARING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    UNDONE = "UNDONE"
    RECOVERED_ABORT = "RECOVERED_ABORT"


class EffectType(str, Enum):
    FILE = "FILE"
    FSM_STATE = "FSM_STATE"
    SQLITE_ROW = "SQLITE_ROW"
    CUSTOM = "CUSTOM"


class UndoConflictError(RuntimeError):
    """Raised when live state diverges from the expected state during undo/redo."""
    pass


# ---------------------------------------------------------------------------
# Value serialization helpers for SQLite (JSON-safe, BLOB & Vector tagged)
# ---------------------------------------------------------------------------

def _encode_sqlite_value(val: Any) -> Any:
    """Encode an SQLite column value for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, dict):
        if "__blob_b64__" in val or "__json__" in val:
            return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        return {"__blob_b64__": base64.b64encode(bytes(val)).decode("ascii")}
    if isinstance(val, (int, float, str, bool)):
        return val
    # Fallback to string or JSON
    try:
        return {"__json__": json.loads(val)}
    except Exception:
        return str(val)


def _decode_sqlite_value(val: Any) -> Any:
    """Decode an SQLite column value from JSON serialization."""
    if val is None:
        return None
    if isinstance(val, dict):
        if "__blob_b64__" in val:
            return base64.b64decode(val["__blob_b64__"].encode("ascii"))
        if "__json__" in val:
            return json.dumps(val["__json__"])
    return val


def _normalize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: _encode_sqlite_value(v) for k, v in row.items()}


def _denormalize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: _decode_sqlite_value(v) for k, v in row.items()}


def _compute_sha256(data: Union[str, bytes]) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _emit_telemetry(
    session_id: str,
    event_type: str,
    action_id: Optional[str] = None,
    step: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    workspace_root: Optional[Path] = None,
    error: Optional[str] = None,
) -> None:
    try:
        from nagato_tools.telemetry import AuditEvent, EventType, TelemetryMode
        from nagato_tools.telemetry import publish_event
        mode = TelemetryMode.STANDALONE if session_id == "standalone" else TelemetryMode.FSM
        publish_event(
            AuditEvent(
                session_id=session_id,
                mode=mode,
                event_type=EventType(event_type),
                action_id=action_id,
                step=step,
                payload=payload or {},
                error=error,
            ),
            workspace_root=workspace_root,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reversible Effects
# ---------------------------------------------------------------------------

class FileEffect:
    """Reversible effect on a file in the workspace or session."""

    effect_type: str = EffectType.FILE.value

    def __init__(
        self,
        file_path: str,
        namespace: str = "workspace",
        before_exists: bool = False,
        before_content: Optional[str] = None,
        before_hash: Optional[str] = None,
        after_exists: bool = False,
        after_content: Optional[str] = None,
        after_hash: Optional[str] = None,
    ):
        self.file_path = file_path.replace("\\", "/")
        self.namespace = namespace
        self.before_exists = before_exists
        self.before_content = before_content
        self.before_hash = before_hash or (_compute_sha256(before_content) if before_content is not None else None)
        self.after_exists = after_exists
        self.after_content = after_content
        self.after_hash = after_hash or (_compute_sha256(after_content) if after_content is not None else None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "file_path": self.file_path,
            "namespace": self.namespace,
            "before_exists": self.before_exists,
            "before_content": self.before_content,
            "before_hash": self.before_hash,
            "after_exists": self.after_exists,
            "after_content": self.after_content,
            "after_hash": self.after_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FileEffect:
        return cls(
            file_path=data["file_path"],
            namespace=data.get("namespace", "workspace"),
            before_exists=data.get("before_exists", False),
            before_content=data.get("before_content"),
            before_hash=data.get("before_hash"),
            after_exists=data.get("after_exists", False),
            after_content=data.get("after_content"),
            after_hash=data.get("after_hash"),
        )

    def _resolve_target(self, workspace_root: Path, session_id: Optional[str] = None) -> Path:
        if self.namespace == "session" and session_id:
            return workspace_root / ".nagato" / "sessions" / session_id / self.file_path
        return workspace_root / self.file_path

    def apply_undo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        target = self._resolve_target(workspace_root, session_id)
        # Check conflict against after state
        if self.after_exists:
            if not target.exists():
                raise UndoConflictError(f"UNDO_CONFLICT: File {self.file_path} expected to exist for undo, but not found.")
            current_content = target.read_text(encoding="utf-8", errors="replace")
            current_hash = _compute_sha256(current_content)
            if self.after_hash and current_hash != self.after_hash:
                raise UndoConflictError(f"UNDO_CONFLICT: File {self.file_path} content hash mismatch before undo.")
        else:
            if target.exists():
                raise UndoConflictError(f"UNDO_CONFLICT: File {self.file_path} expected not to exist, but found on disk.")

        # Revert to before state
        if self.before_exists:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.before_content or "", encoding="utf-8", newline="")
        else:
            if target.exists():
                target.unlink()

    def apply_redo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        target = self._resolve_target(workspace_root, session_id)
        # Check conflict against before state
        if self.before_exists:
            if not target.exists():
                raise UndoConflictError(f"REDO_CONFLICT: File {self.file_path} expected to exist for redo, but not found.")
            current_content = target.read_text(encoding="utf-8", errors="replace")
            current_hash = _compute_sha256(current_content)
            if self.before_hash and current_hash != self.before_hash:
                raise UndoConflictError(f"REDO_CONFLICT: File {self.file_path} content hash mismatch before redo.")
        else:
            if target.exists():
                raise UndoConflictError(f"REDO_CONFLICT: File {self.file_path} expected not to exist, but found on disk.")

        # Reapply after state
        if self.after_exists:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.after_content or "", encoding="utf-8", newline="")
        else:
            if target.exists():
                target.unlink()


class FsmStateEffect:
    """Reversible effect on the FSM state JSON file."""

    effect_type: str = EffectType.FSM_STATE.value

    def __init__(
        self,
        session_id: str,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
    ):
        self.session_id = session_id
        self.before_state = before_state
        self.after_state = after_state

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "session_id": self.session_id,
            "before_state": self.before_state,
            "after_state": self.after_state,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FsmStateEffect:
        return cls(
            session_id=data["session_id"],
            before_state=data.get("before_state"),
            after_state=data.get("after_state"),
        )

    def _resolve_state_file(self, workspace_root: Path) -> Path:
        try:
            from fsm.session import get_state_file_path  # host-only
        except (ImportError, Exception):
            get_state_file_path = None  # type: ignore[misc]  # fsm-only; standalone gets None
        return get_state_file_path(self.session_id)

    def apply_undo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        if not self.before_state:
            return
        try:
            state_file = self._resolve_state_file(workspace_root)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(self.before_state, encoding="utf-8")
        except Exception:
            pass

    def apply_redo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        if not self.after_state:
            return
        try:
            state_file = self._resolve_state_file(workspace_root)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(self.after_state, encoding="utf-8")
        except Exception:
            pass


class SqliteRowEffect:
    """Reversible effect on an SQLite table row (e.g. InsightStore or BlockStore)."""

    effect_type: str = EffectType.SQLITE_ROW.value

    def __init__(
        self,
        db_path: str,
        table: str,
        pk_columns: List[str],
        pk_values: Dict[str, Any],
        op: str,  # "INSERT", "UPDATE", "DELETE"
        before_row: Optional[Dict[str, Any]] = None,
        after_row: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path.replace("\\", "/")
        self.table = table
        self.pk_columns = pk_columns
        self.pk_values = pk_values
        self.op = op.upper()
        self.before_row = _normalize_row(before_row)
        self.after_row = _normalize_row(after_row)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "db_path": self.db_path,
            "table": self.table,
            "pk_columns": self.pk_columns,
            "pk_values": {k: _encode_sqlite_value(v) for k, v in self.pk_values.items()},
            "op": self.op,
            "before_row": self.before_row,
            "after_row": self.after_row,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SqliteRowEffect:
        return cls(
            db_path=data["db_path"],
            table=data["table"],
            pk_columns=data["pk_columns"],
            pk_values={k: _decode_sqlite_value(v) for k, v in data["pk_values"].items()},
            op=data["op"],
            before_row=data.get("before_row"),
            after_row=data.get("after_row"),
        )

    def _get_connection(self, workspace_root: Path) -> sqlite3.Connection:
        db_file = Path(self.db_path)
        if not db_file.is_absolute():
            db_file = workspace_root / db_file
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_current_row(self, conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        where_clauses = [f"{col} = ?" for col in self.pk_columns]
        sql = f"SELECT * FROM {self.table} WHERE {' AND '.join(where_clauses)}"
        params = [self.pk_values[col] for col in self.pk_columns]
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def _rows_match(self, live: Optional[Dict[str, Any]], expected: Optional[Dict[str, Any]]) -> bool:
        if live is None and expected is None:
            return True
        if live is None or expected is None:
            return False
        # Normalize and compare
        norm_live = _normalize_row(live)
        norm_exp = expected  # already normalized
        for k, v in norm_exp.items():
            if k not in norm_live or norm_live[k] != v:
                return False
        return True

    def apply_undo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        conn = self._get_connection(workspace_root)
        try:
            with conn:
                live_row = self._fetch_current_row(conn)
                # Check conflict against after_row
                if self.op in ("INSERT", "UPDATE"):
                    if not self._rows_match(live_row, self.after_row):
                        raise UndoConflictError(
                            f"UNDO_CONFLICT: Table {self.table} PK {self.pk_values} row does not match expected state for undo."
                        )
                elif self.op == "DELETE":
                    if live_row is not None:
                        raise UndoConflictError(
                            f"UNDO_CONFLICT: Table {self.table} PK {self.pk_values} expected deleted, but found row in DB."
                        )

                # Apply inverse operation
                if self.op == "INSERT":
                    # Undo of INSERT is DELETE
                    where_clauses = [f"{col} = ?" for col in self.pk_columns]
                    sql = f"DELETE FROM {self.table} WHERE {' AND '.join(where_clauses)}"
                    params = [self.pk_values[col] for col in self.pk_columns]
                    conn.execute(sql, params)
                elif self.op == "UPDATE":
                    # Undo of UPDATE restores before_row
                    denorm_before = _denormalize_row(self.before_row) or {}
                    set_cols = [k for k in denorm_before.keys() if k not in self.pk_columns]
                    if set_cols:
                        set_clauses = [f"{col} = ?" for col in set_cols]
                        where_clauses = [f"{col} = ?" for col in self.pk_columns]
                        sql = f"UPDATE {self.table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
                        params = [denorm_before[col] for col in set_cols] + [self.pk_values[col] for col in self.pk_columns]
                        conn.execute(sql, params)
                elif self.op == "DELETE":
                    # Undo of DELETE re-inserts before_row
                    denorm_before = _denormalize_row(self.before_row) or {}
                    cols = list(denorm_before.keys())
                    placeholders = ["?"] * len(cols)
                    sql = f"INSERT OR REPLACE INTO {self.table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
                    params = [denorm_before[col] for col in cols]
                    conn.execute(sql, params)
        finally:
            conn.close()

    def apply_redo(self, workspace_root: Path, session_id: Optional[str] = None) -> None:
        conn = self._get_connection(workspace_root)
        try:
            with conn:
                live_row = self._fetch_current_row(conn)
                # Check conflict against before_row
                if self.op in ("UPDATE", "DELETE"):
                    if not self._rows_match(live_row, self.before_row):
                        raise UndoConflictError(
                            f"REDO_CONFLICT: Table {self.table} PK {self.pk_values} row does not match expected state for redo."
                        )
                elif self.op == "INSERT":
                    if live_row is not None and not self._rows_match(live_row, self.before_row):
                        raise UndoConflictError(
                            f"REDO_CONFLICT: Table {self.table} PK {self.pk_values} unexpected existing row before redo."
                        )

                # Reapply forward operation
                if self.op == "INSERT":
                    denorm_after = _denormalize_row(self.after_row) or {}
                    cols = list(denorm_after.keys())
                    placeholders = ["?"] * len(cols)
                    sql = f"INSERT OR REPLACE INTO {self.table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
                    params = [denorm_after[col] for col in cols]
                    conn.execute(sql, params)
                elif self.op == "UPDATE":
                    denorm_after = _denormalize_row(self.after_row) or {}
                    set_cols = [k for k in denorm_after.keys() if k not in self.pk_columns]
                    if set_cols:
                        set_clauses = [f"{col} = ?" for col in set_cols]
                        where_clauses = [f"{col} = ?" for col in self.pk_columns]
                        sql = f"UPDATE {self.table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
                        params = [denorm_after[col] for col in set_cols] + [self.pk_values[col] for col in self.pk_columns]
                        conn.execute(sql, params)
                elif self.op == "DELETE":
                    where_clauses = [f"{col} = ?" for col in self.pk_columns]
                    sql = f"DELETE FROM {self.table} WHERE {' AND '.join(where_clauses)}"
                    params = [self.pk_values[col] for col in self.pk_columns]
                    conn.execute(sql, params)
        finally:
            conn.close()


def deserialize_effect(data: Dict[str, Any]) -> Any:
    etype = data.get("effect_type")
    if etype == EffectType.FILE.value:
        return FileEffect.from_dict(data)
    elif etype == EffectType.FSM_STATE.value:
        return FsmStateEffect.from_dict(data)
    elif etype == EffectType.SQLITE_ROW.value:
        return SqliteRowEffect.from_dict(data)
    else:
        raise ValueError(f"Unknown effect type: {etype}")


# ---------------------------------------------------------------------------
# Action Manifest
# ---------------------------------------------------------------------------

class ActionManifest:
    """Atomic description of a single agent step with all reversible effects."""

    def __init__(
        self,
        step_id: int,
        action_id: str,
        session_id: str,
        command: str,
        args_preview: str = "",
        state: str = ActionState.PREPARING.value,
        state_before: Optional[str] = None,
        state_after: Optional[str] = None,
        created_at: Optional[str] = None,
        committed_at: Optional[str] = None,
        effects: Optional[List[Any]] = None,
        schema_version: int = 2,
    ):
        self.schema_version = schema_version
        self.step_id = step_id
        self.action_id = action_id
        self.session_id = session_id
        self.command = command
        self.args_preview = args_preview
        self.state = state
        self.state_before = state_before
        self.state_after = state_after
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.committed_at = committed_at
        self.effects: List[Any] = effects or []

    def compute_hash(self) -> str:
        payload = {
            "step_id": self.step_id,
            "action_id": self.action_id,
            "session_id": self.session_id,
            "command": self.command,
            "args_preview": self.args_preview,
            "state": self.state,
            "effects": [e.to_dict() if hasattr(e, "to_dict") else e for e in self.effects],
        }
        return _compute_sha256(json.dumps(payload, sort_keys=True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "step_id": self.step_id,
            "action_id": self.action_id,
            "session_id": self.session_id,
            "command": self.command,
            "args_preview": self.args_preview,
            "state": self.state,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "effects": [e.to_dict() if hasattr(e, "to_dict") else e for e in self.effects],
            "manifest_hash": self.compute_hash(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActionManifest:
        raw_effects = data.get("effects", [])
        parsed_effects = [deserialize_effect(e) if isinstance(e, dict) else e for e in raw_effects]
        return cls(
            step_id=data["step_id"],
            action_id=data["action_id"],
            session_id=data["session_id"],
            command=data.get("command", ""),
            args_preview=data.get("args_preview", ""),
            state=data.get("state", ActionState.PREPARING.value),
            state_before=data.get("state_before"),
            state_after=data.get("state_after"),
            created_at=data.get("created_at"),
            committed_at=data.get("committed_at"),
            effects=parsed_effects,
            schema_version=data.get("schema_version", 2),
        )


# ---------------------------------------------------------------------------
# Unit of Work / Action Context
# ---------------------------------------------------------------------------

class ActionContext:
    """Active transaction context for recording effects in a single action."""

    def __init__(self, journal: ActionJournal, manifest: ActionManifest):
        self.journal = journal
        self.manifest = manifest
        self._is_finished = False

    @property
    def action_id(self) -> str:
        return self.manifest.action_id

    @property
    def step_id(self) -> int:
        return self.manifest.step_id

    def add_effect(self, effect: Any) -> None:
        if self._is_finished:
            raise RuntimeError("Cannot add effect to finished action context.")
        self.manifest.effects.append(effect)
        # Flush intermediate manifest to disk
        self.journal._write_manifest(self.manifest)

    def record_file_change(
        self,
        file_path: str,
        before_content: Optional[str] = None,
        after_content: Optional[str] = None,
        is_new: bool = False,
        is_delete: bool = False,
        namespace: str = "workspace",
    ) -> None:
        effect = FileEffect(
            file_path=file_path,
            namespace=namespace,
            before_exists=not is_new and before_content is not None,
            before_content=before_content,
            after_exists=not is_delete and after_content is not None,
            after_content=after_content,
        )
        self.add_effect(effect)

    def record_fsm_state(
        self,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
    ) -> None:
        effect = FsmStateEffect(
            session_id=self.manifest.session_id,
            before_state=before_state,
            after_state=after_state,
        )
        self.add_effect(effect)

    def record_sqlite_row(
        self,
        db_path: str,
        table: str,
        pk_columns: List[str],
        pk_values: Dict[str, Any],
        op: str,
        before_row: Optional[Dict[str, Any]] = None,
        after_row: Optional[Dict[str, Any]] = None,
    ) -> None:
        effect = SqliteRowEffect(
            db_path=db_path,
            table=table,
            pk_columns=pk_columns,
            pk_values=pk_values,
            op=op,
            before_row=before_row,
            after_row=after_row,
        )
        self.add_effect(effect)

    def commit(self, state_after: Optional[str] = None) -> None:
        if self._is_finished:
            return
        if state_after:
            self.manifest.state_after = state_after
        self.manifest.state = ActionState.COMMITTED.value
        self.manifest.committed_at = datetime.now(timezone.utc).isoformat()
        self.journal._commit_action(self.manifest)
        self._is_finished = True

    def abort(self, reason: Optional[str] = None) -> None:
        if self._is_finished:
            return
        # Compensate any recorded effects in reverse order
        self.journal._compensate_effects(self.manifest.effects)
        self.manifest.state = ActionState.ABORTED.value
        self.journal._write_manifest(self.manifest)
        _emit_telemetry(
            self.manifest.session_id,
            "ACTION_ABORTED",
            action_id=self.manifest.action_id,
            step=self.manifest.step_id,
            payload={"reason": reason, "command": self.manifest.command},
            workspace_root=self.journal.workspace_root,
        )
        self._is_finished = True


# ---------------------------------------------------------------------------
# Action Journal Manager
# ---------------------------------------------------------------------------

class ActionJournal:
    """Manages ordered actions, atomic persistence, and deterministic undo/redo walks."""

    def __init__(
        self,
        journal_root: Path,
        workspace_root: Path,
        session_id: str = "standalone",
    ):
        self.journal_root = Path(journal_root)
        self.workspace_root = Path(workspace_root)
        self.session_id = session_id
        self.journal_root.mkdir(parents=True, exist_ok=True)
        self._pointer_file = self.journal_root / "pointer.json"
        self._index_file = self.journal_root / "index.jsonl"
        self._steps_dir = self.journal_root / "steps"
        self._steps_dir.mkdir(parents=True, exist_ok=True)

    def _read_pointer(self) -> Dict[str, Any]:
        if not self._pointer_file.exists():
            return {"schema_version": 2, "current_step": 0, "max_step": 0, "checkpoints": []}
        try:
            data = json.loads(self._pointer_file.read_text(encoding="utf-8"))
            data.setdefault("schema_version", 2)
            data.setdefault("current_step", 0)
            data.setdefault("max_step", 0)
            data.setdefault("checkpoints", [])
            return data
        except Exception:
            return {"schema_version": 2, "current_step": 0, "max_step": 0, "checkpoints": []}

    def _write_pointer(self, current_step: int, max_step: int, checkpoints: Optional[List[int]] = None) -> None:
        data = self._read_pointer()
        data["current_step"] = current_step
        data["max_step"] = max_step
        if checkpoints is not None:
            data["checkpoints"] = checkpoints
        temp_file = self._pointer_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_file.replace(self._pointer_file)

    def _step_dir(self, step_id: int) -> Path:
        d = self._steps_dir / str(step_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, step_id: int) -> Path:
        return self._step_dir(step_id) / "action.json"

    def _write_manifest(self, manifest: ActionManifest) -> None:
        path = self._manifest_path(manifest.step_id)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)

    def load_manifest(self, step_id: int) -> Optional[ActionManifest]:
        path = self._manifest_path(step_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ActionManifest.from_dict(data)
        except Exception:
            return None

    def _append_index(self, manifest: ActionManifest) -> None:
        entry = {
            "step_id": manifest.step_id,
            "action_id": manifest.action_id,
            "session_id": manifest.session_id,
            "command": manifest.command,
            "args_preview": manifest.args_preview,
            "state_before": manifest.state_before,
            "state_after": manifest.state_after,
            "state": manifest.state,
            "created_at": manifest.created_at,
            "committed_at": manifest.committed_at,
            "effects_count": len(manifest.effects),
            "manifest_hash": manifest.compute_hash(),
        }
        with open(self._index_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _compensate_effects(self, effects: List[Any]) -> None:
        """Apply inverse of effects in reverse order."""
        for effect in reversed(effects):
            if hasattr(effect, "apply_undo"):
                try:
                    effect.apply_undo(self.workspace_root, self.session_id)
                except Exception:
                    pass

    def begin_action(
        self,
        command: str,
        args_preview: str = "",
        state_before: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> ActionContext:
        """Begin a new action transaction."""
        pointer = self._read_pointer()
        step_id = pointer["max_step"] + 1
        aid = action_id or str(uuid.uuid4())
        manifest = ActionManifest(
            step_id=step_id,
            action_id=aid,
            session_id=self.session_id,
            command=command,
            args_preview=args_preview,
            state=ActionState.PREPARING.value,
            state_before=state_before,
        )
        self._write_manifest(manifest)
        return ActionContext(self, manifest)

    def _commit_action(self, manifest: ActionManifest) -> None:
        self._write_manifest(manifest)
        self._append_index(manifest)
        # Linear redo: advance both current_step and max_step to this step_id
        self._write_pointer(current_step=manifest.step_id, max_step=manifest.step_id)
        _emit_telemetry(
            manifest.session_id,
            "ACTION_COMMITTED",
            action_id=manifest.action_id,
            step=manifest.step_id,
            payload={
                "command": manifest.command,
                "effects_count": len(manifest.effects),
                "state_before": manifest.state_before,
                "state_after": manifest.state_after,
            },
            workspace_root=self.workspace_root,
        )

    def undo_to_step(self, target_step: int) -> Tuple[bool, str]:
        """Undo backward from current_step to target_step (applying effects in reverse)."""
        pointer = self._read_pointer()
        current_step = pointer["current_step"]
        if target_step >= current_step:
            return True, f"Already at or before step {target_step}."
        if target_step < 0:
            target_step = 0

        # Walk backward from current_step down to target_step + 1
        undone_count = 0
        for step in range(current_step, target_step, -1):
            manifest = self.load_manifest(step)
            if manifest is not None and manifest.state in (ActionState.COMMITTED.value, ActionState.UNDONE.value):
                # Apply undo for all effects in reverse order
                for effect in reversed(manifest.effects):
                    if hasattr(effect, "apply_undo"):
                        effect.apply_undo(self.workspace_root, self.session_id)
                manifest.state = ActionState.UNDONE.value
                self._write_manifest(manifest)
                undone_count += 1

        self._write_pointer(current_step=target_step, max_step=pointer["max_step"])
        _emit_telemetry(
            self.session_id,
            "ACTION_UNDONE",
            step=target_step,
            payload={"from_step": current_step, "to_step": target_step, "undone_count": undone_count},
            workspace_root=self.workspace_root,
        )
        return True, f"Undid {undone_count} step(s) to step {target_step}."

    def redo_to_step(self, target_step: int) -> Tuple[bool, str]:
        """Redo forward from current_step up to target_step (applying effects forward)."""
        pointer = self._read_pointer()
        current_step = pointer["current_step"]
        max_step = pointer["max_step"]
        if target_step <= current_step:
            return True, f"Already at or after step {target_step}."
        if target_step > max_step:
            target_step = max_step

        redone_count = 0
        for step in range(current_step + 1, target_step + 1):
            manifest = self.load_manifest(step)
            if manifest is not None:
                # Apply redo for all effects in forward order
                for effect in manifest.effects:
                    if hasattr(effect, "apply_redo"):
                        effect.apply_redo(self.workspace_root, self.session_id)
                manifest.state = ActionState.COMMITTED.value
                self._write_manifest(manifest)
                redone_count += 1

        self._write_pointer(current_step=target_step, max_step=max_step)
        _emit_telemetry(
            self.session_id,
            "ACTION_REDONE",
            step=target_step,
            payload={"from_step": current_step, "to_step": target_step, "redone_count": redone_count},
            workspace_root=self.workspace_root,
        )
        return True, f"Redid {redone_count} step(s) to step {target_step}."

    def recover_stale_actions(self) -> int:
        """Scan for uncommitted PREPARING actions, roll them back, and mark RECOVERED_ABORT."""
        recovered = 0
        if not self._steps_dir.exists():
            return 0
        for step_dir in sorted(self._steps_dir.iterdir()):
            if not step_dir.is_dir():
                continue
            manifest_file = step_dir / "action.json"
            if not manifest_file.exists():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                if data.get("state") == ActionState.PREPARING.value:
                    manifest = ActionManifest.from_dict(data)
                    self._compensate_effects(manifest.effects)
                    manifest.state = ActionState.RECOVERED_ABORT.value
                    self._write_manifest(manifest)
                    _emit_telemetry(
                        manifest.session_id,
                        "CRASH_RECOVERY",
                        action_id=manifest.action_id,
                        step=manifest.step_id,
                        payload={"action_id": manifest.action_id, "step_id": manifest.step_id},
                        workspace_root=self.workspace_root,
                    )
                    recovered += 1
            except Exception:
                continue
        return recovered
__all__ = []
