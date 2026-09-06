"""
Central Token Calculator Module

Provides consistent token estimation for all tool modules using tiktoken.
This module replaces duplicated token estimation logic in multiple files.
"""

import tiktoken
from typing import List, Dict, Optional
from functools import lru_cache

# Module-level encoding cache
_encoding_cache: Dict[str, tiktoken.Encoding] = {}


def _get_encoding(encoding_name: str = "cl100k_base") -> tiktoken.Encoding:
    """Get or create a cached tiktoken encoding instance."""
    if encoding_name not in _encoding_cache:
        _encoding_cache[encoding_name] = tiktoken.get_encoding(encoding_name)
    return _encoding_cache[encoding_name]


def estimate(text: str, encoding_name: str = "cl100k_base") -> int:
    """
    Estimate token count using tiktoken (accurate, primary method).
    
    Args:
        text: Text to estimate tokens for
        encoding_name: Tiktoken encoding to use (default: cl100k_base for GPT-4/3.5)
        
    Returns:
        Estimated token count
    """
    if not text:
        return 0
    try:
        encoding = _get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception:
        # Fallback to word-based estimation
        words = text.split()
        return max(1, len(words)) if words else 0


def estimate_fast(text: str) -> int:
    """
    Fast token estimation using word count (for hot paths only, NOT for budget math).
    
    This is ~10x faster than tiktoken but less accurate. Use only for:
    - File read truncation heuristics
    - Quick size checks before accurate estimation
    
    Args:
        text: Text to estimate tokens for
        
    Returns:
        Rough token estimate (word count * 1.3 approximation)
    """
    if not text:
        return 0
    words = text.split()
    if not words:
        return 0
    # Rough approximation: ~1.3 tokens per word for English text
    return max(1, int(len(words) * 1.3))


def estimate_messages(messages: List[Dict], encoding_name: str = "cl100k_base") -> int:
    """
    Estimate tokens for chat-format messages.
    
    Args:
        messages: List of message dicts with 'role' and 'content' keys
        encoding_name: Tiktoken encoding to use
        
    Returns:
        Estimated token count including message formatting overhead
    """
    if not messages:
        return 0
    
    total = 0
    encoding = _get_encoding(encoding_name)
    
    for msg in messages:
        # Each message has overhead: role + content + formatting
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        # Add tokens for role and content
        total += len(encoding.encode(role))
        total += len(encoding.encode(content))
        # Add overhead for message formatting (~4 tokens per message)
        total += 4
    
    # Add overhead for chat completion formatting (~3 tokens)
    total += 3
    
    return total


def get_budget_breakdown(context: "NagatoFSMContext") -> "TokenBudget":
    """
    Get structured token budget breakdown for a session context.
    
    Args:
        context: Session context instance
        
    Returns:
        TokenBudget with per-component breakdown
    """
    try:
        from fsm.token_budget import TokenBudget  # host-only
    except ImportError:
        TokenBudget = None  # type: ignore[misc]  # fsm-only; standalone gets None
    # Get the context block (NFSM CTX)
    nfsm_ctx = context.ContextBlock if hasattr(context, 'ContextBlock') else ""
    nfsm_ctx_tokens = estimate(nfsm_ctx)
    
    # System prompt (workflow instructions)
    system_prompt = context.WorkflowDescription if hasattr(context, 'WorkflowDescription') else ""
    system_prompt_tokens = estimate(system_prompt)
    
    # Handoff artifact (if available)
    handoff_artifact = ""
    if hasattr(context, 'last_handoff_artifact') and context.last_handoff_artifact:
        handoff_artifact = context.last_handoff_artifact
    handoff_artifact_tokens = estimate(handoff_artifact)
    
    # Agent output (LastActionResult)
    agent_output = context.LastActionResult if hasattr(context, 'LastActionResult') else ""
    agent_output_tokens = estimate(agent_output)
    
    # MCP advance docstring (approximate)
    mcp_advance_docstring = ""
    if hasattr(context, 'FSM') and hasattr(context.FSM, 'advance_workflow'):
        import inspect
        doc = inspect.getdoc(context.FSM.advance_workflow)
        if doc:
            mcp_advance_docstring = doc
    mcp_advance_tokens = estimate(mcp_advance_docstring)
    
    total = nfsm_ctx_tokens + system_prompt_tokens + handoff_artifact_tokens + agent_output_tokens + mcp_advance_tokens
    limit = getattr(context, 'MaxContextTokens', 1200)
    
    return TokenBudget(
        nfsm_ctx=nfsm_ctx_tokens,
        system_prompt=system_prompt_tokens,
        handoff_artifact=handoff_artifact_tokens,
        agent_output=agent_output_tokens,
        mcp_advance_docstring=mcp_advance_tokens,
        total=total,
        limit=limit
    )


def estimate_completion_tokens(text: str, model_name: str = "", provider: str = "unknown") -> tuple[int, str]:
    """
    Estimate completion tokens from response text.
    
    For API providers (OpenAI, Anthropic, Ollama), the actual usage should come from the API response.
    This function is a fallback for when API usage is not available (e.g., Copilot, local models).
    
    Args:
        text: The completion/response text from the LLM
        model_name: Name of the model used (for encoding selection)
        provider: Provider name ("openai", "anthropic", "ollama", "copilot", "unknown")
        
    Returns:
        Tuple of (estimated_tokens, source) where source is "estimate" or "api_usage"
    """
    if not text:
        return 0, "estimate"
    
    # Determine encoding based on model/provider
    encoding_name = "cl100k_base"  # Default for GPT-4 class models
    if "gpt-4" in model_name.lower() or "gpt-3.5" in model_name.lower() or provider == "openai":
        encoding_name = "cl100k_base"
    elif "claude" in model_name.lower() or provider == "anthropic":
        encoding_name = "cl100k_base"  # Anthropic uses similar tokenization
    elif provider == "ollama":
        encoding_name = "cl100k_base"  # Most Ollama models use similar tokenization
    
    try:
        encoding = _get_encoding(encoding_name)
        tokens = len(encoding.encode(text))
        return tokens, "estimate"
    except Exception:
        # Fallback to word-based estimation
        words = text.split()
        return max(1, len(words)) if words else 0, "estimate"


def get_completion_tokens_from_usage(usage: Dict[str, Any], provider: str) -> tuple[int, int, str]:
    """
    Extract completion tokens from API usage response.
    
    Args:
        usage: Usage dict from API response
        provider: Provider name ("openai", "anthropic", "ollama")
        
    Returns:
        Tuple of (completion_tokens, reasoning_tokens, source)
    """
    if not usage:
        return 0, 0, "unknown"
    
    completion_tokens = 0
    reasoning_tokens = 0
    source = "api_usage"
    
    if provider in ("openai", "openrouter"):
        # OpenAI & OpenRouter: usage.completion_tokens, usage.completion_tokens_details.reasoning_tokens
        completion_tokens = usage.get("completion_tokens", 0)
        if "completion_tokens_details" in usage:
            reasoning_tokens = usage["completion_tokens_details"].get("reasoning_tokens", 0)
    elif provider == "anthropic":
        # Anthropic: usage.output_tokens
        completion_tokens = usage.get("output_tokens", 0)
    elif provider == "ollama":
        # Ollama: response.eval_count or usage.eval_count
        completion_tokens = usage.get("eval_count", 0)
        if completion_tokens == 0:
            completion_tokens = usage.get("completion_tokens", 0)
    else:
        # Unknown provider - try common fields
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", usage.get("eval_count", 0)))
        source = "estimate"
    
    return completion_tokens, reasoning_tokens, source