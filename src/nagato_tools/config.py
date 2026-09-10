"""
Configuration loader for functions_internal modules.

Loads function-specific configuration from .nagato/functions_config.json
with sensible defaults for standalone usage.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

DEFAULT_IGNORED_DIRS = (
    ".venv", "venv", "env", ".git", "__pycache__", ".pytest_cache",
    ".egg-info", "rpgproxy.egg-info", "dist", "build",
)


@dataclass
class SemanticSearchConfig:
    """Configuration for semantic search functionality."""
    db_path: str = ""
    embedding_model: str = "jina"  # "baai" or "jina"
    model_cache_dir: str = ""
    dimension: int = 768  # Jina default, will be updated based on model
    auto_index: bool = True
    search_root: str = ""  # Optional override for semantic search base directory

    def __post_init__(self):
        # Set dimension based on model if not explicitly set
        if self.embedding_model.lower() == "baai":
            self.dimension = 384
        elif self.embedding_model.lower() == "jina":
            self.dimension = 768


@dataclass
class LintConfig:
    """Configuration for linting functionality."""
    enabled: bool = True
    soft_mode: bool = True  # Soft mode: warnings only, don't fail on lint issues
    auto_fix: bool = False  # Auto-fix lint issues with ruff --fix
    ruff_path: str = "ruff"  # Path to ruff executable (or just "ruff" if in PATH)
    select: list[str] = field(default_factory=list)  # Specific rules to enable (empty = all)
    ignore: list[str] = field(default_factory=list)  # Specific rules to ignore


@dataclass
class FunctionsConfig:
    """Root configuration for all functions_internal modules."""
    semantic_search: SemanticSearchConfig = field(default_factory=SemanticSearchConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    ignored_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORED_DIRS))
    insight: dict = field(default_factory=dict)
    tool_token_limits: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FunctionsConfig":
        """Create config from dictionary."""
        config = cls()
        if "ignored_dirs" in data:
            config.ignored_dirs = data["ignored_dirs"]
        if "insight" in data and isinstance(data["insight"], dict):
            config.insight = data["insight"]
        if "tool_token_limits" in data and isinstance(data["tool_token_limits"], dict):
            config.tool_token_limits = data["tool_token_limits"]
        if "semantic_search" in data:
            ss_data = data["semantic_search"]
            config.semantic_search = SemanticSearchConfig(
                db_path=ss_data.get("db_path", ""),
                embedding_model=ss_data.get("embedding_model", "jina"),
                model_cache_dir=ss_data.get("model_cache_dir", ""),
                dimension=ss_data.get("dimension", 768),
                auto_index=ss_data.get("auto_index", True),
                search_root=ss_data.get("search_root", ""),
            )
        if "lint" in data:
            lint_data = data["lint"]
            config.lint = LintConfig(
                enabled=lint_data.get("enabled", True),
                soft_mode=lint_data.get("soft_mode", True),
                auto_fix=lint_data.get("auto_fix", False),
                ruff_path=lint_data.get("ruff_path", "ruff"),
                select=lint_data.get("select", []),
                ignore=lint_data.get("ignore", []),
            )
        return config


def get_workspace_root() -> Path:
    """Get the workspace root directory from centralized config."""
    import importlib
    try:
        fsm_config = None  # fsm.config is host-only; standalone uses defaults
        return fsm_config.get_workspace_root()
    except (ImportError, AttributeError):
        return Path.cwd()


def get_config_path() -> Path:
    """Get the path to the functions config file."""
    return get_workspace_root() / ".nagato" / "functions_config.json"


def load_functions_config(config_path: Optional[Path] = None) -> FunctionsConfig:
    """
    Load functions configuration from JSON file.
    
    Args:
        config_path: Optional custom path to config file. Defaults to .nagato/functions_config.json
        
    Returns:
        FunctionsConfig with loaded values or defaults
    """
    if config_path is None:
        config_path = get_config_path()
    
    if not config_path.exists():
        return FunctionsConfig()  # Return defaults
    
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return FunctionsConfig.from_dict(data)
    except (json.JSONDecodeError, OSError, KeyError) as e:
        # Log warning but return defaults to not break functionality
        print(f"Warning: Failed to load functions config from {config_path}: {e}")
        return FunctionsConfig()


def get_semantic_search_config(config_path: Optional[Path] = None) -> SemanticSearchConfig:
    """Get semantic search configuration with defaults."""
    return load_functions_config(config_path).semantic_search


def get_lint_config(config_path: Optional[Path] = None) -> LintConfig:
    """Get lint configuration with defaults."""
    return load_functions_config(config_path).lint


def get_ignored_dirs(config_path: Optional[Path] = None) -> frozenset[str]:
    """Get ignored directories set as a frozenset."""
    return frozenset(load_functions_config(config_path).ignored_dirs)


def get_insight_config(config_path: Optional[Path] = None) -> dict:
    """Get the insight configuration with sensible defaults.
    
    Returns dict with keys: enabled, embedding_provider, embedding_model, embedding_base_url, embedding_dimension.
    Checks .nagato/functions_config.json first (standalone / project override), then falls back to fsm.config.
    """
    cfg = load_functions_config(config_path)
    if cfg.insight:
        defaults = {
            "enabled": False,
            "embedding_provider": "fastembed",
            "embedding_model": "jinaai/jina-embeddings-v2-base-code",
            "embedding_base_url": "http://localhost:11434",
            "embedding_dimension": 768,
        }
        res = dict(defaults)
        res.update(cfg.insight)
        res["enabled"] = bool(res.get("enabled", False))
        return res

    import importlib
    try:
        fsm_config = None  # fsm.config is host-only; standalone uses defaults
        return fsm_config.get_insight_config()
    except (ImportError, AttributeError):
        pass
    return {
        "enabled": False,
        "embedding_provider": "fastembed",
        "embedding_model": "jinaai/jina-embeddings-v2-base-code",
        "embedding_base_url": "http://localhost:11434",
        "embedding_dimension": 768,
    }


def get_force_insight_config() -> dict:
    """Get the force insight gating configuration."""
    import importlib
    try:
        fsm_config = None  # fsm.config is host-only; standalone uses defaults
        return fsm_config.get_force_insight_config()
    except (ImportError, AttributeError):
        pass
    return {
        "bForceInsight": False,
        "forced_insight_tools": [],
    }


def is_force_insight_enabled() -> bool:
    """Check if force insight gating is enabled."""
    config = get_force_insight_config()
    return config.get("bForceInsight", False)


def is_forced_insight_trigger(tool_name: str, category_name: Optional[str] = None) -> bool:
    """Check if a given tool name or category matches the configured forced_insight_tools list."""
    config = get_force_insight_config()
    forced_tools = config.get("forced_insight_tools", [])
    if tool_name in forced_tools:
        return True
    if tool_name.startswith("nagato_"):
        short_name = tool_name[7:]
        if short_name in forced_tools:
            return True
    return False


def resolve_db_path(config: SemanticSearchConfig, workspace_root: Optional[Path] = None) -> str:
    """
    Resolve the database path from config or use default.
    
    Args:
        config: SemanticSearchConfig instance
        workspace_root: Optional workspace root (defaults to auto-detected)
        
    Returns:
        Resolved absolute path to database file
    """
    if workspace_root is None:
        workspace_root = get_workspace_root()
    
    if config.db_path:
        # If relative path, resolve relative to workspace root
        db_path = Path(config.db_path)
        if not db_path.is_absolute():
            db_path = workspace_root / db_path
        return str(db_path)
    
    # Default: nagato_codebase.db in workspace root
    return str(workspace_root / "nagato_codebase.db")


def get_tool_token_limit(category: str, config_path: Optional[Path] = None) -> Optional[int]:
    """
    Get the token limit for a specific tool category with cascading fallback.
    
    Standalone version: checks .nagato/functions_config.json, then returns sensible defaults.
    
    Resolution order:
    1. Explicit category override in functions_config.json: tool_token_limits.<category>
    2. Global default in functions_config.json: tool_token_limits.default
    3. Standalone built-in default for category
    
    If set to 0, negative, null, or 'unlimited' / 'none' / 'off', truncation is disabled (returns None).
    
    Args:
        category: Tool category string (e.g., "read", "search", "web", "edit", "shell")
        config_path: Optional path to custom config file
        
    Returns:
        Token limit int, or None if truncation is disabled
    """
    cat_lower = category.lower()
    try:
        cfg = load_functions_config(config_path)
        if cfg.tool_token_limits:
            raw_val = None
            if cat_lower in cfg.tool_token_limits:
                raw_val = cfg.tool_token_limits[cat_lower]
            elif "default" in cfg.tool_token_limits:
                raw_val = cfg.tool_token_limits["default"]
            
            if raw_val is not None:
                if isinstance(raw_val, str) and raw_val.lower() in ("none", "unlimited", "disabled", "off"):
                    return None
                try:
                    val_int = int(raw_val)
                    if val_int <= 0:
                        return None  # 0 or negative means unlimited (no truncation)
                    return val_int
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass

    # Standalone defaults
    defaults = {
        "read": 20000,
        "search": 20000,
        "web": 10000,
        "edit": 20000,
        "shell": 20000,
        "execute": 20000,
        "lint": 20000,
        "git": 20000,
        "test": 20000,
        "debug": 20000,
    }
    return defaults.get(cat_lower, 20000)


def get_token_limits() -> tuple[int, int]:
    """
    Get the linked token limits: (max_context_size, max_context_tokens).
    
    Standalone version: returns sensible defaults without YAML config.
    
    Returns:
        tuple: (max_context_size, max_context_tokens)
    """
    max_context_tokens = 1200
    max_context_size = int(max_context_tokens * 0.8)  # 960
    return max_context_size, max_context_tokens


def save_semantic_search_root(path: str, config_path: Optional[Path] = None) -> None:
    """
    Save the semantic search root path to the config file.
    
    This is a read-modify-write operation that preserves other config keys
    (like lint, ignored_dirs) while updating only the semantic_search.search_root field.
    
    Args:
        path: The directory path to set as the semantic search root
        config_path: Optional custom path to config file. Defaults to .nagato/functions_config.json
    """
    if config_path is None:
        config_path = get_config_path()
    
    # Load existing config as raw dict to preserve all keys
    data = {}
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    
    # Ensure semantic_search dict exists
    if "semantic_search" not in data:
        data["semantic_search"] = {}
    
    # Update the search_root field
    data["semantic_search"]["search_root"] = path
    
    # Write back with indentation
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def resolve_semantic_search_root(ctx=None, workspace_root: Optional[Path] = None) -> Path:
    """
    Resolve the semantic search root directory based on precedence:
    1. Session override (ctx.semantic_search_root)
    2. Config file base (semantic_search.search_root, relative paths resolved against workspace_root)
    3. workspace_root fallback (current behavior)
    
    Args:
        ctx: Optional session context with semantic_search_root attribute
        workspace_root: Optional workspace root (defaults to auto-detected)
        
    Returns:
        Resolved absolute Path for semantic search root
    """
    if workspace_root is None:
        workspace_root = get_workspace_root()
    
    # 1. Session override (host session mode)
    if ctx is not None:
        session_override = getattr(ctx, "semantic_search_root", None)
        if session_override:
            return Path(session_override).resolve()
    
    # 2. Config file base
    config_path = workspace_root / ".nagato" / "functions_config.json"
    config = load_functions_config(config_path)
    search_root = config.semantic_search.search_root
    if search_root:
        search_path = Path(search_root)
        if not search_path.is_absolute():
            search_path = workspace_root / search_path
        return search_path.resolve()
    
    # 3. Fallback to workspace_root
    return workspace_root
__all__ = []
