"""
Insight-Node-ID-based Read/Write/Search/List tools.

These tools replace path-based tools (nagato_read_file, nagato_edit, etc.)
and require agents to use Insight node IDs for all file operations.
This enforces proper Insight workflow integration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_insight_config
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.read import (
    _get_context,
    _get_workspace_root,
    _truncate_to_token_limit,
    _check_irrelevance_guard,
    trigger_insight_sync_if_needed,
    check_radar_alert_for_file,
)
from nagato_tools.edit import nagato_edit, nagato_edit_lines, nagato_delete
from nagato_tools.read import nagato_read_file, nagato_read_lines, nagato_read_signatures
from nagato_tools.plan import nagato_write_plan
from nagato_tools.insight_store import InsightStore
def _get_workspace_root(ctx=None) -> Path:
    """Get workspace root from context or fall back to cwd."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return Path.cwd()



# ============================================================================
# Helper Functions
# ============================================================================

def _get_insight_store(ctx: Optional[Any] = None) -> InsightStore:
    """Get InsightStore instance with workspace/session from context."""
    context = _get_context(ctx)
    workspace_root = _get_workspace_root(ctx)
    session_id = getattr(context, "session_id", None)
    return InsightStore(workspace_root=workspace_root, session_id=session_id)


def _resolve_file_from_node(node_id: str, ctx: Optional[Any] = None) -> tuple[str, Any]:
    """Resolve node_id to file path and validate it has an associated file.
    
    Returns:
        Tuple of (file_path, node)
        
    Raises:
        ValueError: If node not found or has no origin_file.
    """
    store = _get_insight_store(ctx)
    try:
        return store.resolve_node_to_file(node_id)
    finally:
        store.close()


def _resolve_symbol_from_node(node_id: str, ctx: Optional[Any] = None) -> tuple[str, str, Any]:
    """Resolve node_id to file path, symbol, and node."""
    store = _get_insight_store(ctx)
    try:
        return store.resolve_node_to_symbol(node_id)
    finally:
        store.close()


def _apply_insight_hooks(file_path: str, ctx: Optional[Any], tool_name: str) -> str:
    """Apply Insight sync and radar alert hooks, return prefix string."""
    # Trigger Insight sync if file is missing or stale
    sync_queued = trigger_insight_sync_if_needed(file_path, ctx)
    sync_notice = "[INSIGHT: knowledge graph entry for this file is being (re)created in background]\n\n" if sync_queued else ""
    
    # Proactive context injection: radar alert
    radar_alert = check_radar_alert_for_file(file_path, ctx)
    alert_prefix = f"{radar_alert}\n\n" if radar_alert else ""
    
    # Irrelevance guard
    context = _get_context(ctx)
    workspace_root = _get_workspace_root(ctx)
    notice = _check_irrelevance_guard(file_path, context, workspace_root, tool_name)
    
    return alert_prefix + sync_notice + notice


# ============================================================================
# Read Tools
# ============================================================================

