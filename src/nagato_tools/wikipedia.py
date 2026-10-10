"""
Wikipedia Vector Search and Retrieval for NagatoFSM.

Provides tools for searching and retrieving Wikipedia knowledge:
- `nagato_wikipedia_search`: Two-stage search step 1 (returns rank, title, section, snippet)
- `nagato_wikipedia_fetch`: Two-stage search step 2 (fetches full section or entire article text)
- `nagato_wikipedia_api_search`: Online Wikipedia REST API search fallback
- `nagato_wikipedia_status`: DB stats, document counts, and configuration
- `nagato_wikipedia_init`: Initialize schema and verify vector support

Uses SQLite + sqlite-vec with Jina embeddings (`jinaai/jina-embeddings-v2-base-en`).
All data is stored locally within NagatoFSM (~/.nagato/wikipedia/vec.db or configured path).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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

_wikipedia_embedder = None


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to central config."""
    if ctx is not None and hasattr(ctx, "workspace_root"):
        return Path(ctx.workspace_root)
    return get_workspace_root()


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to mock."""
    if ctx is not None:
        return ctx
    return get_mock_context(_get_workspace_root(ctx))


def _get_context_token_limit(ctx: Any, default: Optional[int] = None) -> int:
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


def get_wikipedia_db_path(ctx: Optional[Any] = None) -> Path:
    """Resolve the SQLite database path for Wikipedia data."""
    try:
        from nagato_tools.config import load_functions_config
        cfg = load_functions_config(ctx=ctx)
        wp_cfg = getattr(cfg, "wikipedia", None)
        if wp_cfg and getattr(wp_cfg, "db_path", None):
            custom_path = Path(wp_cfg.db_path)
            if not custom_path.is_absolute():
                return _get_workspace_root(ctx) / custom_path
            return custom_path
    except Exception as exc:
        logger.debug("Falling back to default db path: %s", exc)

    # Global user directory default: ~/.nagato/wikipedia/vec.db
    base_dir = Path.home() / ".nagato" / "wikipedia"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "vec.db"


def _get_embedder(model_name: str = DEFAULT_WIKIPEDIA_MODEL):
    """Lazy load FastEmbed embedding model instance for Wikipedia."""
    global _wikipedia_embedder
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


def get_wikipedia_connection(db_path: Optional[Path] = None, ctx: Optional[Any] = None) -> tuple[sqlite3.Connection, bool]:
    """Get or initialize SQLite connection with sqlite-vec vector support."""
    if db_path is None:
        db_path = get_wikipedia_db_path(ctx)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))

    has_vector = False
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        has_vector = True
    except Exception as exc:
        logger.debug("sqlite-vec extension not available for wikipedia db: %s", exc)

    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE,
                revision_id INTEGER,
                categories TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                title TEXT,
                section TEXT,
                chunk_index INTEGER,
                content TEXT,
                FOREIGN KEY (article_id) REFERENCES wiki_articles(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_chunks_title ON wiki_chunks(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_chunks_section ON wiki_chunks(section)")

        if has_vector:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_wiki_chunks USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding_vector float[{DEFAULT_WIKIPEDIA_DIMENSION}]
                )
            """)

    return conn, has_vector


# ----------------------------------------------------------------------
# Core Agent Tools
# ----------------------------------------------------------------------

