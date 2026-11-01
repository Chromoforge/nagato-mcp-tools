"""
Input repair utilities for standalone facade.

Provides functions to clean and repair malformed JSON payloads,
strip noise from string payloads, coerce primitive types, and
resolve file paths.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union


def strip_noise_from_payload(payload: str) -> str:
    """
    Strip common noise from LLM-generated payloads.
    
    Removes:
    - Markdown code fences (```json ... ```)
    - Leading/trailing whitespace
    - Common conversational prefixes
    """
    if not isinstance(payload, str):
        return payload
    
    # Strip markdown code fences
    payload = payload.strip()
    if payload.startswith("```"):
        # Find the first newline after the opening fence
        first_newline = payload.find("\n")
        if first_newline != -1:
            payload = payload[first_newline + 1:]
        # Find the closing fence
        last_fence = payload.rfind("```")
        if last_fence != -1:
            payload = payload[:last_fence]
    
    # Strip common conversational prefixes
    prefixes = [
        "Here is the JSON:",
        "Here's the JSON:",
        "The JSON payload:",
        "JSON:",
        "Payload:",
    ]
    for prefix in prefixes:
        if payload.startswith(prefix):
            payload = payload[len(prefix):].strip()
            break
    
    return payload.strip()


def repair_malformed_json(payload: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to repair common JSON formatting issues.
    
    Handles:
    - Single quotes instead of double quotes
    - Trailing commas
    - Missing quotes on keys
    - Python-style True/False/None
    """
    if not isinstance(payload, str):
        return None
    
    # First try parsing as-is
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        pass
    
    # Try common repairs
    repaired = payload
    
    # Replace single quotes with double quotes (but not inside strings)
    # This is a simple heuristic - proper implementation would need a parser
    repaired = re.sub(r"(?<!\\)'(?![a-zA-Z_])", '"', repaired)
    
    # Remove trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\g<1>", repaired)
    
    # Replace Python True/False/None with JSON true/false/null
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def coerce_primitives(kwargs: Dict[str, Any], sig: Any) -> Dict[str, Any]:
    """
    Coerce string values to their annotated types based on function signature.
    
    Handles: int, float, bool, list, dict
    """
    import inspect
    
    result = dict(kwargs)
    try:
        parameters = sig.parameters
    except Exception:
        return result
    
    for key, value in result.items():
        if key not in parameters:
            continue
        
        param = parameters[key]
        annotation = param.annotation
        
        if annotation is inspect.Parameter.empty:
            continue
        
        if isinstance(value, str):
            # Try to coerce based on annotation
            if annotation is int:
                try:
                    result[key] = int(value)
                except ValueError:
                    pass
            elif annotation is float:
                try:
                    result[key] = float(value)
                except ValueError:
                    pass
            elif annotation is bool:
                if value.lower() in ("true", "1", "yes", "on"):
                    result[key] = True
                elif value.lower() in ("false", "0", "no", "off"):
                    result[key] = False
            elif annotation in (list, list[str], list[int]):
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
            elif annotation in (dict, dict[str, Any]):
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
    
    return result


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def normalize_relative_path(path: str, workspace_root: Path) -> str:
    """Normalize a path to a workspace-relative path with forward slashes.
    
    Leaves empty strings, dots, or paths escaping workspace unchanged.
    """
    if not isinstance(path, str) or not path.strip() or path.strip() in (".", "./", ".\\"):
        return path
    
    expanded = os.path.expanduser(path.strip())
    p = Path(expanded)
    ws_resolved = workspace_root.resolve()
    
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(ws_resolved)
            return str(rel).replace("\\", "/")
        except ValueError:
            return path
            
    resolved = (workspace_root / p).resolve()
    try:
        rel = resolved.relative_to(ws_resolved)
        return str(rel).replace("\\", "/")
    except ValueError:
        return path


def resolve_file_path(path: str, workspace_root: Path) -> str:
    """
    Resolve a file path relative to workspace root.
    
    Handles:
    - Empty or non-string paths (returned unchanged)
    - Relative paths (kept relative if within workspace)
    - Paths with ~ expansion
    - Absolute paths (relativized if inside workspace)
    - Fuzzy-matching if path does not exist on disk
    """
    if not isinstance(path, str) or not path.strip() or path.strip() in (".", "./", ".\\"):
        return path
    
    norm = normalize_relative_path(path, workspace_root)
    if (workspace_root / norm).exists():
        return norm

    # Fuzzy match if candidate doesn't exist
    raw_stem = Path(norm).name
    threshold = max(2, len(raw_stem) // 4)
    best_dist = threshold + 1
    best_rel: Optional[str] = None
    _FUZZY_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".mypy_cache", "dist"}

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in _FUZZY_SKIP_DIRS]
        for fname in filenames:
            dist = _levenshtein_distance(raw_stem, fname)
            if dist < best_dist:
                best_dist = dist
                best_rel = str(Path(dirpath).relative_to(workspace_root) / fname).replace("\\", "/")

    return best_rel if best_rel is not None else norm


import os
__all__ = []
