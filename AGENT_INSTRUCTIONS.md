<!-- 
Nagato MCP Tools — Agent Instructions (Standalone Mode)
-->

# Project Nagato // Nagato MCP Tools — Agent Instructions (Standalone Mode)

## CRITICAL FIRST ACTION (MANDATORY ON EVERY USER TURN)
Whatever the user asks (questions, concepts, code analysis, brainstorming, or edits):
1. **NEVER answer purely from LLM memory.** Do not guess, theorize, or assume project context.
2. **ALWAYS query project knowledge FIRST** before outputting text:
   - Concepts / design rationale / general queries: `nagato_unified_search(query=...)`
   - Architecture / symbol definitions: `nagato_view_radar` or `nagato_read_signatures`
   - Code logic exploration: `nagato_semantic_search` or `nagato_searchInFiles`
3. Tool call MUST happen in your first turn. Zero plain-text answers prior to checking project state.

## Description

You interact with the **standalone** Nagato MCP Tools suite: a flat catalogue of `nagato_*` tools. Each tool is an independent MCP tool called directly with its documented parameters.

> **Tool Invocation**:
> Invoke MCP tools directly using the tool call interface. Do not simulate, describe, or print tool calls in prose.

## General Information

- **Session Scope:** Optionally scoped per project via `NAGATO_SESSION_ID` (default: `"standalone"`).
- **Persistence:** SQLite AST symbol DB.
- **Flat Surface:** All tools (EDIT, READ, SEARCH, TESTING, EXECUTE, WEB, GIT, CREATION, DEBUGGING, SHELL, SYSTEM) can be invoked whenever relevant.


## Operational Protocol

### 1. Code Exploration Rules
- Always use structural index tools (`nagato_read_signatures`, `nagato_extract_callers`, `nagato_semantic_search`) to locate definitions, usages, and logic.
- Use `nagato_read_file` or `nagato_read_lines` solely for reading non-code configs (`.yaml`, `.json`, `.md`) or inspecting specific verified line ranges. Never scan files blindly.

### 1. Codebase Sync & Persistence Pathway
Maintain repository indices using combined single-pass synchronization:
- `nagato_sync_directory_all(root_dir=".")`: Full project sync (updates semantic index and insight graph together).
- `nagato_sync_file_all(file_rel_path="...")`: Incremental sync after modifying a file.
- `nagato_rebuild_symbol_db(dir=".")`: Rebuilds the AST symbol database when cross-file symbol lookups are missing.

### 2. Code Exploration
Select the tool matching your known information:

| Given | Tool | Notes |
| --- | --- | --- |
| Exact symbol name (repo-wide) | `nagato_read_signatures(target_symbol=...)` | Fast lookup across the indexed symbol DB. |
| Exact symbol name in specific file | `nagato_read_signatures(file_path=...)` | Scoped to file; falls back to live AST parse. |
| Partial/fuzzy symbol in single file | `nagato_searchAST(query=..., file=...)` | Structural AST substring match. |
| Callers or callees of symbol | `nagato_extract_callers` / `nagato_extract_callees` | Direct function and method call graph. |
| Literal text, config, or error message | `nagato_searchInFile` / `nagato_searchInFiles` | Plain substring search across text files. |
| Filename | `nagato_find_file(filename=...)` | Exact filename search. |
| Concept or natural language intent | `nagato_semantic_search(query=...)` | Vector search over code chunks. |

### 3. Modification & Verification Workflow
- **Edit**: Apply targeted changes via `nagato_edit` or `nagato_edit_lines`.
- **Sync**: Run `nagato_sync_file_all` on modified files.
- **Lint**: Run `nagato_lint(file=...)` after Python edits.
- **Test**: Run `nagato_run_test` for the focused test node; run `nagato_run_configured_suite` only for full acceptance runs.

## Calling Tools

- Invoke MCP tools directly via the tool-calling mechanism.
- Do not simulate, narrate, or describe tool invocations in text.
- Pass required parameters according to the tool schemas.


## Error Recovery & Undo

- `nagato_undo_standalone`: Roll back or redo workspace edits in chronological steps.
- `nagato_git`: Inspect git state (`status`, `diff`, `log`) or revert commits/files (`revert`, `checkout_file`, `reset_file`).


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
- **You are an autonomous agent. NEVER output reasoning, explanations, or conversational text, unless explicitly asked. ONLY emit tool calls via the function calling mechanism. Silent execution only.**
