# Semantic Search Setup Guide

This document explains how to set up and configure the semantic search functionality (`nagato_semantic_search`) in the NagatoFSM MCP package.

## Overview

The semantic search feature uses:
- **SQLite** with **sqlite-vec** extension for vector storage and similarity search
- **FastEmbed** with **ONNX Runtime** for generating text embeddings
- **BAAI/bge-small-en-v1.5** (384-dim) or **jinaai/jina-embeddings-v2-base-code** (768-dim) models

## Required Dependencies

```bash
# CPU-only (recommended for most users)
pip install sqlite-vec fastembed onnxruntime

# GPU-accelerated (requires CUDA)
pip install sqlite-vec fastembed onnxruntime-gpu
```

### Platform-Specific Notes

**Windows:**
- `sqlite-vec` may require Visual C++ Redistributable
- ONNX Runtime works out of the box

**Linux:**
- May need `sqlite3` development headers: `apt-get install libsqlite3-dev`
- `sqlite-vec` loads as a shared library

**macOS:**
- Works with Homebrew SQLite: `brew install sqlite`

## Configuration

### Config File Location

Create or edit `.nagato/functions_config.json` in your workspace root:

```json
{
  "semantic_search": {
    "db_path": ".nagato/nagato_codebase.db",
    "embedding_model": "jina",
    "model_cache_dir": ".nagato/models",
    "dimension": 768,
    "auto_index": true,
    "search_root": ""
  }
}
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `db_path` | string | `"nagato_codebase.db"` | Path to SQLite database file. Relative paths resolve from workspace root. |
| `embedding_model` | string | `"jina"` | Embedding model: `"baai"` (384-dim, faster) or `"jina"` (768-dim, better for code) |
| `model_cache_dir` | string | `""` | Custom cache directory for FastEmbed models. Empty = use default (`~/.cache/fastembed`) |
| `dimension` | integer | `768` | Vector dimension (auto-set based on model, but can override) |
| `auto_index` | boolean | `true` | Automatically keep index current on search/rebuild |
| `search_root` | string | `""` | Optional override for semantic search base directory. Relative paths resolve from workspace root. Can point outside workspace (external directory). |

### Model Comparison

| Model | Dimensions | Context Length | Best For |
|-------|------------|----------------|----------|
| BAAI/bge-small-en-v1.5 | 384 | 512 tokens | General text, faster, less VRAM |
| jinaai/jina-embeddings-v2-base-code | 768 | 2048 tokens | Code search, larger context |

### Semantic Search Root Precedence

The directory used as the base for semantic indexing/search is resolved with the following precedence:

1. **FSM Session Override** — Set via `nagato_set_semantic_search_root` during an FSM session. Persisted in session state file only (not config file). Takes highest priority.
2. **Config File Base** — The `semantic_search.search_root` value in `.nagato/functions_config.json`. Relative paths resolved against workspace root. Used in standalone mode and as default in FSM mode when no session override exists.
3. **Workspace Root Fallback** — The workspace root directory (current behavior, unchanged for users who don't opt in).

This allows you to point semantic search at any directory (even outside the workspace) without affecting edit/write/execute/git tools.

## Usage

### From MCP Client (Automatic)

When running inside the MCP server, semantic search works automatically:

```json
{
  "name": "nagato_semantic_search",
  "arguments": {
    "query": "how to authenticate user",
    "limit": 5
  }
}
```

### Standalone Usage (Outside MCP)

```python
from fsm.functions_internal.search import nagato_semantic_search
import asyncio

async def main():
    result = await nagato_semantic_search("database connection pooling", limit=3)
    print(result)

asyncio.run(main())
```

### Setting the Semantic Search Root

Use the new `nagato_set_semantic_search_root` tool to configure the base directory and immediately reindex it:

```json
// Via MCP (FSM session mode - session override)
{
  "name": "nagato_set_semantic_search_root",
  "arguments": {
    "path": "../some-other-repo"
  }
}

