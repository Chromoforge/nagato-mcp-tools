"""UUID generation tools for session initialization."""

import uuid
from typing import Any, Optional


async def nagato_generate_uuid4(
    count: int = 1,
    no_hyphens: bool = False,
    _ctx: Optional[Any] = None
) -> str:
    """
    Generate one or more UUID4 values.
    
    This is essential for session initialization — every session requires
    a UUID4 session_id for session bootstrap.
    
    Args:
        count: Number of UUID4 values to generate (default: 1)
        no_hyphens: If True, output UUIDs without hyphens
        _ctx: Optional session context (injected by facade)
    
    Returns:
        Generated UUID4 value(s), one per line if count > 1
    """
    if count < 1:
        return "Error: count must be at least 1"
    
    results = []
    for _ in range(count):
        value = str(uuid.uuid4())
        if no_hyphens:
            value = value.replace("-", "")
        results.append(value)
    
    return "\n".join(results)


__all__ = ['nagato_generate_uuid4']