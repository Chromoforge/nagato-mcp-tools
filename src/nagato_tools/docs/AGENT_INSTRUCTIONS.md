<!-- 
Nagato MCP Tools — Agent Instructions (Standalone Mode)
-->

# Project Nagato // Nagato MCP Tools — Agent Instructions (Standalone Mode)

## Description

You are interacting with the **standalone** Nagato MCP Tools suite: a flat catalogue of `nagato_*` tools exposed directly by an MCP server. Each tool is a normal, independently-callable MCP tool — call whichever one fits the task at hand, using exactly the parameters its own tool description documents.

> **Tool Invocation**:
> Every tool (e.g. `nagato_edit`, `nagato_searchInFiles`, `nagato_read_signatures`, `nagato_undo_standalone`) is exposed directly as an individual MCP tool.
> Call the specific tool you need directly with its documented parameters. Do NOT simulate or print JSON tool calls in plain text — always invoke the tool directly.

## General Information

a) **Stateless per call**
   Every tool call is independent. A tool returns its result; the next tool call starts
   fresh. If you need multi-step context, keep it in your own reasoning or in
   scratch files.

b) **Session-scoped via `NAGATO_SESSION_ID`**
   Undo/redo history and scratch dirs can be scoped per project via the `NAGATO_SESSION_ID`
   environment variable or `--session-id` flag (e.g. `proj:my-app`). Default if unset: `"standalone"`.

c) **Persistent AST symbol database:
   - A lightweight **AST symbol database** (plain SQLite, no embeddings, no LLM involved) behind
     `nagato_read_signatures` / `nagato_extract_callers` / `nagato_extract_callees`. Built and
     refreshed on demand with `nagato_rebuild_symbol_db`.**

d) **Flat tool surface**
   Tools are grouped by category (EDIT, READ, SEARCH, TESTING, EXECUTE,
   WEB, GIT, CREATION, DEBUGGING, SHELL, SYSTEM) for documentation purposes only —
   there is no gating on which category may run when.


## Operational Protocol


1. **Getting started:**
   Call whichever tool fits your task directly — no setup call is required first.
   Optional: set `NAGATO_SESSION_ID` for session-isolated undo/scratch history (defaults to `standalone`).

2. **Choosing a code-exploration tool:**
   Several tools overlap in what they can technically find — picking the wrong one wastes turns
   or misses results. Match the tool to what you actually know:

   | You know... | Use | Why |
   | --- | --- | --- |
   | Exact symbol name, any file | `nagato_read_signatures(target_symbol=...)` (no `file_path`) | Queries the pre-built symbol DB across the whole repo — fast, no false positives from comments/strings. **PRIMARY tool for symbol lookup.** |
   | Exact symbol name + which file | `nagato_read_signatures(file_path=...)` | Same DB, scoped to one file; falls back to a live AST parse if that file isn't indexed, so it always works even on an empty DB. |
   | Partial/fuzzy symbol name, single file only | `nagato_searchAST(query, file)` | Structural AST substring match. Single-file only — for fuzzy cross-file lookup, there is no direct equivalent; narrow with `nagato_find_file` first, or use `nagato_read_signatures` with the exact name once you know it. |
   | Who calls / is called by a symbol | `nagato_extract_callers` / `nagato_extract_callees` | Reads the same symbol DB's call table. Direct function/method calls only — no inheritance, no decorators. |
   | A literal string, constant, error message, import, config value | `nagato_searchInFile` / `nagato_searchInFiles` | Plain case-insensitive substring match, no AST — works on any text file. NOT for symbol lookup: no structural awareness, will false-positive on comments/strings/docs. |
   | Only the filename, not its path | `nagato_find_file` | Recursive exact-filename search from a starting directory. |
   | Intent/behavior, not exact names (e.g. "where do we validate emails?") | `nagato_semantic_search` | Vector search over indexed code chunks. Self-indexes on every call (`auto_index`), so — unlike the symbol DB — it never needs a manual rebuild. |
   **Symbol DB staleness caveat:** the DB behind `nagato_read_signatures` (global mode), `nagato_extract_callers`,
   and `nagato_extract_callees` does **not** auto-reindex like `nagato_semantic_search` does. If a global lookup
   for a symbol you know exists comes back empty, run `nagato_rebuild_symbol_db` (optionally scoped to a `dir`)
   before concluding the symbol doesn't exist — it may just be stale or never indexed.

