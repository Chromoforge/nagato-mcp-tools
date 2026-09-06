import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ddgs import DDGS

from nagato_tools.config import get_tool_token_limit, get_workspace_root
from nagato_tools.ctx_mock import get_mock_context
from nagato_tools.errors import _nagato_error as nagato_error
from nagato_tools.token_calculator import estimate

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 10
MAX_RESULT_TOKENS = 1000  # Token limit instead of character limit
SEARCH_TIMEOUT_SECONDS = 20


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


def _get_context(ctx: Optional[Any] = None) -> Any:
    """Get context from injected parameter or fall back to mock."""
    if ctx is not None:
        return ctx
    return get_mock_context(_get_workspace_root(ctx))


def _get_context_token_limit(ctx: Any, default: int = None) -> int:
    """Get token limit from context or use cascading config default for 'web' category."""
    max_context_size = getattr(ctx, "MaxContextSize", None)
    if isinstance(max_context_size, int) and max_context_size > 0:
        return max_context_size
    # Fallback to cascading config (web category)
    if default is None:
        default = get_tool_token_limit("web")
    return default


def _truncate_to_token_limit(text: str, max_tokens: int | None = None, ctx: Any = None) -> str:
    """Truncate text to fit within token limit."""
    if max_tokens is None:
        max_tokens = _get_context_token_limit(ctx)

    if max_tokens <= 0 or not text:
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


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str


def _search_sync(query: str, max_results: int) -> list[WebSearchResult]:
    with DDGS() as provider:
        raw_results = provider.text(query, max_results=max_results) or []
        return [
            WebSearchResult(
                title=str(result.get("title", "")).strip(),
                url=str(result.get("href", result.get("url", ""))).strip(),
                snippet=str(result.get("body", result.get("snippet", ""))).strip(),
            )
            for result in raw_results
        ]


def _format_results(query: str, results: list[WebSearchResult]) -> str:
    if not results:
        return f"No web results for '{query}'."

    lines = [f"Web results for '{query}' ({len(results)}):"]
    for number, result in enumerate(results, start=1):
        lines.extend((
            f"{number}. {result.title or '(ohne Titel)'}",
            f"   URL: {result.url or '(ohne URL)'}",
            f"   Snippet: {result.snippet or '(kein Snippet)'}",
        ))
    return "\n".join(lines)


async def nagato_web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS, _ctx: Optional[Any] = None) -> str:
    """
    Searches the public web and returns a small, source-preserving result set.

    This is retrieval only: results are external, may be incomplete, and must
    be checked against their linked sources before being treated as evidence.
    """
    ctx = _get_context(_ctx)
    normalized_query = query.strip()
    if not normalized_query:
        return nagato_error(
            "The web search requires a non-empty query.",
            tool="nagato_web_search",
        )

    bounded_results = min(max(1, max_results), MAX_RESULTS)
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_search_sync, normalized_query, bounded_results),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return nagato_error("Web search timed out.", tool="nagato_web_search")
    except Exception as e:
        return nagato_error(f"Web search failed: {e}", tool="nagato_web_search")
        
    formatted = _format_results(normalized_query, results)
    return _truncate_to_token_limit(formatted, ctx=ctx)


# Non-prefixed aliases for MCP server compatibility
web_search = nagato_web_search
__all__ = ['nagato_web_search']
