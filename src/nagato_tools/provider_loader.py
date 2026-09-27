"""
Nagato Provider Loader (V1)

This module handles dynamic provider discovery, config parsing, and loading
of repo-local test providers. In V1, a provider is a Python module located at
.nagato/test_provider.py and configured via .nagato/config.json.

Provider Interface (contract):
- run_suite(suite_name: str, target: str | None = None, context: dict | None = None) -> dict
- Optional: list_suites() -> list[dict]
- Optional: provider_info() -> dict
- Optional: healthcheck() -> dict
"""

import json
import sys
import importlib.util
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Tuple
from dataclasses import dataclass

from nagato_tools.suite_contract import AcceptancePolicy


@dataclass
class ProviderConfig:
    """Parsed .nagato/config.json configuration."""
    provider_module: str
    suite_roles: Dict[str, str]  # e.g. {"gold": "gold", "targeted": "unit"}
    primary_suite: str = "gold"  # semantic role used by nagato_run_configured_suite when no override given
    timeouts: Dict[str, int] = None  # optional per-role timeouts in seconds
    artifacts: Dict[str, Any] = None  # optional artifact storage policy
    acceptance_policies: Dict[str, Any] = None  # optional acceptance policies for report_fix
    
    def __post_init__(self):
        if self.timeouts is None:
            self.timeouts = {}
        if self.artifacts is None:
            self.artifacts = {}
        if self.acceptance_policies is None:
            self.acceptance_policies = {}


class ProviderLoadError(Exception):
    """Raised when provider loading fails."""
    pass


def load_provider_config(workspace_root: Path) -> ProviderConfig:
    """
    Load and parse config.json from the workspace.
    
    Args:
        workspace_root: Root of the Nagato workspace.
    
    Returns:
        Parsed ProviderConfig object.
    
    Raises:
        ProviderLoadError: If config file is missing or malformed.
    """
    # First check TitanTest/config.json (new location)
    config_path = workspace_root / "TitanTest" / "config.json"
    
    # Fall back to .nagato/config.json (legacy location)
    if not config_path.exists():
        config_path = workspace_root / ".nagato" / "config.json"
    
    if not config_path.exists():
        raise ProviderLoadError(
            f"Provider config not found at {config_path}. "
            f"Create TitanTest/config.json or .nagato/config.json with at least provider_module and suite_roles."
        )
    
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProviderLoadError(f"Invalid JSON in {config_path}: {e}")
    except Exception as e:
        raise ProviderLoadError(f"Failed to read {config_path}: {e}")
    
    # Validate required fields
    if "provider_module" not in config_data:
        raise ProviderLoadError("config.json missing required 'provider_module' field")
    if "suite_roles" not in config_data:
        raise ProviderLoadError("config.json missing required 'suite_roles' field")
    
    suite_roles = config_data.get("suite_roles", {})
    if not isinstance(suite_roles, dict):
        raise ProviderLoadError("'suite_roles' must be a dict mapping semantic roles to provider suite names")
    
    return ProviderConfig(
        provider_module=str(config_data["provider_module"]),
        suite_roles=suite_roles,
        primary_suite=config_data.get("primary_suite", "gold"),
        timeouts=config_data.get("timeouts", {}),
        artifacts=config_data.get("artifacts", {}),
        acceptance_policies=config_data.get("acceptance_policies", {}),
    )


