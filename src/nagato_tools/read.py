from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools import edit as edit_module
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.token_calculator import estimate
# Database path - can be overridden for testing
DB_PATH = None


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to the mock context."""
    if ctx is not None:
        return ctx
    # Try to get context from global session instance
    fsm = edit_module._get_fsm_instance()
    if fsm is not None:
        return fsm.Context
    return get_mock_context()


def _get_context_token_limit(ctx: Any, default: int = None) -> int:
    """Get token limit from context or use cascading config default for 'read' category."""
    max_context_size = getattr(ctx, "MaxContextSize", None)
    if isinstance(max_context_size, int) and max_context_size > 0:
        return max_context_size
    # Fallback to cascading config (read category)
    if default is None:
        default = get_tool_token_limit("read")
    return default


def _truncate_to_token_limit(text: str, max_tokens: int | None = None, ctx: Any = None) -> str:
    if max_tokens is None:
        max_tokens = _get_context_token_limit(ctx)

    if max_tokens <= 0 or not text:
        return ""

    token_count = estimate(text)
    if token_count <= max_tokens:
        return text

    if max_tokens <= 1:
        return text[:1] + "[TRUNCATED]"

    marker = f"\n[TRUNCATED: {token_count - max_tokens} tokens omitted]"
    marker_tokens = estimate(marker)
    if marker_tokens >= max_tokens:
        return text[: max(1, max_tokens * 4)] + marker

    available_tokens = max_tokens - marker_tokens
    if available_tokens <= 0:
        return marker

    prefix = text[: max(1, available_tokens * 4)]
    return prefix + marker


def _normalize_window(start_offset: int, limit: int) -> tuple[int, int]:
    start = max(0, start_offset)
    page_limit = max(1, limit)
    return start, page_limit


def _paginate_items(items: list, start_offset: int, limit: int) -> tuple[list, int]:
    start, page_limit = _normalize_window(start_offset, limit)
    total_items = len(items)
    return items[start:start + page_limit], total_items


def _append_paging_suffix(header: str, total_items: int, start_offset: int, returned_items: int) -> str:
    if total_items == 0:
        return header

    if returned_items == 0:
        return f"{header} ({total_items} total) [empty window from start_offset={start_offset}]"

    window_start = start_offset + 1
    window_end = start_offset + returned_items
    header = f"{header} ({total_items} total) [showing {window_start}-{window_end}]"
    if window_end < total_items:
        header += f" [next_start_offset={window_end}]"
    return header


def _get_irrelevance_warning(file: str, ctx: Any, workspace_root: Path) -> str:
    """Check if file is marked as IRRELEVANT_TO in Insight knowledge graph for current context."""
    if not ctx:
        return ""
    try:
        from nagato_tools.config import get_insight_config
        if not get_insight_config().get("enabled", False):
            return ""
    except Exception:
        return ""

    try:
        session_id = getattr(ctx, "session_id", None)
        from nagato_tools.insight_models import NodeType, RelationType
        from nagato_tools.insight_store import InsightStore

        store = InsightStore(workspace_root=workspace_root, session_id=session_id)
        clean_file = file.replace("\\", "/").strip()
        matching_nodes = [
            n for n in store.search_nodes(clean_file, limit=5, include_orphaned=True)
            if n.type == NodeType.FILE and (n.title == clean_file or n.origin_file == clean_file)
        ]
        if not matching_nodes:
            store.close()
            return ""

        file_node = matching_nodes[0]
        edges, _ = store.get_direct_edges_filtered(file_node.id, relation=RelationType.IRRELEVANT_TO)
        if not edges:
            store.close()
            return ""

        reasons = []
        for edge in edges:
            target_node = store.get_node(edge.target_id)
            target_label = target_node.title if target_node else "Current Goal"
            reasons.append(f"to '{target_label}' ({edge.semantic_reason})")
        store.close()

        if reasons:
            return f"[INSIGHT NOTICE: '{clean_file}' was previously marked IRRELEVANT {'; '.join(reasons)}]\n\n"
    except Exception:
        pass
    return ""


def _check_irrelevance_guard(file: str, ctx: Any, workspace_root: Path, tool_name: str) -> str:
    """Check if file is marked as IRRELEVANT_TO and return warning or empty string.
    
    For read operations: returns notice string to prepend to content.
    For write operations: returns error string if file is irrelevant, empty if OK.
    """
    if not ctx:
        return ""
    try:
        from nagato_tools.config import get_insight_config
        if not get_insight_config().get("enabled", False):
            return ""
    except Exception:
        return ""

    try:
        session_id = getattr(ctx, "session_id", None)
        from nagato_tools.insight_models import NodeType, RelationType
        from nagato_tools.insight_store import InsightStore

        store = InsightStore(workspace_root=workspace_root, session_id=session_id)
        clean_file = file.replace("\\", "/").strip()
        matching_nodes = [
            n for n in store.search_nodes(clean_file, limit=5, include_orphaned=True)
            if n.type == NodeType.FILE and (n.title == clean_file or n.origin_file == clean_file)
        ]
        if not matching_nodes:
            store.close()
            return ""

        file_node = matching_nodes[0]
        edges, _ = store.get_direct_edges_filtered(file_node.id, relation=RelationType.IRRELEVANT_TO)
        if not edges:
            store.close()
            return ""

        reasons = []
        for edge in edges:
            target_node = store.get_node(edge.target_id)
            target_label = target_node.title if target_node else "Current Goal"
            reasons.append(f"to '{target_label}' ({edge.semantic_reason})")
        store.close()

        if reasons:
            notice = f"[INSIGHT NOTICE: '{clean_file}' was previously marked IRRELEVANT {'; '.join(reasons)}]"
            if tool_name.startswith("nagato_read") or tool_name.startswith("nagato_search") or tool_name.startswith("nagato_list") or tool_name.startswith("nagato_find"):
                return notice + "\n\n"
            else:
                return f"ERROR: {notice}. Use refute_theory to override if intentional.\n"
    except Exception:
        pass
    return ""


async def nagato_read_file(file: str, _ctx: Optional[Any] = None) -> str:
    """
    Reads the content of a file and returns it.
    Args:
        file: Relative path to the file from the workspace root.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_file = workspace_root / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_read_file")

    if not target_file.exists():
        return nagato_error(f"File {file} not found.", tool="nagato_read_file")

    try:
        with target_file.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        
        # Trigger Insight sync if file is missing or stale
        sync_queued = False  # Insight disabled
        sync_notice = "[INSIGHT: knowledge graph entry for this file is being (re)created in background]\n\n" if sync_queued else ""
        
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_read_file")
        return sync_notice + notice + _truncate_to_token_limit(content, ctx=ctx)
    except Exception as e:
        return nagato_error(str(e), tool="nagato_read_file")