async def nagato_read_node(
    node_id: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Read file content by Insight node ID instead of file path.
    
    Args:
        node_id: Insight node ID (FILE, COMPONENT, or AST node with origin_file).
        start_line: Optional start line (1-indexed). If provided with end_line, reads a range.
        end_line: Optional end line (1-indexed, inclusive).
        _ctx: Injected FSM context.
        
    Returns:
        File content (or line range) with Insight context injected.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_read_node")
    
    # Apply Insight hooks (sync, radar, irrelevance)
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_read_node")
    
    # Delegate to existing read function
    if start_line is not None and end_line is not None:
        content = await nagato_read_lines(file_path, start_line, end_line, _ctx)
    else:
        content = await nagato_read_file(file_path, _ctx)
    
    return prefix + content


async def nagato_read_node_signatures(
    node_id: str,
    target_symbol: Optional[str] = None,
    limit: int = 20,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Read signatures (functions, classes, methods) from a file by Insight node ID.
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        target_symbol: Optional specific symbol to search for.
        limit: Maximum number of signatures to return.
        _ctx: Injected FSM context.
        
    Returns:
        Formatted signatures with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_read_node_signatures")
    
    # Apply Insight hooks
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_read_node_signatures")
    
    # Delegate to existing read_signatures (sync function)
    content = nagato_read_signatures(file_path=file_path, target_symbol=target_symbol, limit=limit, start_offset=0, _ctx=_ctx)
    
    return prefix + content


# ============================================================================
# Write Tools
# ============================================================================

async def nagato_edit_node(
    node_id: str,
    searchstring: str,
    replacement: str,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Edit a file by Insight node ID (search/replace).
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        searchstring: Text to search for.
        replacement: Text to replace with.
        _ctx: Injected FSM context.
        
    Returns:
        Edit confirmation with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_edit_node")
    
    # Apply Insight hooks (for write operations, irrelevance guard returns error)
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_edit_node")
    
    # Delegate to existing edit function
    result = await nagato_edit(file_path, searchstring, replacement, _ctx)
    
    return prefix + result


async def nagato_edit_node_lines(
    node_id: str,
    start_line: int,
    end_line: int,
    new_code: str,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Edit a file by Insight node ID (line range replacement).
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        start_line: Start line (1-indexed).
        end_line: End line (1-indexed, inclusive).
        new_code: New code to insert.
        _ctx: Injected FSM context.
        
    Returns:
        Edit confirmation with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_edit_node_lines")
    
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_edit_node_lines")
    
    result = await nagato_edit_lines(file_path, start_line, end_line, new_code, _ctx)
    
    return prefix + result


async def nagato_delete_node(
    node_id: str,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Delete a file by Insight node ID.
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        _ctx: Injected FSM context.
        
    Returns:
        Deletion confirmation with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_delete_node")
    
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_delete_node")
    
    result = await nagato_delete(file_path, _ctx)
    
    return prefix + result


async def nagato_write_plan_node(
    node_id: str,
    content: str,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Write a plan file by Insight node ID.
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        content: Plan content to write.
        _ctx: Injected FSM context.
        
    Returns:
        Write confirmation with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_write_plan_node")
    
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_write_plan_node")
    
    result = nagato_write_plan(file_path, content, _ctx)
    
    return prefix + result


# ============================================================================
# Search Tools
# ============================================================================

async def nagato_search_node(
    node_id: str,
    query: str,
    max_results: int = 5,
    start_offset: int = 0,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Search within a file by Insight node ID (replaces nagato_searchInFile).
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        query: Search query string.
        max_results: Maximum number of results.
        start_offset: Offset for pagination.
        _ctx: Injected FSM context.
        
    Returns:
        Search results with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_search_node")
    
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_search_node")
    
    # Import and delegate to searchInFile
    from nagato_tools.search import nagato_searchInFile
    result = await nagato_searchInFile(query, file_path, max_results, start_offset, _ctx)
    
    return prefix + result


async def nagato_search_node_ast(
    node_id: str,
    query: str,
    max_results: int = 5,
    start_offset: int = 0,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Search AST symbols within a file by Insight node ID (replaces nagato_searchAST).
    
    Args:
        node_id: Insight node ID (FILE or COMPONENT node).
        query: Search query string (symbol name pattern).
        max_results: Maximum number of results.
        start_offset: Offset for pagination.
        _ctx: Injected FSM context.
        
    Returns:
        AST search results with Insight context.
    """
    try:
        file_path, node = _resolve_file_from_node(node_id, _ctx)
    except ValueError as e:
        return nagato_error(str(e), tool="nagato_search_node_ast")
    
    prefix = _apply_insight_hooks(file_path, _ctx, "nagato_search_node_ast")
    
    # Import and delegate to searchAST
    from nagato_tools.search import nagato_searchAST
    result = await nagato_searchAST(file_path, query, max_results, start_offset, _ctx)
    
    return prefix + result


# ============================================================================
# List/Find Tools
# ============================================================================

async def nagato_list_node_children(
    node_id: str,
    limit: int = 50,
    _ctx: Optional[Any] = None,
) -> str:
    """
    List child nodes of a directory/component node (replaces nagato_list_dir).
    
    Uses CONTAINS edges in the Insight graph to list children.
    
    Args:
        node_id: Insight node ID (DIRECTORY, COMPONENT, or FILE node with children).
        limit: Maximum number of children to return.
        _ctx: Injected FSM context.
        
    Returns:
        Formatted list of child nodes.
    """
    store = _get_insight_store(_ctx)
    try:
        node = store.get_node(node_id, include_collapsed=True)
        if node is None:
            return nagato_error(f"Node '{node_id}' not found in Insight graph", tool="nagato_list_node_children")
        
        # Get CONTAINS edges from this node
        from nagato_tools.insight_models import RelationType
        edges, _ = store.get_direct_edges_filtered(node_id, relation=RelationType.CONTAINS, limit=limit)
        
        if not edges:
            return f"INSIGHT_LIST_CHILDREN: node={node_id} ({node.title}) count=0\n  (no children found)"
        
        # Get target nodes
        child_ids = [e.target_id for e in edges]
        children = store.get_nodes_by_ids(child_ids, include_orphaned=True)
        
        lines = [f"INSIGHT_LIST_CHILDREN: node={node_id} ({node.title}) count={len(children)}"]
        for child in children:
            status_tag = f" [{child.status}]" if child.status != "ACTIVE" else ""
            file_info = f" in {child.origin_file}" if child.origin_file else ""
            lines.append(f"  [{child.id[:8]}] {child.title} ({child.type.value}){status_tag}{file_info}")
            if child.summary:
                summary_line = child.summary.split("\n")[0][:100]
                lines.append(f"    — {summary_line}")
        
        return "\n".join(lines)
    finally:
        store.close()


async def nagato_find_node(
    pattern: str,
    node_type: Optional[str] = None,
    limit: int = 20,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Find nodes by title/symbol pattern in the Insight graph (replaces nagato_find_file).
    
    Args:
        pattern: Search pattern (substring match on title/origin_symbol).
        node_type: Optional filter by node type (FILE, COMPONENT, CONCEPT, etc.).
        limit: Maximum number of results.
        _ctx: Injected FSM context.
        
    Returns:
        Formatted list of matching nodes.
    """
    store = _get_insight_store(_ctx)
    try:
        # Search nodes by pattern
        nodes = store.search_nodes(pattern, limit=limit * 2, include_orphaned=True)
        
        # Filter by node_type if provided
        if node_type:
            from nagato_tools.insight_models import NodeType
            try:
                type_filter = NodeType(node_type.upper())
                nodes = [n for n in nodes if n.type == type_filter]
            except ValueError:
                return nagato_error(f"Invalid node_type: {node_type}", tool="nagato_find_node")
        
        nodes = nodes[:limit]
        
        if not nodes:
            return f"INSIGHT_FIND_NODE: pattern='{pattern}' count=0"
        
        lines = [f"INSIGHT_FIND_NODE: pattern='{pattern}' count={len(nodes)}"]
        for node in nodes:
            status_tag = f" [{node.status}]" if node.status != "ACTIVE" else ""
            file_info = f" in {node.origin_file}" if node.origin_file else ""
            symbol_info = f" :: {node.origin_symbol}" if node.origin_symbol else ""
            lines.append(f"  [{node.id[:8]}] {node.title} ({node.type.value}){status_tag}{file_info}{symbol_info}")
            if node.summary:
                summary_line = node.summary.split("\n")[0][:100]
                lines.append(f"    — {summary_line}")
        
        return "\n".join(lines)
    finally:
        store.close()
__all__ = ['nagato_read_node', 'nagato_read_node_signatures', 'nagato_edit_node', 'nagato_edit_node_lines', 'nagato_delete_node', 'nagato_write_plan_node', 'nagato_search_node', 'nagato_search_node_ast', 'nagato_list_node_children', 'nagato_find_node']
