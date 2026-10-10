# Changelog

Changelog for Nagato MCP Tools, based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]

### Added
- **Wikipedia Tool Suite** (`fsm/functions_internal/wikipedia.py`, `fsm/tool_categories.py`, `fsm/tool_registry_builder.py`): New dedicated `WIKIPEDIA` tool category with 5 tools for local vector similarity search and live API fallback over Wikipedia encyclopedic knowledge:
  - `nagato_wikipedia_search`: Two-stage search returning compact preview snippets, distance scores, and section metadata without polluting LLM context
  - `nagato_wikipedia_fetch`: Targeted full-text retrieval for specific articles or sections identified during search
  - `nagato_wikipedia_api_search`: Live Wikipedia REST API fallback when local dumps not yet indexed
  - `nagato_wikipedia_init`: Idempotent SQLite database and `sqlite-vec` virtual table initialization
  - `nagato_wikipedia_status`: Database statistics (article count, chunk count, index size, vector engine status)
- **Two-Stage Search → Fetch Pattern**: Token-conservative retrieval architecture using `jinaai/jina-embeddings-v2-base-en` (768-dim) embeddings with `sqlite-vec` virtual tables
- **Standalone Integration** (`fsm/function_facade.py`, `sync_tools.py`): Wikipedia tools registered in standalone facade, synced to `nagato_tools` package, and exposed via MCP server
- **Configuration Schema** (`fsm/functions_internal/config.py`): New `WikipediaConfig` dataclass with `db_path`, `embedding_model`, `dimension`, `chunk_size`, `chunk_overlap`, `batch_size`, `device` options

### Changed
- **Tool Count Update** (`sync_tools.py`): Base standalone tool count increased from 31 to 36 (36 standard + 15 Insight = 51 total when Insight enabled)
- **Landing Page Manifest** (`sync_tools.py`): Added "Wikipedia & Knowledge Retrieval" category to exported `tools.json`
- **README Documentation** (`README.md`, `.mcptools/MCP_TOOLS_README.md`): Expanded both READMEs with Wikipedia tool suite documentation, configuration examples, and category listings
- **Category Mapping** (`fsm/function_facade.py`): Added `finalize_handler` → `ToolCategory.FINALIZE` and `wikipedia` → `ToolCategory.WIKIPEDIA` mappings; fixed `_get_tool_category_for_tool` signature

### Fixed
- **Standalone Facade Category Resolution** (`fsm/function_facade.py`): All 42 registered standalone tools now cleanly resolve to their respective `ToolCategory` (no UNKNOWN entries) 

### Documentation
- **Three Independent Persistence Systems** (`.github/copilot-instructions.md`, `AGENT_INSTRUCTIONS.md`, `src/nagato_tools/docs/AGENT_INSTRUCTIONS.md`): Restructured system initialization docs to explicitly separate Symbol DB, Semantic Search, and Insight Graph with comparison tables showing purpose, initialization tool, and auto-reindex behavior. Added dedicated "System Initialization" section emphasizing no single "init all" function exists. Clarified `docstring_only` config applies only to Semantic Search embeddings.

### Refactored
- **Semantic Index Combined Sync & AST-Aware Chunking** (`src/nagato_tools/semanticindex.py`): Added `sync_file_all()` for combined single-pass sync updating both semantic index (code_chunks, global_symbols, global_calls) AND Insight knowledge graph (AST nodes, edges) in one atomic operation. Replaced EmbeddingModels with FastEmbed-based lazy loading. Implemented AST-aware chunk splitting via `_split_large_chunk_ast_aware()` with statement-boundary awareness. Added `docstring_only` mode support. Enhanced code_chunks table with symbol_name, node_id, chunk_type columns. Added graceful fallback for non-Python files using sliding-window chunking. Linked chunks to Insight node_ids for cross-system navigation.

### Added
- **Workspace Registry Integration** (`src/nagato_mcp_tools/server.py`, `src/nagato_mcp_tools/ui_server.py`, `src/nagato_tools/workspace_registry.py`): Register workspace on server startup. Added `/api/v1/standalone/workspaces` endpoint to list registered workspaces. Enhanced `/api/v1/standalone/sessions` with optional workspace query parameter and validation via `is_workspace_allowed()`. Returns workspace_root in sessions response. Graceful fallback when workspace_registry unavailable.

### Maintenance
- **WebUI Assets** (`src/nagato_tools/webui_dist/`): Updated to new Vite build (`index-CGTfLYGS.js`, `index-MtSVh_jA.css`).

## [0.2.5] - 2026-10-10

### Changed
- **Version Bump** (`pyproject.toml`): 0.2.4 → 0.2.5

## [0.2.4] - 2026-10-08