async def nagato_read_lines(file: str, start_line: int, end_line: int, _ctx: Optional[Any] = None) -> str:
    """
    Reads specific lines from a file and returns them.
    Args:
        file: Relative path to the file.
        start_line: First line (1-indexed).
        end_line: Last line (inclusive).
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_file = workspace_root / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_read_lines")

    if not target_file.exists():
        return nagato_error(f"File {file} not found.", tool="nagato_read_lines")

    try:
        with target_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if start_line < 1 or end_line > len(lines) or start_line > end_line:
            return nagato_error(
                f"Invalid line numbers. File has {len(lines)} lines.",
                tool="nagato_read_lines",
            )

        selected_lines = lines[start_line - 1:end_line]
        
        # Trigger Insight sync if file is missing or stale
        sync_queued = False  # Insight disabled
        sync_notice = "[INSIGHT: knowledge graph entry for this file is being (re)created in background]\n\n" if sync_queued else ""
        
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_read_lines")
        return sync_notice + notice + _truncate_to_token_limit("".join(selected_lines), ctx=ctx)
    except Exception as e:
        return nagato_error(str(e), tool="nagato_read_lines")

async def nagato_list_dir(dir: str, _ctx: Optional[Any] = None) -> str:
    """
    Lists files and subdirectories in a directory.
    Args:
        dir: Relative path to the directory from the workspace root.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_dir = workspace_root / dir

    # Guard: prevent path traversal outside workspace
    resolved = target_dir.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {dir} resolves outside workspace root.", tool="nagato_list_dir")

    if not target_dir.exists() or not target_dir.is_dir():
        return nagato_error(f"Directory {dir} not found.", tool="nagato_list_dir")

    try:
        entries = list(target_dir.iterdir())
        if not entries:
            return _truncate_to_token_limit(f"Directory: {dir}\nDirectory is empty.", ctx=ctx)
        
        result = [f"Directory: {dir}"]
        for entry in entries:
            if entry.is_dir():
                result.append(f"[DIR] {entry.name}")
            else:
                result.append(f"      {entry.name}")
        rendered = "\n".join(result)
        return _truncate_to_token_limit(rendered, ctx=ctx)
    except Exception as e:
        return nagato_error(str(e), tool="nagato_list_dir")



