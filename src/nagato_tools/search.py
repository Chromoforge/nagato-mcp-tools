from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.config import (
    get_ignored_dirs,
    get_semantic_search_config,
    save_semantic_search_root,
)
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.read import _check_irrelevance_guard
from nagato_tools.semanticindex import SemanticIndexSearch
from nagato_tools.token_calculator import estimate
from nagato_tools.insight_sync_hooks import check_radar_alert_for_file

# Text file suffixes that are considered searchable
SEARCHABLE_TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".txt", ".md", ".json", ".yaml", ".yml", ".html", ".css",
    ".xml", ".csv", ".ini", ".cfg", ".conf", ".log", ".sh", ".bat", ".ps1",
    ".rs", ".go", ".java", ".cpp", ".c", ".h", ".hpp", ".cs", ".php", ".rb",
    ".pl", ".swift", ".kt", ".scala", ".clj", ".hs", ".ml", ".fs", ".vb",
    ".sql", ".r", ".m", ".jl", ".lua", ".dart", ".ex", ".exs", ".erl", ".hrl",
    ".nim", ".zig", ".v", ".sv", ".vhd", ".vhdl", ".asm", ".s", ".S", ".inc",
    ".hxx", ".cxx", ".cc", ".mm", ".mpp", ".mxx", ".tpp", ".ipp", ".inl",
    ".def", ".mak", ".mk", ".cmake", ".gradle", ".bazel", ".scons", ".ninja",
    ".make", ".am", ".ac", ".in", ".m4", ".spec", ".patch", ".diff", ".rej",
    ".orig", ".bak", ".backup", ".tmp", ".temp", ".swp", ".swo", "~",
    ".lock", ".pid", ".sock", ".pipe", ".fifo"
})