### Changed
- **Config Key Renames** (`config.py`): Renamed `subgoals_limit` → `subgoal_stack_limit`, `trail_max_depth` → `subgoal_context_max_depth`, `trail_line_max_chars` → `subgoal_context_line_max_chars` for clarity and consistency.
- **Standalone Compatibility** (`ctx_mock.py`): Replaced `fsm.functions_internal` imports with `nagato_tools` equivalents; wrapped `ContextRenderer` imports in try/except for optional FSM dependency.

### Fixed
- **FSM Crash in Read/Edit Tools** (`read.py`, `edit.py`): Wrapped FSM instance lookup in try/except; gracefully falls back to mock context when no session is bound (standalone mode).

### Maintenance
- **WebUI Assets** (`webui_dist/`): Updated to new Vite build (`index-BgL3iPtX.js`, `index-MtSVh_jA.css`).
- **Documentation** (`README.md`, `SEMANTIC_SEARCH_SETUP.md`): Added `docstring_only` config option to schema and examples.

## [0.2.3] - 2026-10-07

### Added
- **Redundant Read Detection** (`config.py`, `ctx_mock.py`, `read.py`): New `redundant_read_detection.enabled` config option (default: `true`). When enabled, `nagato_read_file` and `nagato_read_lines` track file reads and prepend a warning if the same unchanged file is read again without intervening edits or disk changes. Agent edits via `nagato_edit`/`nagato_edit_lines` reset the warning; external file modifications (mtime/hash change) also reset it.
- **Hierarchical Compound Nodes in WebUI** (`webui_dist/`): Insight Graph now renders AST containment hierarchies as collapsible containers (Domains → Concepts → Components → Files). Double-click to expand/collapse; clickable breadcrumb trail for instant navigation.
- **Session Listing API** (`telemetry.py`, `ui_server.py`): New `list_standalone_sessions()` function and `/api/v1/standalone/sessions` REST endpoint to enumerate all available standalone sessions with undo/redo/audit status.
- **Concept Batch Tool** (`facade.py`, `tool_categories.py`): Added `nagato_concept_batch` to the list of action-journal-tracked tools and INSIGHT category metadata.

### Changed
- **WebUI Asset Resolution Priority** (`ui_server.py`): Local workspace `dashboard/dist` (Vite dev build) is now checked first before packaged assets, improving development iteration speed.
- **Context Rendering Delegation** (`ctx_mock.py`): `MockFSMContext.generateNAGATO_BOOTContext()` now delegates to canonical `ContextRenderer` (`_render_yaml`, `_render_legacy`, `_render_json`) instead of duplicating logic, ensuring format parity with FSM mode.
- **Context Limits Defaults** (`config.py`): Added new defaults for `trail_max_depth`, `subgoal_context_max_depth`, `subgoal_context_children_limit`, `trail_line_max_chars`, `handoff_context_max_items`, `handoff_context_max_chars`, `handoff_context_token_budget`, and `debug` flag.
- **Token Limit Helper** (`config.py`): New `limit_or_none()` helper converts `0` or `None` to `None` (unlimited), preserving positive ints.

### Fixed
- **WebUI Asset Hash Update** (`webui_dist/index.html`): Updated script reference from `index-7IoJ1upB.js` to `index-DzCaGswg.js` (new Vite build output).

## [0.2.2] - 2026-09-30

### Added
- **Session & Workspace Isolation for Undo System** (`ctx_mock.py`, `undo.py`, `config.py`, docs): Isolated undo/redo caches per `(workspace, session_id)` pair enabling safe concurrent usage by multiple agents. Auto-generates session ID from workspace path hash (`ws_a1b2c3d4`) when no explicit ID provided. Supports `proj:<name>` format for project-scoped sessions. New directory structure: `.nagato/sessions/<session_id>/undo_cache` and `.nagato/sessions/<session_id>/redo_cache`.
- **Semantic Search Whitelist Mode** (`config.py`, `search.py`, `semanticindex.py`): New `search_dirs` config option to restrict indexing/searching to specific subdirectories (whitelist). `ignored_dirs` still applies to hidden directories. Works across `nagato_searchInFiles`, `nagato_semantic_search`, `nagato_rebuild_symbol_db`, `nagato_set_semantic_search_root`, and `ensure_index_current()`.
- **Docstring-Only Embedding Mode** (`config.py`, `semanticindex.py`, `search.py`, `edit.py`): New `docstring_only` config option to embed only docstrings instead of full function/class bodies. Reduces index size and improves relevance for documentation-focused searches.
- **AST-Aware Chunk Splitting** (`semanticindex.py`): Large functions/classes are now split using AST-aware logic (by statements, decorators, class methods) respecting model token limits. Adds `EmbeddingModels.get_max_chunk_chars()` for model-aware chunk sizing (BAAI: ~2048 chars, Jina: ~8192 chars).
- **Thread Pools for Embedding & Auto-Indexing** (`semanticindex.py`, `search.py`): Global thread pools (`_get_embedding_executor`, `_get_auto_index_executor`) offload CPU-intensive embedding work and auto-indexing from the event loop. Query embedding and `ensure_index_current()` now run asynchronously.
- **Session-Aware Config Loading** (`config.py`): All config loading functions (`load_functions_config`, `get_semantic_search_config`, `get_lint_config`, `get_ignored_dirs`, `get_config_path`, `resolve_semantic_search_root`) now accept optional `ctx` parameter for workspace root resolution.

