# Changelog

Changelog for Nagato MCP Tools, based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [0.2.0] - 2026-09-20

### Added
- **Action Journal Throttling & Atomic Writes** (`action_journal.py`): SQLite write throttling (flush every 25 row effects or 1 second), safe atomic file replacement with retry on Windows file locks (WinError 5/32), immediate flush for FileEffect/FsmStateEffect.
- **Content Hash for Signatures** (`extractsignature.py`): SHA256 content_hash field in signature dictionaries for change detection.
- **Configurable Ignored Directories** (`config.py`): Added `playbooks_poc` and `playbooks_challenge` to `DEFAULT_IGNORED_DIRS`.
- **Extended Test Runner Features** (`test.py`): Hang detection via line-by-line streaming, `idle_timeout_seconds=30`, default timeout increased to 600s (was 120s), `PYTHONUNBUFFERED=1`.

### Changed
- **Legacy NFSM Rename**: Renamed legacy NFSM references to `NAGATO_BOOT` across configuration and execution modules.
- **Docstring Cleanup** (`read.py`, `search.py`, `execute.py`, `web.py`, `git.py`, `lint.py`, `create.py`): Removed WHEN-TO-USE/WHEN-NOT-TO-USE/DO-NOT guidance notes, added structured `Args:` sections.
- **Edit Tool Rollback Messages** (`edit.py`, `_post_edit_safety.py`): Clearer messages (`EDIT ROLLED BACK` / `EDIT FAILED (PERMANENT)`), added `RollbackReason` categories and explicit retry guidance.
- **Execute Tool Timeouts** (`execute.py`): Increased `DEFAULT_TIMEOUT_SECONDS` from 30s to 180s, set `PYTHONUNBUFFERED=1`.

### Fixed
- **Edit Tools Parameter Validation** (`edit.py`): Validation blocks unregistered arguments.
- **Edit Tool Returns** (`edit.py`): Reduced noise and removed redundant return payloads.
- **Semantic Index Silent Failures** (`semanticindex.py`): AST/syntax errors are now logged and re-raised instead of silently swallowed; added logging for SQLite-Vec initialization.
- **Standalone Undo Crashes**: Fixed edge-case crashes occurring during standalone undo operations.
- **Symbol DB Rebuild Crashes**: Resolved crashes triggered during full symbol database rebuilds.