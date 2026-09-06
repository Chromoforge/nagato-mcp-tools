import ast
import re
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root
from nagato_tools._post_edit_safety import _maybe_rollback_after_edit
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.lint import nagato_lint, validate_content_syntax
from nagato_tools.read import _check_irrelevance_guard
from nagato_tools.semanticindex import SemanticIndexSearch
def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


# Bound by NagatoFSM on startup.
fsm_instance = None


def set_active_fsm(instance) -> None:
    """Registers the active session instance for edit tracking hooks."""
    global fsm_instance
    fsm_instance = instance


def _get_fsm_instance():
    if fsm_instance is None:
        raise RuntimeError("Session instance not bound. Call set_active_session() during host initialization.")
    return fsm_instance


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to global session."""
    if ctx is not None:
        return ctx
    fsm = _get_fsm_instance()
    return fsm.Context


def _get_completion_message(ctx: Any) -> str:
    """Generates a context-aware completion message based on session state."""
    state = getattr(ctx, "State", "INITIAL")
    return f"If {state} is completely done, advance the session with 'report_fix'."


def _check_post_edit(file: str, _ctx: Any, tool_name: str) -> Optional[str]:
    """Run _maybe_rollback_after_edit; if it rolled back, return a clear message.

    Returns None on the happy path (caller proceeds normally).
    Returns a "PARTIAL SUCCESS (auto-rolled back): ..." string the caller should
    forward to the LLM so the failure mode is visible — instead of silently
    reporting SUCCESS for an edit that will be invisible to the Insight graph.
    """
    try:
        result = _maybe_rollback_after_edit(file, _ctx, tool_name)
    except Exception as e:  # pragma: no cover - defensive
        return None
    if not result.get("rolled_back"):
        return None
    return (
        f"PARTIAL SUCCESS (auto-rolled back): edit to {file} was reverted "
        f"because Insight sync did not complete. Reason: {result.get('reason', 'unknown')}. "
        f"Retry the edit after ensuring Insight is enabled and an event loop is running."
    )


def _ensure_parent_dirs(target_file: Path) -> None:
    """Ensure parent directories exist atomically."""
    target_file.parent.mkdir(parents=True, exist_ok=True)


def _detect_ast_symbol_bounds(
    content: str,
    new_code: str,
    file: str,
    start_line: int,
    end_line: int,
) -> tuple[int, int, str]:
    """
    Attempts to auto-correct start_line and end_line if the LLM hallucinated
    line numbers for a function or class definition.
    """
    if not file.endswith(".py") or not new_code.strip():
        return start_line, end_line, ""

    # Check if new_code defines a top-level or method symbol
    match = re.search(
        r'^\s*(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)|^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)',
        new_code,
        re.MULTILINE,
    )
    if not match:
        return start_line, end_line, ""

    sym_name = match.group(1) or match.group(2)
    try:
        tree = ast.parse(content)
    except Exception:
        return start_line, end_line, ""

    actual_bounds: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == sym_name:
                start = node.lineno
                end = getattr(node, "end_lineno", node.lineno)
                actual_bounds = (start, end)
                break

    if not actual_bounds:
        return start_line, end_line, ""

    actual_start, actual_end = actual_bounds

    # Check if start_line and end_line are significantly off
    total_lines = len(content.splitlines())
    is_out_of_bounds = (
        start_line < 1
        or start_line > total_lines
        or end_line < start_line - 1
        or end_line > total_lines
    )
    is_mislocated = not (actual_start <= start_line <= actual_end)

    if is_out_of_bounds or is_mislocated:
        note = (
            f" (Note: Lines auto-corrected from {start_line}-{end_line} "
            f"to {actual_start}-{actual_end} based on symbol '{sym_name}')"
        )
        return actual_start, actual_end, note

    return start_line, end_line, ""


async def nagato_edit(
    file: str,
    searchstring: str,
    replacement: str,
    _ctx: Optional[Any] = None
) -> str:
    """
    Performs a single targeted string replacement in a workspace file.
    Creates the file if it doesn't exist (auto-create).

    Args:
        file: Relative path to the file from the workspace root.
        searchstring: Case-sensitive string to replace. Must match exactly one occurrence.
                      For NEW files (auto-create), this MUST be empty string "" or None.
        replacement: Replacement text written back to disk.
                     For NEW files, this becomes the entire file content.
        _ctx: Optional session context (injected by facade). If not provided, uses the global session.

    Notes:
        The edit tracks the file for undo support, updates the symbol index,
        snapshots the post-edit state for revert support, and then runs lint/syntax
        validation. For MCP callers, JSON in `target` is the safest way to pass
        text containing spaces or newlines.

        Atomicity (Insight-graph + on-disk file):
        If force-insight is enabled AND this tool is in `forced_insight_tools`,
        AND the follow-up Insight sync returns `sync_queued=False`, the engine
        auto-rolls back the file change to preserve file+Insight atomicity.
        The return value in that case is "PARTIAL SUCCESS (auto-rolled back): ..."
        instead of "SUCCESS: ...". See fsm/functions_internal/_post_edit_safety.py.
    """
    target_file = _get_workspace_root(_ctx) / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = _get_workspace_root(_ctx).resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_edit")

    file_exists = target_file.exists()

    # AUTO-CREATE LOGIC: If file doesn't exist, validate searchstring is empty
    if not file_exists:
        if searchstring not in ("", None):
            return nagato_error(
                f"File {file} does not exist. To auto-create a file with nagato_edit, "
                f"'searchstring' must be empty string or None.",
                tool="nagato_edit"
            )
        # Pre-validate syntax in-memory before disk write
        syntax_err = validate_content_syntax(replacement, file)
        if syntax_err:
            return nagato_error(f"Edit rejected: {syntax_err}", tool="nagato_edit")

        # Ensure parent directories exist
        _ensure_parent_dirs(target_file)
        ctx = _get_context(_ctx)
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(file, old_content=None, new_content=replacement, is_new=True)
        action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
        if action_context is not None and hasattr(action_context, "record_file_change"):
            action_context.record_file_change(file, before_content=None, after_content=replacement, is_new=True)
        
        # Write replacement as full file content
        target_file.write_text(replacement, encoding="utf-8", newline="")
        ctx.mark_file_modified(file)

        # SYMBOL UPDATE: AST-only, kein ML-Modell, kein OOM-Risiko
        SemanticIndexSearch().update_symbol_tables_only(file)
        sync_queued = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued)
            rolled = _check_post_edit(file, _ctx, "nagato_edit")
            if rolled:
                return rolled

        lint_result = await nagato_lint(file)
        if "SUCCESS" in lint_result:
            msg = _get_completion_message(ctx)
            notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if sync_queued else ""
            return f"{notice}SUCCESS: File {file} created with content ({len(replacement)} bytes). Lint: OK.\n{msg}"
        else:
            return f"PARTIAL SUCCESS: File {file} created, but LINT/SYNTAX FAILED:\n{lint_result}"

    # EXISTING FILE LOGIC (unchanged)
    try:
        ctx = _get_context(_ctx)
        
        # Check irrelevance guard for existing files
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=_get_workspace_root(_ctx), tool_name="nagato_edit")
        if notice.startswith("ERROR:"):
            return notice
        
        # Read as binary first to detect encoding issues
        raw_content = target_file.read_bytes()
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as e:
            return nagato_error(
                f"File {file} contains invalid UTF-8 at byte {e.start}: {e.reason}. "
                f"Nagato only supports UTF-8 encoded files. Convert the file first.",
                tool="nagato_edit"
            )
        match_count = content.count(searchstring)
        if match_count == 0:
            # 1. Line-ending normalization (CRLF vs LF)
            if "\r\n" in content:
                # File uses CRLF, searchstring may use LF
                search_crlf = searchstring.replace("\r\n", "\n").replace("\n", "\r\n")
                if content.count(search_crlf) > 0:
                    searchstring = search_crlf
                    replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
                    match_count = content.count(searchstring)
            elif "\n" in content and "\r\n" not in content:
                # File uses LF, searchstring may use CRLF
                search_lf = searchstring.replace("\r\n", "\n")
                if content.count(search_lf) > 0:
                    searchstring = search_lf
                    replacement = replacement.replace("\r\n", "\n")
                    match_count = content.count(searchstring)

            # 2. Trailing newline tolerance at EOF
            if match_count == 0:
                if searchstring.endswith(("\r\n", "\n")):
                    stripped_search = searchstring.rstrip("\r\n")
                    if stripped_search and content.count(stripped_search) == 1:
                        searchstring = stripped_search
                        replacement = replacement.rstrip("\r\n")
                        match_count = content.count(searchstring)
                else:
                    file_ending = "\r\n" if "\r\n" in content else "\n"
                    search_with_end = searchstring + file_ending
                    if content.count(search_with_end) == 1:
                        searchstring = search_with_end
                        if not replacement.endswith(("\r\n", "\n")):
                            replacement = replacement + file_ending
                        match_count = content.count(searchstring)

        if match_count == 0:
            return nagato_error(f"'{searchstring}' not found in {file}.", tool="nagato_edit")
        if match_count > 1:
            return nagato_error(
                f"'{searchstring}' matched {match_count} times in {file}. "
                f"nagato_edit requires exactly one match. Use a more specific searchstring "
                f"or nagato_edit_lines for line-targeted edits.",
                tool="nagato_edit",
            )

        new_content = content.replace(searchstring, replacement, 1)

        # Pre-validate syntax in-memory before disk write
        syntax_err = validate_content_syntax(new_content, file)
        if syntax_err:
            return nagato_error(f"Edit rejected: {syntax_err}", tool="nagato_edit")
        
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(file, old_content=content, new_content=new_content, is_new=False)
        action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
        if action_context is not None and hasattr(action_context, "record_file_change"):
            action_context.record_file_change(file, before_content=content, after_content=new_content, is_new=False)
        
        # newline="" disables Windows os.linesep translation; content already
        # carries its original \n/\r\n bytes verbatim from the manual decode above.
        target_file.write_text(new_content, encoding="utf-8", newline="")
        ctx.mark_file_modified(file)

        # SYMBOL UPDATE: AST-only, kein ML-Modell, kein OOM-Risiko
        SemanticIndexSearch().update_symbol_tables_only(file)
        sync_queued = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued)
            rolled = _check_post_edit(file, _ctx, "nagato_edit")
            if rolled:
                return rolled

        lint_result = await nagato_lint(file)
        if "SUCCESS" in lint_result:
            msg = _get_completion_message(ctx)
            notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if sync_queued else ""
            return f"{notice}SUCCESS: '{searchstring}' was replaced with '{replacement}' in {file}. Lint: OK.\n{msg}"
        else:
            return f"PARTIAL SUCCESS: '{searchstring}' was replaced in {file}, but LINT/SYNTAX FAILED:\n{lint_result}"

    except Exception as e:
        return nagato_error(str(e), tool="nagato_edit")


async def nagato_edit_lines(
    file: str,
    start_line: int,
    end_line: int,
    new_code: str,
    _ctx: Optional[Any] = None
) -> str:
    """
    Replaces an inclusive line range in a file with new code.
    Creates the file if it doesn't exist (auto-create).

    Args:
        file: Relative path to the file.
        start_line: First line to replace (1-indexed).
        end_line: Last line to replace (inclusive, 1-indexed).
        new_code: Multi-line replacement text. An empty string deletes the range.
                  For NEW files (auto-create), this becomes the entire file content.
        _ctx: Optional session context (injected by facade). If not provided, uses the global session.

    Notes:
        The file is read preserving original line endings (CRLF/LF) and written back
        with the same line endings. The edit also updates session dirty tracking,
        records a post-edit snapshot, refreshes the symbol index, and runs
        lint/syntax validation. For MCP callers, JSON in `target` is recommended
        for multiline `new_code` payloads.
        
        For NEW files (auto-create): Must use start_line=1 and end_line=0 (or end_line < start_line).
        The new_code becomes the complete file content.
    """
    target_file = _get_workspace_root(_ctx) / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = _get_workspace_root(_ctx).resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_edit_lines")

    file_exists = target_file.exists()

    # AUTO-CREATE LOGIC: If file doesn't exist, validate start_line/end_line
    if not file_exists:
        # Strict validation: only start_line=1 and end_line=0 (or end_line < start_line) allowed
        if not (start_line == 1 and (end_line == 0 or end_line < start_line)):
            return nagato_error(
                f"File {file} does not exist. To create a new file via nagato_edit_lines, "
                f"set start_line=1 and end_line=0.",
                tool="nagato_edit_lines"
            )
        # Pre-validate syntax in-memory before disk write
        syntax_err = validate_content_syntax(new_code, file)
        if syntax_err:
            return nagato_error(f"Edit rejected: {syntax_err}", tool="nagato_edit_lines")

        # Ensure parent directories exist
        _ensure_parent_dirs(target_file)
        ctx = _get_context(_ctx)
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(file, old_content=None, new_content=new_code, is_new=True)
        action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
        if action_context is not None and hasattr(action_context, "record_file_change"):
            action_context.record_file_change(file, before_content=None, after_content=new_code, is_new=True)
        
        # Write new_code as full file content
        target_file.write_text(new_code, encoding="utf-8", newline="")
        ctx.mark_file_modified(file)

        # SYMBOL UPDATE: AST-only, kein ML-Modell, kein OOM-Risiko
        SemanticIndexSearch().update_symbol_tables_only(file)
        sync_queued = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued)
            rolled = _check_post_edit(file, _ctx, "nagato_edit_lines")
            if rolled:
                return rolled

        lint_result = await nagato_lint(file)
        if "SUCCESS" in lint_result:
            msg = _get_completion_message(ctx)
            notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if sync_queued else ""
            return f"{notice}SUCCESS: File {file} created with content ({len(new_code)} bytes). Lint: OK.\n{msg}"
        else:
            return f"PARTIAL SUCCESS: File {file} created, but LINT/SYNTAX FAILED:\n{lint_result}"

    # EXISTING FILE LOGIC (unchanged)
    try:
        ctx = _get_context(_ctx)
        
        # Check irrelevance guard for existing files
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=_get_workspace_root(_ctx), tool_name="nagato_edit_lines")
        if notice.startswith("ERROR:"):
            return notice
        
        # Read file preserving line endings
        raw_content = target_file.read_bytes()
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as e:
            return nagato_error(
                f"File {file} contains invalid UTF-8 at byte {e.start}: {e.reason}. "
                f"Nagato only supports UTF-8 encoded files. Convert the file first.",
                tool="nagato_edit_lines"
            )

        # Adopt target file's predominant line ending style for new_code
        if "\r\n" in content and "\r\n" not in new_code and "\n" in new_code:
            new_code = new_code.replace("\r\n", "\n").replace("\n", "\r\n")
        elif "\r\n" not in content and "\n" in content and "\r\n" in new_code:
            new_code = new_code.replace("\r\n", "\n")
        
        # Split preserving line endings
        lines_with_ends = content.splitlines(keepends=True)
        total_lines = len(lines_with_ends)
        
        # AST Line Auto-Correction (for drifted or hallucinated line numbers)
        start_line, end_line, auto_corrected_info = _detect_ast_symbol_bounds(
            content, new_code, file, start_line, end_line
        )

        # ALIGNMENT LOGIC (pre-parser auto-fix for context line duplication)
        if start_line <= end_line and new_code.strip() and not auto_corrected_info:
            file_lines_clean = [line.strip() for line in lines_with_ends]
            new_lines_clean = [line.strip() for line in new_code.splitlines()]

            # 1. Prefix alignment
            prefix_len = 0
            max_possible_prefix = min(start_line - 1, len(new_lines_clean))
            for i in range(1, max_possible_prefix + 1):
                if new_lines_clean[:i] == file_lines_clean[start_line - 1 - i : start_line - 1]:
                    prefix_len = i

            if prefix_len > 0:
                start_line = start_line - prefix_len

            # 2. Suffix alignment
            def is_trivial_line(line: str) -> bool:
                s_line = line.strip()
                return not s_line or s_line in (
                    ")", "]", "}", "},", "),", "],", "", '""', "''", "[],", "()", ",", ";"
                )

            suffix_len = 0
            matched_idx_in_file = -1
            max_possible_suffix = min(len(new_lines_clean) - prefix_len, 50)

            found_suffix = False
            for s in range(max_possible_suffix, 0, -1):
                suffix = new_lines_clean[len(new_lines_clean) - s :]
                has_non_trivial = any(not is_trivial_line(l) for l in suffix)
                if not has_non_trivial and s < 3:
                    continue

                max_lookahead = 100
                for file_idx in range(end_line, min(total_lines - s + 1, end_line + max_lookahead)):
                    if file_lines_clean[file_idx : file_idx + s] == suffix:
                        suffix_len = s
                        matched_idx_in_file = file_idx
                        found_suffix = True
                        break
                if found_suffix:
                    break

            if found_suffix and matched_idx_in_file != -1:
                end_line = matched_idx_in_file + suffix_len
        
        # Out-of-bounds check
        if start_line < 1 or start_line > total_lines:
            return nagato_error(
                f"start_line {start_line} is out of bounds (file has {total_lines} lines).",
                tool="nagato_edit_lines"
            )
        if end_line < start_line - 1 or end_line > total_lines:
            return nagato_error(
                f"end_line {end_line} is out of bounds (file has {total_lines} lines, start_line={start_line}).",
                tool="nagato_edit_lines"
            )
        
        # Track for standalone undo - capture old content before modification
        old_content_for_undo = content
        
        idx_start = start_line - 1
        idx_end = end_line  # end_line is inclusive, so slice end is end_line
        
        # Split new_code preserving its line endings
        new_lines_with_ends = new_code.splitlines(keepends=True)
        
        # Determine if the original file ended with a line ending
        original_ends_with_newline = lines_with_ends and lines_with_ends[-1].endswith(('\n', '\r\n'))
        
        # If new_code doesn't end with a line ending but we're not at the end of file,
        # we need to preserve the line ending from the original line that follows
        if new_lines_with_ends and not new_lines_with_ends[-1].endswith(('\n', '\r\n')):
            if idx_end < total_lines:
                # Get the line ending from the next original line
                next_line = lines_with_ends[idx_end]
                if next_line.endswith('\r\n'):
                    new_lines_with_ends[-1] += '\r\n'
                elif next_line.endswith('\n'):
                    new_lines_with_ends[-1] += '\n'
            elif original_ends_with_newline:
                # We're replacing at the end of file and original had a trailing newline
                # Preserve the line ending style from the original last line
                last_original_line = lines_with_ends[-1]
                if last_original_line.endswith('\r\n'):
                    new_lines_with_ends[-1] += '\r\n'
                elif last_original_line.endswith('\n'):
                    new_lines_with_ends[-1] += '\n'
        
        working_lines = list(lines_with_ends)
        working_lines[idx_start:idx_end] = new_lines_with_ends
        
        new_content_for_undo = "".join(working_lines)

        # Pre-validate syntax in-memory before disk write
        syntax_err = validate_content_syntax(new_content_for_undo, file)
        if syntax_err:
            return nagato_error(f"Edit rejected: {syntax_err}", tool="nagato_edit_lines")
        
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(file, old_content=old_content_for_undo, new_content=new_content_for_undo, is_new=False)
        action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
        if action_context is not None and hasattr(action_context, "record_file_change"):
            action_context.record_file_change(file, before_content=old_content_for_undo, after_content=new_content_for_undo, is_new=False)
        
        # newline="" disables Windows os.linesep translation to avoid doubling
        # the \r\n endings we just assembled by hand.
        target_file.write_text(new_content_for_undo, encoding="utf-8", newline="")
        ctx.mark_file_modified(file)

        SemanticIndexSearch().update_symbol_tables_only(file)
        sync_queued = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued)
            rolled = _check_post_edit(file, _ctx, "nagato_edit_lines")
            if rolled:
                return rolled

        lint_result = await nagato_lint(file)
        if "SUCCESS" in lint_result:
            msg = _get_completion_message(ctx)
            notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if sync_queued else ""
            return f"{notice}SUCCESS: Lines {start_line} to {end_line} in {file} successfully updated{auto_corrected_info}. Lint: OK.\n{msg}"
        else:
            return f"PARTIAL SUCCESS: Lines {start_line} to {end_line} in {file} updated{auto_corrected_info}, but LINT/SYNTAX FAILED:\n{lint_result}"

    except Exception as e:
        return nagato_error(str(e), tool="nagato_edit_lines")


async def nagato_delete(
    file: str,
    _ctx: Optional[Any] = None
) -> str:
    """
    Deletes a file from the workspace.
    
    Args:
        file: Relative path to the file from the workspace root.
        _ctx: Optional session context (injected by facade). If not provided, uses the global session.

    Notes:
        - The file must exist (no silent no-op).
        - Marks the file dirty in the active session for ledger tracking.
        - Tracks as existing file (is_new: False) so restore_from_ledger can recover it (null-to-restore).
        - Returns success message with file path and size deleted.
    """
    target_file = _get_workspace_root(_ctx) / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = _get_workspace_root(_ctx).resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_delete")

    if not target_file.exists():
        return nagato_error(f"File {file} not found.", tool="nagato_delete")

    try:
        ctx = _get_context(_ctx)
        
        # Check irrelevance guard for delete
        notice = _check_irrelevance_guard(file, ctx=ctx, workspace_root=_get_workspace_root(_ctx), tool_name="nagato_delete")
        if notice.startswith("ERROR:"):
            return notice
        
        # Get file size before deletion for success message
        file_size = target_file.stat().st_size
        
        # Read content for standalone undo tracking
        content = target_file.read_text(encoding="utf-8")

        ctx = _get_context(_ctx)
        
        # Mark file dirty in context (works with both real session and mock)
        ctx.mark_file_modified(file)
        
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(file, old_content=content, is_new=False, is_delete=True)
        action_context = getattr(ctx, "active_action", None) or getattr(ctx, "action_context", None)
        if action_context is not None and hasattr(action_context, "record_file_change"):
            action_context.record_file_change(file, before_content=content, after_content=None, is_new=False, is_delete=True)
        
        # Delete the file
        target_file.unlink()

        # Update symbol index
        SemanticIndexSearch().update_symbol_tables_only(file)
        sync_queued = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued)
            rolled = _check_post_edit(file, _ctx, "nagato_delete")
            if rolled:
                return rolled

        notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if sync_queued else ""
        return f"{notice}SUCCESS: File {file} deleted ({file_size} bytes)."
    except Exception as e:
        return nagato_error(str(e), tool="nagato_delete")


async def nagato_rename(
    source: str,
    destination: str,
    overwrite: bool = False,
    _ctx: Optional[Any] = None
) -> str:
    """
    Renames/moves a file within the workspace.
    
    Args:
        source: Relative path to the source file from the workspace root.
        destination: Relative path to the destination file from the workspace root.
        overwrite: If True, overwrite destination if it exists. Default: False.
        _ctx: Optional session context (injected by facade). If not provided, uses the global session.

    Notes:
        - The source file must exist.
        - Destination must not exist unless overwrite=True.
        - Both source and destination must be within workspace root.
        - Uses atomic os.replace() on same filesystem; falls back to shutil.move() for cross-filesystem.
        - Marks both source and destination dirty in the active session for ledger tracking.
        - Tracks both as existing files (is_new: False) so restore_from_ledger can recover them.
        - Returns success message with source path, destination path, and size.
    """
    source_file = _get_workspace_root(_ctx) / source
    dest_file = _get_workspace_root(_ctx) / destination

    # Guard: prevent path traversal outside workspace for source
    resolved_source = source_file.resolve()
    workspace_root_resolved = _get_workspace_root(_ctx).resolve()
    if not resolved_source.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Source path {source} resolves outside workspace root.", tool="nagato_rename")

    # Guard: prevent path traversal outside workspace for destination
    resolved_dest = dest_file.resolve()
    if not resolved_dest.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Destination path {destination} resolves outside workspace root.", tool="nagato_rename")

    if not source_file.exists():
        return nagato_error(f"Source file {source} not found.", tool="nagato_rename")

    if dest_file.exists() and not overwrite:
        return nagato_error(f"Destination file {destination} already exists. Use overwrite=True to replace.", tool="nagato_rename")

    try:
        ctx = _get_context(_ctx)
        
        # Check irrelevance guard for rename (source file)
        notice = _check_irrelevance_guard(source, ctx=ctx, workspace_root=_get_workspace_root(_ctx), tool_name="nagato_rename")
        if notice.startswith("ERROR:"):
            return notice
        
        # Get file size before rename for success message
        file_size = source_file.stat().st_size
        
        # Read content for standalone undo tracking
        content = source_file.read_text(encoding="utf-8")

        ctx = _get_context(_ctx)
        
        # Mark both source and destination dirty in context
        ctx.mark_file_modified(source)
        ctx.mark_file_modified(destination)
        
        # Track for standalone undo
        if hasattr(ctx, 'track_edit_for_undo'):
            ctx.track_edit_for_undo(source, old_content=content, new_content=None, is_new=False, is_delete=True)
            ctx.track_edit_for_undo(destination, old_content=None, new_content=content, is_new=True)
        
        # Perform the rename/move
        # Use os.replace() for atomic operation on same filesystem
        # Falls back to shutil.move() for cross-filesystem moves
        try:
            source_file.replace(dest_file)
        except OSError as e:
            if e.errno == 18:  # EXDEV - cross-device link
                import shutil
                shutil.move(str(source_file), str(dest_file))
            else:
                raise
        
        # Update symbol index for both files
        SemanticIndexSearch().update_symbol_tables_only(source)
        SemanticIndexSearch().update_symbol_tables_only(destination)
        sync_queued_source = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued_source)
        sync_queued_dest = None  # Insight disabled
        if _ctx is not None:
            setattr(_ctx, "_last_insight_sync_queued", sync_queued_dest)
            rolled = _check_post_edit(destination, _ctx, "nagato_rename")
            if rolled:
                return rolled

        notice = "[INSIGHT: refreshing knowledge graph for this file in background]\n" if (sync_queued_source or sync_queued_dest) else ""
        return f"{notice}SUCCESS: File {source} renamed to {destination} ({file_size} bytes)."
    except Exception as e:
        return nagato_error(str(e), tool="nagato_rename")


async def nagato_create_dir(
    dir: str,
    parents: bool = True,
    exist_ok: bool = True,
    _ctx: Optional[Any] = None
) -> str:
    """
    Creates a directory in the workspace.
    
    Args:
        dir: Relative path to the directory from the workspace root.
        parents: If True, create missing parent directories as needed. Default: True.
        exist_ok: If True, do not raise an error if directory already exists. Default: True.
        _ctx: Optional session context (injected by facade). If not provided, uses the global session.

    Notes:
        - Prevents path traversal outside workspace root.
        - Creates parent directories atomically when parents=True.
        - Returns success message indicating whether the directory was created or already existed.
    """
    workspace_root = _get_workspace_root(_ctx)
    target_dir = workspace_root / dir

    # Guard: prevent path traversal outside workspace
    resolved = target_dir.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {dir} resolves outside workspace root.", tool="nagato_create_dir")

    if target_dir.exists():
        if target_dir.is_file():
            return nagato_error(f"Path {dir} already exists and is a file.", tool="nagato_create_dir")
        if not exist_ok:
            return nagato_error(f"Directory {dir} already exists.", tool="nagato_create_dir")
        return f"Directory {dir} already exists."

    try:
        target_dir.mkdir(parents=parents, exist_ok=exist_ok)
        return f"SUCCESS: Directory {dir} created."
    except Exception as e:
        return nagato_error(str(e), tool="nagato_create_dir")


# Non-prefixed aliases for MCP server compatibility
edit = nagato_edit
edit_lines = nagato_edit_lines
delete = nagato_delete
rename = nagato_rename
create_dir = nagato_create_dir
__all__ = ['nagato_edit', 'nagato_edit_lines', 'nagato_delete', 'nagato_rename', 'nagato_create_dir']