### Changed
- **Semantic Index Memory & Performance** (`semanticindex.py`): Streaming embeddings with `batch_size=32`, `parallel=1` to prevent OOM; AST-aware chunking replaces hard 8000-char truncation; whitelist mode for targeted indexing; thread pool offloading for query embedding and auto-indexing.
- **Search Tools** (`search.py`): `_iter_searchable_text_files` supports whitelist mode; `nagato_semantic_search` auto-indexing runs in thread pool; `nagato_rebuild_symbol_db` and `nagato_set_semantic_search_root` use config-aware indexer with `docstring_only`.
- **Edit Tools** (`edit.py`): Symbol updates use config-aware `_get_semantic_indexer()` helper respecting `docstring_only` setting.
- **Undo System** (`undo.py`, `ctx_mock.py`): Uses `ctx.workspace_root` for correct directory resolution; `MockFSMContext` creates session-scoped undo/redo directories.
- **Documentation** (`.github/copilot-instructions.md`, `AGENT_INSTRUCTIONS.md`, `README.md`, `src/nagato_tools/docs/AGENT_INSTRUCTIONS.md`, `src/nagato_tools/docs/STANDALONE_UNDO.md`): Added session/workspace isolation docs, concurrency model tables, MCP client config examples, zero-config isolation explanation, and updated directory structure diagrams.
- **WebUI Assets** (`src/nagato_tools/webui_dist/`): Updated to new hashed asset filenames (`index-7IoJ1upB.js`, `index-laAmOdvw.css`).

### Fixed
- **Session ID Resolution** (`ctx_mock.py`): Auto-generation from workspace hash when no explicit session ID provided; sanitization replaces `:` with `_` for filesystem safety.
- **Config Path Resolution** (`config.py`): `_get_workspace_root(ctx)` helper ensures correct workspace detection from context.

## [0.2.1] - Unreleased

### Added
- **V1 Provider Architecture** (`provider_loader.py`, `suite_contract.py`, `suite_dispatcher.py`): New modular test provider system with dynamic discovery, config parsing, normalized result contract (`NormalizedSuiteResult`), and dispatcher layer. Providers live at `.nagato/test_provider.py` and are configured via `TitanTest/config.json` (or legacy `.nagato/config.json`).
- **Context Pruning & Token Budgeting** (`config.py`, `ctx_mock.py`, `token_calculator.py`): New config functions `get_context_limits()`, `get_context_format()`, `get_show_token_budget_to_llm()`; `get_token_limits()` now returns `(max_context_size, max_context_tokens, history_token_ratio)`; `get_budget_breakdown()` includes `conversation_history` tokens from LLM message history.
- **Semantic Index Streaming & Progress** (`semanticindex.py`): Embeddings now streamed with `batch_size=32` and `parallel=None` to avoid loading all vectors into memory; new `progress_callback(current, total, filename)` support for `index_directory_tree()`, `rebuild_symbol_db()`, and `ensure_index_current()`; model cache directory support via `model_cache_dir` config.
- **Symbol Name Sanitization** (`facade.py`): `_preprocess_arguments()` now strips common LLM prefixes (`call `, `def `, `async def `, `class `, `function `, `method `) from symbol-name parameters (`target_symbol`, `function_name`, `symbol`, `symbol_name`, `name`).
- **AST Search Line Ranges** (`search.py`): `nagato_searchAST` now reports `end_lineno` (e.g., `lines 10-15`) instead of single line numbers.
- **Semantic Search Config Defaults** (`config.py`): Default `db_path` changed to `.nagato/nagato_codebase.db`, `model_cache_dir` to `.nagato/models`; `resolve_db_path()` now defaults to `.nagato` folder.
- **Insight Display Config** (`config.py`): New `get_insight_display_config()` for search/radar formatting options.
- **Server Type Hint Handling** (`server.py`): `_public_parameters()` now uses `typing.get_type_hints()` for accurate annotation resolution.

