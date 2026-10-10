"""
Nagato Workspace Registry

Tracks known/active workspace roots across standalone MCP tools, CLI agents, and WebUI.
Persists in ~/.nagato/workspaces.json with last-seen timestamps and session metadata.
Enforces security boundaries: only registered workspaces (or subpaths) can be served.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_global_nagato_home() -> Path:
    """Get the user-global ~/.nagato directory."""
    nagato_home = Path.home() / ".nagato"
    nagato_home.mkdir(parents=True, exist_ok=True)
    return nagato_home


def get_registry_file() -> Path:
    """Get the path to ~/.nagato/workspaces.json."""
    return get_global_nagato_home() / "workspaces.json"


def load_workspaces_registry() -> Dict[str, Any]:
    """Load the registry JSON from ~/.nagato/workspaces.json safely."""
    path = get_registry_file()
    if not path.exists():
        return {"workspaces": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"workspaces": {}}
            if "workspaces" not in data or not isinstance(data["workspaces"], dict):
                data["workspaces"] = {}
            return data
    except Exception as e:
        logger.warning("Failed to load workspace registry: %s", e)
        return {"workspaces": {}}


def save_workspaces_registry(registry: Dict[str, Any]) -> None:
    """Save the registry JSON atomically."""
    path = get_registry_file()
    tmp_path = path.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
        tmp_path.replace(path)
    except Exception as e:
        logger.warning("Failed to save workspace registry: %s", e)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def register_workspace(
    workspace_root: Path | str,
    alias: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Path:
    """Register or touch a workspace in the global registry."""
    ws = Path(workspace_root).resolve()
    key = str(ws).lower()
    data = load_workspaces_registry()

    now = time.time()
    existing = data["workspaces"].get(key, {})
    sessions = set(existing.get("sessions", []))
    if session_id:
        sessions.add(session_id)

    name = alias or ws.name or str(ws)

    data["workspaces"][key] = {
        "path": str(ws),
        "name": name,
        "last_seen": now,
        "sessions": sorted(list(sessions)),
    }

    save_workspaces_registry(data)
    return ws


def unregister_workspace(workspace_root: Path | str) -> None:
    """Remove a workspace from the registry."""
    ws = Path(workspace_root).resolve()
    key = str(ws).lower()
    data = load_workspaces_registry()
    if key in data.get("workspaces", {}):
        del data["workspaces"][key]
        save_workspaces_registry(data)


def list_registered_workspaces() -> List[Dict[str, Any]]:
    """List all registered workspaces sorted by last_seen descending."""
    data = load_workspaces_registry()
    items = []
    for k, item in data.get("workspaces", {}).items():
        p = Path(item.get("path", k))
        # Keep entry even if temporarily offline, but indicate existence
        entry = dict(item)
        entry["exists"] = p.exists() and p.is_dir()
        items.append(entry)

    items.sort(key=lambda x: x.get("last_seen", 0), reverse=True)
    return items


def is_workspace_allowed(
    workspace_root: Path | str,
    fallback_workspace: Optional[Path] = None,
) -> bool:
    """Validate whether the given workspace_root is allowed to be accessed.
    
    Allowed if:
    1. Equals or is relative to the server's default/fallback workspace
    2. Exactly matches or is inside any path registered in ~/.nagato/workspaces.json
    """
    target = Path(workspace_root).resolve()

    if fallback_workspace:
        fb = Path(fallback_workspace).resolve()
        if target == fb:
            return True
        try:
            target.relative_to(fb)
            return True
        except ValueError:
            pass

    data = load_workspaces_registry()
    for item in data.get("workspaces", {}).values():
        p_str = item.get("path")
        if not p_str:
            continue
        p = Path(p_str).resolve()
        if target == p:
            return True
        try:
            target.relative_to(p)
            return True
        except ValueError:
            continue

    return False