// Via MCP (standalone mode - config base)
{
  "name": "nagato_set_semantic_search_root",
  "arguments": {
    "path": "fsm/functions_internal"
  }
}
```

The tool:
- Resolves the path (absolute or relative to workspace root)
- Validates it exists and is a directory
- Persists it appropriately (session state file in FSM mode, config file in standalone mode)
- **Immediately reindexes synchronously** — no more "search triggers a surprise lazy index of the wrong dir"
- Returns a summary including the resolved path, persistence scope, external flag, and indexing statistics

### Rebuilding the Index

To re-index your codebase after changes:

```json
// Via MCP
{
  "name": "nagato_rebuild_symbol_db",
  "arguments": {}
}

// Or specific directory
{
  "name": "nagato_rebuild_symbol_db",
  "arguments": {
    "target_dir": "fsm/functions_internal"
  }
}
```

### Updating a Single File

```python
from fsm.functions_internal.semanticindex import SemanticIndexSearch

indexer = SemanticIndexSearch()
result = indexer.update_file_index("fsm/functions_internal/search.py")
print(result)
```

### Indexing a Directory Tree (Programmatic)

```python
from fsm.functions_internal.semanticindex import SemanticIndexSearch

indexer = SemanticIndexSearch()

# Index a directory within the workspace (default behavior)
result = indexer.index_directory_tree("fsm/functions_internal")
print(result)

# Index an external directory (outside workspace)
result = indexer.index_directory_tree("/absolute/path/to/external/repo", external=True)
print(result)
```

## Troubleshooting

### sqlite-vec Load Error

```
sqlite3.OperationalError: The specified module could not be found
```

**Solutions:**
- **Windows:** Install Visual C++ Redistributable
- **Linux:** `apt-get install libsqlite3-dev` then reinstall `sqlite-vec`
- Ensure Python can load extensions: `conn.enable_load_extension(True)`

### Model Download Issues

FastEmbed downloads models on first use (~50-100MB). If behind a proxy:
```bash
export HF_ENDPOINT=https://hf-mirror.com  # Use mirror if needed
```

### Out of Memory

For large codebases, use the smaller BAAI model:
```json
{
  "semantic_search": {
    "embedding_model": "baai",
    "dimension": 384
  }
}
```

Then rebuild the index.

### No Results / Empty Database

Run the index rebuild:
```bash
# Via MCP tool
nagato_rebuild_symbol_db

# Or via Python
python -c "
from fsm.functions_internal.semanticindex import SemanticIndexSearch
idx = SemanticIndexSearch()
idx.index_directory_tree('fsm')
"
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    nagato_semantic_search                    │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  SemanticIndexSearch (fsm/functions_internal/semanticindex.py)│
│  - Lazy-loads embedding model (FastEmbed + ONNX)             │
│  - SQLite + sqlite-vec for vector storage                    │
│  - Tables: code_chunks, vec_code_chunks, global_symbols,     │
│            global_calls                                       │
└────────────────────────────┬────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
    ┌─────────────────────┐       ┌─────────────────────┐
    │  Embedding Model    │       │   Vector Database   │
    │  (BAAI or Jina)     │       │   (SQLite + vec)    │
    └─────────────────────┘       └─────────────────────┘
```

## Security Note

The `semantic_search_root` setting **only affects the read+embed indexing path**. It never affects edit/write/execute/git tools. External directories (outside the workspace) are explicitly allowed for indexing — this is an opt-in feature where the user types the path explicitly. Indexed source text and embeddings from outside the workspace are stored in the local semantic-search database with absolute paths.

## Performance Tips

1. **Use Jina model** for code search (better understanding of code semantics)
2. **Rebuild incrementally** - `update_file_index()` is faster than full rebuild
3. **Limit results** - `limit=3` is usually sufficient for LLM context
4. **Cache models** - Set `model_cache_dir` to avoid re-downloads