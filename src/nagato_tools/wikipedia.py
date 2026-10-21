"""
Wikipedia Hybrid Search and Retrieval for NagatoFSM (SOTA Implementation).

Provides tools for searching and retrieving Wikipedia knowledge using a hybrid
BM25 (FTS5) + Dense Vector (USearch HNSW) approach with Reciprocal Rank Fusion (RRF).

Tools:
- `nagato_wikipedia_search`: Hybrid search (BM25 + Dense) with RRF merge
- `nagato_wikipedia_fetch`: Exact B-Tree lookup for full section/article text
- `nagato_wikipedia_api_search`: Online Wikipedia REST API search fallback
- `nagato_wikipedia_status`: DB stats, document counts, and configuration
- `nagato_wikipedia_init`: Initialize schema and verify vector support

Architecture:
- SQLite: wiki_articles, wiki_sections (structural sections, no naive chunking)
- FTS5: wiki_sections_fts (BM25 lexical search)
- USearch HNSW: wiki_usearch.index (dense vector ANN search)
- RRF: Deterministic fusion of BM25 and dense ranks

All data is stored locally within NagatoFSM (~/.nagato/wikipedia/).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.token_calculator import estimate

logger = logging.getLogger(__name__)

# Default model optimized for fast, lightweight embedding (384-dim, low VRAM & RAM footprint)
DEFAULT_WIKIPEDIA_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_WIKIPEDIA_DIMENSION = 384
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20

# RRF constant (standard value from literature)
RRF_K = 60

# FTS5 special characters that need escaping
_FTS5_SPECIAL_CHARS = set('"*()+-:<>@')

# Allowed domains for external API calls (SSRF protection)
_ALLOWED_API_DOMAINS = {
    "en.wikipedia.org",
    "de.wikipedia.org",
    "wikimedia.org",
}

# Thread safety locks for global mutable state
_embedder_lock = threading.Lock()
_usearch_lock = threading.Lock()

def _sanitize_fts5_query(query: str) -> str:
    """
    Sanitize user query for FTS5 MATCH operator.
    Escapes special characters that could alter query semantics.
    """
    if not query:
        return ""
    # Escape double quotes by doubling them (FTS5 phrase syntax)
    # Escape other special chars by wrapping in quotes
    result = []
    for char in query:
        if char == '"':
            result.append('""')
        elif char in _FTS5_SPECIAL_CHARS:
            result.append(f'"{char}"')
        else:
            result.append(char)
    return ''.join(result)


def _validate_api_url(url: str) -> bool:
    """
    Validate that URL is from an allowed domain (SSRF protection).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc.lower() in _ALLOWED_API_DOMAINS
    except Exception:
        return False


_wikipedia_embedder = None
_usearch_index = None
_usearch_index_path = None
_chunk_id_map = None  # Maps usearch internal IDs to SQLite section IDs


def _get_workspace_root(ctx: Any | None = None) -> Path:
    """Get workspace root from context or fall back to central config."""
    if ctx is not None and hasattr(ctx, "workspace_root"):
        return Path(ctx.workspace_root)
    return get_workspace_root()


def _get_context(ctx: Any | None = None) -> Any:
    """Get context from injected parameter or fall back to mock."""
    if ctx is not None:
        return ctx
    return get_mock_context(_get_workspace_root(ctx))


def _get_context_token_limit(ctx: Any, default: int | None = None) -> int:
    """Get token limit from context or use cascading config default for 'wikipedia' / 'read' category."""
    max_context_size = getattr(ctx, "MaxContextSize", None)
    if isinstance(max_context_size, int) and max_context_size > 0:
        return max_context_size
    if default is None:
        default = get_tool_token_limit("wikipedia")
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