### Changed
- **Semantic Index RAM throttle** — streaming embeddings, progress callbacks, model cache dir
- **Test Suite Imports** (`test.py`): Switched from `fsm.*` imports to local `nagato_tools.*` modules for provider loader, suite contract, and dispatcher.
- **Agent Instructions** (`.github/copilot-instructions.md`, `AGENT_INSTRUCTIONS.md`): Removed FSM-specific wrapper reference; added autonomous agent rule (silent execution only).
- **Version Bump** (`pyproject.toml`): 0.2.0 → 0.2.1

### Fixed
- **Pre-Parsing missing file** — handled in semantic index
- **Symbol DB Rebuild Crashes**: Resolved crashes triggered during full symbol database rebuilds.
- **Standalone Undo Crashes**: Fixed edge-case crashes occurring during standalone undo operations.
- **Semantic Index Silent Failures** (`semanticindex.py`): AST/syntax errors are now logged and re-raised instead of silently swallowed; added logging for SQLite-Vec initialization.
- **Edit Tools Parameter Validation** (`edit.py`): Validation blocks unregistered arguments.
- **Edit Tool Returns** (`edit.py`): Reduced noise and removed redundant return payloads.

## [0.2.0] - 2026-09-20

### Added
- **Action Journal Throttling & Atomic Writes** (`action_journal.py`): SQLite write throttling (flush every 25 row effects or 1 second), safe atomic file replacement with retry on Windows file locks (WinError 5/32), immediate flush for FileEffect/FsmStateEffect.
- **Content Hash for Signatures** (`extractsignature.py`): SHA256 content_hash field in signature dictionaries for change detection.
- **Configurable Ignored Directories** (`config.py`): Added `playbooks_poc` and `playbooks_challenge` to `DEFAULT_IGNORED_DIRS`.
- **Extended Test Runner Features** (`test.py`): Hang detection via line-by-line streaming, `idle_timeout_seconds=30`, default timeout increased to 600s (was 120s), `PYTHONUNBUFFERED=1`.
- **Input Repair Utilities** (`input_repair.py`): New module with `strip_noise_from_payload()` (markdown fences, conversational prefixes), `repair_malformed_json()` (single quotes, trailing commas, Python True/False/None), `coerce_primitives()` (string→int/float/bool/list/dict), `resolve_file_path()` (workspace-relative with security checks). Integrated into facade via `_HAS_INPUT_REPAIR` flag.
- **Shell Allowlist Security Model** (`shell.py`): Replaced denylist with configurable allowlist (`DEFAULT_ALLOWED_COMMANDS`: python, pytest, ruff, mypy, uv, pip, cmd, powershell, pwsh). Configurable via `.nagato/functions_config.json` under `shell.allowed_commands`. Added environment variable allowlist (`DEFAULT_ALLOWED_ENV_KEYS`) with PATH traversal protection. Defense-in-depth: `DANGEROUS_PATTERNS` still blocked regardless of allowlist.

### Changed
- **Legacy NFSM Rename**: Renamed legacy NFSM references to `NAGATO_BOOT` across configuration and execution modules.
- **Docstring Cleanup** (`read.py`, `search.py`, `execute.py`, `web.py`, `git.py`, `lint.py`, `create.py`): Removed WHEN-TO-USE/WHEN-NOT-TO-USE/DO-NOT guidance notes, added structured `Args:` sections.
- **Edit Tool Rollback Messages** (`edit.py`, `_post_edit_safety.py`): Clearer messages (`EDIT ROLLED BACK` / `EDIT FAILED (PERMANENT)`), added `RollbackReason` categories and explicit retry guidance.
- **Execute Tool Timeouts** (`execute.py`): Increased `DEFAULT_TIMEOUT_SECONDS` from 30s to 180s, set `PYTHONUNBUFFERED=1`.
- **Git FSM Detection** (`git.py`): Prioritize FSM from `_ctx.fsm` (injected by facade) over global import; fallback to `edit_module._get_fsm_instance()` for backward compatibility; defer `fsm.events`/`fsm.transition_apply` imports until FSM confirmed present; remove direct `NagatoFSM` import attempt. Cleaner standalone/host separation.

### Fixed
- **Edit Tools Parameter Validation** (`edit.py`): Validation blocks unregistered arguments.
- **Edit Tool Returns** (`edit.py`): Reduced noise and removed redundant return payloads.
- **Semantic Index Silent Failures** (`semanticindex.py`): AST/syntax errors are now logged and re-raised instead of silently swallowed; added logging for SQLite-Vec initialization.
- **Standalone Undo Crashes**: Fixed edge-case crashes occurring during standalone undo operations.
- **Symbol DB Rebuild Crashes**: Resolved crashes triggered during full symbol database rebuilds.