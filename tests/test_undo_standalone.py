"""Tests for standalone undo/redo round-trip and snapshot collision safety.

Locks down:
- snapshot filename uses the collision-safe UUID-suffixed convention
- snapshots carry a schema_version field
- undo restores previous content for new/modified/deleted files
- snapshots produced by both writers (`undo.py` and `ctx_mock.py`) coexist in
  the same directory without one clobbering the other (the C3 bug).
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from nagato_tools.undo import _track_edit_for_undo, _standalone_undo, _get_standalone_undo_dir
from nagato_tools.ctx_mock import MockFSMContext


@pytest.fixture
def workspace(tmp_path):
    """Provide a temporary workspace root with mock context."""
    ws = tmp_path
    (ws / "subdir").mkdir()
    return ws


@pytest.fixture
def mock_ctx(workspace):
    """Provide a MockFSMContext pointed at the temp workspace."""
    return MockFSMContext(workspace_root=workspace, session_id="test-session")


def _read_snapshot(snapshot_file: Path) -> dict:
    with open(snapshot_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _run(coro):
    """Drive an async coroutine from a sync test."""
    return asyncio.run(coro)


def test_snapshot_filename_has_uuid_suffix(mock_ctx, workspace):
    """The C3 fix: filename must be `snapshot_<ms>_<uuid8>.json` (collision-safe)."""
    _track_edit_for_undo(mock_ctx, "hello.py", old_content="a", new_content="b")
    undo_dir = _get_standalone_undo_dir(mock_ctx)
    snapshots = list(undo_dir.glob("snapshot_*.json"))
    assert len(snapshots) == 1
    name = snapshots[0].name
    # shape: snapshot_<ms>_<uuid8>.json — three underscore-separated parts
    assert name.startswith("snapshot_")
    assert name.endswith(".json")
    parts = name[:-len(".json")].split("_")
    assert len(parts) == 3, f"Expected 3 parts (ms, uuid8), got {parts!r}"
    assert parts[1].isdigit()
    assert len(parts[2]) == 8 and all(c in "0123456789abcdef" for c in parts[2])


def test_snapshot_has_schema_version(mock_ctx):
    """Every snapshot carries schema_version=1 (forward-compat marker)."""
    _track_edit_for_undo(mock_ctx, "hello.py", old_content="a", new_content="b")
    undo_dir = _get_standalone_undo_dir(mock_ctx)
    snapshot = _read_snapshot(next(undo_dir.glob("snapshot_*.json")))
    assert snapshot.get("schema_version") == 1
    assert snapshot["file"] == "hello.py"
    assert snapshot["old_content"] == "a"
    assert snapshot["new_content"] == "b"
    assert snapshot["is_new"] is False
    assert snapshot["is_delete"] is False


def test_undo_restores_old_content_for_modification(mock_ctx, workspace):
    """Round-trip: write → snapshot → write → undo → old content restored."""
    target = workspace / "hello.py"
    target.write_text("OLD", encoding="utf-8")
    _track_edit_for_undo(mock_ctx, "hello.py", old_content="OLD", new_content="NEW")
    target.write_text("NEW", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "NEW"

    result = _run(_standalone_undo(mock_ctx, mode="step", value=1))
    assert "Undid modification" in result
    assert target.read_text(encoding="utf-8") == "OLD"


def test_undo_deletes_new_file(mock_ctx, workspace):
    """If the snapshot marks the file as new, undo removes it."""
    target = workspace / "fresh.py"
    target.write_text("brand new", encoding="utf-8")
    _track_edit_for_undo(mock_ctx, "fresh.py", old_content=None, new_content="brand new", is_new=True)
    assert target.exists()

    result = _run(_standalone_undo(mock_ctx, mode="step", value=1))
    assert "Undid creation" in result
    assert not target.exists()


def test_undo_restores_deleted_file(mock_ctx, workspace):
    """If the snapshot marks a deletion, undo re-creates the file with old content."""
    target = workspace / "deleted.py"
    target.write_text("original", encoding="utf-8")
    target.unlink()
    _track_edit_for_undo(mock_ctx, "deleted.py", old_content="original", new_content=None, is_delete=True)
    assert not target.exists()

    result = _run(_standalone_undo(mock_ctx, mode="step", value=1))
    assert "Undid deletion" in result
    assert target.read_text(encoding="utf-8") == "original"


def test_snapshot_writers_coexist_in_same_dir(mock_ctx, workspace):
    """C3 regression: undo.py and ctx_mock.py writes must not collide.

    Both writers save snapshots into the same .nagato/undo_cache/ dir.
    The fix unifies the filename convention so neither writer trips over
    the other's snapshots during `_standalone_undo`'s glob + apply loop.
    """
    # undo.py write
    _track_edit_for_undo(mock_ctx, "from_undo.py", old_content="a", new_content="b")
    # ctx_mock.py write
    mock_ctx.track_edit_for_undo("from_ctx.py", old_content="x", new_content="y")

    undo_dir = _get_standalone_undo_dir(mock_ctx)
    snapshots = sorted(undo_dir.glob("snapshot_*.json"))
    assert len(snapshots) == 2

    files_tracked = {_read_snapshot(s)["file"] for s in snapshots}
    assert files_tracked == {"from_undo.py", "from_ctx.py"}

    # Both snapshots must be parseable by the undo loader (same schema_version).
    for s in snapshots:
        snap = _read_snapshot(s)
        assert snap.get("schema_version") == 1


def test_prune_keeps_max_50_snapshots(mock_ctx, workspace):
    """Pruning cap holds the undo history at 50 snapshots."""
    for i in range(60):
        _track_edit_for_undo(mock_ctx, f"file_{i}.py", old_content="o", new_content="n")
        # Tiny sleep to guarantee monotonically increasing ms timestamps so
        # `sorted()` orders by time, not by filename collision.
        time.sleep(0.002)

    undo_dir = _get_standalone_undo_dir(mock_ctx)
    snapshots = list(undo_dir.glob("snapshot_*.json"))
    assert len(snapshots) == 50