def get_wikipedia_db_path(ctx: Any | None = None) -> Path:
    """Resolve the SQLite database path for Wikipedia data."""
    try:
        from nagato_tools.config import load_functions_config
        cfg = load_functions_config(ctx=ctx)
        wp_cfg = getattr(cfg, "wikipedia", None)
        if wp_cfg and getattr(wp_cfg, "db_path", None):
            custom_path = Path(wp_cfg.db_path)
            if not custom_path.is_absolute():
                custom_path = _get_workspace_root(ctx) / custom_path
            # Security: Ensure resolved path is within workspace or user directory
            custom_path = custom_path.resolve()
            workspace_root = _get_workspace_root(ctx).resolve()
            user_nagato = (Path.home() / ".nagato").resolve()
            if not (custom_path.is_relative_to(workspace_root) or custom_path.is_relative_to(user_nagato)):
                raise ValueError(f"Database path must be within workspace or ~/.nagato, got: {custom_path}")
            return custom_path
    except Exception as exc:
        logger.debug("Falling back to default db path: %s", exc)

    # Global user directory default: ~/.nagato/wikipedia/hybrid.db
    base_dir = Path.home() / ".nagato" / "wikipedia"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "hybrid.db"


def get_usearch_index_path(ctx: Any | None = None) -> Path:
    """Resolve the USearch index file path."""
    db_path = get_wikipedia_db_path(ctx)
    return db_path.parent / "wiki_usearch.index"


def _get_embedder(model_name: str = DEFAULT_WIKIPEDIA_MODEL):
    """Lazy load FastEmbed embedding model instance for Wikipedia (thread-safe)."""
    global _wikipedia_embedder
    if _wikipedia_embedder is None:
        with _embedder_lock:
            # Double-checked locking pattern
            if _wikipedia_embedder is None:
                try:
                    from fastembed import TextEmbedding
                    try:
                        _wikipedia_embedder = TextEmbedding(
                            model_name=model_name,
                            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                            max_length=2048,
                        )
                    except Exception:
                        _wikipedia_embedder = TextEmbedding(
                            model_name=model_name,
                            providers=["CPUExecutionProvider"],
                            max_length=2048,
                        )
                except ImportError:
                    _wikipedia_embedder = None
    return _wikipedia_embedder


def _load_usearch_index(index_path: Path) -> tuple[Any, np.ndarray]:
    """Load USearch index and chunk_id mapping from disk (thread-safe)."""
    global _usearch_index, _chunk_id_map, _usearch_index_path

    with _usearch_lock:
        if _usearch_index is not None and _usearch_index_path == index_path:
            return _usearch_index, _chunk_id_map

        if not index_path.exists():
            raise FileNotFoundError(f"USearch index not found at {index_path}. Run build_usearch_index() first.")

        logger.info(f"Loading USearch index from {index_path}...")
        import usearch.index
        _usearch_index = usearch.index.Index(
            ndim=DEFAULT_WIKIPEDIA_DIMENSION,
            metric=usearch.index.MetricKind.Cos,
            dtype=usearch.index.ScalarKind.F32,
        )
        _usearch_index.load(str(index_path))
        logger.info(f"USearch index loaded (size={_usearch_index.size})")

        # Load chunk_id mapping
        map_path = index_path.with_suffix('.npy')
        if map_path.exists():
            _chunk_id_map = np.load(map_path)
            logger.info(f"Loaded chunk_id map: {len(_chunk_id_map)} entries")
        else:
            # Fallback: assume sequential mapping
            _chunk_id_map = np.arange(_usearch_index.size, dtype=np.int64)
            logger.warning("No chunk_id map found, assuming sequential mapping")

        _usearch_index_path = index_path

        return _usearch_index, _chunk_id_map


