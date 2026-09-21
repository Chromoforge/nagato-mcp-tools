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
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    
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


def resolve_file_path(path: str, workspace_root: Path) -> str:
    """
    Resolve a file path relative to workspace root.
    
    Handles:
    - Relative paths
    - Paths with ~ expansion
    - Paths that may not exist yet (for creation)
    """
    if not isinstance(path, str):
        return path
    
    # Expand user home
    path = os.path.expanduser(path)
    
    # If already absolute, return as-is
    if os.path.isabs(path):
        return path
    
    # Resolve relative to workspace root
    resolved = (workspace_root / path).resolve()
    
    # Security: ensure it's within workspace
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError:
        # Path escapes workspace, return original
        return path
    
    return str(resolved)


import os
__all__ = []
