<!-- 
Nagato MCP Tools — Agent Instructions (Standalone Mode)
-->

# Project Nagato // Nagato MCP Tools — Agent Instructions (Standalone Mode)

## CRITICAL FIRST ACTION (MANDATORY ON EVERY USER TURN)
Project knowledge lives in the symbol and semantic indices — not in your memory and not in the file tree. Directory listings and plain grep see text only.

Your FIRST tool call on every turn (questions, code analysis, brainstorming, edits) MUST come from this table:

| Turn type | First call |
| --- | --- |
| Named symbol / definition / signature | `nagato_read_signatures(target_symbol=...)` |
| Callers or callees | `nagato_extract_callers` / `nagato_extract_callees` |
| Code logic / behavior / semantic intent | `nagato_semantic_search(query=...)` |

- **Forbidden as first call:** `nagato_list_dir`, `nagato_searchInFiles`, and IDE built-in search/grep/list tools. Use them only afterwards for exact literals or filenames.
- **Ground the answer:** base it on the returned code chunks/signatures and name them.
- **Zero plain-text answers before the first call.** After the tool results, answer in text.

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
| Literal text, config, or error message | `nagato_searchInFile` / `nagato_searchInFiles` | Exact substring only; never for concepts or architecture. |
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


## Wikipedia Hybrid Search

The standalone package includes **Wikipedia Hybrid Search** tools for searching and retrieving Wikipedia articles using local vector similarity search with sqlite-vec and live API fallback.

### Tools

| Tool | Purpose |
|------|---------|
| `nagato_wikipedia_search` | Two-stage search returning ranked previews with section metadata, distance scores, and preview snippets |
| `nagato_wikipedia_fetch` | Exact B-Tree lookup for full article or section text identified during search |
| `nagato_wikipedia_api_search` | Live Wikipedia REST API fallback when local dumps not yet indexed |
| `nagato_wikipedia_init` | Idempotent SQLite database and `sqlite-vec` virtual table initialization |
| `nagato_wikipedia_status` | Database statistics (article count, chunk count, index size, vector engine status) |

### Architecture

- **SQLite + sqlite-vec**: `wikipedia` (main entries), `wikipedia_chunks` (semantic chunks with embeddings)
- **FTS5**: `wikipedia_chunks_fts` (BM25 lexical search)
- **Vector Search**: `sqlite-vec` virtual table for dense vector similarity search (cosine distance)
- **Embeddings**: FastEmbed `jina-embeddings-v2-base-en` (768-dim) or `all-MiniLM-L6-v2` (384-dim)
- **Two-Stage Search → Fetch**: Search returns compact previews; fetch retrieves full content

All data stored locally in `~/.nagato/wikipedia/`.

### Ingestion

Wikipedia data is ingested via the streaming dump parser:

```bash
# Download and ingest Wikipedia dump (streaming, checkpoint/resume)
python tools/import_wikipedia_dump.py --lang en --limit 10000
```

The ingestion pipeline:
- Streams XML dump without loading full file into memory
- Chunks articles by semantic sections using `mwparserfromhell`
- Batch inserts (2000 articles/transaction) with checkpoint/resume
- Generates embeddings via FastEmbed

### Usage Examples

```python
# Initialize database
nagato_wikipedia_init()

# Search for articles about quantum computing
nagato_wikipedia_search("quantum computing", limit=10)

# Fetch full article
nagato_wikipedia_fetch("Quantum_computing")

# Fetch specific section
nagato_wikipedia_fetch("Quantum_computing", section="History")

# Live API fallback (when local index not available)
nagato_wikipedia_api_search("quantum computing", limit=5)

# Check database status
nagato_wikipedia_status()
```

### Configuration

Configure in `.nagato/functions_config.json`:

```json
{
  "wikipedia": {
    "db_path": "~/.nagato/wikipedia/vec.db",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "dimension": 384,
    "dump_cache_dir": "~/.nagato/wikipedia/dumps",
    "chunk_size": 512
  }
}
```

### Category

Tools are grouped under the **WIKIPEDIA** category in the tool registry.

## German Law & Court Decisions Hybrid Search

The standalone package includes **German Law & Court Decisions Hybrid Search** tools for searching and retrieving German federal laws and court decisions using local hybrid search (BM25 + Dense Vector) with Reciprocal Rank Fusion (RRF).

### Tools

| Tool | Purpose |
|------|---------|
| `nagato_law_search` | Hybrid RRF search over laws and court decisions with filters (court, book_code, pack, date range, source) |
| `nagato_law_fetch` | Exact lookup by identifier (laws: "bgb:823" or "bgb:823:1"; cases: "decision_id" or "slug") |
| `nagato_law_init` | Create schema, verify index support, initialize database |
| `nagato_law_status` | Database stats, index health, disk usage, models, packs |
| `nagato_law_build_index` | Build per-model, per-pack USearch HNSW vector indexes (incremental supported) |
| `nagato_law_ingest` | Modular ingestion from OLDP REST API, Gesetze im Internet XML, or Hugging Face (pack or book_codes filter) |
| `nagato_law_compare_models` | Compare retrieval across embedding models (A/B testing) |
| `nagato_law_list_packs` | List available packs with book/case counts |