def _build_usearch_index(db_path: Path, index_path: Path, batch_size: int = 100000) -> None:
    """Build USearch HNSW index from SQLite section embeddings (thread-safe)."""
    global _usearch_index, _chunk_id_map

    with _usearch_lock:
        logger.info(f"Building USearch index from {db_path}...")
        import time
        start = time.time()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Get total count of sections with embeddings
        total = cursor.execute("SELECT COUNT(*) FROM wiki_sections WHERE embedding IS NOT NULL").fetchone()[0]
        logger.info(f"Total sections with embeddings to index: {total:,}")

        if total == 0:
            logger.warning("No sections with embeddings found, skipping index build")
            conn.close()
            return

        # Create USearch index
        import usearch.index
        _usearch_index = usearch.index.Index(
            ndim=DEFAULT_WIKIPEDIA_DIMENSION,
            metric=usearch.index.MetricKind.Cos,
            dtype=usearch.index.ScalarKind.F32,
        )

        # Pre-allocate chunk_id map
        _chunk_id_map = np.empty(total, dtype=np.int64)

        # Process in batches
        offset = 0
        vectors_added = 0

        while offset < total:
            cursor.execute(
                "SELECT id, embedding FROM wiki_sections WHERE embedding IS NOT NULL LIMIT ? OFFSET ?",
                (batch_size, offset)
            )
            rows = cursor.fetchall()

            if not rows:
                break

            # Prepare batch
            batch_size_actual = len(rows)
            vectors = np.empty((batch_size_actual, DEFAULT_WIKIPEDIA_DIMENSION), dtype=np.float32)
            section_ids = np.empty(batch_size_actual, dtype=np.int64)

            for i, (section_id, vec_blob) in enumerate(rows):
                vectors[i] = np.frombuffer(vec_blob, dtype=np.float32)
                section_ids[i] = section_id

            # Add to USearch index (keys are sequential integers)
            keys = np.arange(vectors_added, vectors_added + batch_size_actual, dtype=np.int64)
            _usearch_index.add(keys, vectors)
            _chunk_id_map[vectors_added:vectors_added + batch_size_actual] = section_ids
            vectors_added += batch_size_actual

            offset += batch_size
            elapsed = time.time() - start
            rate = vectors_added / elapsed if elapsed > 0 else 0
            logger.info(f"  Indexed {vectors_added:,}/{total:,} vectors ({rate:.0f} vec/s)")

        conn.close()

        # Save index and mapping
        logger.info(f"Saving USearch index to {index_path}...")
        _usearch_index.save(str(index_path))

        map_path = index_path.with_suffix('.npy')
        np.save(map_path, _chunk_id_map)

        total_time = time.time() - start
        logger.info(f"USearch index built in {total_time:.1f}s ({vectors_added:,} vectors)")


def _ensure_usearch_index(ctx: Any | None = None) -> tuple[Any, np.ndarray]:
    """Ensure USearch index is loaded, building if necessary."""
    db_path = get_wikipedia_db_path(ctx)
    index_path = get_usearch_index_path(ctx)

    # Check if there are embeddings to index before building
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM wiki_sections WHERE embedding IS NOT NULL")
    embedding_count = cursor.fetchone()[0]
    conn.close()

    if embedding_count == 0:
        raise FileNotFoundError("No embeddings in database to build USearch index")

    if not index_path.exists():
        logger.info("USearch index not found, building...")
        _build_usearch_index(db_path, index_path)

    return _load_usearch_index(index_path)


