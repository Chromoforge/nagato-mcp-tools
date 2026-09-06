import ast
import sqlite3
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import (
    get_ignored_dirs,
    get_semantic_search_config,
    resolve_db_path,
    resolve_semantic_search_root,
)
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.extractcallgraph import CallgraphBuilder
from nagato_tools.extractsignature import SignatureExtractor


# Lazy imports for heavy semantic search dependencies
# These are imported on-demand to avoid requiring them for basic usage
_SemanticIndexSearch = None
_fastembed = None
_sqlite_vec = None
_onnxruntime = None

def _get_semantic_index_search():
    """Lazy-load SemanticIndexSearch and its heavy dependencies."""
    global _SemanticIndexSearch, _fastembed, _sqlite_vec, _onnxruntime
    if _SemanticIndexSearch is None:
        try:
            import fastembed
            _fastembed = fastembed
        except ImportError:
            raise ImportError(
                "Semantic search requires 'fastembed'. "
                "Install with: pip install nagato-mcp-functions[semantic]"
            )
        try:
            import sqlite_vec
            _sqlite_vec = sqlite_vec
        except ImportError:
            raise ImportError(
                "Semantic search requires 'sqlite-vec'. "
                "Install with: pip install nagato-mcp-functions[semantic]"
            )
        try:
            import onnxruntime
            _onnxruntime = onnxruntime
        except ImportError:
            raise ImportError(
                "Semantic search requires 'onnxruntime'. "
                "Install with: pip install nagato-mcp-functions[semantic]"
            )
        # SemanticIndexSearch is defined later in this same module
        # Use local reference to avoid circular import
        global SemanticIndexSearch
        _SemanticIndexSearch = SemanticIndexSearch
    return _SemanticIndexSearch


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to current working directory."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def get_workspace_root() -> Path:
    """Get the workspace root directory (current working directory as fallback)."""
    return Path.cwd()


def _normalize_window(start_offset: int, limit: int) -> tuple[int, int]:
    start = max(0, start_offset)
    page_limit = max(1, limit)
    return start, page_limit


def _format_window_header(prefix: str, total_hits: int, start_offset: int, returned_hits: int) -> str:
    if total_hits == 0:
        return prefix

    window_start = start_offset + 1
    window_end = start_offset + returned_hits
    header = f"{prefix} ({total_hits} total) [showing {window_start}-{window_end}]"
    if window_end < total_hits:
        header += f" [next_start_offset={window_end}]"
    return header


class EmbeddingModels:
    """Manages the initialization and provision of text embedding models.

    Supports various embedding providers (e.g., BAAI, Jina) and configures
    them by default for local execution with CUDA acceleration.

    Attributes:
        model (str): Name of the chosen embedding provider in lowercase.
        dimension (int): Vector dimension of the chosen model.
        Embedding (TextEmbedding): The FastEmbed instance for BAAI.
    """

    def __init__(self, model: str):
        """Initializes the model configuration.

        Args:
            model (str): Name of the model ('baai' or 'jina').
        """
        self.model = model.lower()
        self.dimension = 768 if self.model == "jina" else 384
        self.Embedding = self.getModel()

    def getModel(self) -> TextEmbedding:
        """Selects the appropriate model based on the choice.

        Returns:
            TextEmbedding: The configured FastEmbed instance.

        Raises:
            ValueError: If an unknown model name is passed.
        """
        if self.model == "baai":
            return self.getBAAI()
        elif self.model == "jina":
            return self.getJina()
        else:
            raise ValueError(
                f"Unknown model: {self.model}. Available models: 'baai', 'jina'."
            )

    def getBAAI(self) -> TextEmbedding:
        """Initializes the BAAI/bge-small-en-v1.5 model.

        Suitable for general, short text passages. Saves VRAM through smaller
        context length.

        Returns:
            TextEmbedding: The FastEmbed instance for BAAI.
        """
        return TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            providers=["CPUExecutionProvider"],
            max_length=512,
        )

    def getJina(self) -> TextEmbedding:
        """Initializes the jinaai/jina-embeddings-v2-base-code model.

        Optimized for code representations and larger context windows.

        Returns:
            TextEmbedding: The FastEmbed instance for Jina Code.
        """
        return TextEmbedding(
            model_name="jinaai/jina-embeddings-v2-base-code",
            providers=["CPUExecutionProvider"],
            max_length=2048,
        )


