"""
Nagato Workspace Registry & Allowlist

Tracks known/active workspace roots across standalone MCP tools, CLI agents, and WebUI.
Persists in ~/.nagato/workspaces.json with last-seen timestamps and session metadata.
Enforces security boundaries via ~/.nagato/allowed_workspaces.yaml (explicit allowlist).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


def get_global_nagato_home() -> Path:
    """Get the user-global ~/.nagato directory, overridable via NAGATO_GLOBAL_HOME."""
    env_home = os.environ.get("NAGATO_GLOBAL_HOME")
    if env_home:
        nagato_home = Path(env_home).resolve()
    else:
        nagato_home = Path.home() / ".nagato"
    nagato_home.mkdir(parents=True, exist_ok=True)
    return nagato_home


def get_registry_file() -> Path:
    """Get the path to ~/.nagato/workspaces.json."""
    return get_global_nagato_home() / "workspaces.json"


def get_allowlist_file() -> Path:
    """Get the path to ~/.nagato/allowed_workspaces.yaml."""
    return get_global_nagato_home() / "allowed_workspaces.yaml"


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


def load_allowlist() -> Set[Path]:
    """Load the allowlist from ~/.nagato/allowed_workspaces.yaml.
    
    Returns a set of resolved Path objects for fast lookup.
    """
    path = get_allowlist_file()
    if not path.exists():
        return set()
    if not YAML_AVAILABLE:
        logger.warning("PyYAML not available, cannot load allowlist")
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if not isinstance(data, list):
                logger.warning("Allowlist file does not contain a list, ignoring")
                return set()
            allowed = set()
            for item in data:
                if isinstance(item, str):
                    try:
                        allowed.add(Path(item).resolve())
                    except Exception:
                        logger.warning("Invalid path in allowlist: %s", item)
            return allowed
    except Exception as e:
        logger.warning("Failed to load workspace allowlist: %s", e)
        return set()


def save_allowlist(allowed: Set[Path]) -> None:
    """Save the allowlist to ~/.nagato/allowed_workspaces.yaml atomically."""
    if not YAML_AVAILABLE:
        logger.warning("PyYAML not available, cannot save allowlist")
        return
    path = get_allowlist_file()
    tmp_path = path.with_suffix(".tmp")
    try:
        # Sort for consistent output
        paths = sorted(str(p) for p in allowed)
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.dump(paths, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(path)
    except Exception as e:
        logger.warning("Failed to save workspace allowlist: %s", e)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def ensure_allowlist_exists() -> Set[Path]:
    """Ensure allowlist file exists, auto-populating from registry if empty.
    
    Returns the current allowlist.
    """
    path = get_allowlist_file()
    if path.exists():
        allowed = load_allowlist()
        # If file exists but is empty, auto-populate from registry
        if not allowed:
            registry = load_workspaces_registry()
            allowed = set()
            for item in registry.get("workspaces", {}).values():
                p_str = item.get("path")
                if p_str:
                    try:
                        allowed.add(Path(p_str).resolve())
                    except Exception:
                        pass
            if allowed:
                save_allowlist(allowed)
                logger.info("Auto-populated allowlist with %d workspaces from registry", len(allowed))
        return allowed
    
    # Auto-populate from registry on first run
    registry = load_workspaces_registry()
    allowed = set()
    for item in registry.get("workspaces", {}).values():
        p_str = item.get("path")
        if p_str:
            try:
                allowed.add(Path(p_str).resolve())
            except Exception:
                pass
    
    if allowed:
        save_allowlist(allowed)
        logger.info("Auto-populated allowlist with %d workspaces from registry", len(allowed))
    
    return allowed


def add_to_allowlist(workspace_root: Path | str) -> Path:
    """Add a workspace to the allowlist."""
    ws = Path(workspace_root).resolve()
    allowed = ensure_allowlist_exists()
    allowed.add(ws)
    save_allowlist(allowed)
    return ws


def remove_from_allowlist(workspace_root: Path | str) -> bool:
    """Remove a workspace from the allowlist. Returns True if removed."""
    ws = Path(workspace_root).resolve()
    allowed = load_allowlist()
    if ws in allowed:
        allowed.remove(ws)
        save_allowlist(allowed)
        return True
    return False


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


def list_registered_workspaces(only_existing: bool = True) -> List[Dict[str, Any]]:
    """List all registered workspaces sorted by last_seen descending.
    
    Args:
        only_existing: If True (default), filter out workspaces that no longer
                       exist on disk to keep the registry clean.
    """
    data = load_workspaces_registry()
    items = []
    stale_keys = []

    for k, item in data.get("workspaces", {}).items():
        p = Path(item.get("path", k))
        exists = p.exists() and p.is_dir()
        if not exists:
            stale_keys.append(k)
            if only_existing:
                continue

        entry = dict(item)
        entry["exists"] = exists
        items.append(entry)

    # Automatically purge non-existent workspaces from registry if only_existing is active
    if only_existing and stale_keys:
        for k in stale_keys:
            del data["workspaces"][k]
        save_workspaces_registry(data)

    items.sort(key=lambda x: x.get("last_seen", 0), reverse=True)
    return items


def is_workspace_allowed(
    workspace_root: Path | str,
    fallback_workspace: Optional[Path] = None,
    transport: str = "http",  # "http", "websocket", "stdio"
) -> bool:
    """Validate whether the given workspace_root is allowed to be accessed.
    
    For stdio transport (MCP): Always allow - process boundary provides isolation.
    For http/websocket transports: Check allowlist first, then fallback to registry.
    
    Allowed if (http/websocket only):
    1. Equals or is relative to the server's default/fallback workspace
    2. Exactly matches or is inside any path in the allowlist (~/.nagato/allowed_workspaces.yaml)
    """
    # stdio transport: process-per-workspace = isolation, no allowlist needed
    if transport == "stdio":
        return True
    
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

    # Check explicit allowlist (primary authorization)
    allowed = load_allowlist()
    for allowed_path in allowed:
        if target == allowed_path:
            return True
        try:
            target.relative_to(allowed_path)
            return True
        except ValueError:
            continue

    return False