from pathlib import Path
import sqlite3
from nagato_tools.config import resolve_db_path, get_semantic_search_config, get_workspace_root as config_get_workspace_root

def _get_db_path(ctx: Optional[Any] = None) -> str:
    """Get the database path from config with workspace root from context."""
    # Allow test override via module-level DB_PATH
    if DB_PATH is not None:
        return DB_PATH
    config = get_semantic_search_config()
    workspace_root = _get_workspace_root(ctx)
    return resolve_db_path(config, workspace_root)

def nagato_read_signatures(file_path: str = None, target_symbol: str = None, limit: int = 20, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    PRIMARY TOOL for symbol lookup. Two modes:
    1. GLOBAL (cross-file): omit file_path, provide target_symbol — searches the entire indexed codebase via DB. Fast, no false positives from comments or strings. Use this instead of regex when looking for a symbol by name across multiple files.
    2. LOCAL (single-file): provide file_path — returns all signatures in that file, optionally filtered by target_symbol.
    PREFER this over nagato_searchInFiles or regex for any symbol/function/class lookup task.
    Args:
        file_path: Optional. Relative path to a specific file. Omit for project-wide search.
        target_symbol: Optional. Exact symbol name to look up. Required for global mode.
        limit: Maximum number of signatures to return in this page.
        start_offset: Optional zero-based offset into the result list.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    start_offset, limit = _normalize_window(start_offset, limit)

    # Case 1: Pure global search via database
    if not file_path and target_symbol:
        try:
            conn = sqlite3.connect(_get_db_path(_ctx))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, signature_text FROM global_symbols WHERE symbol_name = ?",
                (target_symbol,)
            )
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return nagato_error(
                    f"Symbol '{target_symbol}' was not found anywhere globally.",
                    tool="nagato_read_signatures",
                )

            paged_rows, total_rows = _paginate_items(rows, start_offset, limit)
            if not paged_rows:
                return _append_paging_suffix(
                    f"[GLOBAL SIGNATURE RESOLUTION FOR '{target_symbol}']",
                    total_rows,
                    start_offset,
                    0,
                )

            output = [_append_paging_suffix(
                f"[GLOBAL SIGNATURE RESOLUTION FOR '{target_symbol}']",
                total_rows,
                start_offset,
                len(paged_rows),
            )]
            for f_path, sig_text in paged_rows:
                output.append(f"  In {f_path}:\n    {sig_text}")
            return _truncate_to_token_limit("\n".join(output), ctx=ctx)
        except Exception as e:
            return nagato_error(f"Global DB query failed: {str(e)}", tool="nagato_read_signatures")

    # Case 2: local file analysis (either via DB or live AST)
    if file_path:
        workspace_root = _get_workspace_root(_ctx)
        target_file = workspace_root / file_path
        resolved = target_file.resolve()
        workspace_root_resolved = workspace_root.resolve()
        if not resolved.is_relative_to(workspace_root_resolved):
            return nagato_error(f"Path {file_path} resolves outside workspace root.", tool="nagato_read_signatures")

        # Optional: First check the DB for data on this file
        try:
            conn = sqlite3.connect(_get_db_path(_ctx))
            cursor = conn.cursor()
            if target_symbol:
                cursor.execute(
                    "SELECT signature_text FROM global_symbols WHERE file_path = ? AND symbol_name = ?",
                    (file_path, target_symbol)
                )
            else:
                cursor.execute(
                    "SELECT signature_text FROM global_symbols WHERE file_path = ?",
                    (file_path,)
                )
            rows = cursor.fetchall()
            conn.close()
            
            if rows:
                formatted_rows = [r[0] for r in rows]
                paged_rows, total_rows = _paginate_items(formatted_rows, start_offset, limit)
                if not paged_rows:
                    return _append_paging_suffix(
                        f"[SIGNATURES IN {file_path}]",
                        total_rows,
                        start_offset,
                        0,
                    )
                output = [_append_paging_suffix(
                    f"[SIGNATURES IN {file_path}]",
                    total_rows,
                    start_offset,
                    len(paged_rows),
                )]
                output.extend(paged_rows)
                return _truncate_to_token_limit("\n".join(output), ctx=ctx)
        except Exception:
            pass # If DB fails, we use the live fallback below
            
        # Live fallback via SignatureExtractor (your original code)
        try:
            from nagato_tools.extractsignature import SignatureExtractor
            extractor = SignatureExtractor(str(target_file))
            signatures = extractor.extract()
            
            if target_symbol:
                signatures = [s for s in signatures if s["symbol"] == target_symbol]
                if not signatures:
                    return nagato_error(
                        f"Symbol '{target_symbol}' not found in {file_path}.",
                        tool="nagato_read_signatures",
                    )

            output = []
            for sig in signatures:
                decorators = f"@{', @'.join(sig['decorators'])} " if sig['decorators'] else ""
                args = ", ".join([f"{a['name']}: {a['annotation'] or 'Any'}" for a in sig['args']])
                ret = f" -> {sig['returns']}" if sig['returns'] else ""
                doc = f"\n    # {sig['doc']}" if sig['doc'] else ""
                output.append(f"{decorators}{sig['kind']} {sig['symbol']}({args}){ret}:{doc}")

            paged_output, total_rows = _paginate_items(output, start_offset, limit)
            if not paged_output:
                return _append_paging_suffix(
                    f"[SIGNATURES IN {file_path}]",
                    total_rows,
                    start_offset,
                    0,
                )

            rendered = [_append_paging_suffix(
                f"[SIGNATURES IN {file_path}]",
                total_rows,
                start_offset,
                len(paged_output),
            )]
            rendered.extend(paged_output)
            return _truncate_to_token_limit("\n".join(rendered), ctx=ctx)
        except Exception as e:
            return nagato_error(
                f"Error parsing live signatures: {str(e)}",
                tool="nagato_read_signatures",
            )

    return nagato_error(
        "Invalid parameters: neither file_path nor target_symbol provided.",
    )
    