def load_provider_module(workspace_root: Path, config: ProviderConfig) -> Any:
    """
    Dynamically load the provider module specified in config.
    
    Args:
        workspace_root: Root of the Nagato workspace.
        config: Parsed ProviderConfig.
    
    Returns:
        The loaded provider module object.
    
    Raises:
        ProviderLoadError: If module cannot be loaded or is malformed.
    """
    module_path_str = config.provider_module
    if not module_path_str.endswith(".py"):
        module_path_str += ".py"
    
    module_path = workspace_root / module_path_str
    
    if not module_path.exists():
        raise ProviderLoadError(f"Provider module not found at {module_path}")
    
    try:
        spec = importlib.util.spec_from_file_location("nagato_test_provider", module_path)
        if spec is None or spec.loader is None:
            raise ProviderLoadError(f"Cannot create spec for {module_path}")
        
        module = importlib.util.module_from_spec(spec)
        sys.modules["nagato_test_provider"] = module
        spec.loader.exec_module(module)
        
        return module
    except Exception as e:
        raise ProviderLoadError(f"Failed to load provider module {module_path}: {e}")


def get_provider_run_suite(provider: Any) -> Callable:
    """
    Extract the run_suite function from a loaded provider module.
    
    Args:
        provider: Loaded provider module.
    
    Returns:
        The run_suite callable.
    
    Raises:
        ProviderLoadError: If run_suite is not callable or missing.
    """
    if not hasattr(provider, "run_suite"):
        raise ProviderLoadError("Provider module must define a run_suite function")
    
    run_suite_func = getattr(provider, "run_suite")
    if not callable(run_suite_func):
        raise ProviderLoadError("Provider.run_suite must be callable")
    
    return run_suite_func


def resolve_suite_name(config: ProviderConfig, semantic_role: str) -> str:
    """
    Resolve a semantic role (e.g. "gold", "targeted") to the provider's suite name.
    
    Args:
        config: ProviderConfig with suite_roles mapping.
        semantic_role: Semantic role like "gold" or "targeted".
    
    Returns:
        The concrete suite name to pass to provider.run_suite().
    
    Raises:
        ProviderLoadError: If semantic role is not mapped in config.
    """
    if semantic_role not in config.suite_roles:
        raise ProviderLoadError(
            f"Semantic role '{semantic_role}' not mapped in config.suite_roles. "
            f"Available roles: {list(config.suite_roles.keys())}"
        )
    
    return config.suite_roles[semantic_role]


def get_timeout_for_role(config: ProviderConfig, semantic_role: str) -> int:
    """
    Get the timeout in seconds for a given semantic role.
    
    Args:
        config: ProviderConfig with optional timeouts mapping.
        semantic_role: Semantic role like "gold" or "targeted".
    
    Returns:
        Timeout in seconds, default 600.
    """
    return config.timeouts.get(semantic_role, 600)


