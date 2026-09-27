"""
Nagato Suite Dispatcher (V1)

This module dispatches suite execution requests to providers and handles
result normalization, validation, and artifact persistence. In V1, this is
the abstraction layer between FSM workflow logic and concrete test runners.

Responsibilities:
1. Load provider config and module.
2. Build context for the provider.
3. Call provider.run_suite() with appropriate parameters.
4. Validate and normalize the returned result.
5. Persist normalized result and legacy compat artifacts.
6. Return normalized result to FSM.
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any

from nagato_tools.suite_contract import (
    NormalizedSuiteResult,
    SuiteStatus,
    ContractValidationError,
    create_error_result,
    validate_result,
    project_to_legacy_gold_status,
)
from nagato_tools.provider_loader import (
    load_provider_config,
    load_provider_module,
    get_provider_run_suite,
    resolve_suite_name,
    get_timeout_for_role,
    build_provider_context,
    ProviderLoadError,
)


class DispatcherError(Exception):
    """Raised when dispatcher operations fail."""
    pass


def run_suite_via_provider(
    workspace_root: Path,
    semantic_role: str,
    session_id: str,
    fsm_state: str,
    target: Optional[str] = None,
    baseline_reds: Optional[int] = None,
) -> NormalizedSuiteResult:
    """
    Run a test suite via the configured provider.
    
    This is the main entry point from FSM code. It handles:
    1. Provider loading and configuration resolution.
    2. Semantic role -> concrete suite name mapping.
    3. Provider execution with appropriate context and timeout.
    4. Result validation and error recovery.
    5. Artifact persistence for later retrieval.
    
    Args:
        workspace_root: Root of the Nagato workspace.
        semantic_role: Semantic suite role (e.g. "gold", "targeted").
        session_id: Current Nagato session ID.
        fsm_state: Current FSM state name.
        target: Optional specific file or test target to run.
        baseline_reds: Optional baseline red count for context.
    
    Returns:
        A NormalizedSuiteResult object.
    """
    try:
        # Step 1: Load config and provider
        config = load_provider_config(workspace_root)
        module = load_provider_module(workspace_root, config)
        run_suite_func = get_provider_run_suite(module)
        
        # Step 2: Resolve semantic role to concrete suite name
        suite_name = resolve_suite_name(config, semantic_role)
        timeout_seconds = get_timeout_for_role(config, semantic_role)
        
        # Step 3: Build provider context
        provider_context = build_provider_context(
            session_id=session_id,
            fsm_state=fsm_state,
            workspace_root=workspace_root,
            semantic_role=semantic_role,
            target=target,
            baseline_reds=baseline_reds,
        )
        
        # Step 4: Call provider with timeout handling
        # TODO: In a full implementation, wrap this in asyncio.wait_for or similar for timeout enforcement.
        try:
            provider_result = run_suite_func(
                suite_name=suite_name,
                target=target,
                context=provider_context,
            )
        except TimeoutError as e:
            return create_error_result(
                suite_name=suite_name,
                reason=f"Timeout after {timeout_seconds}s",
                logs=str(e),
            )
        except Exception as e:
            return create_error_result(
                suite_name=suite_name,
                reason=f"Provider exception: {type(e).__name__}",
                logs=str(e),
            )
        
        # Step 5: Normalize and validate the result
        result = _normalize_and_validate_provider_result(provider_result, suite_name)
        
        # Step 6: Persist normalized result and compat artifacts
        _persist_suite_result(workspace_root, semantic_role, result)
        
        return result
        
    except ProviderLoadError as e:
        return create_error_result(
            suite_name="unknown",
            reason=f"Provider loading failed: {str(e)}",
            logs=str(e),
        )
    except Exception as e:
        return create_error_result(
            suite_name="unknown",
            reason=f"Dispatcher error: {type(e).__name__}: {str(e)}",
            logs=str(e),
        )


def _normalize_and_validate_provider_result(
    provider_result: Any,
    expected_suite_name: str,
) -> NormalizedSuiteResult:
    """
    Normalize a provider result to NormalizedSuiteResult and validate it.
    
    If the provider returns something that's not a dict, or if validation fails,
    return a deterministic error result instead of raising.
    
    Args:
        provider_result: Raw result from provider.run_suite().
        expected_suite_name: Suite name for error reporting.
    
    Returns:
        A NormalizedSuiteResult, or an error result if normalization/validation failed.
    """
    # Step 1: Ensure result is a dict
    if not isinstance(provider_result, dict):
        return create_error_result(
            suite_name=expected_suite_name,
            reason="Provider returned non-dict result",
            logs=f"Expected dict, got {type(provider_result).__name__}: {provider_result}",
        )
    
    # Step 2: Convert to NormalizedSuiteResult
    try:
        result = NormalizedSuiteResult.from_dict(provider_result)
    except Exception as e:
        return create_error_result(
            suite_name=expected_suite_name,
            reason="Failed to deserialize provider result",
            logs=str(e),
        )
    
    # Step 3: Validate against contract rules
    try:
        validate_result(result)
    except ContractValidationError as e:
        return create_error_result(
            suite_name=expected_suite_name,
            reason=f"Contract violation: {str(e)}",
            logs=str(e),
        )
    
    return result


def _get_suite_results_dir(workspace_root: Path) -> Path:
    env_dir = os.environ.get("NAGATO_SUITE_RESULTS_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    try:
        from nagato_tools.config import _get_yaml_value
        config_dir = _get_yaml_value("fsm.paths.suite_results_dir", ".nagato/suite_results")
        if config_dir:
            return (workspace_root / config_dir).resolve()
    except Exception:
        pass
    return (workspace_root / ".nagato" / "suite_results").resolve()


def _persist_suite_result(
    workspace_root: Path,
    semantic_role: str,
    result: NormalizedSuiteResult,
) -> None:
    """
    Persist a normalized suite result to disk.
    
    In V1, this writes:
    1. A generic suite result artifact at .nagato/suite_results/{semantic_role}_latest.json
    2. For the "gold" role, also emit legacy compat at debug_outputs/gold_full_latest.status.json
    
    Args:
        workspace_root: Root of the Nagato workspace.
        semantic_role: Semantic role of the suite (e.g. "gold", "targeted").
        result: Normalized result to persist.
    """
    # Ensure artifact directories exist
    suite_results_dir = _get_suite_results_dir(workspace_root)
    suite_results_dir.mkdir(parents=True, exist_ok=True)
    
    # Write generic suite result artifact
    result_file = suite_results_dir / f"{semantic_role}_latest.json"
    try:
        result_file.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    except Exception as e:
        # Log but don't fail; result persistence is best-effort
        pass
    
    # For "gold" role, also write legacy compat artifact
    if semantic_role == "gold":
        legacy_file = workspace_root / "debug_outputs" / "gold_full_latest.status.json"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_payload = project_to_legacy_gold_status(result)
        try:
            legacy_file.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")
        except Exception as e:
            # Log but don't fail
            pass


def get_latest_suite_result(
    workspace_root: Path,
    semantic_role: str,
) -> Optional[NormalizedSuiteResult]:
    """
    Retrieve the latest persisted result for a given semantic role.
    
    Args:
        workspace_root: Root of the Nagato workspace.
        semantic_role: Semantic role like "gold" or "targeted".
    
    Returns:
        NormalizedSuiteResult if found, None otherwise.
    """
    suite_results_dir = _get_suite_results_dir(workspace_root)
    result_file = suite_results_dir / f"{semantic_role}_latest.json"
    
    if not result_file.exists():
        return None
    
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
        return NormalizedSuiteResult.from_dict(data)
    except Exception:
        return None