def nagato_extract_callees(symbol: str, file_path: str = None, limit: int = 20, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    Returns a list of all functions/methods called by the target symbol.
    If no file_path is provided, the relational global call table is queried.
    Results are paged via limit/start_offset to avoid flooding the context.
    """
    ctx = _get_context(_ctx)
    start_offset, limit = _normalize_window(start_offset, limit)
    try:
        conn = sqlite3.connect(_get_db_path(_ctx))
        cursor = conn.cursor()
        
        if file_path:
            cursor.execute(
                "SELECT callee FROM global_calls WHERE file_path = ? AND caller = ?",
                (file_path, symbol)
            )
        else:
            cursor.execute(
                "SELECT DISTINCT callee, file_path FROM global_calls WHERE caller = ? OR caller LIKE ?",
                (symbol, f"%.{symbol}")
            )
            
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return f"[NFSM INFO] Symbol '{symbol}' does not call any other symbols globally, or is not indexed."

        normalized_rows = sorted(set(rows))
        paged_rows, total_rows = _paginate_items(normalized_rows, start_offset, limit)
        if not paged_rows:
            return _append_paging_suffix(f"[CALLEES FOR: {symbol}]", total_rows, start_offset, 0)

        output = [_append_paging_suffix(f"[CALLEES FOR: {symbol}]", total_rows, start_offset, len(paged_rows))]
        if file_path:
            for r in paged_rows:
                output.append(f"  -> {r[0]}")
        else:
            # Also show where this call takes place, for a global search
            for callee, f_path in paged_rows:
                output.append(f"  -> {callee} (defined/called in {f_path})")

        return _truncate_to_token_limit("\n".join(output), ctx=ctx)

    except Exception as e:
        # Last-resort fallback: live AST if the DB connection is flaky and file_path is present
        if file_path:
            try:
                from extractcallgraph import CallgraphBuilder
                builder = CallgraphBuilder(file_path)
                graph = builder.build()
                callees = graph["calls"].get(symbol, [])
                if not callees:
                    return f"[NFSM INFO] Symbol '{symbol}' does not call anything in {file_path}."
                normalized_callees = sorted(set(callees))
                paged_callees, total_rows = _paginate_items(normalized_callees, start_offset, limit)
                if not paged_callees:
                    return _append_paging_suffix(f"[CALLEES: {symbol}]", total_rows, start_offset, 0)
                return _truncate_to_token_limit(
                    "\n".join(
                        [_append_paging_suffix(f"[CALLEES: {symbol}]", total_rows, start_offset, len(paged_callees))]
                        + [f"  -> {c}" for c in paged_callees]
                    ),
                    ctx=ctx
                )
            except Exception as ex:
                return nagato_error(
                    f"Global & local callees lookup failed: {str(ex)}",
                    tool="nagato_extract_callees",
                )
        return nagato_error(
            f"Global extract_callees query failed: {str(e)}",
            tool="nagato_extract_callees",
        )

def nagato_extract_callers(symbol: str, file_path: str = None, limit: int = 20, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    Finds all symbols that call the target symbol.
    If no file_path is provided, the entire project is scanned (Global Called-From).
    Results are paged via limit/start_offset to avoid flooding the context.
    """
    ctx = _get_context(_ctx)
    start_offset, limit = _normalize_window(start_offset, limit)
    try:
        conn = sqlite3.connect(_get_db_path(_ctx))
        cursor = conn.cursor()
        
        if file_path:
            # Strictly local to a single file
            cursor.execute(
                "SELECT caller FROM global_calls WHERE file_path = ? AND (callee = ? OR callee LIKE ?)",
                (file_path, symbol, f"%.{symbol}")
            )
        else:
            # Project-wide, global scope
            cursor.execute(
                "SELECT file_path, caller FROM global_calls WHERE callee = ? OR callee LIKE ? ORDER BY file_path",
                (symbol, f"%.{symbol}")
            )
            
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return f"[NFSM INFO] No callers found for '{symbol}' in the selected scope."

        normalized_rows = sorted(set(rows))
        paged_rows, total_rows = _paginate_items(normalized_rows, start_offset, limit)
        if not paged_rows:
            return _append_paging_suffix(f"[CALLERS / DEPENDENCIES FOR: {symbol}]", total_rows, start_offset, 0)

        output = [_append_paging_suffix(f"[CALLERS / DEPENDENCIES FOR: {symbol}]", total_rows, start_offset, len(paged_rows))]
        if file_path:
            for r in paged_rows:
                output.append(f"  <- {r[0]}")
        else:
            for f_path, caller in paged_rows:
                output.append(f"  <- {f_path} :: {caller}")

        return _truncate_to_token_limit("\n".join(output), ctx=ctx)

    except Exception as e:
        return nagato_error(
            f"Error in extract_callers for '{symbol}': {str(e)}",
            tool="nagato_extract_callers",
        )


# Non-prefixed aliases for MCP server compatibility
read_file = nagato_read_file
read_lines = nagato_read_lines
list_dir = nagato_list_dir
read_signatures = nagato_read_signatures
extract_callees = nagato_extract_callees
extract_callers = nagato_extract_callers
__all__ = ['nagato_read_file', 'nagato_read_lines', 'nagato_list_dir', 'nagato_read_signatures', 'nagato_extract_callees', 'nagato_extract_callers']