3. **Safe Modification & Creation Loop:**
   - **Edit & Create**: Use `nagato_edit` for targeted string replacement or creating new files (auto-creates if non-existent — there is no separate `nagato_create` tool), and `nagato_edit_lines` for line-range rewrites.
   - **Directories**: Use `nagato_create_dir` for creating new directories.
   - **Syntax & Style**: Run `nagato_lint` immediately after editing — it does a hard syntax check plus a configurable Ruff pass, but only for `.py` files (no-op on everything else).
   - **Verify**: Run `nagato_run_test` for one narrow pytest node/file — it waits for completion and returns the tail of the output. Reach for `nagato_run_configured_suite` / `nagato_run_gold_full` only when you actually need the project's full configured acceptance suite (`TitanTest/config.json`); they're slower and not meant for iterating on a single fix.
   - **Rollback**: If an edit broke dependencies or introduced bugs, use `nagato_undo_standalone` to revert cleanly (see Error Recovery below).

4. **File Deletions & Renames:**
   - `nagato_delete` to safely remove files.
   - `nagato_rename` to rename or move files.

5. **Running commands / code:**
   - `nagato_shell` / `nagato_shell_str`: run an arbitrary shell command. They are identical — `nagato_shell_str` is a display-oriented alias, not a different tool. Default timeout is 5s (max 30s), so use these for short, quick commands only.
   - `nagato_execute_snippet`: runs a Python code snippet in a subprocess (30s default timeout), with `PYTHONPATH` set to the workspace root. **This is NOT a sandbox** — it runs with your full user permissions. Never point it at untrusted or externally-sourced code.
   - `nagato_git`: a restricted wrapper, **not** a general git passthrough — only `log`, `status`, `diff`, `revert`, `checkout_file`, `reset_file` are accepted as the `command` argument, with a separate `args` string (e.g. `nagato_git(command="revert", args="a1b2c3d")`, or `nagato_git(command="checkout_file", args="HEAD~2 -- path/to/file.py")`).
   - `nagato_web_search`: web search for docs, error messages, or API references.
   - `nagato_is_agent_running`: heuristic CPU/GPU/VRAM check for whether another agent process is already busy on this machine — check before kicking off resource-heavy parallel work.

## Error Recovery & Undo

- **`nagato_undo_standalone`**: persistent undo/redo over every edit/create/delete this tool made, tracked as a **single global chronological stack** — not scoped per file.
  - `mode="step"` (default), `value=N` — undo the last N operations, most recent first, regardless of which file each one touched.
  - `mode="redo"`, `value=N` — redo N previously-undone operations.
  - There is **no** `mode="time"` or `mode="command"` — only `"step"` and `"redo"` are supported; passing anything other than `"redo"` is silently treated as `"step"`.
- For regressions this tool can't reach (changes made outside these tools, or you need to inspect history first), use `nagato_git` directly: `status`/`diff`/`log` to inspect, `revert`/`checkout_file`/`reset_file` to act.

## Coding Guidelines

### DOs
- Write pythonic code that is clear, readable, and explicit — prioritize clarity over cleverness.
- **Extraction-on-Touch:** Extract modified code clusters if they can be cleanly isolated into dedicated, decoupled components.
- Always run `nagato_lint` after making modifications.

### DON'Ts
- **No `try/except/pass` blocks** under any circumstances. Errors must fail fast and explicitly.
- **No closures** — keep functions top-level and stateless where possible.
- **Never weaken assertions** — fix the underlying implementation, never soften tests to force a pass.
- **No quick fixes, workarounds, or downstream patches** — always fix the issue directly at the root source.