### Architecture

- **SQLite**: `law_books` (book_code, title, revision_date, pack_id), `law_sections` (id, book_code, section, title, content, markdown_content, pack_id), `court_decisions` (decision_id, court, date, leitsatz, tenor, gruende, pack_id), `court_decision_chunks` (id, decision_id, chunk_index, section, content, pack_id)
- **FTS5**: `law_sections_fts`, `court_decision_chunks_fts` (BM25 lexical search with `unicode61` tokenizer)
- **USearch HNSW**: Per-model, per-pack indexes at `~/.nagato/law/indexes/{model_slug}/{pack_id}_law.index`
- **RRF**: Deterministic fusion of BM25 and dense ranks (k=60)
- **Multi-Model Embeddings**: FastEmbed with configurable models (default: `multilingual-e5-small`, `bge-m3`)

All data stored locally in `~/.nagato/law/`.

### Modular Packs

Law data is organized into **packs** for selective loading:

| Pack | Description |
|------|-------------|
| `core` | Core civil/criminal law (BGB, StGB, GG, etc.) |
| `social` | Social law (SGB I-XII) |
| `labor` | Labor law (ArbGG, KSchG, etc.) |
| `admin` | Administrative law (VwGO, VwVfG, etc.) |
| `cases_bgh` | BGH (Federal Court of Justice) decisions |
| `cases_bverfg` | BVerfG (Federal Constitutional Court) decisions |
| `cases_bagh` | BAG (Federal Labor Court) decisions |
| `cases_bshg` | BSHG (Federal Social Court) decisions |

Custom packs can be defined in `.nagato/functions_config.json` under `law.packs`.

### Multi-Model Embeddings

Supports multiple embedding models with separate indexes:

| Model | Dimensions | Strengths |
|-------|------------|-----------|
| `multilingual-e5-small` | 384 | Excellent multilingual (German legal texts) |
| `bge-m3` | 1024 | Strong multilingual, long context |
| `all-MiniLM-L6-v2` | 384 | Fast, English-centric (for comparison) |

Use `nagato_law_compare_models` to evaluate models side-by-side.

### Data Sources

1. **OLDP REST API** (primary): `https://de.openlegaldata.io/api/` - paginated laws & cases with `return_text=1`
2. **Gesetze im Internet (GII)**: `https://www.gesetze-im-internet.de/` - XML bulk downloads via `gii-toc.xml`
3. **Hugging Face Datasets**: `openlegaldata/laws-germany`, `openlegaldata/court-decisions-germany`, `openlegaldata/legal-citation-graph-germany` (GATED - requires HF_TOKEN)

### Usage Examples

```python
# Initialize database
nagato_law_init()

# Ingest core law pack (first 100 laws)
nagato_law_ingest(source="api", pack="core", limit=100)

# Build vector index for core pack
nagato_law_build_index(model="multilingual-e5-small", pack="core")

# Search for "Schadensersatz" in core laws
nagato_law_search("Schadensersatz", pack="core", model="multilingual-e5-small", limit=10)

# Fetch specific law section
nagato_law_fetch("bgb:823")  # §823 BGB
nagato_law_fetch("bgb:823:1")  # §823 Abs. 1 BGB

# Search court decisions
nagato_law_search("Arbeitnehmer", source="cases", court="BAG", limit=10)

# Fetch court decision
nagato_law_fetch("decision_id_12345")

# Compare models
nagato_law_compare_models("Kündigungsschutz", models=["multilingual-e5-small", "bge-m3"], pack="labor")

# List available packs
nagato_law_list_packs()
```

### Configuration

Configure in `.nagato/functions_config.json`:

```json
{
  "law": {
    "db_path": "~/.nagato/law/hybrid.db",
    "models": ["multilingual-e5-small", "bge-m3"],
    "default_model": "multilingual-e5-small",
    "packs": {
      "core": ["bgb", "stgb", "gg", "bgb_eg", "hgb", "zpo", "stpo"],
      "social": ["sgb_1", "sgb_2", "sgb_3", "sgb_4", "sgb_5", "sgb_6", "sgb_7", "sgb_8", "sgb_9", "sgb_10", "sgb_11", "sgb_12"],
      "labor": ["arbgg", "kschg", "betrvg", "tvog", "entgfg"],
      "admin": ["vwgo", "vwvfg", "vwsig", "bvrg"],
      "cases_bgh": ["bgh"],
      "cases_bverfg": ["bverfg"],
      "cases_bagh": ["bag"],
      "cases_bshg": ["bshg"]
    },
    "oldp_api_base": "https://de.openlegaldata.io/api/",
    "gii_base": "https://www.gesetze-im-internet.de/",
    "hf_token": "${HF_TOKEN}"
  }
}
```

### Category

Tools are grouped under the **LAW** category in the tool registry.

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
- **No narration between tool calls.** Final answer: concise, grounded in tool results.