def build_provider_context(
    session_id: str,
    fsm_state: str,
    workspace_root: Path,
    semantic_role: str,
    target: Optional[str] = None,
    baseline_reds: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build a context dict to pass to provider.run_suite() as the 'context' parameter.
    
    Args:
        session_id: Current Nagato session ID.
        fsm_state: Current FSM state name.
        workspace_root: Root of the Nagato workspace.
        semantic_role: Semantic suite role like "gold" or "targeted".
        target: Optional specific file or test target.
        baseline_reds: Optional baseline red count from previous run.
    
    Returns:
        Context dict for provider consumption.
    """
    return {
        "session_id": session_id,
        "fsm_state": fsm_state,
        "workspace_root": str(workspace_root),
        "semantic_role": semantic_role,
        "target": target,
        "baseline_reds": baseline_reds,
    }


def load_provider_pipeline(
    workspace_root: Path,
) -> Tuple[ProviderConfig, Any, Callable]:
    """
    Complete provider loading pipeline: config -> module -> run_suite function.
    
    Args:
        workspace_root: Root of the Nagato workspace.
    
    Returns:
        Tuple of (config, module, run_suite_callable).
    
    Raises:
        ProviderLoadError: If any step fails.
    """
    config = load_provider_config(workspace_root)
    module = load_provider_module(workspace_root, config)
    run_suite = get_provider_run_suite(module)
    return config, module, run_suite


def get_report_fix_policy_binding(config: ProviderConfig, target_class: str = "default_on_grid") -> Optional[str]:
    """
    Get the acceptance policy name for report_fix() given a target class.
    
    Args:
        config: ProviderConfig with acceptance_policies.
        target_class: Classification of the target (e.g. 'on_grid', 'off_grid').
    
    Returns:
        Policy name (str), or None if no binding found.
    """
    if not config.acceptance_policies:
        return None
    
    bindings = config.acceptance_policies.get("bindings", {})
    report_fix_bindings = bindings.get("report_fix", {})
    
    # Check target_class-specific override first
    target_classes = report_fix_bindings.get("target_classes", {})
    if target_class in target_classes:
        return target_classes[target_class]
    
    # Fall back to default
    return report_fix_bindings.get("default_policy")


def get_policy_definition(config: ProviderConfig, policy_name: str) -> Optional[Dict[str, Any]]:
    """
    Get the full definition of an acceptance policy.
    
    Args:
        config: ProviderConfig with acceptance_policies.
        policy_name: Name of the policy (e.g. 'require_gold_zero').
    
    Returns:
        Policy definition dict, or None if not found.
    """
    if not config.acceptance_policies:
        return None
    
    definitions = config.acceptance_policies.get("definitions", {})
    return definitions.get(policy_name)


def validate_report_fix_config(
    config: ProviderConfig,
) -> Tuple[Optional[str], list[str]]:
    """Validate the provider configuration used by report_fix()."""
    if not config.acceptance_policies:
        return None, []

    bindings = config.acceptance_policies.get("bindings", {})
    report_fix = bindings.get("report_fix", {})
    if not report_fix:
        return None, []

    definitions = config.acceptance_policies.get("definitions", {})
    policy_names = set()
    default_policy = report_fix.get("default_policy")
    if default_policy:
        policy_names.add(default_policy)
    policy_names.update(report_fix.get("target_classes", {}).values())

    missing_definitions = sorted(name for name in policy_names if name not in definitions)
    if missing_definitions:
        return (
            "report_fix references undefined policies: "
            + ", ".join(missing_definitions),
            [],
        )

    warnings = []
    for policy_name in sorted(policy_names):
        policy_data = definitions[policy_name]
        if not isinstance(policy_data, dict):
            return f"Policy '{policy_name}' must be an object", []

        try:
            policy = AcceptancePolicy.from_dict(policy_data)
        except ValueError as error:
            return f"Policy '{policy_name}' is invalid: {error}", []

        missing_roles = sorted(
            role for role in policy.required_suites if role not in config.suite_roles
        )
        if missing_roles:
            return (
                f"Policy '{policy_name}' requires unmapped suite roles: "
                + ", ".join(missing_roles),
                [],
            )

        if "targeted" in policy.required_suites:
            warnings.append(
                f"Policy '{policy_name}' requires targeted suite execution"
            )

    return None, warnings


def load_provider_config_standalone(
    workspace_root: Path, 
    config_override: Optional[Dict[str, Any]] = None
) -> ProviderConfig:
    """
    Load provider config with optional override for standalone (non-FSM) usage.
    
    Args:
        workspace_root: Root of the Nagato workspace.
        config_override: Optional dict to override config values. If provided,
            skips reading .nagato/config.json and uses the override directly.
    
    Returns:
        Parsed ProviderConfig object.
    
    Raises:
        ProviderLoadError: If config file is missing and no override provided.
    """
    if config_override is not None:
        # Use override config directly
        return ProviderConfig(
            provider_module=config_override.get("provider_module", ".nagato/test_provider.py"),
            suite_roles=config_override.get("suite_roles", {"gold": "gold", "targeted": "unit"}),
            primary_suite=config_override.get("primary_suite", "gold"),
            timeouts=config_override.get("timeouts", {}),
            artifacts=config_override.get("artifacts", {}),
            acceptance_policies=config_override.get("acceptance_policies", {}),
        )
    
    # Fall back to file-based config
    return load_provider_config(workspace_root)
