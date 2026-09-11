"""
Authoritative Tool Category Definitions for NagatoFSM.

This module is the single source of truth for:
- ToolCategory enum (lean, no metadata)
- CategoryGroup enum for higher-level reasoning
- CategoryMeta dataclass with rich metadata
- CATEGORY_METADATA mapping: ToolCategory -> CategoryMeta
- Helper functions for category queries

Design principle: Enum stays lean; metadata lives in a separate frozen dataclass mapping.
This keeps the tool layer clean while providing full type safety and documentation.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Final


class ToolCategory(Enum):
    """Lean enum for tool categories — no metadata, just identity.

    Use CATEGORY_METADATA[cat] to access description, group, example_tools, etc.
    """
    EDIT = auto()
    TESTING = auto()
    GIT = auto()
    SEARCH = auto()
    WEB = auto()
    READ = auto()
    EXECUTE = auto()
    DEBUGGING = auto()
    SYSTEM = auto()
    SHELL = auto()
    CREATION = auto()
    PLANNING = auto()
    INSIGHT = auto()
    FINALIZE = auto()

    @property
    def display_name(self) -> str:
        """Return the human-readable display name for this category (backward compat)."""
        return get_category_display_name(self)


class CategoryGroup(Enum):
    """Higher-level category groups for state-level reasoning.

    States can allow/deny entire groups instead of listing individual categories.
    """
    READ_ONLY = auto()      # READ, SEARCH, INSIGHT - no side effects
    MUTATING = auto()       # EDIT, GIT, SHELL - modifies workspace
    TESTING = auto()        # TESTING, DEBUGGING - runs tests/debuggers
    EXECUTION = auto()      # EXECUTE, SYSTEM - runs code/commands
    PLANNING = auto()       # PLANNING, CREATION - creates plans/artifacts
    WEB = auto()            # WEB - external network access
    INSIGHT = auto()        # INSIGHT - knowledge graph operations


@dataclass(frozen=True)
class CategoryMeta:
    """Rich metadata for a tool category.

    Attributes:
        description: Human-readable description of the category's purpose.
        group: CategoryGroup this category belongs to.
        example_tools: List of example tool names (for documentation).
        purpose: One-sentence summary of when to use this category.
        allows_state_changes: Whether tools in this category can modify session state.
    """
    description: str
    group: CategoryGroup
    example_tools: list[str]
    purpose: str
    allows_state_changes: bool = False


# Single source of truth: ToolCategory -> CategoryMeta
# Keep this mapping complete and in sync with tool_registry_builder.py
CATEGORY_METADATA: Final[dict[ToolCategory, CategoryMeta]] = {
    ToolCategory.EDIT: CategoryMeta(
        description="Tools that modify source code files and directory structure (create, edit, delete, rename, create directory).",
        group=CategoryGroup.MUTATING,
        example_tools=["nagato_edit", "nagato_edit_lines", "nagato_delete", "nagato_rename", "nagato_create_dir"],
        purpose="Use when you need to change file contents or structure.",
        allows_state_changes=True,
    ),
    ToolCategory.TESTING: CategoryMeta(
        description="Tools for running tests and test suites (pytest, gold full, configured suites).",
        group=CategoryGroup.TESTING,
        example_tools=["nagato_run_test", "nagato_run_gold_full", "nagato_run_configured_suite"],
        purpose="Use to verify code correctness via automated tests.",
        allows_state_changes=False,
    ),
    ToolCategory.GIT: CategoryMeta(
        description="Git operations (commit, push, pull, status, diff, upload).",
        group=CategoryGroup.MUTATING,
        example_tools=["nagato_git", "nagato_upload"],
        purpose="Use for version control operations that modify repository state.",
        allows_state_changes=True,
    ),
    ToolCategory.SEARCH: CategoryMeta(
        description="Code search tools (text search, AST search, semantic search, file finding).",
        group=CategoryGroup.READ_ONLY,
        example_tools=[
            "nagato_searchInFile", "nagato_searchAST", "nagato_searchInFiles",
            "nagato_semantic_search", "nagato_find_file", "nagato_rebuild_symbol_db",
            "nagato_set_semantic_search_root"
        ],
        purpose="Use to locate code, symbols, or patterns without modifying anything.",
        allows_state_changes=False,
    ),
    ToolCategory.WEB: CategoryMeta(
        description="Web search and external API access.",
        group=CategoryGroup.WEB,
        example_tools=["nagato_web_search"],
        purpose="Use to fetch information from the internet.",
        allows_state_changes=False,
    ),
    ToolCategory.READ: CategoryMeta(
        description="File reading and code inspection tools (read, list, signatures, call graphs).",
        group=CategoryGroup.READ_ONLY,
        example_tools=[
            "nagato_read_file", "nagato_read_lines", "nagato_list_dir",
            "nagato_read_signatures", "nagato_extract_callees", "nagato_extract_callers"
        ],
        purpose="Use to examine code and directory structure without modifications.",
        allows_state_changes=False,
    ),
    ToolCategory.EXECUTE: CategoryMeta(
        description="Code execution tools (run snippets, scripts).",
        group=CategoryGroup.EXECUTION,
        example_tools=["nagato_execute_snippet"],
        purpose="Use to run Python code snippets for exploration or verification.",
        allows_state_changes=False,
    ),
    ToolCategory.DEBUGGING: CategoryMeta(
        description="Debugging and inspection tools (placeholder category; see system/debugger tools).",
        group=CategoryGroup.TESTING,
        example_tools=[],
        purpose="Reserved for future debugging/inspection tools.",
        allows_state_changes=False,
    ),
    ToolCategory.SYSTEM: CategoryMeta(
        description="System-level tools (HITL questions, agent monitoring).",
        group=CategoryGroup.EXECUTION,
        example_tools=["nagato_ask_user", "nagato_answer", "nagato_is_agent_running"],
        purpose="Use for human-in-the-loop interaction and system monitoring.",
        allows_state_changes=False,
    ),
    ToolCategory.SHELL: CategoryMeta(
        description="Shell command execution.",
        group=CategoryGroup.MUTATING,
        example_tools=["nagato_shell"],
        purpose="Use to run arbitrary shell commands (use sparingly).",
        allows_state_changes=True,
    ),
    ToolCategory.CREATION: CategoryMeta(
        description="Artifact creation tools (UUID generation for sessions).",
        group=CategoryGroup.PLANNING,
        example_tools=["nagato_generate_uuid4"],
        purpose="Use to create new session identifiers and bootstrap artifacts.",
        allows_state_changes=False,
    ),
    ToolCategory.PLANNING: CategoryMeta(
        description="Planning tools (write plans, create specifications).",
        group=CategoryGroup.PLANNING,
        example_tools=["nagato_write_plan"],
        purpose="Use to create and modify execution plans.",
        allows_state_changes=False,
    ),
    ToolCategory.INSIGHT: CategoryMeta(
        description="Epistemic World Model / Knowledge Graph tools (concepts, links, radar, search).",
        group=CategoryGroup.INSIGHT,
        example_tools=[
            "nagato_concept_create", "nagato_concept_link", "nagato_concept_collapse",
            "nagato_view_radar", "nagato_search_concept", "nagato_insights_search",
            "nagato_sync_ast_to_insight", "nagato_unified_search", "nagato_drift_report",
            "nagato_impact_analysis", "nagato_block_save", "nagato_block_load",
            "nagato_block_list", "nagato_block_delete", "nagato_insight_promote_to_global"
        ],
        purpose="Use to build and query the architectural knowledge graph.",
        allows_state_changes=False,
    ),
    ToolCategory.FINALIZE: CategoryMeta(
        description="Finalize tools for retrying or skipping VCS upload and ticket closing.",
        group=CategoryGroup.MUTATING,
        example_tools=["nagato_finalize_retry", "nagato_finalize_skip"],
        purpose="Use to retry or skip upload and issue resolution in FINALIZE state.",
        allows_state_changes=True,
    ),
}


# --- Helper Functions ---

def get_category_meta(category: ToolCategory) -> CategoryMeta:
    """Get metadata for a category. Raises KeyError if missing."""
    return CATEGORY_METADATA[category]


def get_categories_in_group(group: CategoryGroup) -> list[ToolCategory]:
    """Return all categories belonging to a group."""
    return [cat for cat, meta in CATEGORY_METADATA.items() if meta.group == group]


def get_all_categories() -> list[ToolCategory]:
    """Return all defined categories in declaration order."""
    return list(ToolCategory)


def validate_metadata_complete() -> list[str]:
    """Verify every ToolCategory has metadata. Returns list of missing categories."""
    missing = [cat for cat in ToolCategory if cat not in CATEGORY_METADATA]
    return [cat.name for cat in missing]


def get_category_display_name(category: ToolCategory) -> str:
    """Get human-readable display name (backward compat with old string_key)."""
    return CATEGORY_METADATA[category].description.split(".")[0]


def get_category_group(category: ToolCategory) -> CategoryGroup:
    """Get the CategoryGroup for a ToolCategory."""
    return get_category_meta(category).group


def category_allows_state_changes(category: ToolCategory) -> bool:
    """Check if tools in this category can modify session/workspace state."""
    return get_category_meta(category).allows_state_changes


# --- Predefined Category Sets for Common State Configurations ---

# Read-only categories: safe for inspection without side effects
READ_ONLY_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.READ_ONLY)

# Mutating categories: modify workspace/files
MUTATING_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.MUTATING)

# Testing/debugging categories
TESTING_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.TESTING)

# Execution categories: run code/commands
EXECUTION_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.EXECUTION)

# Planning categories: create plans/artifacts
PLANNING_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.PLANNING)

# Web access
WEB_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.WEB)

# Insight/Knowledge Graph
INSIGHT_CATEGORIES: Final[list[ToolCategory]] = get_categories_in_group(CategoryGroup.INSIGHT)

# Common state presets (host-application state definitions)
BUG_FIX_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.EDIT, ToolCategory.SEARCH, ToolCategory.READ,
    ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.DEBUGGING,
    ToolCategory.INSIGHT,
]

REFACTOR_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.EDIT, ToolCategory.SEARCH, ToolCategory.READ,
    ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

NEW_CORPUS_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.CREATION, ToolCategory.READ, ToolCategory.SEARCH,
    ToolCategory.WEB, ToolCategory.GIT, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

NEW_SUBSYSTEM_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.CREATION, ToolCategory.EDIT, ToolCategory.READ,
    ToolCategory.GIT, ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

XFAIL_FIX_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.EDIT, ToolCategory.SEARCH, ToolCategory.READ,
    ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.DEBUGGING, ToolCategory.INSIGHT,
]

FIXTURE_FIX_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.EDIT, ToolCategory.SEARCH, ToolCategory.READ,
    ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

CREATE_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.CREATION, ToolCategory.EDIT, ToolCategory.SEARCH,
    ToolCategory.READ, ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

PLANNING_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.PLANNING, ToolCategory.WEB, ToolCategory.EXECUTE, ToolCategory.INSIGHT,
]

IMPLEMENTING_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.EDIT, ToolCategory.SEARCH, ToolCategory.READ,
    ToolCategory.TESTING, ToolCategory.EXECUTE, ToolCategory.DEBUGGING,
    ToolCategory.CREATION, ToolCategory.GIT, ToolCategory.INSIGHT,
]

FINALIZE_CATEGORIES: Final[list[ToolCategory]] = [
    ToolCategory.GIT, ToolCategory.READ, ToolCategory.SEARCH,
    ToolCategory.EDIT, ToolCategory.FINALIZE,
]

# Tools explicitly denied in certain states (keep minimal)
DENIED_TOOLS_DEFAULT: Final[list[str]] = [
    "nagato_delete", "nagato_rename", "nagato_undo_fsm",
]