def get_wikipedia_connection(db_path: Path | None = None, ctx: Any | None = None) -> tuple[sqlite3.Connection, bool]:
    """Get or initialize SQLite connection with FTS5 and optional USearch support."""
    if db_path is None:
        db_path = get_wikipedia_db_path(ctx)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))

    with conn:
        # wiki_articles table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_articles (
                id INTEGER PRIMARY KEY,
                pageid INTEGER UNIQUE,
                title TEXT UNIQUE,
                lead_paragraph TEXT,
                categories TEXT,
                last_modified TEXT
            )
        """)

        # wiki_sections table (structural sections, no naive chunking)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_sections (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES wiki_articles(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                section_name TEXT NOT NULL,
                section_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                char_count INTEGER NOT NULL,
                embedding BLOB  -- 384-dim float32 vector
            )
        """)

        # B-Tree indexes for exact lookups
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_art_sec ON wiki_sections(article_id, section_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_title_sec ON wiki_sections(title, section_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_embedding ON wiki_sections(embedding) WHERE embedding IS NOT NULL")

        # FTS5 virtual table for BM25 lexical search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_sections_fts USING fts5(
                title, section_name, content,
                content=wiki_sections,
                content_rowid=id,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)

        # FTS5 triggers to keep in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS wiki_sections_ai AFTER INSERT ON wiki_sections BEGIN
                INSERT INTO wiki_sections_fts(rowid, title, section_name, content)
                VALUES (new.id, new.title, new.section_name, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS wiki_sections_ad AFTER DELETE ON wiki_sections BEGIN
                INSERT INTO wiki_sections_fts(wiki_sections_fts, rowid, title, section_name, content)
                VALUES ('delete', old.id, old.title, old.section_name, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS wiki_sections_au AFTER UPDATE ON wiki_sections BEGIN
                INSERT INTO wiki_sections_fts(wiki_sections_fts, rowid, title, section_name, content)
                VALUES ('delete', old.id, old.title, old.section_name, old.content);
                INSERT INTO wiki_sections_fts(rowid, title, section_name, content)
                VALUES (new.id, new.title, new.section_name, new.content);
            END
        """)

    return conn, True  # Always return True for has_vector (FTS5 is always available)


# ----------------------------------------------------------------------
# Core Agent Tools
# ----------------------------------------------------------------------

async def nagato_wikipedia_search(
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
    start_offset: int = 0,
    _ctx: Any | None = None,
) -> str:
    """
    Hybrid Search (BM25 + Dense Vector) with Reciprocal Rank Fusion (RRF).

    Step 1 of the Search -> Fetch pattern: searches Wikipedia and returns a
    lightweight preview (titles, sections, snippets) to protect token budget.

    Args:
        query: Search term or question.
        limit: Number of matches to return (default: 5, max: 20).
        start_offset: Zero-based offset into search results.
        _ctx: Optional context (injected by facade).
    """
    ctx = _get_context(_ctx)
    normalized_query = query.strip()
    if not normalized_query:
        return nagato_error("Query must not be empty.", tool="nagato_wikipedia_search")

    bounded_limit = min(max(1, limit), MAX_SEARCH_LIMIT)
    db_path = get_wikipedia_db_path(_ctx)

    if not db_path.exists():
        return (
            f"Wikipedia local database not found at {db_path}. "
            "Call `nagato_wikipedia_init()` or use `nagato_wikipedia_api_search()` for online queries."
        )

    try:
        conn, _ = get_wikipedia_connection(db_path, _ctx)
    except Exception as exc:
        return nagato_error(f"Failed to connect to Wikipedia database: {exc}", tool="nagato_wikipedia_search")

    cursor = conn.cursor()
    try:
        # Check total available sections
        total_sections = cursor.execute("SELECT COUNT(*) FROM wiki_sections").fetchone()[0]
        if total_sections == 0:
            return (
                "Wikipedia local database is empty. No articles have been imported yet. "
                "Use `nagato_wikipedia_api_search()` to search online."
            )

        # --- BM25 Search via FTS5 ---
        bm25_results = []
        # Sanitize query for FTS5 MATCH to prevent injection
        safe_query = _sanitize_fts5_query(normalized_query)
        try:
            cursor.execute(
                """
                SELECT rowid, bm25(wiki_sections_fts) as score
                FROM wiki_sections_fts
                WHERE wiki_sections_fts MATCH ?
                ORDER BY score ASC
                LIMIT 50
                """,
                (safe_query,)
            )
            bm25_rows = cursor.fetchall()
            bm25_results = [(row[0], row[1]) for row in bm25_rows]  # (section_id, bm25_score)
        except Exception as exc:
            logger.debug(f"BM25 search failed: {exc}")

        # --- Dense Vector Search via USearch ---
        dense_results = []
        embedder = _get_embedder()
        if embedder is not None:
            try:
                # Check if there are any embeddings in the database before attempting dense search
                cursor.execute("SELECT COUNT(*) FROM wiki_sections WHERE embedding IS NOT NULL")
                embedding_count = cursor.fetchone()[0]
                if embedding_count > 0:
                    index, chunk_id_map = _ensure_usearch_index(_ctx)
                    query_vecs = list(embedder.embed([normalized_query]))
                    if query_vecs:
                        query_vec = query_vecs[0].astype(np.float32)
                        matches = index.search(query_vec, 50)
                        result_keys = matches.keys
                        result_distances = matches.distances
                        result_section_ids = chunk_id_map[result_keys]
                        dense_results = list(zip(result_section_ids, result_distances, strict=False))  # (section_id, cosine_distance)
                else:
                    logger.debug("No embeddings in database, skipping dense search")
            except Exception as exc:
                logger.debug(f"Dense search failed (USearch index may not exist): {exc}")

        # --- RRF Fusion ---
        # RRF_score = 1/(k + rank_bm25) + 1/(k + rank_dense)
        # Lower BM25 score = better, lower cosine distance = better
        fused_scores = {}

        # Add BM25 ranks
        for rank, (section_id, _) in enumerate(bm25_results):
            fused_scores[section_id] = fused_scores.get(section_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        # Add Dense ranks
        for rank, (section_id, _) in enumerate(dense_results):
            fused_scores[section_id] = fused_scores.get(section_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        if not fused_scores:
            return f"No Wikipedia entries found matching '{normalized_query}'."

        # Sort by RRF score descending
        sorted_sections = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)

        # Apply offset and limit
        final_sections = sorted_sections[start_offset:start_offset + bounded_limit]
        result_section_ids = [sid for sid, _ in final_sections]

        if not result_section_ids:
            return f"No Wikipedia entries found matching '{normalized_query}'."

        # Fetch metadata for results
        placeholders = ','.join('?' * len(result_section_ids))
        cursor.execute(
            f"""
            SELECT id, title, section_name, content
            FROM wiki_sections
            WHERE id IN ({placeholders})
            """,
            result_section_ids
        )

        # Preserve RRF ranking order
        rows_by_id = {row[0]: row for row in cursor.fetchall()}
        rows = [rows_by_id[sid] for sid in result_section_ids if sid in rows_by_id]

        if not rows:
            return f"No Wikipedia entries found matching '{normalized_query}'."

        lines = [f"Wikipedia Hybrid Search Results for '{normalized_query}' ({len(rows)} hits) [RRF]:"]
        for idx, (_section_id, title, section_name, content) in enumerate(rows, start=start_offset + 1):
            snippet = content[:200].replace("\n", " ").strip()
            section_display = f" -> Section: {section_name}" if section_name and section_name != "__lead__" else ""
            lines.append(f"{idx}. [{title}]{section_display}")
            lines.append(f"   Snippet: {snippet}...")
            lines.append(f"   (Use `nagato_wikipedia_fetch(title=\"{title}\", section=\"{section_name if section_name != '__lead__' else ''}\")` for full text)")

        return _truncate_to_token_limit("\n".join(lines), ctx=ctx)

    finally:
        cursor.close()
        conn.close()


async def nagato_wikipedia_fetch(
    title: str,
    section: str | None = None,
    _ctx: Any | None = None,
) -> str:
    """
    Step 2 of the Search -> Fetch pattern: retrieves the complete content
    of a specific Wikipedia article or section identified by search.
    Uses exact B-Tree lookup on wiki_sections(title, section_name).

    Args:
        title: Exact title of the article.
        section: Optional section name to narrow retrieval. If omitted, returns entire article (all sections in order).
        _ctx: Optional context (injected by facade).
    """
    ctx = _get_context(_ctx)
    normalized_title = title.strip()
    if not normalized_title:
        return nagato_error("Title must not be empty.", tool="nagato_wikipedia_fetch")

    db_path = get_wikipedia_db_path(_ctx)
    if not db_path.exists():
        return nagato_error(f"Wikipedia database not found at {db_path}.", tool="nagato_wikipedia_fetch")

    try:
        conn, _ = get_wikipedia_connection(db_path, _ctx)
    except Exception as exc:
        return nagato_error(f"Failed to connect to Wikipedia database: {exc}", tool="nagato_wikipedia_fetch")

    cursor = conn.cursor()
    try:
        if section and section.strip():
            normalized_section = section.strip()
            cursor.execute(
                """
                SELECT section_name, content
                FROM wiki_sections
                WHERE title = ? AND section_name = ?
                ORDER BY section_index ASC
                """,
                (normalized_title, normalized_section),
            )
        else:
            cursor.execute(
                """
                SELECT section_name, content
                FROM wiki_sections
                WHERE title = ?
                ORDER BY section_index ASC
                """,
                (normalized_title,),
            )

        rows = cursor.fetchall()
        if not rows:
            sec_hint = f" section '{section}'" if section else ""
            return f"Article '{normalized_title}'{sec_hint} was not found in the local Wikipedia database."

        output_parts = [f"# {normalized_title}"]
        for sec, content in rows:
            if sec and sec != "__lead__":
                output_parts.append(f"\n## {sec}")
            output_parts.append(content)

        full_text = "\n\n".join(output_parts)
        return _truncate_to_token_limit(full_text, ctx=ctx)

    finally:
        cursor.close()
        conn.close()


async def nagato_wikipedia_api_search(
    query: str,
    limit: int = 5,
    _ctx: Any | None = None,
) -> str:
    """
    Online Wikipedia REST API search fallback. Searches live Wikipedia without
    requiring local database or imported dumps.

    Args:
        query: Search query string.
        limit: Max results (default: 5).
        _ctx: Optional context (injected by facade).
    """
    ctx = _get_context(_ctx)
    normalized_query = query.strip()
    if not normalized_query:
        return nagato_error("Query must not be empty.", tool="nagato_wikipedia_api_search")

    encoded_query = urllib.parse.quote(normalized_query)
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={encoded_query}&limit={limit}&namespace=0&format=json"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NagatoFSM/1.0 (https://github.com/NagatoFSM)"})
        # SSRF protection: validate URL domain
        if not _validate_api_url(url):
            return nagato_error(f"Blocked request to disallowed domain: {url}", tool="nagato_wikipedia_api_search")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # OpenSearch format: [query, [titles], [descriptions], [urls]]
        if len(data) >= 4:
            titles = data[1]
            descriptions = data[2]
            urls = data[3]
            if not titles:
                return f"No online Wikipedia results for '{normalized_query}'."

            lines = [f"Online Wikipedia results for '{normalized_query}':"]
            for i, (t, desc, u) in enumerate(zip(titles, descriptions, urls, strict=False), start=1):
                lines.append(f"{i}. {t}")
                lines.append(f"   URL: {u}")
                if desc:
                    lines.append(f"   Summary: {desc}")
            return _truncate_to_token_limit("\n".join(lines), ctx=ctx)

        return f"Unexpected API response for '{normalized_query}'."

    except Exception as exc:
        return nagato_error(f"Online Wikipedia API request failed: {exc}", tool="nagato_wikipedia_api_search")


def nagato_wikipedia_init(_ctx: Any | None = None) -> str:
    """
    Initializes the local Wikipedia SQLite schema (FTS5 + structural sections).
    Safe and idempotent to call anytime.
    """
    db_path = get_wikipedia_db_path(_ctx)
    try:
        conn, _ = get_wikipedia_connection(db_path, _ctx)
        conn.close()

        # Check USearch index
        index_path = get_usearch_index_path(_ctx)
        usearch_status = "READY" if index_path.exists() else "NOT BUILT (run build_usearch_index)"

        return (
            f"Wikipedia database initialized at: {db_path}\n"
            f"Lexical Search: FTS5 BM25 (always available)\n"
            f"Dense Vector Search: USearch HNSW ({usearch_status})\n"
            f"Default Embedding Model: {DEFAULT_WIKIPEDIA_MODEL} ({DEFAULT_WIKIPEDIA_DIMENSION}-dim)"
        )
    except Exception as exc:
        return nagato_error(f"Initialization failed: {exc}", tool="nagato_wikipedia_init")


def nagato_wikipedia_status(_ctx: Any | None = None) -> str:
    """
    Returns statistics and status of the local Wikipedia database and USearch index.
    """
    db_path = get_wikipedia_db_path(_ctx)
    index_path = get_usearch_index_path(_ctx)

    if not db_path.exists():
        return f"No Wikipedia database found at {db_path}."

    try:
        conn, _ = get_wikipedia_connection(db_path, _ctx)
        cursor = conn.cursor()
        article_count = cursor.execute("SELECT COUNT(*) FROM wiki_articles").fetchone()[0]
        section_count = cursor.execute("SELECT COUNT(*) FROM wiki_sections").fetchone()[0]
        embedded_count = cursor.execute("SELECT COUNT(*) FROM wiki_sections WHERE embedding IS NOT NULL").fetchone()[0]
        cursor.close()
        conn.close()

        size_mb = db_path.stat().st_size / (1024 * 1024)

        # USearch index info
        usearch_info = ""
        if index_path.exists():
            try:
                index, chunk_id_map = _load_usearch_index(index_path)
                index_size_mb = index_path.stat().st_size / (1024 * 1024)
                usearch_info = (
                    f"\n- USearch Index: {index_path}\n"
                    f"  - Index Size: {index_size_mb:.2f} MB\n"
                    f"  - Indexed Vectors: {index.size:,}\n"
                    f"  - Index Type: HNSW (M=32, efConstruction=200)\n"
                    f"  - Search efSearch: 64\n"
                    f"  - Metric: Cosine"
                )
            except Exception as e:
                usearch_info = f"\n- USearch Index: ERROR loading ({e})"
        else:
            usearch_info = "\n- USearch Index: NOT BUILT (run build_usearch_index)"

        return (
            f"Wikipedia Database Status:\n"
            f"- Path: {db_path}\n"
            f"- Database Size: {size_mb:.2f} MB\n"
            f"- Total Articles: {article_count:,}\n"
            f"- Total Sections: {section_count:,}\n"
            f"- Sections with Embeddings: {embedded_count:,}{usearch_info}\n"
            f"- Recommended Model: {DEFAULT_WIKIPEDIA_MODEL}"
        )
    except Exception as exc:
        return nagato_error(f"Status check failed: {exc}", tool="nagato_wikipedia_status")


def nagato_build_usearch_index(_ctx: Any | None = None) -> str:
    """
    Builds the USearch HNSW index from the SQLite section embeddings.
    This is a one-time operation (or run after new data ingestion).
    """
    db_path = get_wikipedia_db_path(_ctx)
    index_path = get_usearch_index_path(_ctx)

    if not db_path.exists():
        return nagato_error(f"Wikipedia database not found at {db_path}.", tool="nagato_build_usearch_index")

    try:
        _build_usearch_index(db_path, index_path)
        index_size_mb = index_path.stat().st_size / (1024 * 1024)
        return (
            f"USearch index built successfully!\n"
            f"- Index: {index_path}\n"
            f"- Size: {index_size_mb:.2f} MB"
        )
    except Exception as exc:
        return nagato_error(f"USearch index build failed: {exc}", tool="nagato_build_usearch_index")


# Aliases for compatibility
wikipedia_search = nagato_wikipedia_search
wikipedia_fetch = nagato_wikipedia_fetch
wikipedia_init = nagato_wikipedia_init
wikipedia_status = nagato_wikipedia_status
wikipedia_api_search = nagato_wikipedia_api_search
build_usearch_index = nagato_build_usearch_index
build_usearch_index_tool = nagato_build_usearch_index
__all__ = ['nagato_wikipedia_search', 'nagato_wikipedia_fetch', 'nagato_wikipedia_api_search', 'nagato_wikipedia_init', 'nagato_wikipedia_status', 'nagato_build_usearch_index']