async def nagato_wikipedia_search(
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
    start_offset: int = 0,
    _ctx: Optional[Any] = None,
) -> str:
    """
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
        conn, has_vector = get_wikipedia_connection(db_path, _ctx)
    except Exception as exc:
        return nagato_error(f"Failed to connect to Wikipedia database: {exc}", tool="nagato_wikipedia_search")

    cursor = conn.cursor()
    try:
        # Check total available chunks
        total_chunks = cursor.execute("SELECT COUNT(*) FROM wiki_chunks").fetchone()[0]
        if total_chunks == 0:
            return (
                "Wikipedia local database is empty. No articles have been imported yet. "
                "Use `nagato_wikipedia_api_search()` to search online."
            )

        embedder = _get_embedder()
        rows = []

        if has_vector and embedder is not None:
            # Semantic vector match via sqlite-vec
            query_vecs = list(embedder.embed([normalized_query]))
            if query_vecs:
                query_vec = query_vecs[0].astype(np.float32).tobytes()
                cursor.execute(
                    """
                    SELECT c.title, c.section, c.content, v.distance
                    FROM vec_wiki_chunks v
                    JOIN wiki_chunks c ON c.id = v.chunk_id
                    WHERE embedding_vector MATCH ? AND k = ?
                    ORDER BY distance ASC
                    LIMIT ? OFFSET ?
                    """,
                    (query_vec, start_offset + bounded_limit, bounded_limit, start_offset),
                )
                rows = cursor.fetchall()

        if not rows:
            # Fallback to substring match if vector is unavailable or returned nothing
            like_pattern = f"%{normalized_query}%"
            cursor.execute(
                """
                SELECT title, section, content, 0.0 as distance
                FROM wiki_chunks
                WHERE content LIKE ? OR title LIKE ?
                LIMIT ? OFFSET ?
                """,
                (like_pattern, like_pattern, bounded_limit, start_offset),
            )
            rows = cursor.fetchall()

        if not rows:
            return f"No Wikipedia entries found matching '{normalized_query}'."

        lines = [f"Wikipedia Search Results for '{normalized_query}' ({len(rows)} hits):"]
        for idx, row in enumerate(rows, start=start_offset + 1):
            title, section, content, distance = row[0], row[1], row[2], row[3]
            snippet = content[:200].replace("\n", " ").strip()
            section_display = f" -> Section: {section}" if section else ""
            lines.append(f"{idx}. [{title}]{section_display} (Distance: {distance:.3f})")
            lines.append(f"   Snippet: {snippet}...")
            lines.append(f"   (Use `nagato_wikipedia_fetch(title=\"{title}\", section=\"{section or ''}\")` for full text)")

        return _truncate_to_token_limit("\n".join(lines), ctx=ctx)

    finally:
        cursor.close()
        conn.close()


async def nagato_wikipedia_fetch(
    title: str,
    section: Optional[str] = None,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Step 2 of the Search -> Fetch pattern: retrieves the complete content
    of a specific Wikipedia article or section identified by search.

    Args:
        title: Exact title of the article.
        section: Optional section name to narrow retrieval. If omitted, returns entire article.
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
            cursor.execute(
                """
                SELECT section, content
                FROM wiki_chunks
                WHERE (title = ? OR title LIKE ?) AND section = ?
                ORDER BY chunk_index ASC
                """,
                (normalized_title, f"%{normalized_title}%", section.strip()),
            )
        else:
            cursor.execute(
                """
                SELECT section, content
                FROM wiki_chunks
                WHERE title = ? OR title LIKE ?
                ORDER BY chunk_index ASC
                """,
                (normalized_title, f"%{normalized_title}%"),
            )

        rows = cursor.fetchall()
        if not rows:
            sec_hint = f" section '{section}'" if section else ""
            return f"Article '{normalized_title}'{sec_hint} was not found in the local Wikipedia database."

        output_parts = [f"# {normalized_title}"]
        current_section = None
        for sec, content in rows:
            if sec and sec != current_section:
                current_section = sec
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
    _ctx: Optional[Any] = None,
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
            for i, (t, desc, u) in enumerate(zip(titles, descriptions, urls), start=1):
                lines.append(f"{i}. {t}")
                lines.append(f"   URL: {u}")
                if desc:
                    lines.append(f"   Summary: {desc}")
            return _truncate_to_token_limit("\n".join(lines), ctx=ctx)

        return f"Unexpected API response for '{normalized_query}'."

    except Exception as exc:
        return nagato_error(f"Online Wikipedia API request failed: {exc}", tool="nagato_wikipedia_api_search")


def nagato_wikipedia_init(_ctx: Optional[Any] = None) -> str:
    """
    Initializes the local Wikipedia SQLite schema and checks vector capability.
    Safe and idempotent to call anytime.
    """
    db_path = get_wikipedia_db_path(_ctx)
    try:
        conn, has_vector = get_wikipedia_connection(db_path, _ctx)
        conn.close()
        vec_status = "ENABLED (sqlite-vec available)" if has_vector else "DISABLED (sqlite-vec not loaded, text fallback active)"
        return (
            f"Wikipedia database initialized at: {db_path}\n"
            f"Vector Search: {vec_status}\n"
            f"Default Embedding Model: {DEFAULT_WIKIPEDIA_MODEL} ({DEFAULT_WIKIPEDIA_DIMENSION}-dim)"
        )
    except Exception as exc:
        return nagato_error(f"Initialization failed: {exc}", tool="nagato_wikipedia_init")


def nagato_wikipedia_status(_ctx: Optional[Any] = None) -> str:
    """
    Returns statistics and status of the local Wikipedia database.
    """
    db_path = get_wikipedia_db_path(_ctx)
    if not db_path.exists():
        return f"No Wikipedia database found at {db_path}."

    try:
        conn, has_vector = get_wikipedia_connection(db_path, _ctx)
        cursor = conn.cursor()
        article_count = cursor.execute("SELECT COUNT(*) FROM wiki_articles").fetchone()[0]
        chunk_count = cursor.execute("SELECT COUNT(*) FROM wiki_chunks").fetchone()[0]
        vec_count = 0
        if has_vector:
            vec_count = cursor.execute("SELECT COUNT(*) FROM vec_wiki_chunks").fetchone()[0]
        cursor.close()
        conn.close()

        size_mb = db_path.stat().st_size / (1024 * 1024)
        return (
            f"Wikipedia Database Status:\n"
            f"- Path: {db_path}\n"
            f"- Database Size: {size_mb:.2f} MB\n"
            f"- Total Articles: {article_count:,}\n"
            f"- Total Chunks: {chunk_count:,}\n"
            f"- Indexed Vectors: {vec_count:,}\n"
            f"- Vector Engine: {'sqlite-vec active' if has_vector else 'unavailable (text fallback)'}\n"
            f"- Recommended Model: {DEFAULT_WIKIPEDIA_MODEL}"
        )
    except Exception as exc:
        return nagato_error(f"Status check failed: {exc}", tool="nagato_wikipedia_status")


# Aliases for compatibility
wikipedia_search = nagato_wikipedia_search
wikipedia_fetch = nagato_wikipedia_fetch
wikipedia_init = nagato_wikipedia_init
wikipedia_status = nagato_wikipedia_status
wikipedia_api_search = nagato_wikipedia_api_search
__all__ = ['nagato_wikipedia_search', 'nagato_wikipedia_fetch', 'nagato_wikipedia_api_search', 'nagato_wikipedia_init', 'nagato_wikipedia_status']