def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to mock."""
    if ctx is not None:
        return ctx
    return get_mock_context(_get_workspace_root(ctx))


def _get_semantic_config():
    """Get semantic search configuration."""
    return get_semantic_search_config()


def _normalize_window(start_offset: int, limit: int) -> tuple[int, int]:
    start = max(0, start_offset)
    page_limit = max(1, limit)
    return start, page_limit


def _get_context_token_limit(ctx: Any, default: int = None) -> int:
    """Get token limit from context or use cascading config default for 'search' category."""
    max_context_size = getattr(ctx, "MaxContextSize", None)
    if isinstance(max_context_size, int) and max_context_size > 0:
        return max_context_size
    # Fallback to cascading config (search category)
    if default is None:
        default = get_tool_token_limit("search")
    return default


def _truncate_to_token_limit(text: str, max_tokens: int | None = None, ctx: Any = None) -> str:
    """Truncate text to fit within token limit."""
    if max_tokens is None:
        max_tokens = _get_context_token_limit(ctx)

    if max_tokens is None or max_tokens <= 0:
        return text

    if not text:
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


def _format_window_header(prefix: str, total_hits: int, start_offset: int, returned_hits: int) -> str:
    if total_hits == 0:
        return prefix

    window_start = start_offset + 1
    window_end = start_offset + returned_hits
    header = f"{prefix} ({total_hits} total) [showing {window_start}-{window_end}]"
    if window_end < total_hits:
        header += f" [next_start_offset={window_end}]"
    return header


def _collect_plaintext_matches(lines: list[str], query: str, start_offset: int, limit: int) -> tuple[list[str], int]:
    query_clean = query.replace("\r\n", "\n").replace("\r", "\n")
    query_lower = query_clean.lower()
    matches = []
    total_matches = 0
    window_end = start_offset + limit

    if "\n" in query_lower:
        full_content = "".join(lines)
        full_content_lower = full_content.lower()
        pos = 0
        while True:
            idx = full_content_lower.find(query_lower, pos)
            if idx == -1:
                break
            line_number = full_content[:idx].count("\n") + 1
            first_line = lines[line_number - 1].strip() if line_number - 1 < len(lines) else ""
            if start_offset <= total_matches < window_end:
                matches.append(f"{line_number}: {first_line}")
            total_matches += 1
            pos = idx + 1
    else:
        for line_number, line in enumerate(lines, start=1):
            if query_lower not in line.lower():
                continue

            if start_offset <= total_matches < window_end:
                matches.append(f"{line_number}: {line.strip()}")
            total_matches += 1

    return matches, total_matches


def _iter_plaintext_match_records(lines: list[str], query: str, file_label: str):
    query_clean = query.replace("\r\n", "\n").replace("\r", "\n")
    query_lower = query_clean.lower()
    if "\n" in query_lower:
        full_content = "".join(lines)
        full_content_lower = full_content.lower()
        pos = 0
        while True:
            idx = full_content_lower.find(query_lower, pos)
            if idx == -1:
                break
            line_number = full_content[:idx].count("\n") + 1
            first_line = lines[line_number - 1].strip() if line_number - 1 < len(lines) else ""
            yield f"{file_label}:{line_number}: {first_line}"
            pos = idx + 1
    else:
        for line_number, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                yield f"{file_label}:{line_number}: {line.strip()}"


def _iter_searchable_text_files(target_dir: Path):
    ignored_dirs = get_ignored_dirs()
    for file_path in target_dir.rglob("*"):
        if not file_path.is_file():
            continue

        relative_parts = file_path.relative_to(target_dir).parts[:-1]
        if any(part in ignored_dirs or part.startswith(".") for part in relative_parts):
            continue
        if file_path.suffix.lower() not in SEARCHABLE_TEXT_SUFFIXES:
            continue
        yield file_path

async def nagato_searchInFile(query: str, file: str, max_results: int = 5, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    Performs a simple full-text search within a specific source file.
    This is a PLAIN SUBSTRING match — no regex, no wildcards.
    WHEN TO USE: searching for literal strings, variable values, import names, or specific text in comments/docstrings.
    WHEN NOT TO USE: searching for functions, classes, or variables by name — use nagato_searchAST instead.
    DO NOT simulate regex by calling this multiple times with partial patterns — use nagato_searchAST for pattern-like symbol lookups.
    Args:
        query: Literal search term (case-insensitive, substring match). NOT a regex pattern.
        file: Relative path to the file from the workspace root.
        max_results: Maximum number of results to display (default: 5).
        start_offset: Optional zero-based offset into the match list.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_file = workspace_root / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_searchInFile")

    if not target_file.exists():
        return nagato_error(f"File {file} not found.", tool="nagato_searchInFile")

    try:
        with target_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        start_offset, max_results = _normalize_window(start_offset, max_results)
        matches, total_matches = _collect_plaintext_matches(lines, query, start_offset, max_results)

        if not matches:
            if total_matches == 0:
                return f"No matches for '{query}' in {file}."
            return (
                f"Matches for '{query}' in {file} ({total_matches} total), "
                f"but none in the requested window [start_offset={start_offset}, limit={max_results}]."
            )

        header = _format_window_header(f"Matches for '{query}' in {file}", total_matches, start_offset, len(matches))
        
        result = header + ":\n" + "\n".join(matches)
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_searchInFile")
        return notice + _truncate_to_token_limit(result, ctx=ctx)

    except Exception as e:
        return nagato_error(str(e), tool="nagato_searchInFile")

async def nagato_searchInFiles(query: str, dir: str, max_results_per_file: int = 3, start_offset: int = 0, max_files: int = 10, _ctx: Optional[Any] = None) -> str:
    """
    Performs a simple full-text search across project text files in a directory.
    This is a PLAIN SUBSTRING match — no regex, no wildcards.
    WHEN TO USE: searching for a literal string that may appear across multiple files (e.g. a constant name, an import, a specific error message).
    WHEN NOT TO USE: searching for functions, classes, or variables by name — use nagato_searchAST instead.
    DO NOT use this as a substitute for regex pattern matching across files — that approach is error-prone and slow.
    Args:
        query: Literal search term (case-insensitive, substring match). NOT a regex pattern.
        dir: Relative path to the directory from the workspace root.
        max_results_per_file: Maximum number of hits to display per file within one page (default: 3).
        start_offset: Optional zero-based offset into the flattened global hit list.
        max_files: Maximum number of total hits to display in this page (default: 10).
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_dir = workspace_root / dir

    # Guard: prevent path traversal outside workspace
    resolved = target_dir.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {dir} resolves outside workspace root.", tool="nagato_searchInFiles")

    if not target_dir.exists() or not target_dir.is_dir():
        return nagato_error(f"Directory {dir} not found.", tool="nagato_searchInFiles")

    start_offset, max_files = _normalize_window(start_offset, max_files)
    per_file_limit = max(1, max_results_per_file)

    results = []
    per_file_counts: dict[str, int] = {}
    total_hits = 0
    files_scanned = 0
    window_end = start_offset + max_files
    for file_path in _iter_searchable_text_files(target_dir):
        files_scanned += 1
        relative_path = file_path.relative_to(workspace_root)
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for match_record in _iter_plaintext_match_records(lines, query, str(relative_path)):
            if start_offset <= total_hits < window_end:
                file_hits_in_page = per_file_counts.get(str(relative_path), 0)
                if file_hits_in_page < per_file_limit:
                    results.append(match_record)
                    per_file_counts[str(relative_path)] = file_hits_in_page + 1
            total_hits += 1

    if not results:
        if total_hits == 0:
            return f"No matches for '{query}' in {dir} (searched {files_scanned} text files)."
        return (
            f"Matches for '{query}' in {dir} found ({total_hits} total), "
            f"but none in the requested window [start_offset={start_offset}, limit={max_files}]."
        )

    header = _format_window_header(f"Matches for '{query}' in {dir}", total_hits, start_offset, len(results))
    header += f" [file_cap={per_file_limit}]"

    result = header + ":\n" + "\n".join(results)
    notice = _check_irrelevance_guard(dir, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_searchInFiles")

    alerts = []
    seen_alert_files = set()
    for fp in per_file_counts:
        if fp not in seen_alert_files:
            seen_alert_files.add(fp)
            alert = check_radar_alert_for_file(fp, _ctx)
            if alert:
                alerts.append(alert)
    alert_prefix = ("\n".join(alerts) + "\n\n") if alerts else ""

    return alert_prefix + notice + _truncate_to_token_limit(result, ctx=ctx)


async def nagato_semantic_search(query: str, limit: int = 3, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    Performs semantic vector search across the indexed code chunks.
    WHEN TO USE: searching by intent, behavior, or natural-language description when literal text or exact symbol names are unknown.
    WHEN NOT TO USE: searching for exact strings or known symbol names — use nagato_searchInFile or nagato_searchAST instead.
    Args:
        query: Natural-language or code-intent query for semantic retrieval.
        limit: Maximum number of results to display in this page (default: 3).
        start_offset: Optional zero-based offset into the global semantic ranking.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    try:
        config = _get_semantic_config()
        indexer = SemanticIndexSearch(config={
            "db_path": config.db_path,
            "embedding_model": config.embedding_model,
        })
        
        # Auto-index check - pass ctx so it uses the resolved semantic search root
        auto_index_summary = ""
        try:
            auto_index_summary = indexer.ensure_index_current(ctx=ctx)
        except Exception as e:
            print(f"Warning: Failed to ensure semantic index is current: {e}")

        result = indexer.core_nagato_semantic_search(query, limit=limit, start_offset=start_offset)
        
        if auto_index_summary:
            result = f"{auto_index_summary}\n\n{result}"
            
        return _truncate_to_token_limit(result, ctx=ctx)
    except Exception as e:
        return nagato_error(str(e), tool="nagato_semantic_search")

async def nagato_find_file(filename: str, dir: str = ".", _ctx: Optional[Any] = None) -> str:
    """
    Recursively searches for a file by its exact name within the specified directory.
    WHEN TO USE: You know the filename (e.g. 'test_fixture.py') but not its relative path.
    Args:
        filename: The exact name of the file to search for.
        dir: Relative path to the starting directory from the workspace root (default: ".").
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    workspace_root = _get_workspace_root(_ctx)
    target_dir = workspace_root / dir

    # Guard: prevent path traversal outside workspace
    resolved = target_dir.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {dir} resolves outside workspace root.", tool="nagato_find_file")

    if not target_dir.exists() or not target_dir.is_dir():
        return nagato_error(f"Directory {dir} not found.", tool="nagato_find_file")

    try:
        # Recursive glob search for the exact filename
        matches = list(target_dir.rglob(filename))
        
        if not matches:
            return f"No file named '{filename}' found in {dir}."
        
        results = []
        for p in matches:
            # Return path relative to the workspace root for consistency with other tools
            results.append(str(p.relative_to(workspace_root)))
            
        notice = _check_irrelevance_guard(dir, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_find_file")
        return notice + "Files found:\n" + "\n".join(results)

    except Exception as e:
        return nagato_error(str(e), tool="nagato_find_file")

async def nagato_searchAST(query: str, file: str, max_results: int = 5, start_offset: int = 0, _ctx: Optional[Any] = None) -> str:
    """
    Searches for functions, classes, or variables in the AST of a SINGLE FILE that match the query in their name.
    Structurally aware: only matches actual symbol definitions, never false-positives from comments or strings.
    LIMITATION: Single-file only. For cross-file / project-wide symbol search, use nagato_read_signatures(target_symbol=...) instead.
    WHEN TO USE: you know which file to look in and want to find symbols by partial name.
    WHEN NOT TO USE: you want to find a symbol across the whole codebase — use nagato_read_signatures for that.
    Args:
        query: Partial symbol name to search for (case-insensitive substring match). NOT a regex.
        file: Relative path to the file from the workspace root.
        max_results: Maximum number of results to display (default: 5).
        start_offset: Optional zero-based offset into the match list.
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    import ast

    workspace_root = _get_workspace_root(_ctx)
    target_file = workspace_root / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_searchAST")

    if not target_file.exists():
        return nagato_error(f"File {file} not found.", tool="nagato_searchAST")

    try:
        query_clean = query.strip()
        with target_file.open("r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=str(target_file))

        start_offset, max_results = _normalize_window(start_offset, max_results)
        matches = []
        total_matches = 0
        window_end = start_offset + max_results
        # Inside your ast.walk(tree) loop:
        for node in ast.walk(tree):
            # 1. Classes and Functions
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if query_clean.lower() in node.name.lower():
                    kind = "Class" if isinstance(node, ast.ClassDef) else "Function"
                    if start_offset <= total_matches < window_end:
                        matches.append(f"{kind}: {node.name} (line {node.lineno})")
                    total_matches += 1

            # 2. Zuweisungen (Variablen & Attribute)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    # Fall A: Normale Variable (z.B. MAX_RETRIES = 5)
                    if isinstance(target, ast.Name) and query_clean.lower() in target.id.lower():
                        if start_offset <= total_matches < window_end:
                            matches.append(f"Variable: {target.id} (line {node.lineno})")
                        total_matches += 1
                    
                    # Fall B: Instanz-Attribut (z.B. self.RECOVERY = ...)
                    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                        if target.value.id == "self" and query_clean.lower() in target.attr.lower():
                            if start_offset <= total_matches < window_end:
                                matches.append(f"Attribute: self.{target.attr} (line {node.lineno})")
                            total_matches += 1

            # 3. Typisierte Zuweisungen (z.B. state: str = "INITIAL")
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and query_clean.lower() in node.target.id.lower():
                    if start_offset <= total_matches < window_end:
                        matches.append(f"Variable: {node.target.id} (line {node.lineno})")
                    total_matches += 1
            

        if not matches:
            if total_matches == 0:
                return f"No AST matches for '{query}' in {file}."
            return (
                f"AST matches for '{query}' in {file} ({total_matches} total), "
                f"but none in the requested window [start_offset={start_offset}, limit={max_results}]."
            )

        header = _format_window_header(f"AST matches for '{query}' in {file}", total_matches, start_offset, len(matches))
        
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=workspace_root, tool_name="nagato_searchAST")
        return notice + header + ":\n" + "\n".join(matches)

    except Exception as e:
        return nagato_error(str(e), tool="nagato_searchAST")

async def nagato_rebuild_symbol_db(dir: str = None, _ctx: Optional[Any] = None) -> str:
    """
    Completely rebuilds the SQLite symbol and vector database by scanning Python files.
    If dir is specified, only that directory is cleared and re-indexed.
    Args:
        dir: Optional relative path of the directory to rebuild (e.g., 'Server').
        _ctx: Optional session context (injected by facade).
    """
    ctx = _get_context(_ctx)
    try:
        indexer = SemanticIndexSearch()
        result = indexer.rebuild_symbol_db(dir, ctx)
        return result
    except Exception as e:
        return nagato_error(str(e), tool="nagato_rebuild_symbol_db")


async def nagato_set_semantic_search_root(path: str, _ctx: Optional[Any] = None) -> str:
    """
    Set the semantic search root directory and immediately reindex it.
    
    This tool configures which directory is used as the base for semantic indexing/search.
    It supports both session mode (session-scoped override) and standalone mode (config file base).
    
    The new root takes effect immediately with a synchronous reindex, avoiding the "search triggers
    a surprise lazy index of the wrong dir" problem.
    
    Args:
        path: Directory path to set as the semantic search root. Can be absolute or relative to workspace root.
              Can point outside the workspace (external directory) - this is explicitly allowed for
              read+embed indexing only, never for edit/write/execute/git tools.
        _ctx: Optional session context (injected by facade).
    
    Returns:
        Summary message including resolved path, persistence scope, external flag, and indexing summary.
    """
    ctx = _get_context(_ctx)
    
    # Resolve the path (absolute as-is, or relative to workspace root)
    workspace_root = _get_workspace_root(_ctx)
    target_path = Path(path)
    if not target_path.is_absolute():
        target_path = (workspace_root / target_path).resolve()
    else:
        target_path = target_path.resolve()
    
    # Validate: must exist and be a directory
    if not target_path.exists() or not target_path.is_dir():
        return nagato_error(f"Path '{path}' does not exist or is not a directory.", tool="nagato_set_semantic_search_root")
    
    # Detect session mode: host session vs standalone
    session_id = getattr(ctx, "session_id", None)
    is_session_mode = session_id not in (None, "standalone")
    
    # Determine if this is an external directory (outside workspace_root)
    is_external = not target_path.is_relative_to(workspace_root)
    
    # Persist based on mode
    if is_session_mode:
        # Session mode: set session override and persist to state file immediately
        ctx.semantic_search_root = str(target_path)
        # Import here to avoid circular import
        try:
            try:
                from fsm.session import write_state_file  # host-only  # host-only
            except (ImportError, Exception):
                write_state_file  # host-only = None  # type: ignore[misc]  # fsm-only; standalone gets None
        except ImportError:
            write_state_file = None  # type: ignore[assignment]
        (write_state_file or (lambda *_a, **_k: None))(ctx.FSM)
        persistence_scope = "session override (state file)"
    else:
        # Standalone mode: persist to config file
        save_semantic_search_root(str(target_path))
        persistence_scope = "config base (.nagato/functions_config.json)"
    
    # Immediately reindex synchronously
    indexer = SemanticIndexSearch()
    index_summary = indexer.index_directory_tree(str(target_path), external=is_external)
    
    # Build return message
    external_note = ""
    if is_external:
        external_note = "\nNote: Indexed source text/embeddings from outside the workspace are now stored in the local semantic-search DB (absolute paths used for external files)."
    
    return (
        f"Semantic search root set to: {target_path}\n"
        f"Persisted to: {persistence_scope}\n"
        f"External directory: {is_external}\n"
        f"Reindex summary: {index_summary}{external_note}"
    )


# Non-prefixed aliases for MCP server compatibility
searchInFile = nagato_searchInFile
searchInFiles = nagato_searchInFiles
semantic_search = nagato_semantic_search
find_file = nagato_find_file
searchAST = nagato_searchAST
rebuild_symbol_db = nagato_rebuild_symbol_db
set_semantic_search_root = nagato_set_semantic_search_root
__all__ = ['nagato_searchInFile', 'nagato_searchInFiles', 'nagato_semantic_search', 'nagato_find_file', 'nagato_searchAST', 'nagato_rebuild_symbol_db', 'nagato_set_semantic_search_root']