class SemanticIndexSearch:
    """Enables semantic vector search over a codebase using SQLite and sqlite-vec.

    This class breaks Python files into logical chunks (classes/functions),
    creates vectors via FastEmbed, and stores them in a local SQLite database.
    Provides methods for real-time updates when files change within the workspace.

    Attributes:
        conn (sqlite3.Connection): The active SQLite database connection.
        dimension (int): Dimension of the generated vectors.
        Model (TextEmbedding): The embedding model used for embeddings.
        db_path (str): Path to the SQLite database file.
        embedding_model (str): Name of the embedding model used ('baai' or 'jina').
    """

    def __init__(self, config: Optional[dict] = None, ctx: Optional[Any] = None):
        """Initializes the search system and the database. The embedding model is only loaded on first embedding call.
        
        Args:
            config: Optional configuration dict with keys 'db_path', 'embedding_model'.
                   If not provided, loads from .nagato/functions_config.json
            ctx: Optional session context for workspace root resolution
        """
        self.conn = None
        self._model = None
        self._model_wrapper = None
        
        # Load configuration
        if config is None:
            config_obj = get_semantic_search_config()
            workspace_root = _get_workspace_root(ctx)
            self.db_path = resolve_db_path(config_obj, workspace_root)
            self.embedding_model = config_obj.embedding_model
            self.dimension = config_obj.dimension
        else:
            workspace_root = _get_workspace_root(ctx)
            self.db_path = config.get("db_path") or resolve_db_path(get_semantic_search_config(), workspace_root)
            self.embedding_model = config.get("embedding_model", "jina")
            self.dimension = 384 if self.embedding_model == "baai" else 768
            
        self.init_vector_db(self.db_path)

    @property
    def Model(self):
        """Lazy-loads the embedding model on first access."""
        if self._model is None:
            self._model_wrapper = EmbeddingModels(self.embedding_model)
            self._model = self._model_wrapper.Embedding
            self.dimension = self._model_wrapper.dimension
        return self._model

    def init_vector_db(self, db_path: str = None) -> sqlite3.Connection:
        if db_path is None:
            db_path = self.db_path
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        
        # Load sqlite-vec extension if available
        self.has_vector_support = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.has_vector_support = True
        except (ImportError, Exception):
            # Fallback for standalone mode without sqlite-vec extension
            pass

        with self.conn:
            # Existing chunks for vector search
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS code_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT,
                    line_number INTEGER,
                    content TEXT
                )
            """)
            if self.has_vector_support:
                self.conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_code_chunks USING vec0(
                        chunk_id INTEGER PRIMARY KEY,
                        embedding_vector float[{self.dimension}]
                    )
                """)
            
            # --- NEW: global AST symbol index ---
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS global_symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT,
                    symbol_name TEXT,
                    kind TEXT,
                    line_number INTEGER,
                    signature_text TEXT
                )
            """)
            
            # --- NEW: global callgraph ---
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS global_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT,
                    caller TEXT,
                    callee TEXT
                )
            """)
            
            # --- NEW: index-freshness ledger ---
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS indexed_file_meta (
                    file_path TEXT PRIMARY KEY,
                    mtime REAL NOT NULL
                )
            """)
        return self.conn

    def core_nagato_semantic_search(self, query_str: str, limit: int = 3, start_offset: int = 0) -> str:
        """Performs a semantic vector search on the index.

        Converts the search query into a vector and uses the MATCH operator of
        sqlite-vec to find the most semantically similar code snippets.

        Args:
            query_str (str): The search query (e.g., a question or code description).
            limit (int): Maximum number of results to return. Default: 3.
            start_offset (int): Optional zero-based offset into the global ranking.

        Returns:
            str: An LLM-readable formatted list of hits including metadata.
        """
        if not getattr(self, "has_vector_support", False):
            return "Semantic search is unavailable because the 'sqlite-vec' library is not loaded."

        start_offset, limit = _normalize_window(start_offset, limit)
        
        cursor = self.conn.cursor()
        try:
            total_hits = cursor.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
    
            if total_hits == 0:
                return "No matching code snippets found."
    
            if start_offset >= total_hits:
                return (
                    f"Semantic hits for '{query_str}' ({total_hits} total), "
                    f"but none in the requested window [start_offset={start_offset}, limit={limit}]."
                )

    
            # Embed the search term and convert to bytes
            query_vec = list(self.Model.embed([query_str]))[0]
            # Convert to float32 for sqlite-vec (expects 4 bytes per float)
            query_vec_f32 = query_vec.astype(np.float32)
            query_vec_bytes = query_vec_f32.tobytes()
            requested_k = min(total_hits, start_offset + limit)
    
            # KNN search over the virtual table with join on the content table
            cursor.execute(
                """
                SELECT file_path, line_number, content, distance
                FROM (
                    SELECT c.file_path, c.line_number, c.content, v.distance
                    FROM vec_code_chunks v
                    JOIN code_chunks c ON c.id = v.chunk_id
                    WHERE embedding_vector MATCH ? AND k = ?
                    ORDER BY distance ASC
                ) ranked
                ORDER BY distance ASC
                LIMIT ? OFFSET ?
            """,
                (query_vec_bytes, requested_k, limit, start_offset),
            )
    
            rows = cursor.fetchall()
        finally:
            cursor.close()

        if not rows:
            return (
                f"Semantic hits for '{query_str}' ({total_hits} total), "
                f"but none in the requested window [start_offset={start_offset}, limit={limit}]."
            )

        header = _format_window_header(
            f"Semantic hits for '{query_str}'",
            total_hits,
            start_offset,
            len(rows),
        )

        results = [
            f"Match in {r[0]} (Line {r[1]}) [Distance: {r[3]:.4f}]:\n{r[2][:200]}...\n"
            for r in rows
        ]
        return header + ":\n" + "\n".join(results)

    def _index_single_file(self, abs_path: Path, stored_path: str) -> str:
        """
        Index a single file given its absolute path and the path to store in the DB.
        
        This is the shared per-file indexing logic used by both workspace-scoped
        and external directory indexing.
        
        Args:
            abs_path: Absolute path to the file on disk
            stored_path: Path to store in the database (workspace-relative POSIX or absolute POSIX)
            
        Returns:
            Summary string of the indexing result
        """
        if not abs_path.exists():
            return f"File not found: {abs_path}"

        with abs_path.open("r", encoding="utf-8", errors="replace") as f:
            source_code = f.read()

        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return f"Skipped: syntax error in {stored_path}"

        # 1. Fire AST extractions for symbols and calls
        try:
            sig_extractor = SignatureExtractor(str(abs_path))
            extracted_sigs = sig_extractor.extract()  # returns a list of dicts
            
            cg_builder = CallgraphBuilder(str(abs_path))
            call_data = cg_builder.build()  # returns {"calls": ..., "called_from": ...}
        except Exception as e:
            return f"AST analysis failed for {stored_path}: {str(e)}"

        # 2. Collect logical code chunks for vector search
        chunks, metas = [], []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                segment = ast.get_source_segment(source_code, node)
                if segment:
                    # Truncate chunks to max 8000 characters to prevent ONNX runtime out-of-memory errors on massive files
                    chunk_text = segment[:8000]
                    chunks.append(chunk_text)
                    metas.append({
                        "file": stored_path,
                        "line": node.lineno,
                        "name": node.name,
                    })

        # Write DB entries
        with self.conn:
            cursor = self.conn.cursor()
            
            # Delete any existing prior data for this file to ensure idempotence
            cursor.execute(
                "DELETE FROM vec_code_chunks WHERE chunk_id IN (SELECT id FROM code_chunks WHERE file_path = ?)",
                (stored_path,),
            )
            cursor.execute("DELETE FROM code_chunks WHERE file_path = ?", (stored_path,))
            cursor.execute("DELETE FROM global_symbols WHERE file_path = ?", (stored_path,))
            cursor.execute("DELETE FROM global_calls WHERE file_path = ?", (stored_path,))
            cursor.execute("DELETE FROM indexed_file_meta WHERE file_path = ?", (stored_path,))

            # A) Write vector index (if chunks are present)
            if chunks:
                vectors = list(self.Model.embed(chunks))
                for code, meta, vec in zip(chunks, metas, vectors):
                    cursor.execute(
                        "INSERT INTO code_chunks (file_path, line_number, content) VALUES (?, ?, ?)",
                        (meta["file"], meta["line"], f"# {meta['name']}\n{code}"),
                    )
                    # Convert to float32 for sqlite-vec (expects 4 bytes per float)
                    vec_f32 = vec.astype(np.float32)
                    cursor.execute(
                        "INSERT INTO vec_code_chunks (chunk_id, embedding_vector) VALUES (?, ?)",
                        (cursor.lastrowid, vec_f32.tobytes()),
                    )
            
            # B) Write global signatures into the relational table
            for sig in extracted_sigs:
                # Compact the signature for fast LLM access
                decorators = f"@{', @'.join(sig['decorators'])} " if sig['decorators'] else ""
                args = ", ".join([f"{a['name']}: {a['annotation'] or 'Any'}" for a in sig['args']])
                ret = f" -> {sig['returns']}" if sig['returns'] else ""
                doc = f"  # {sig['doc']}" if sig['doc'] else ""
                formatted_sig = f"{decorators}{sig['kind']} {sig['symbol']}({args}){ret}:{doc}"
                
                cursor.execute(
                    """
                    INSERT INTO global_symbols (file_path, symbol_name, kind, line_number, signature_text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (stored_path, sig["symbol"], sig["kind"], sig["line"], formatted_sig)
                )

            # C) Write global calls (caller -> callee)
            for caller, callees in call_data["calls"].items():
                for callee in callees:
                    cursor.execute(
                        "INSERT INTO global_calls (file_path, caller, callee) VALUES (?, ?, ?)",
                        (stored_path, caller, callee)
                    )

            # D) Write index-freshness ledger
            try:
                mtime = abs_path.stat().st_mtime
            except Exception:
                mtime = 0.0
            
            cursor.execute(
                "INSERT INTO indexed_file_meta (file_path, mtime) VALUES (?, ?)",
                (stored_path, mtime),
            )

        return f"Indexed: {len(chunks)} chunks, {len(extracted_sigs)} symbols in {stored_path}."

    def index_directory_tree(self, root_dir: str, *, external: bool = False, ctx: Optional[Any] = None) -> str:
        """
        Recursively traverses a directory and indexes all Python files.
        
        Args:
            root_dir (str): Path to the target directory. If external=False, this is a relative
                           path within the workspace. If external=True, this is an absolute path.
            external (bool): If True, root_dir is treated as an absolute path outside the workspace.
                           Containment check is skipped. Files are stored with absolute POSIX paths.
                           If False (default), root_dir is resolved relative to workspace_root,
                           containment check is enforced, and files are stored with workspace-relative POSIX paths.
            ctx: Optional session context for workspace root resolution
        
        Returns:
            str: Summary of indexing results (e.g., "Indexed 42 files (3 skipped, 0 errors) under <path>.")
        """
        workspace_root = _get_workspace_root(ctx)
        
        if external:
            # External mode: root_dir is already an absolute path
            target_root = Path(root_dir).resolve()
            if not target_root.exists() or not target_root.is_dir():
                return f"ERROR: {root_dir} does not exist or is not a directory."
        else:
            # Workspace mode: resolve relative to workspace_root
            target_root = (workspace_root / root_dir).resolve()
            
            if not target_root.is_relative_to(workspace_root):
                return f"ERROR: Path traversal detected. {root_dir} is outside workspace."
            
            if not target_root.exists() or not target_root.is_dir():
                return f"ERROR: {root_dir} does not exist or is not a directory."

        ignored_dirs = get_ignored_dirs()
        indexed_count = 0
        skipped_count = 0
        error_count = 0
        
        for file_path in target_root.rglob("*.py"):
            parts = file_path.relative_to(target_root).parts
            if any(part in ignored_dirs or part.startswith(".") for part in parts):
                continue
            
            if external:
                # For external files, store absolute POSIX path
                stored_path = file_path.resolve().as_posix()
            else:
                # For workspace files, store workspace-relative POSIX path
                stored_path = str(file_path.relative_to(workspace_root).as_posix())
            
            result = self._index_single_file(file_path, stored_path)
            if "Indexed:" in result:
                indexed_count += 1
            elif "Skipped:" in result or "ERROR:" in result or "not found" in result:
                skipped_count += 1
            else:
                error_count += 1

        return f"Indexed {indexed_count} files ({skipped_count} skipped, {error_count} errors) under {target_root}."

    def nagato_index_file_semantic(self, file_rel_path: str, ctx: Optional[Any] = None) -> str:
        workspace_root = _get_workspace_root(ctx)
        target_file = (workspace_root / file_rel_path).resolve()

        if not target_file.is_relative_to(workspace_root):
            return f"ERROR: Path traversal detected. {file_rel_path} is outside workspace."
            
        file_rel_path = str(target_file.relative_to(workspace_root).as_posix())

        return self._index_single_file(target_file, file_rel_path)

    def update_file_index(self, file_rel_path: str, ctx: Optional[Any] = None) -> str:
        """Prevents drift from code changes made by the agent."""
        return self.nagato_index_file_semantic(file_rel_path, ctx)

    def update_symbol_tables_only(self, file_rel_path: str, ctx: Optional[Any] = None) -> str:
        """Updates only global_symbols and global_calls (no embedding, no ML model). Fast and memory-safe."""
        workspace_root = _get_workspace_root(ctx)
        target_file = (workspace_root / file_rel_path).resolve()

        if not target_file.is_relative_to(workspace_root):
            return f"ERROR: Path traversal detected. {file_rel_path} is outside workspace."

        file_rel_path = str(target_file.relative_to(workspace_root).as_posix())

        if not target_file.exists():
            return f"ERROR: File {file_rel_path} not found."

        with target_file.open("r", encoding="utf-8", errors="replace") as f:
            source_code = f.read()

        try:
            ast.parse(source_code)
        except SyntaxError:
            return f"Skipped: syntax error in {file_rel_path}"

        try:
            sig_extractor = SignatureExtractor(str(target_file))
            extracted_sigs = sig_extractor.extract()

            cg_builder = CallgraphBuilder(str(target_file))
            call_data = cg_builder.build()
        except Exception as e:
            return f"AST analysis failed for {file_rel_path}: {str(e)}"

        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM global_symbols WHERE file_path = ?", (file_rel_path,))
            cursor.execute("DELETE FROM global_calls WHERE file_path = ?", (file_rel_path,))

            for sig in extracted_sigs:
                decorators = f"@{', @'.join(sig['decorators'])} " if sig['decorators'] else ""
                args = ", ".join([f"{a['name']}: {a['annotation'] or 'Any'}" for a in sig['args']])
                ret = f" -> {sig['returns']}" if sig['returns'] else ""
                doc = f"  # {sig['doc']}" if sig['doc'] else ""
                formatted_sig = f"{decorators}{sig['kind']} {sig['symbol']}({args}){ret}:{doc}"
                cursor.execute(
                    "INSERT INTO global_symbols (file_path, symbol_name, kind, line_number, signature_text) VALUES (?, ?, ?, ?, ?)",
                    (file_rel_path, sig["symbol"], sig["kind"], sig["line"], formatted_sig)
                )

            for caller, callees in call_data["calls"].items():
                for callee in callees:
                    cursor.execute(
                        "INSERT INTO global_calls (file_path, caller, callee) VALUES (?, ?, ?)",
                        (file_rel_path, caller, callee)
                    )

        return f"Symbol update: {len(extracted_sigs)} symbols updated in {file_rel_path}."

    def rebuild_symbol_db(self, target_dir: str = None, ctx: Optional[Any] = None) -> str:
        """
        Full rebuild of the SQLite and vector index.
        Deletes the relevant tables and re-indexes all found Python files.
        """
        workspace_root = _get_workspace_root(ctx)
        
        # Determine the scan root using the new precedence chain
        if target_dir:
            scan_root = (workspace_root / target_dir).resolve()
            
            if not scan_root.is_relative_to(workspace_root):
                return nagato_error("Path traversal detected. Target directory is outside workspace.", tool="nagato_rebuild_symbol_db")
            
            if not scan_root.exists() or not scan_root.is_dir():
                return nagato_error(
                    f"Directory '{target_dir}' does not exist or is not a folder.",
                    tool="nagato_rebuild_symbol_db",
                )
        else:
            # Use the resolved semantic search root (respects session override, config, fallback)
            scan_root = resolve_semantic_search_root(ctx, workspace_root)

        # Collect Python files to process
        python_files = []
        ignored_dirs = get_ignored_dirs()
        
        # Scan target directory for Python files
        for file_path in scan_root.rglob("*.py"):
            # For files under workspace_root, check ignored_dirs using workspace-relative parts
            # For external files, check ignored_dirs using parts relative to scan_root
            if scan_root.is_relative_to(workspace_root):
                try:
                    parts = file_path.relative_to(workspace_root).parts
                except ValueError:
                    # File is outside workspace_root (shouldn't happen in this branch)
                    parts = file_path.relative_to(scan_root).parts
            else:
                parts = file_path.relative_to(scan_root).parts
                
            if any(part in ignored_dirs or part.startswith(".") for part in parts):
                continue
            python_files.append(file_path)
        
        # Delete existing data for these files
        with self.conn:
            cursor = self.conn.cursor()
            for file_path in python_files:
                try:
                    if scan_root.is_relative_to(workspace_root):
                        file_rel_path = str(file_path.relative_to(workspace_root).as_posix())
                    else:
                        file_rel_path = file_path.resolve().as_posix()
                    cursor.execute(
                        "DELETE FROM vec_code_chunks WHERE chunk_id IN (SELECT id FROM code_chunks WHERE file_path = ?)",
                        (file_rel_path,),
                    )
                    cursor.execute("DELETE FROM code_chunks WHERE file_path = ?", (file_rel_path,))
                    cursor.execute("DELETE FROM global_symbols WHERE file_path = ?", (file_rel_path,))
                    cursor.execute("DELETE FROM global_calls WHERE file_path = ?", (file_rel_path,))
                    cursor.execute("DELETE FROM indexed_file_meta WHERE file_path = ?", (file_rel_path,))
                except Exception:
                    pass

        indexed_count = 0
        error_count = 0
        for file_path in python_files:
            try:
                if scan_root.is_relative_to(workspace_root):
                    file_rel_path = str(file_path.relative_to(workspace_root).as_posix())
                else:
                    file_rel_path = file_path.resolve().as_posix()
                res = self.nagato_index_file_semantic(file_rel_path)
                if "Indexed:" in res:
                    indexed_count += 1
                else:
                    error_count += 1
            except Exception:
                error_count += 1

        return f"SUCCESS: symbol database re-indexed. {indexed_count} files processed successfully, {error_count} errors."

    def ensure_index_current(self, target_dir: str = None, ctx: Optional[Any] = None) -> str:
        """
        Ensures the semantic index is current by scanning the codebase and re-indexing any new/changed files,
        and purging deleted files from the index.
        """
        # Read auto_index from get_semantic_search_config()
        config = get_semantic_search_config()
        if not getattr(config, "auto_index", True):
            return "AUTO-INDEX: disabled by config."

        workspace_root = _get_workspace_root(ctx)
        
        # Determine the scan root using the new precedence chain
        if target_dir:
            scan_root = (workspace_root / target_dir).resolve()
            if not scan_root.is_relative_to(workspace_root):
                return nagato_error("Path traversal detected. Target directory is outside workspace.", tool="ensure_index_current")
            if not scan_root.exists() or not scan_root.is_dir():
                return f"ERROR: Directory '{target_dir}' does not exist or is not a folder."
            target_dir_rel = str(scan_root.relative_to(workspace_root).as_posix())
            if target_dir_rel == ".":
                target_dir_prefix = ""
            else:
                target_dir_prefix = target_dir_rel if target_dir_rel.endswith("/") else target_dir_rel + "/"
            is_external = False
        else:
            # Use the resolved semantic search root (respects session override, config, fallback)
            scan_root = resolve_semantic_search_root(ctx, workspace_root)
            target_dir_rel = ""
            target_dir_prefix = ""
            is_external = not scan_root.is_relative_to(workspace_root)

        # Cheap first check: empty DB and meta?
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM code_chunks")
            chunks_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM indexed_file_meta")
            meta_count = cursor.fetchone()[0]
        except Exception:
            chunks_count = 0
            meta_count = 0
        finally:
            cursor.close()

        if chunks_count == 0 and meta_count == 0:
            rebuild_res = self.rebuild_symbol_db(target_dir, ctx)
            return f"AUTO-INDEX (first build): {rebuild_res}"

        try:
            cursor = self.conn.cursor()
            try:
                cursor.execute("SELECT file_path, mtime FROM indexed_file_meta")
                all_ledger = cursor.fetchall()
            finally:
                cursor.close()

            ledger = {}
            for fp, m_time in all_ledger:
                if target_dir:
                    if fp == target_dir_rel or fp.startswith(target_dir_prefix):
                        ledger[fp] = m_time
                else:
                    ledger[fp] = m_time

            to_reindex = []
            ignored_dirs = get_ignored_dirs()
            current_files_rel = set()

            for file_path in scan_root.rglob("*.py"):
                # For files under workspace_root, check ignored_dirs using workspace-relative parts
                # For external files, check ignored_dirs using parts relative to scan_root
                if scan_root.is_relative_to(workspace_root):
                    try:
                        parts = file_path.relative_to(workspace_root).parts
                    except ValueError:
                        parts = file_path.relative_to(scan_root).parts
                else:
                    parts = file_path.relative_to(scan_root).parts
                    
                if any(part in ignored_dirs or part.startswith(".") for part in parts):
                    continue
                
                if scan_root.is_relative_to(workspace_root):
                    file_rel_path = str(file_path.relative_to(workspace_root).as_posix())
                else:
                    file_rel_path = file_path.resolve().as_posix()
                    
                current_files_rel.add(file_rel_path)
                
                try:
                    mtime = file_path.stat().st_mtime
                except Exception:
                    mtime = 0.0
                
                if file_rel_path not in ledger:
                    to_reindex.append(file_rel_path)
                elif mtime > ledger[file_rel_path]:
                    to_reindex.append(file_rel_path)

            # Purge deleted files
            to_purge = []
            for ledger_path in ledger:
                if is_external:
                    # For external paths, ledger_path is already absolute
                    actual_path = Path(ledger_path)
                else:
                    actual_path = workspace_root / ledger_path
                if not actual_path.exists():
                    to_purge.append(ledger_path)

            if not to_reindex and not to_purge:
                return ""

            # Delete purge files
            if to_purge:
                with self.conn:
                    cursor = self.conn.cursor()
                    for file_rel_path in to_purge:
                        cursor.execute(
                            "DELETE FROM vec_code_chunks WHERE chunk_id IN (SELECT id FROM code_chunks WHERE file_path = ?)",
                            (file_rel_path,),
                        )
                        cursor.execute("DELETE FROM code_chunks WHERE file_path = ?", (file_rel_path,))
                        cursor.execute("DELETE FROM global_symbols WHERE file_path = ?", (file_rel_path,))
                        cursor.execute("DELETE FROM global_calls WHERE file_path = ?", (file_rel_path,))
                        cursor.execute("DELETE FROM indexed_file_meta WHERE file_path = ?", (file_rel_path,))

            # Re-index files
            updated_count = 0
            skipped_count = 0
            for file_rel_path in to_reindex:
                try:
                    res = self.nagato_index_file_semantic(file_rel_path)
                    if "Skipped:" in res or "ERROR:" in res or "not found" in res:
                        skipped_count += 1
                    else:
                        updated_count += 1
                except Exception as e:
                    print(f"Error auto-indexing file {file_rel_path}: {e}")
                    skipped_count += 1

            unchanged_count = len(current_files_rel) - len(to_reindex)
            if unchanged_count < 0:
                unchanged_count = 0
            return f"AUTO-INDEX: {updated_count} updated, {len(to_purge)} removed, {unchanged_count} unchanged."

        except Exception as e:
            # Broad rescue so search can proceed even if auto-indexing blows up
            print(f"Warning: Auto-index check failed: {e}")
            return f"AUTO-INDEX FAIL: {e}"
__all__ = []
