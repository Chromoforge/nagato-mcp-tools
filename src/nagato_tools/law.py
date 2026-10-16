"""
German Laws and Court Decisions Hybrid Search and Retrieval for NagatoFSM.

Provides tools for searching and retrieving German federal laws and court decisions
using a hybrid BM25 (FTS5) + Dense Vector (USearch HNSW) approach with Reciprocal
Rank Fusion (RRF).

Data Sources:
- Gesetze im Internet (GII): Federal laws XML
- Open Legal Data Platform (OLDP): REST API & Datasets for laws and court decisions

Tools:
- `nagato_law_search`: Hybrid search across laws and court decisions
- `nagato_law_fetch`: Exact lookup for section/paragraph or court decision
- `nagato_law_init`: Initialize local SQLite schema and indexes
- `nagato_law_status`: DB stats, document counts, packs, and embedding models
- `nagato_law_build_index`: Build/rebuild per-model, per-pack USearch indexes
- `nagato_law_ingest`: Ingest laws and/or court decisions modularly by pack/source
- `nagato_law_compare_models`: Compare retrieval quality across embedding models
- `nagato_law_list_packs`: List available law and case packs
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import numpy as np

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.token_calculator import estimate

logger = logging.getLogger(__name__)

# Constants
DEFAULT_LAW_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_DIMENSIONS: dict[str, int] = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "intfloat/multilingual-e5-large": 1024,
    "intfloat/multilingual-e5-small": 384,
    "BAAI/bge-m3": 1024,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50
RRF_K = 60

# Model embedder cache: model_name -> embedder
_embedders: dict[str, Any] = {}

# USearch index cache: (model_slug, pack_id) -> (Index, np.ndarray[id_map])
_index_cache: dict[tuple[str, str], tuple[Any, np.ndarray]] = {}


def _slugify(val: str) -> str:
    """Sanitize model name or pack ID for filesystem paths."""
    return re.sub(r"[^\w\-.]", "_", val.strip().lower())


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
    """Get token limit from context or use cascading config default for 'law' / 'read' category."""
    max_context_size = getattr(ctx, "MaxContextSize", None)
    if isinstance(max_context_size, int) and max_context_size > 0:
        return max_context_size
    if default is None:
        default = get_tool_token_limit("law")
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


def get_law_config(ctx: Optional[Any] = None):
    """Retrieve LawConfig from FunctionsConfig."""
    try:
        from nagato_tools.config import load_functions_config
        cfg = load_functions_config(ctx=ctx)
        return getattr(cfg, "law", None)
    except Exception as exc:
        logger.debug(f"Failed to load LawConfig: {exc}")
        return None


def get_law_db_path(ctx: Optional[Any] = None) -> Path:
    """Resolve the SQLite database path for German law data."""
    law_cfg = get_law_config(ctx)
    if law_cfg and getattr(law_cfg, "db_path", None):
        return Path(law_cfg.db_path)
    return Path.home() / ".nagato" / "law" / "hybrid.db"


def get_law_index_dir(ctx: Optional[Any] = None) -> Path:
    """Resolve base directory for USearch vector indexes."""
    db_path = get_law_db_path(ctx)
    return db_path.parent / "indexes"


def get_law_usearch_index_path(model_name: str, pack_id: str, ctx: Optional[Any] = None) -> Path:
    """Resolve file path for a specific model & pack USearch index."""
    idx_dir = get_law_index_dir(ctx)
    model_slug = _slugify(model_name)
    pack_slug = _slugify(pack_id)
    return idx_dir / model_slug / f"{pack_slug}_law.index"


def _get_law_embedder(model_name: Optional[str] = None, ctx: Optional[Any] = None) -> Optional[Any]:
    """Load or retrieve cached FastEmbed TextEmbedding model."""
    global _embedders
    if not model_name:
        law_cfg = get_law_config(ctx)
        model_name = getattr(law_cfg, "default_model", DEFAULT_LAW_MODEL) if law_cfg else DEFAULT_LAW_MODEL

    if model_name in _embedders:
        return _embedders[model_name]

    try:
        from fastembed import TextEmbedding
        try:
            embedder = TextEmbedding(model_name=model_name, providers=["CPUExecutionProvider"])
        except TypeError:
            embedder = TextEmbedding(model_name=model_name)
        _embedders[model_name] = embedder
        logger.info(f"Loaded embedding model: {model_name}")
        return embedder
    except Exception as exc:
        logger.warning(f"Could not initialize FastEmbed with model {model_name}: {exc}")
        return None


def get_law_connection(db_path: Optional[Path] = None, ctx: Optional[Any] = None) -> sqlite3.Connection:
    """Get or initialize SQLite connection with WAL, FTS5 and multi-model embedding tables."""
    if db_path is None:
        db_path = get_law_db_path(ctx)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))

    # WAL mode & normal synchronous for fast concurrent reads and ACID writes
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    with conn:
        # 1. Law books metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS law_books (
                book_code TEXT PRIMARY KEY,
                title TEXT,
                revision_date TEXT,
                latest INTEGER DEFAULT 1,
                pack_id TEXT
            )
        """)

        # 2. Law sections (1 § / paragraph = 1 row)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS law_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_code TEXT NOT NULL,
                section TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                markdown_content TEXT,
                pack_id TEXT,
                UNIQUE(book_code, section)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_law_sec_book ON law_sections(book_code, section)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_law_sec_pack ON law_sections(pack_id)")

        # 3. Multi-model embeddings for law sections
        conn.execute("""
            CREATE TABLE IF NOT EXISTS law_section_embeddings (
                section_id INTEGER NOT NULL REFERENCES law_sections(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (section_id, model_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lse_model ON law_section_embeddings(model_name)")

        # 4. Court decisions metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS court_decisions (
                id INTEGER PRIMARY KEY,
                court TEXT NOT NULL,
                file_number TEXT NOT NULL,
                date TEXT,
                ecli TEXT,
                type TEXT,
                slug TEXT,
                source_url TEXT,
                review_status TEXT,
                pack_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_court_dec_fn ON court_decisions(court, file_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_court_dec_slug ON court_decisions(slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_court_dec_pack ON court_decisions(pack_id)")

        # 5. Court decision chunks
        conn.execute("""
            CREATE TABLE IF NOT EXISTS court_decision_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL REFERENCES court_decisions(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                chunk_type TEXT,
                content TEXT NOT NULL,
                markdown_content TEXT,
                metadata_header TEXT,
                pack_id TEXT,
                UNIQUE(decision_id, chunk_index)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdc_dec ON court_decision_chunks(decision_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdc_pack ON court_decision_chunks(pack_id)")

        # 6. Multi-model embeddings for court decision chunks
        conn.execute("""
            CREATE TABLE IF NOT EXISTS court_decision_chunk_embeddings (
                chunk_id INTEGER NOT NULL REFERENCES court_decision_chunks(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chunk_id, model_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdce_model ON court_decision_chunk_embeddings(model_name)")

        # 7. FTS5 for Law Sections (BM25)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS law_sections_fts USING fts5(
                book_code, section, title, content,
                content=law_sections,
                content_rowid=id,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS law_sections_ai AFTER INSERT ON law_sections BEGIN
                INSERT INTO law_sections_fts(rowid, book_code, section, title, content)
                VALUES (new.id, new.book_code, new.section, new.title, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS law_sections_ad AFTER DELETE ON law_sections BEGIN
                INSERT INTO law_sections_fts(law_sections_fts, rowid, book_code, section, title, content)
                VALUES ('delete', old.id, old.book_code, old.section, old.title, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS law_sections_au AFTER UPDATE ON law_sections BEGIN
                INSERT INTO law_sections_fts(law_sections_fts, rowid, book_code, section, title, content)
                VALUES ('delete', old.id, old.book_code, old.section, old.title, old.content);
                INSERT INTO law_sections_fts(rowid, book_code, section, title, content)
                VALUES (new.id, new.book_code, new.section, new.title, new.content);
            END
        """)

        # 8. FTS5 for Court Decision Chunks (BM25)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS court_decision_chunks_fts USING fts5(
                metadata_header, content,
                content=court_decision_chunks,
                content_rowid=id,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cdc_ai AFTER INSERT ON court_decision_chunks BEGIN
                INSERT INTO court_decision_chunks_fts(rowid, metadata_header, content)
                VALUES (new.id, new.metadata_header, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cdc_ad AFTER DELETE ON court_decision_chunks BEGIN
                INSERT INTO court_decision_chunks_fts(court_decision_chunks_fts, rowid, metadata_header, content)
                VALUES ('delete', old.id, old.metadata_header, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cdc_au AFTER UPDATE ON court_decision_chunks BEGIN
                INSERT INTO court_decision_chunks_fts(court_decision_chunks_fts, rowid, metadata_header, content)
                VALUES ('delete', old.id, old.metadata_header, old.content);
                INSERT INTO court_decision_chunks_fts(rowid, metadata_header, content)
                VALUES (new.id, new.metadata_header, new.content);
            END
        """)

    return conn


def _load_usearch_index(index_path: Path, dimension: int) -> tuple[Any, np.ndarray]:
    """Load USearch index and mapping from disk."""
    import usearch.index

    idx = usearch.index.Index(
        ndim=dimension,
        metric=usearch.index.MetricKind.Cos,
        dtype=usearch.index.ScalarKind.F32,
    )
    idx.load(str(index_path))

    map_path = index_path.with_suffix(".npy")
    if map_path.exists():
        id_map = np.load(str(map_path))
    else:
        id_map = np.arange(idx.size, dtype=np.int64)

    return idx, id_map


def _build_usearch_index(
    model_name: str,
    pack_id: str,
    db_path: Path,
    index_path: Path,
    incremental: bool = False,
    ctx: Optional[Any] = None,
) -> int:
    """Build or incrementally update a USearch index for a specific model and pack."""
    import usearch.index

    dimension = MODEL_DIMENSIONS.get(model_name, 384)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    map_path = index_path.with_suffix(".npy")

    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    existing_idx: Optional[Any] = None
    existing_map: list[int] = []

    if incremental and index_path.exists() and map_path.exists():
        try:
            existing_idx, loaded_map = _load_usearch_index(index_path, dimension)
            existing_map = loaded_map.tolist()
        except Exception as e:
            logger.warning(f"Could not load existing index for incremental update: {e}")
            existing_idx = None
            existing_map = []

    already_indexed = set(existing_map)

    # Gather law sections embeddings for this pack & model
    # Note: Global ID scheme encodes source:
    # Positive ID: law section id
    # Negative ID: -chunk_id for court decision chunks
    cursor.execute("""
        SELECT l.id, e.embedding
        FROM law_sections l
        JOIN law_section_embeddings e ON l.id = e.section_id
        WHERE e.model_name = ? AND (? = 'all' OR l.pack_id = ?)
    """, (model_name, pack_id, pack_id))
    law_rows = cursor.fetchall()

    cursor.execute("""
        SELECT c.id, e.embedding
        FROM court_decision_chunks c
        JOIN court_decision_chunk_embeddings e ON c.id = e.chunk_id
        WHERE e.model_name = ? AND (? = 'all' OR c.pack_id = ?)
    """, (model_name, pack_id, pack_id))
    case_rows = cursor.fetchall()
    conn.close()

    new_items: list[tuple[int, bytes]] = []
    for sid, emb in law_rows:
        if sid not in already_indexed:
            new_items.append((sid, emb))
    for cid, emb in case_rows:
        neg_cid = -cid
        if neg_cid not in already_indexed:
            new_items.append((neg_cid, emb))

    if not new_items and existing_idx is not None:
        logger.info(f"Index {index_path} is already up to date ({len(existing_map)} items).")
        return len(existing_map)

    if existing_idx is None:
        idx = usearch.index.Index(
            ndim=dimension,
            metric=usearch.index.MetricKind.Cos,
            dtype=usearch.index.ScalarKind.F32,
        )
        combined_map = []
        vectors_to_add = []
        keys_to_add = []

        total_rows = [(sid, emb) for sid, emb in law_rows] + [(-cid, emb) for cid, emb in case_rows]
        for idx_key, (item_id, emb_blob) in enumerate(total_rows):
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            if len(vec) == dimension:
                vectors_to_add.append(vec)
                keys_to_add.append(idx_key)
                combined_map.append(item_id)

        if vectors_to_add:
            idx.add(np.array(keys_to_add, dtype=np.int64), np.array(vectors_to_add, dtype=np.float32))

        idx.save(str(index_path))
        np.save(str(map_path), np.array(combined_map, dtype=np.int64))
        _index_cache[(_slugify(model_name), _slugify(pack_id))] = (idx, np.array(combined_map, dtype=np.int64))
        return len(combined_map)
    else:
        # Incremental add
        start_key = len(existing_map)
        keys_to_add = []
        vectors_to_add = []
        for offset, (item_id, emb_blob) in enumerate(new_items):
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            if len(vec) == dimension:
                vectors_to_add.append(vec)
                keys_to_add.append(start_key + offset)
                existing_map.append(item_id)

        if vectors_to_add:
            existing_idx.add(np.array(keys_to_add, dtype=np.int64), np.array(vectors_to_add, dtype=np.float32))

        existing_idx.save(str(index_path))
        np.save(str(map_path), np.array(existing_map, dtype=np.int64))
        _index_cache[(_slugify(model_name), _slugify(pack_id))] = (existing_idx, np.array(existing_map, dtype=np.int64))
        return len(existing_map)


def _get_or_load_index(model_name: str, pack_id: str, ctx: Optional[Any] = None) -> Optional[tuple[Any, np.ndarray]]:
    """Retrieve or load cached index for model and pack."""
    global _index_cache
    m_slug = _slugify(model_name)
    p_slug = _slugify(pack_id)
    cache_key = (m_slug, p_slug)

    if cache_key in _index_cache:
        return _index_cache[cache_key]

    index_path = get_law_usearch_index_path(model_name, pack_id, ctx)
    if not index_path.exists():
        return None

    dimension = MODEL_DIMENSIONS.get(model_name, 384)
    try:
        idx, id_map = _load_usearch_index(index_path, dimension)
        _index_cache[cache_key] = (idx, id_map)
        return idx, id_map
    except Exception as exc:
        logger.warning(f"Failed to load USearch index from {index_path}: {exc}")
        return None


# ============================================================================
# Tool Implementations
# ============================================================================

async def nagato_law_init(_ctx: Optional[Any] = None) -> str:
    """
    Initialize the German Law and Decisions SQLite database schema, pragmas,
    and FTS5 tables under ~/.nagato/law/hybrid.db.
    """
    ctx = _get_context(_ctx)
    try:
        db_path = get_law_db_path(ctx)
        conn = get_law_connection(db_path, ctx=ctx)
        cursor = conn.cursor()

        # Check WAL mode
        journal_mode = cursor.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()

        idx_dir = get_law_index_dir(ctx)
        idx_dir.mkdir(parents=True, exist_ok=True)

        return (
            f"Successfully initialized German Law database at: {db_path}\n"
            f"PRAGMA journal_mode={journal_mode}\n"
            f"USearch index root: {idx_dir}\n"
            "Ready for ingestion via `nagato_law_ingest`."
        )
    except Exception as exc:
        return nagato_error(f"Failed to initialize law database: {exc}", tool="nagato_law_init")


async def nagato_law_list_packs(_ctx: Optional[Any] = None) -> str:
    """
    List available preset law and court decision packs with descriptions and book/court filters.
    """
    ctx = _get_context(_ctx)
    law_cfg = get_law_config(ctx)
    packs = getattr(law_cfg, "packs", {}) if law_cfg else {
        "core": ["bgb", "stgb", "gg", "zpo", "hgb", "bnoto"],
        "social": [f"sgb_{i}" for i in range(1, 13)],
        "labor": ["arbzg", "kschg", "betrvg", "entgfg", "muschg", "burlg", "tvog"],
        "admin": ["vwgo", "vwvfg", "bverfg", "thrg", "uvg"],
        "cases_bgh": ["bgh"],
        "cases_bverfg": ["bverfg"],
        "cases_bagh": ["bag"],
        "cases_bshg": ["bsg"],
    }

    conn = get_law_connection(ctx=ctx)
    cursor = conn.cursor()

    pack_counts: dict[str, dict[str, int]] = {}
    for p_id in packs:
        c_laws = cursor.execute("SELECT COUNT(*) FROM law_sections WHERE pack_id = ?", (p_id,)).fetchone()[0]
        c_cases = cursor.execute("SELECT COUNT(*) FROM court_decision_chunks WHERE pack_id = ?", (p_id,)).fetchone()[0]
        pack_counts[p_id] = {"laws": c_laws, "case_chunks": c_cases}
    conn.close()

    lines = ["Available German Law & Court Decision Packs:"]
    for p_id, books in packs.items():
        stats = pack_counts.get(p_id, {"laws": 0, "case_chunks": 0})
        lines.append(
            f"- Pack `{p_id}`: items/codes=[{', '.join(books[:6])}{', ...' if len(books) > 6 else ''}] "
            f"| Ingested sections: {stats['laws']} | Case chunks: {stats['case_chunks']}"
        )

    return _truncate_to_token_limit("\n".join(lines), ctx=ctx)


async def nagato_law_status(_ctx: Optional[Any] = None) -> str:
    """
    Show statistics, document counts, disk usage, active models, and index health.
    """
    ctx = _get_context(_ctx)
    db_path = get_law_db_path(ctx)
    if not db_path.exists():
        return (
            f"Law database not found at {db_path}. "
            "Run `nagato_law_init()` to create the schema."
        )

    try:
        conn = get_law_connection(db_path, ctx=ctx)
        cursor = conn.cursor()

        total_books = cursor.execute("SELECT COUNT(*) FROM law_books").fetchone()[0]
        total_sections = cursor.execute("SELECT COUNT(*) FROM law_sections").fetchone()[0]
        total_decisions = cursor.execute("SELECT COUNT(*) FROM court_decisions").fetchone()[0]
        total_chunks = cursor.execute("SELECT COUNT(*) FROM court_decision_chunks").fetchone()[0]

        cursor.execute("SELECT model_name, COUNT(*) FROM law_section_embeddings GROUP BY model_name")
        law_emb_counts = cursor.fetchall()

        cursor.execute("SELECT model_name, COUNT(*) FROM court_decision_chunk_embeddings GROUP BY model_name")
        case_emb_counts = cursor.fetchall()
        conn.close()

        db_size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0

        idx_dir = get_law_index_dir(ctx)
        index_files = list(idx_dir.glob("*/*.index")) if idx_dir.exists() else []
        idx_size_mb = sum(f.stat().st_size for f in index_files) / (1024 * 1024) if index_files else 0.0

        lines = [
            "German Law & Court Decisions Database Status:",
            f"- Database path: {db_path} ({db_size_mb:.2f} MB)",
            f"- Index directory: {idx_dir} ({len(index_files)} indexes, {idx_size_mb:.2f} MB)",
            f"- Total Law Books: {total_books}",
            f"- Total Law Sections (§): {total_sections}",
            f"- Total Court Decisions: {total_decisions}",
            f"- Total Court Decision Chunks: {total_chunks}",
            "\nEmbeddings by Model (Law Sections):",
        ]
        if law_emb_counts:
            for m_name, count in law_emb_counts:
                lines.append(f"  - {m_name}: {count} vectors")
        else:
            lines.append("  - None")

        lines.append("\nEmbeddings by Model (Court Decision Chunks):")
        if case_emb_counts:
            for m_name, count in case_emb_counts:
                lines.append(f"  - {m_name}: {count} vectors")
        else:
            lines.append("  - None")

        if index_files:
            lines.append("\nUSearch Index Files:")
            for idx_file in index_files:
                rel = idx_file.relative_to(idx_dir)
                size_kb = idx_file.stat().st_size / 1024
                lines.append(f"  - {rel} ({size_kb:.1f} KB)")

        return _truncate_to_token_limit("\n".join(lines), ctx=ctx)
    except Exception as exc:
        return nagato_error(f"Failed to query law status: {exc}", tool="nagato_law_status")


def _chunk_court_decision(
    decision_id: int,
    court: str,
    file_number: str,
    date_str: str,
    content: str,
    pack_id: str,
) -> list[dict[str, Any]]:
    """
    Split court decision into Leitsatz, Tenor, and sub-chunked Entscheidungsgründe
    with prepended metadata headers.
    """
    metadata_header = f"[Gericht: {court} | Aktenzeichen: {file_number} | Datum: {date_str}]"
    chunks: list[dict[str, Any]] = []

    # Rough section splitting heuristics
    sections = re.split(r"\n(?=(?:Leitsatz|Tenor|Tatbestand|Gründe|Entscheidungsgründe)[:\s])", content, flags=re.IGNORECASE)
    chunk_idx = 0

    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue

        chunk_type = "content"
        lower_prefix = sec[:30].lower()
        if "leitsatz" in lower_prefix:
            chunk_type = "leitsatz"
        elif "tenor" in lower_prefix:
            chunk_type = "tenor"
        elif "tatbestand" in lower_prefix or "gründe" in lower_prefix:
            chunk_type = "gruende"

        # Sub-chunk if > 2500 characters (~600-750 tokens)
        target_len = 2500
        overlap = 350
        if len(sec) <= target_len or chunk_type in ("leitsatz", "tenor"):
            chunks.append({
                "decision_id": decision_id,
                "chunk_index": chunk_idx,
                "chunk_type": chunk_type,
                "metadata_header": metadata_header,
                "content": sec,
                "markdown_content": f"### {chunk_type.upper()}\n{metadata_header}\n\n{sec}",
                "pack_id": pack_id,
            })
            chunk_idx += 1
        else:
            start = 0
            while start < len(sec):
                sub_text = sec[start:start + target_len]
                chunks.append({
                    "decision_id": decision_id,
                    "chunk_index": chunk_idx,
                    "chunk_type": chunk_type,
                    "metadata_header": metadata_header,
                    "content": sub_text,
                    "markdown_content": f"### {chunk_type.upper()} (Teil {chunk_idx})\n{metadata_header}\n\n{sub_text}",
                    "pack_id": pack_id,
                })
                chunk_idx += 1
                start += (target_len - overlap)

    return chunks


async def nagato_law_ingest(
    source: str = "api",
    limit: Optional[int] = None,
    since: Optional[str] = None,
    courts: Optional[list[str]] = None,
    book_codes: Optional[list[str]] = None,
    pack: Optional[str] = None,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Ingest German federal laws or court decisions modularly by pack or book codes.
    source: "api" (OLDP REST API), "gii" (Gesetze im Internet XML), or "hf" (Hugging Face Datasets)
    pack: preset pack e.g. "core", "social", "labor", "cases_bgh", etc.
    """
    ctx = _get_context(_ctx)
    law_cfg = get_law_config(ctx)
    preset_packs = getattr(law_cfg, "packs", {}) if law_cfg else {}

    # Resolve pack books and courts
    resolved_books: list[str] = list(book_codes or [])
    resolved_courts: list[str] = list(courts or [])
    resolved_pack = pack or "custom"

    if pack and pack in preset_packs:
        targets = preset_packs[pack]
        if pack.startswith("cases_"):
            resolved_courts.extend(targets)
        else:
            resolved_books.extend(targets)

    # Normalize book codes & courts to lowercase
    resolved_books = [b.lower() for b in resolved_books]
    resolved_courts = [c.lower() for c in resolved_courts]

    db_path = get_law_db_path(ctx)
    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    ingested_laws = 0
    ingested_cases = 0

    try:
        if source == "gii":
            # Gesetze im Internet XML bulk ingestion
            # If book_codes specified, download specific ZIPs or iterate TOC
            if not resolved_books:
                resolved_books = ["bgb"]  # Default minimal set

            for b_code in resolved_books:
                zip_url = f"https://www.gesetze-im-internet.de/{b_code}/xml.zip"
                req = urllib.request.Request(zip_url, headers={"User-Agent": "NagatoFSM/0.26"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        zip_data = resp.read()
                    with zipfile.ZipFile(BytesIO(zip_data)) as zf:
                        xml_files = [f for f in zf.namelist() if f.endswith(".xml")]
                        for xml_name in xml_files:
                            xml_bytes = zf.read(xml_name)
                            root = ET.fromstring(xml_bytes)
                            book_title = root.findtext(".//jurabk") or b_code.upper()

                            cursor.execute(
                                "INSERT OR REPLACE INTO law_books(book_code, title, revision_date, latest, pack_id) VALUES (?, ?, ?, ?, ?)",
                                (b_code, book_title, time.strftime("%Y-%m-%d"), 1, resolved_pack),
                            )

                            for norm in root.findall(".//norm"):
                                enbez = norm.findtext(".//metadaten/enbez") or ""
                                titel = norm.findtext(".//metadaten/titel") or ""
                                text_elem = norm.find(".//textdaten/text")
                                if text_elem is not None:
                                    text_content = "".join(text_elem.itertext()).strip()
                                else:
                                    text_content = ""

                                if enbez and text_content:
                                    sec_num = re.sub(r"[^\d\w]", "", enbez)
                                    md_content = f"# {b_code.upper()} {enbez}: {titel}\n\n{text_content}"
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO law_sections
                                        (book_code, section, title, content, markdown_content, pack_id)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                        """,
                                        (b_code, sec_num, titel, text_content, md_content, resolved_pack),
                                    )
                                    ingested_laws += 1
                                    if limit and ingested_laws >= limit:
                                        break
                except Exception as exc:
                    logger.warning(f"Could not download GII XML for {b_code}: {exc}")

        elif source == "api":
            # OLDP REST API (https://de.openlegaldata.io/api/)
            base_api = "https://de.openlegaldata.io/api"

            # Ingest laws if book_codes/pack targets laws
            is_case_pack = resolved_pack.startswith("cases_") or bool(resolved_courts)
            should_ingest_laws = not is_case_pack or bool(resolved_books)

            if should_ingest_laws:
                query_params: dict[str, Any] = {"format": "json"}
                if resolved_books:
                    query_params["slug"] = resolved_books[0]
                if since:
                    query_params["revision_date__gte"] = since

                url = f"{base_api}/laws/?{urllib.parse.urlencode(query_params)}"
                req = urllib.request.Request(url, headers={"User-Agent": "NagatoFSM/0.26"})
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        results = data.get("results", []) if isinstance(data, dict) else []
                        for item in results:
                            b_code = item.get("slug") or item.get("book_code") or "law"
                            b_title = item.get("title") or b_code
                            sections = item.get("sections") or []

                            cursor.execute(
                                "INSERT OR REPLACE INTO law_books(book_code, title, revision_date, latest, pack_id) VALUES (?, ?, ?, ?, ?)",
                                (b_code, b_title, item.get("revision_date"), 1, resolved_pack),
                            )

                            for sec in sections:
                                sec_num = sec.get("number") or sec.get("section") or str(sec.get("id"))
                                sec_title = sec.get("title") or ""
                                content = sec.get("content") or sec.get("text") or ""
                                if content:
                                    md = f"# {b_code.upper()} § {sec_num}: {sec_title}\n\n{content}"
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO law_sections
                                        (book_code, section, title, content, markdown_content, pack_id)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                        """,
                                        (b_code, sec_num, sec_title, content, md, resolved_pack),
                                    )
                                    ingested_laws += 1
                                    if limit and ingested_laws >= limit:
                                        break
                except Exception as exc:
                    logger.warning(f"OLDP API laws fetch failed: {exc}")

            # Ingest cases if court requested
            if is_case_pack or resolved_courts:
                case_params: dict[str, Any] = {"format": "json"}
                if resolved_courts:
                    case_params["court__slug"] = resolved_courts[0]
                if since:
                    case_params["date__gte"] = since

                url = f"{base_api}/cases/?{urllib.parse.urlencode(case_params)}"
                req = urllib.request.Request(url, headers={"User-Agent": "NagatoFSM/0.26"})
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        results = data.get("results", []) if isinstance(data, dict) else []
                        for item in results:
                            d_id = item.get("id")
                            court_name = item.get("court", {}).get("name") if isinstance(item.get("court"), dict) else str(item.get("court", "BGH"))
                            file_num = item.get("file_number") or ""
                            date_val = item.get("date") or ""
                            content = item.get("content") or item.get("text") or ""

                            if d_id and file_num:
                                cursor.execute(
                                    """
                                    INSERT OR REPLACE INTO court_decisions
                                    (id, court, file_number, date, ecli, type, slug, source_url, review_status, pack_id)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        d_id,
                                        court_name,
                                        file_num,
                                        date_val,
                                        item.get("ecli"),
                                        item.get("type"),
                                        item.get("slug"),
                                        item.get("source_url"),
                                        item.get("review_status"),
                                        resolved_pack,
                                    ),
                                )
                                chunks = _chunk_court_decision(d_id, court_name, file_num, date_val, content, resolved_pack)
                                for c in chunks:
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO court_decision_chunks
                                        (decision_id, chunk_index, chunk_type, content, markdown_content, metadata_header, pack_id)
                                        VALUES (?, ?, ?, ?, ?, ?, ?)
                                        """,
                                        (
                                            c["decision_id"],
                                            c["chunk_index"],
                                            c["chunk_type"],
                                            c["content"],
                                            c["markdown_content"],
                                            c["metadata_header"],
                                            c["pack_id"],
                                        ),
                                    )
                                    ingested_cases += 1
                                    if limit and ingested_cases >= limit:
                                        break
                except Exception as exc:
                    logger.warning(f"OLDP API cases fetch failed: {exc}")

        elif source == "hf":
            # Hugging Face Datasets path (requires accepted terms & token)
            try:
                import datasets
                ds = datasets.load_dataset("openlegaldata/laws-germany", split="train", streaming=True)
                for item in ds:
                    b_code = (item.get("book_code") or "law").lower()
                    if resolved_books and b_code not in resolved_books:
                        continue
                    sec_num = str(item.get("section", ""))
                    content = item.get("content", "")
                    title = item.get("title", "")
                    if content:
                        md = f"# {b_code.upper()} § {sec_num}: {title}\n\n{content}"
                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO law_sections
                            (book_code, section, title, content, markdown_content, pack_id)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (b_code, sec_num, title, content, md, resolved_pack),
                        )
                        ingested_laws += 1
                        if limit and ingested_laws >= limit:
                            break
            except Exception as exc:
                return nagato_error(f"HF Datasets ingestion failed: {exc}", tool="nagato_law_ingest")

        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return (
        f"Ingestion completed from source '{source}' (pack: '{resolved_pack}'):\n"
        f"- Ingested law sections: {ingested_laws}\n"
        f"- Ingested court decision chunks: {ingested_cases}\n"
        "Next step: generate embeddings and build vector indexes via `nagato_law_build_index`."
    )


async def nagato_law_build_index(
    model: Optional[str] = None,
    pack: Optional[str] = None,
    incremental: bool = False,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Generate dense embeddings and build/update USearch indexes for specific or all models and packs.
    """
    ctx = _get_context(_ctx)
    law_cfg = get_law_config(ctx)

    models_to_build = [model] if model else getattr(law_cfg, "models", [DEFAULT_LAW_MODEL]) if law_cfg else [DEFAULT_LAW_MODEL]
    db_path = get_law_db_path(ctx)
    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    # Determine packs to build
    if pack:
        packs_to_build = [pack]
    else:
        cursor.execute("SELECT DISTINCT pack_id FROM law_sections WHERE pack_id IS NOT NULL")
        p_laws = [r[0] for r in cursor.fetchall() if r[0]]
        cursor.execute("SELECT DISTINCT pack_id FROM court_decision_chunks WHERE pack_id IS NOT NULL")
        p_cases = [r[0] for r in cursor.fetchall() if r[0]]
        packs_to_build = list(set(p_laws + p_cases))
        if not packs_to_build:
            packs_to_build = ["core"]

    summary_lines = [f"Building USearch vector indexes (incremental={incremental}):"]

    for m_name in models_to_build:
        embedder = _get_law_embedder(m_name, ctx=ctx)
        if embedder is None:
            summary_lines.append(f"- Model {m_name}: SKIPPED (FastEmbed not available)")
            continue

        dim = MODEL_DIMENSIONS.get(m_name, 384)

        # 1. Embed un-embedded law sections
        cursor.execute("""
            SELECT l.id, l.content
            FROM law_sections l
            LEFT JOIN law_section_embeddings e ON l.id = e.section_id AND e.model_name = ?
            WHERE e.section_id IS NULL
        """, (m_name,))
        unembedded_laws = cursor.fetchall()

        if unembedded_laws:
            batch_size = 64
            for i in range(0, len(unembedded_laws), batch_size):
                batch = unembedded_laws[i:i + batch_size]
                texts = [row[1] for row in batch]
                vecs = list(embedder.embed(texts))
                insert_batch = []
                for (sid, _), vec in zip(batch, vecs):
                    vec_f32 = np.array(vec, dtype=np.float32)
                    insert_batch.append((sid, m_name, vec_f32.tobytes(), len(vec_f32)))
                cursor.executemany("""
                    INSERT OR REPLACE INTO law_section_embeddings(section_id, model_name, embedding, dimensions)
                    VALUES (?, ?, ?, ?)
                """, insert_batch)
                conn.commit()

        # 2. Embed un-embedded court decision chunks
        cursor.execute("""
            SELECT c.id, c.content
            FROM court_decision_chunks c
            LEFT JOIN court_decision_chunk_embeddings e ON c.id = e.chunk_id AND e.model_name = ?
            WHERE e.chunk_id IS NULL
        """, (m_name,))
        unembedded_cases = cursor.fetchall()

        if unembedded_cases:
            batch_size = 64
            for i in range(0, len(unembedded_cases), batch_size):
                batch = unembedded_cases[i:i + batch_size]
                texts = [row[1] for row in batch]
                vecs = list(embedder.embed(texts))
                insert_batch = []
                for (cid, _), vec in zip(batch, vecs):
                    vec_f32 = np.array(vec, dtype=np.float32)
                    insert_batch.append((cid, m_name, vec_f32.tobytes(), len(vec_f32)))
                cursor.executemany("""
                    INSERT OR REPLACE INTO court_decision_chunk_embeddings(chunk_id, model_name, embedding, dimensions)
                    VALUES (?, ?, ?, ?)
                """, insert_batch)
                conn.commit()

        # 3. Build index for each pack
        for p_id in packs_to_build:
            idx_path = get_law_usearch_index_path(m_name, p_id, ctx)
            count = _build_usearch_index(m_name, p_id, db_path, idx_path, incremental=incremental, ctx=ctx)
            summary_lines.append(f"- Model `{m_name}` | Pack `{p_id}`: {count} vectors indexed at {idx_path.name}")

    conn.close()
    return _truncate_to_token_limit("\n".join(summary_lines), ctx=ctx)


async def nagato_law_search(
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
    source: str = "both",
    court: Optional[str] = None,
    book_code: Optional[str] = None,
    pack: Optional[str] = None,
    model: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    start_offset: int = 0,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Hybrid Search (BM25 + USearch Dense Vector) across German federal laws and court decisions.
    Uses Reciprocal Rank Fusion (RRF) to merge lexical and semantic matches.
    """
    ctx = _get_context(_ctx)
    if not query or not query.strip():
        return nagato_error("Query must not be empty.", tool="nagato_law_search")

    normalized_query = query.strip()
    bounded_limit = max(1, min(limit, MAX_SEARCH_LIMIT))

    law_cfg = get_law_config(ctx)
    active_model = model or getattr(law_cfg, "default_model", DEFAULT_LAW_MODEL) if law_cfg else DEFAULT_LAW_MODEL

    db_path = get_law_db_path(ctx)
    if not db_path.exists():
        return (
            "Law local database is empty. No laws or court decisions have been imported yet. "
            "Use `nagato_law_ingest()` to ingest German laws."
        )

    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    try:
        bm25_results: list[tuple[int, float]] = []  # (item_id, score), item_id > 0 for laws, < 0 for cases

        # 1. BM25 Search on Law Sections
        if source in ("both", "laws"):
            fts_query = normalized_query.replace('"', '""')
            try:
                where_clauses = ["law_sections_fts MATCH ?"]
                params: list[Any] = [fts_query]

                if book_code:
                    where_clauses.append("LOWER(l.book_code) = ?")
                    params.append(book_code.lower())
                if pack:
                    where_clauses.append("l.pack_id = ?")
                    params.append(pack)

                sql = f"""
                    SELECT l.id, bm25(law_sections_fts) as score
                    FROM law_sections_fts f
                    JOIN law_sections l ON f.rowid = l.id
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY score ASC
                    LIMIT 50
                """
                cursor.execute(sql, params)
                for r in cursor.fetchall():
                    bm25_results.append((r[0], r[1]))
            except Exception as exc:
                logger.debug(f"BM25 law search error: {exc}")

        # 2. BM25 Search on Court Decision Chunks
        if source in ("both", "cases"):
            fts_query = normalized_query.replace('"', '""')
            try:
                where_clauses = ["court_decision_chunks_fts MATCH ?"]
                params = [fts_query]
                if court:
                    where_clauses.append("LOWER(d.court) = ?")
                    params.append(court.lower())
                if pack:
                    where_clauses.append("c.pack_id = ?")
                    params.append(pack)
                if date_from:
                    where_clauses.append("d.date >= ?")
                    params.append(date_from)
                if date_to:
                    where_clauses.append("d.date <= ?")
                    params.append(date_to)

                sql = f"""
                    SELECT c.id, bm25(court_decision_chunks_fts) as score
                    FROM court_decision_chunks_fts f
                    JOIN court_decision_chunks c ON f.rowid = c.id
                    JOIN court_decisions d ON c.decision_id = d.id
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY score ASC
                    LIMIT 50
                """
                cursor.execute(sql, params)
                for r in cursor.fetchall():
                    bm25_results.append((-r[0], r[1]))  # negative ID for court chunks
            except Exception as exc:
                logger.debug(f"BM25 court decision search error: {exc}")

        # 3. Dense Vector Search via USearch
        dense_results: list[tuple[int, float]] = []
        embedder = _get_law_embedder(active_model, ctx=ctx)
        if embedder is not None:
            try:
                packs_to_search = [pack] if pack else [
                    p for p in (getattr(law_cfg, "packs", {}).keys() if law_cfg else ["core"])
                ]
                query_vecs = list(embedder.embed([normalized_query]))
                if query_vecs:
                    q_vec = np.array(query_vecs[0], dtype=np.float32)
                    for p_id in packs_to_search:
                        loaded = _get_or_load_index(active_model, p_id, ctx=ctx)
                        if loaded is not None:
                            idx, id_map = loaded
                            if idx.size > 0:
                                matches = idx.search(q_vec, 30)
                                for k, dist in zip(matches.keys, matches.distances):
                                    if k < len(id_map):
                                        real_id = int(id_map[k])
                                        # Filter by source type
                                        if real_id > 0 and source not in ("both", "laws"):
                                            continue
                                        if real_id < 0 and source not in ("both", "cases"):
                                            continue
                                        dense_results.append((real_id, float(dist)))
            except Exception as exc:
                logger.debug(f"Dense vector search failed: {exc}")

        # 4. RRF Fusion
        fused_scores: dict[int, float] = {}

        # Sort BM25 results by score ascending
        bm25_results_sorted = sorted(bm25_results, key=lambda x: x[1])
        for rank, (item_id, _) in enumerate(bm25_results_sorted):
            fused_scores[item_id] = fused_scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        # Sort Dense results by cosine distance ascending
        dense_results_sorted = sorted(dense_results, key=lambda x: x[1])
        for rank, (item_id, _) in enumerate(dense_results_sorted):
            fused_scores[item_id] = fused_scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        if not fused_scores:
            return f"No German law or court decision entries found matching '{normalized_query}'."

        sorted_items = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)
        final_items = sorted_items[start_offset:start_offset + bounded_limit]

        lines = [
            f"German Law Hybrid Search Results for '{normalized_query}' ({len(final_items)} hits) [RRF | Model: {active_model}]:"
        ]

        for rank_idx, (item_id, score) in enumerate(final_items, start=start_offset + 1):
            if item_id > 0:
                # Law Section
                cursor.execute(
                    "SELECT book_code, section, title, content, pack_id FROM law_sections WHERE id = ?",
                    (item_id,)
                )
                row = cursor.fetchone()
                if row:
                    b_code, sec, sec_title, content, p_id = row
                    snippet = content[:180].replace("\n", " ").strip()
                    lines.append(f"{rank_idx}. [LAW] {b_code.upper()} § {sec}: {sec_title} (pack: {p_id}, score: {score:.4f})")
                    lines.append(f"   Snippet: {snippet}...")
                    lines.append(f"   (Use `nagato_law_fetch(source=\"laws\", identifier=\"{b_code}:{sec}\")` for full text)")
            else:
                # Court Decision Chunk
                chunk_id = -item_id
                cursor.execute("""
                    SELECT c.chunk_type, c.metadata_header, c.content, d.court, d.file_number, d.date, d.id, c.pack_id
                    FROM court_decision_chunks c
                    JOIN court_decisions d ON c.decision_id = d.id
                    WHERE c.id = ?
                """, (chunk_id,))
                row = cursor.fetchone()
                if row:
                    c_type, m_header, content, court_name, file_num, d_date, dec_id, p_id = row
                    snippet = content[:180].replace("\n", " ").strip()
                    lines.append(f"{rank_idx}. [CASE] {court_name} {file_num} ({d_date}) [{c_type.upper()}] (pack: {p_id}, score: {score:.4f})")
                    lines.append(f"   Header: {m_header}")
                    lines.append(f"   Snippet: {snippet}...")
                    lines.append(f"   (Use `nagato_law_fetch(source=\"cases\", identifier=\"{dec_id}\")` for full decision)")

        return _truncate_to_token_limit("\n".join(lines), ctx=ctx)
    finally:
        cursor.close()
        conn.close()


async def nagato_law_fetch(
    source: str,
    identifier: str,
    _ctx: Optional[Any] = None,
) -> str:
    """
    Retrieve full content of a German law section or court decision.
    - source="laws": identifier="bgb:823" or "bgb:823:1"
    - source="cases": identifier="decision_id" (numeric) or slug
    """
    ctx = _get_context(_ctx)
    if not identifier or not identifier.strip():
        return nagato_error("Identifier must not be empty.", tool="nagato_law_fetch")

    norm_id = identifier.strip().lower()
    db_path = get_law_db_path(ctx)
    if not db_path.exists():
        return nagato_error("Law database not found.", tool="nagato_law_fetch")

    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    try:
        if source == "laws":
            # Format: "book_code:section" (e.g. "bgb:823")
            parts = norm_id.split(":")
            if len(parts) < 2:
                return nagato_error("Law identifier format must be 'book_code:section' (e.g. 'bgb:823').", tool="nagato_law_fetch")
            book_code = parts[0]
            sec_num = re.sub(r"[^\d\w]", "", parts[1])

            cursor.execute("""
                SELECT l.book_code, l.section, l.title, l.markdown_content, l.content, b.title, l.pack_id
                FROM law_sections l
                LEFT JOIN law_books b ON l.book_code = b.book_code
                WHERE LOWER(l.book_code) = ? AND (LOWER(l.section) = ? OR l.section = ?)
            """, (book_code, sec_num, parts[1]))
            row = cursor.fetchone()

            if not row:
                return f"No law section found for '{norm_id}'."

            b_code, sec, title, md_content, raw_content, b_title, pack_id = row
            output = md_content if md_content else f"# {b_code.upper()} § {sec}: {title}\n\n{raw_content}"
            header = f"[Book: {b_title or b_code.upper()} | Pack: {pack_id}]\n\n"
            return _truncate_to_token_limit(header + output, ctx=ctx)

        elif source == "cases":
            # Format: numeric decision_id or slug
            if norm_id.isdigit():
                cursor.execute("SELECT * FROM court_decisions WHERE id = ?", (int(norm_id),))
            else:
                cursor.execute("SELECT * FROM court_decisions WHERE LOWER(slug) = ? OR LOWER(file_number) = ?", (norm_id, norm_id))
            dec_row = cursor.fetchone()

            if not dec_row:
                return f"No court decision found for identifier '{norm_id}'."

            dec_id = dec_row[0]
            court = dec_row[1]
            file_num = dec_row[2]
            date_val = dec_row[3]
            ecli = dec_row[4]
            p_id = dec_row[9]

            # Fetch all chunks ordered by chunk_index
            cursor.execute("""
                SELECT chunk_type, markdown_content, content
                FROM court_decision_chunks
                WHERE decision_id = ?
                ORDER BY chunk_index ASC
            """, (dec_id,))
            chunks = cursor.fetchall()

            lines = [
                f"# Decision: {court} - {file_num} ({date_val})",
                f"**ECLI**: {ecli or 'N/A'} | **Pack**: {p_id} | **Decision ID**: {dec_id}\n",
            ]
            for c_type, md_c, raw_c in chunks:
                lines.append(md_c if md_c else raw_c)
                lines.append("\n---\n")

            return _truncate_to_token_limit("\n".join(lines), ctx=ctx)

        else:
            return nagato_error("Source must be either 'laws' or 'cases'.", tool="nagato_law_fetch")
    finally:
        cursor.close()
        conn.close()


async def nagato_law_compare_models(
    query: str,
    limit: int = 5,
    models: Optional[list[str]] = None,
    pack: str = "core",
    _ctx: Optional[Any] = None,
) -> str:
    """
    Compare retrieval results and ranking across multiple embedding models for the same query.
    """
    ctx = _get_context(_ctx)
    if not query or not query.strip():
        return nagato_error("Query must not be empty.", tool="nagato_law_compare_models")

    law_cfg = get_law_config(ctx)
    candidate_models = models or (
        getattr(law_cfg, "models", [
            "intfloat/multilingual-e5-small",
            "BAAI/bge-m3",
        ]) if law_cfg else [
            "intfloat/multilingual-e5-small",
            "BAAI/bge-m3",
        ]
    )

    db_path = get_law_db_path(ctx)
    conn = get_law_connection(db_path, ctx=ctx)
    cursor = conn.cursor()

    report_lines = [f"=== Law Model Retrieval Comparison for query: '{query}' (Pack: '{pack}') ==="]

    try:
        for m_name in candidate_models:
            report_lines.append(f"\n--- Model: {m_name} ---")
            embedder = _get_law_embedder(m_name, ctx=ctx)
            if embedder is None:
                report_lines.append("  [Embedder failed to load or model not found]")
                continue

            loaded = _get_or_load_index(m_name, pack, ctx=ctx)
            if loaded is None:
                report_lines.append(f"  [Index for pack '{pack}' not found. Run `nagato_law_build_index` first.]")
                continue

            idx, id_map = loaded
            if idx.size == 0:
                report_lines.append(f"  [Index '{pack}' is empty]")
                continue

            t0 = time.time()
            vecs = list(embedder.embed([query]))
            q_vec = np.array(vecs[0], dtype=np.float32)
            matches = idx.search(q_vec, limit)
            t_elapsed_ms = (time.time() - t0) * 1000

            report_lines.append(f"  Latency: {t_elapsed_ms:.1f}ms | Matches: {len(matches.keys)}")
            for rank_i, (k, dist) in enumerate(zip(matches.keys, matches.distances), start=1):
                if k < len(id_map):
                    item_id = int(id_map[k])
                    if item_id > 0:
                        cursor.execute("SELECT book_code, section, title FROM law_sections WHERE id = ?", (item_id,))
                        r = cursor.fetchone()
                        if r:
                            report_lines.append(f"  {rank_i}. [LAW] {r[0].upper()} § {r[1]}: {r[2]} (cos_dist: {dist:.4f})")
                    else:
                        cursor.execute("""
                            SELECT d.court, d.file_number, c.chunk_type
                            FROM court_decision_chunks c
                            JOIN court_decisions d ON c.decision_id = d.id
                            WHERE c.id = ?
                        """, (-item_id,))
                        r = cursor.fetchone()
                        if r:
                            report_lines.append(f"  {rank_i}. [CASE] {r[0]} {r[1]} [{r[2]}] (cos_dist: {dist:.4f})")

        return _truncate_to_token_limit("\n".join(report_lines), ctx=ctx)
    finally:
        cursor.close()
        conn.close()


# Backwards compatibility / aliases
law_search = nagato_law_search
law_fetch = nagato_law_fetch
law_init = nagato_law_init
law_status = nagato_law_status
law_build_index = nagato_law_build_index
law_ingest = nagato_law_ingest
law_compare_models = nagato_law_compare_models
law_list_packs = nagato_law_list_packs
__all__ = ['nagato_law_init', 'nagato_law_list_packs', 'nagato_law_status', 'nagato_law_ingest', 'nagato_law_build_index', 'nagato_law_search', 'nagato_law_fetch', 'nagato_law_compare_models']
