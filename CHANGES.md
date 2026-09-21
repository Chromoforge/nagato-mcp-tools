# Changelog

Changelog for Nagato MCP Tools, based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [0.2.0] - 2026-09-20

### Added
- **Persistent Standalone Undo/Redo** (undo.py): Complete undo/redo system with snapshot tracking, retry logic (max 3 attempts, exponential backoff), and telemetry events (_publish_standalone_undo_event).
- **Action Journal Throttling & Atomic Writes** (ction_journal.py): SQLite write throttling (flush every 25 row effects or 1 second), safe atomic file replacement with retry on Windows file locks (WinError 5/32), immediate flush for FileEffect/FsmStateEffect.
- **Content Hash for Signatures** (extractsignature.py): SHA256 content_hash field in signature dictionaries for change detection.
- **New Insight Tools in Facade** (acade.py, 	ool_categories.py): 
agato_concept_expand, 
agato_log_event, 
agato_register_algorithm, 
agato_register_protocol, 
agato_link_implementation now tracked in action journal.
- **WebUI Asset Rebuild** (webui_dist/): New JS bundle (index-D-8pJRXe.js), updated index.html.
- **Configurable Ignored Directories** (config.py): Added playbooks_poc, playbooks_challenge to DEFAULT_IGNORED_DIRS.
- **Extended Test Runner Features** (	est.py): Hang detection via line-by-line streaming, idle_timeout_seconds=30, default timeout 600s (was 120s), PYTHONUNBUFFERED=1.
- **Agent Instructions: Anti-Lazy Protocol** (.github/copilot-instructions.md, AGENT_INSTRUCTIONS.md, docs/AGENT_INSTRUCTIONS.md): Mandatory use of specialized index tools (
agato_read_signatures, 
agato_extract_callers, 
agato_semantic_search), prohibition of blind 
agato_read_file for code exploration, concrete tool invocation syntax, error recovery section.

### Changed
- **NFSM to NAGATO_BOOT Rename** (multiple files): Consistent rename in comments, log messages, telemetry keys (
fsm_ctx_tokens to 
agatoboot_ctx_tokens), mock context (generateNFSMContext to generateNAGATO_BOOTContext).
- **Docstring Cleanup** (ead.py, search.py, execute.py, web.py, git.py, lint.py, create.py): Removed WHEN-TO-USE/WHEN-NOT-TO-USE/DO-NOT guidance notes, added structured Args: sections.
- **Semantic Index Error Handling** (semanticindex.py): Syntax/AST errors now logged and re-raised instead of silently swallowed; SQLite-Vec initialization logged.
- **Edit Tool Rollback Messages** (edit.py, _post_edit_safety.py): Clearer messages (EDIT ROLLED BACK / EDIT FAILED (PERMANENT)), rollback reason categories (RollbackReason), retry guidance.
- **Execute Tool Timeouts** (execute.py): DEFAULT_TIMEOUT_SECONDS 30 to 180, PYTHONUNBUFFERED=1.
- **Git Tool Imports** (git.py): Removed sm.events/sm.transition_apply imports.

### Fixed
- **Edit Tools Parameter Validation** (edit.py): Validation blocks unregistered arguments.
- **Edit Tool Returns** (edit.py): No more "spammy" returns.
- **Semantic Index Silent Failures** (semanticindex.py): AST/syntax errors no longer swallowed.

## [0.1.9] - 2026-09-15
