"""
Nagato Suite Result Contract (V1)

This module defines the normalized result model for all test suites,
the validation rules for provider responses, and compatibility projections
for legacy FSM artifacts like gold_full_latest.status.json.

Key concepts:
- A provider returns a NormalizedSuiteResult (rich internal representation).
- Nagato validates and optionally coerces it to ensure FSM stability.
- A compat projection derives legacy fields like reds, anchor, state, run_id for backward compatibility.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime


class SuiteStatus(str, Enum):
    """Canonical suite execution status."""
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    RUNNING = "running"
    MISSING = "missing"


@dataclass
class SuiteMetadata:
    """Optional execution metadata for enriched reporting."""
    exit_code: Optional[int] = None
    duration_seconds: Optional[float] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class SuiteArtifacts:
    """Optional artifact references for rich reporting integration."""
    native_report_path: Optional[str] = None
    log_path: Optional[str] = None
    junit_path: Optional[str] = None
    json_report_path: Optional[str] = None


@dataclass
class NormalizedSuiteResult:
    """
    The canonical normalized result returned by a provider after run_suite().
    
    Mandatory fields:
    - schema_version: contract version, e.g. "nagato.suite_result.v1"
    - suite_name: provider-level suite identifier actually executed
    - status: one of SuiteStatus enum values
    - reds: canonical FSM gating metric (0 for passed, positive for any red state)
    - total_count: total executed checks/tests, 0 if unknown
    - summary_line: one-line human-readable summary
    - run_id: unique execution identifier for artifact tracking
    - logs: truncated log tail or diagnostic summary for LLM consumption
    - anchor: failing test node or file path, or "unknown" if none
    
    Recommended count fields (inferred if omitted):
    - passed_count, failed_count, error_count, skipped_count, xfailed_count, xpassed_count
    
    Optional execution metadata and artifact references.
    """
    schema_version: str
    suite_name: str
    status: SuiteStatus
    reds: int
    total_count: int
    summary_line: str
    run_id: str
    logs: str
    anchor: str = "unknown"
    
    # Recommended count fields
    passed_count: int = 0
    failed_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    xfailed_count: int = 0
    xpassed_count: int = 0
    
    # Detail lists
    failed_targets: List[str] = field(default_factory=list)
    error_targets: List[str] = field(default_factory=list)
    
    # Optional metadata
    metadata: Optional[SuiteMetadata] = None
    artifacts: Optional[SuiteArtifacts] = None
    
    # Opaque provider passthrough
    provider_meta: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict, normalizing nested dataclasses."""
        result = {
            "schema_version": self.schema_version,
            "suite_name": self.suite_name,
            "status": self.status.value if isinstance(self.status, SuiteStatus) else str(self.status),
            "reds": self.reds,
            "total_count": self.total_count,
            "summary_line": self.summary_line,
            "run_id": self.run_id,
            "logs": self.logs,
            "anchor": self.anchor,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "error_count": self.error_count,
            "skipped_count": self.skipped_count,
            "xfailed_count": self.xfailed_count,
            "xpassed_count": self.xpassed_count,
            "failed_targets": self.failed_targets,
            "error_targets": self.error_targets,
        }
        if self.metadata:
            result["metadata"] = asdict(self.metadata)
        if self.artifacts:
            result["artifacts"] = asdict(self.artifacts)
        if self.provider_meta:
            result["provider_meta"] = self.provider_meta
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NormalizedSuiteResult":
        """Deserialize from dict, normalizing nested structures."""
        # Handle status enum
        status_val = data.get("status")
        if isinstance(status_val, str):
            try:
                status = SuiteStatus(status_val)
            except ValueError:
                status = SuiteStatus.ERROR
        else:
            status = status_val or SuiteStatus.ERROR
        
        # Handle nested metadata
        metadata = None
        if data.get("metadata"):
            metadata = SuiteMetadata(**data["metadata"])
        
        # Handle nested artifacts
        artifacts = None
        if data.get("artifacts"):
            artifacts = SuiteArtifacts(**data["artifacts"])
        
        return cls(
            schema_version=str(data.get("schema_version", "nagato.suite_result.v1")),
            suite_name=str(data.get("suite_name", "unknown")),
            status=status,
            reds=int(data.get("reds", 1)),
            total_count=int(data.get("total_count", 0)),
            summary_line=str(data.get("summary_line", "")),
            run_id=str(data.get("run_id", "")),
            logs=str(data.get("logs", "")),
            anchor=str(data.get("anchor", "unknown")),
            passed_count=int(data.get("passed_count", 0)),
            failed_count=int(data.get("failed_count", 0)),
            error_count=int(data.get("error_count", 0)),
            skipped_count=int(data.get("skipped_count", 0)),
            xfailed_count=int(data.get("xfailed_count", 0)),
            xpassed_count=int(data.get("xpassed_count", 0)),
            failed_targets=list(data.get("failed_targets", [])),
            error_targets=list(data.get("error_targets", [])),
            metadata=metadata,
            artifacts=artifacts,
            provider_meta=dict(data.get("provider_meta", {})),
        )


class ContractValidationError(Exception):
    """Raised when a provider result violates contract rules."""
    pass


def validate_result(result: NormalizedSuiteResult) -> None:
    """
    Validate a normalized suite result against contract rules.
    Raises ContractValidationError on violation.
    
    Rules:
    - status must be a valid enum value.
    - reds must be non-negative integer.
    - If status == PASSED, reds must be 0.
    - If status == FAILED or ERROR, reds must be >= 1.
    - total_count must be non-negative integer.
    - required string fields must be non-empty.
    """
    if not isinstance(result.status, SuiteStatus):
        raise ContractValidationError(f"Invalid status: {result.status}")
    
    if not isinstance(result.reds, int) or result.reds < 0:
        raise ContractValidationError(f"reds must be non-negative integer, got {result.reds}")
    
    if result.status == SuiteStatus.PASSED and result.reds != 0:
        raise ContractValidationError(f"Passed suite must have reds=0, got {result.reds}")
    
    if result.status in (SuiteStatus.FAILED, SuiteStatus.ERROR) and result.reds < 1:
        raise ContractValidationError(f"Failed/error suite must have reds >= 1, got {result.reds}")
    
    if not isinstance(result.total_count, int) or result.total_count < 0:
        raise ContractValidationError(f"total_count must be non-negative integer, got {result.total_count}")
    
    required_strings = ["schema_version", "suite_name", "summary_line", "run_id", "logs"]
    for field_name in required_strings:
        field_val = getattr(result, field_name, "")
        if not isinstance(field_val, str) or not field_val.strip():
            raise ContractValidationError(f"Required field '{field_name}' is missing or empty")


def create_error_result(suite_name: str, reason: str, logs: str = "") -> NormalizedSuiteResult:
    """
    Create a deterministic error result when a provider fails or violates contract.
    
    Args:
        suite_name: The suite that failed to execute properly.
        reason: Human-readable error reason.
        logs: Optional diagnostic logs.
    
    Returns:
        A NormalizedSuiteResult with error status and reds=1.
    """
    return NormalizedSuiteResult(
        schema_version="nagato.suite_result.v1",
        suite_name=suite_name,
        status=SuiteStatus.ERROR,
        reds=1,
        total_count=0,
        summary_line=f"Provider error: {reason}",
        run_id=f"error_{datetime.now().isoformat()}",
        logs=logs or f"Suite execution failed: {reason}",
        anchor="unknown",
    )


def project_to_legacy_gold_status(result: NormalizedSuiteResult) -> Dict[str, Any]:
    """
    Project a normalized result to the legacy gold_full_latest.status.json shape.
    
    This ensures backward compatibility with existing FSM logic that reads
    debug_outputs/gold_full_latest.status.json and expects specific fields.
    
    The projection maps:
    - reds -> reds (identity)
    - anchor -> anchor
    - status -> state (PASSED -> completed, FAILED/ERROR -> failed, RUNNING -> running, etc.)
    - run_id -> run_id
    - summary_line -> summary_line
    - optional counts -> passed, failed_files, xfailed, xpassed, skipped, exit_code
    """
    # Map suite status to legacy FSM state
    state_map = {
        SuiteStatus.PASSED: "completed",
        SuiteStatus.FAILED: "failed",
        SuiteStatus.ERROR: "error",
        SuiteStatus.RUNNING: "running",
        SuiteStatus.MISSING: "missing",
    }
    legacy_state = state_map.get(result.status, "unknown")
    
    payload = {
        "reds": result.reds,
        "anchor": result.anchor,
        "state": legacy_state,
        "run_id": result.run_id,
        "summary_line": result.summary_line,
    }
    
    # Optional fields for richer FSM messaging
    if result.passed_count > 0 or result.status == SuiteStatus.PASSED:
        payload["passed"] = result.passed_count
    if result.failed_targets:
        payload["failed_files"] = result.failed_targets
    if result.xfailed_count > 0:
        payload["xfailed"] = result.xfailed_count
    if result.xpassed_count > 0:
        payload["xpassed"] = result.xpassed_count
    if result.skipped_count > 0:
        payload["skipped"] = result.skipped_count
    
    # Safely extract exit_code from metadata if present
    if result.metadata:
        exit_code = None
        if isinstance(result.metadata, SuiteMetadata):
            exit_code = result.metadata.exit_code
        elif isinstance(result.metadata, dict):
            exit_code = result.metadata.get("exit_code")
        
        if exit_code is not None:
            payload["exit_code"] = exit_code
    
    return payload


# ========== Acceptance Policy Contracts (V1) ==========

class PolicyCheckType(str, Enum):
    """Canonical check types for acceptance policies."""
    REDS_EQ_0 = "reds_eq_0"
    REDS_LTE_BASELINE = "reds_lte_baseline"
    SUITE_STATUS_PASSED = "suite_status_passed"


@dataclass
class PolicyRule:
    """
    A single rule to evaluate within an acceptance policy.
    
    Fields:
    - suite: the semantic role (e.g. 'gold', 'targeted')
    - check: one of PolicyCheckType values
    """
    suite: str
    check: PolicyCheckType
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolicyRule":
        """Deserialize from dict."""
        check_val = data.get("check")
        if isinstance(check_val, str):
            try:
                check = PolicyCheckType(check_val)
            except ValueError:
                raise ValueError(f"Unknown policy check type: {check_val}")
        else:
            check = check_val
        return cls(suite=str(data.get("suite", "")), check=check)


@dataclass
class AcceptancePolicy:
    """
    A named policy that defines acceptance criteria for report_fix() decisions.
    
    Fields:
    - required_suites: list of semantic roles that must be available
    - decision_mode: how to combine rules ('all', 'any')
    - on_missing_suite: what to do if a required suite is missing ('fail', 'skip')
    - rules: list of PolicyRule objects
    """
    required_suites: List[str]
    decision_mode: str
    on_missing_suite: str
    rules: List[PolicyRule]
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AcceptancePolicy":
        """Deserialize from dict."""
        rules_data = data.get("rules", [])
        rules = [PolicyRule.from_dict(r) for r in rules_data]
        missing_behavior = str(data.get("on_missing_suite", "fail")).lower()
        missing_behavior = {"reject": "fail", "pass": "skip"}.get(missing_behavior, missing_behavior)
        if missing_behavior not in {"fail", "skip"}:
            raise ValueError(
                "on_missing_suite must be one of 'fail' or 'skip' "
                "(legacy aliases: 'reject' or 'pass')"
            )
        return cls(
            required_suites=list(data.get("required_suites", [])),
            decision_mode=str(data.get("decision_mode", "all")),
            on_missing_suite=missing_behavior,
            rules=rules,
        )


@dataclass
class RuleEvaluation:
    """
    Result of evaluating a single rule.
    
    Fields:
    - rule: the PolicyRule that was evaluated
    - passed: whether the rule evaluated to True
    - reason: human-readable explanation
    - suite_reds: actual reds from the suite result
    - baseline_reds: baseline for comparison (if applicable)
    """
    rule: PolicyRule
    passed: bool
    reason: str
    suite_reds: Optional[int] = None
    baseline_reds: Optional[int] = None


@dataclass
class DecisionResult:
    """
    Final decision from applying an acceptance policy.
    
    Fields:
    - accepted: whether the fix is accepted (True) or rejected (False)
    - policy_name: name of the policy that made the decision
    - target_class: classification of the target (e.g. 'on_grid', 'off_grid')
    - reasons: list of human-readable explanation strings
    - evaluations: list of RuleEvaluation results
    - suite_results_used: dict mapping suite role to (reds, status)
    - baseline_reds: baseline red count used for comparisons
    """
    accepted: bool
    policy_name: str
    target_class: str
    reasons: List[str]
    evaluations: List[RuleEvaluation]
    suite_results_used: Dict[str, tuple]
    baseline_reds: Optional[int] = None


def evaluate_rule_check(
    rule: PolicyRule,
    suite_result: NormalizedSuiteResult,
    baseline_reds: Optional[int] = None,
) -> RuleEvaluation:
    """
    Evaluate a single rule against a suite result.
    
    Args:
        rule: The PolicyRule to evaluate.
        suite_result: The NormalizedSuiteResult from a provider.
        baseline_reds: Baseline red count for comparison (if applicable).
    
    Returns:
        A RuleEvaluation with the result.
    """
    check = rule.check
    
    if check == PolicyCheckType.REDS_EQ_0:
        passed = suite_result.reds == 0
        reason = f"{rule.suite}: reds = {suite_result.reds} (required: 0)"
    elif check == PolicyCheckType.REDS_LTE_BASELINE:
        if baseline_reds is None:
            passed = suite_result.reds == 0
            reason = f"{rule.suite}: reds = {suite_result.reds}, baseline = None (no baseline, requiring reds == 0)"
        else:
            passed = suite_result.reds <= baseline_reds
            reason = f"{rule.suite}: reds = {suite_result.reds} <= baseline {baseline_reds}? {passed}"
    elif check == PolicyCheckType.SUITE_STATUS_PASSED:
        passed = suite_result.status == SuiteStatus.PASSED
        reason = f"{rule.suite}: status = {suite_result.status.value} (required: passed)"
    else:
        passed = False
        reason = f"{rule.suite}: unknown check type {check}"
    
    return RuleEvaluation(
        rule=rule,
        passed=passed,
        reason=reason,
        suite_reds=suite_result.reds,
        baseline_reds=baseline_reds,
    )


def evaluate_acceptance_policy(
    policy: AcceptancePolicy,
    suite_results: Dict[str, NormalizedSuiteResult],
    baseline_reds: Optional[int] = None,
    target_class: str = "default_on_grid",
    policy_name: Optional[str] = None,
) -> DecisionResult:
    """
    Apply an acceptance policy against a set of suite results.
    
    Args:
        policy: The AcceptancePolicy to apply.
        suite_results: dict mapping semantic role to NormalizedSuiteResult.
        baseline_reds: Baseline red count for non-zero comparisons.
        target_class: Classification of the target being tested (for audit trail).
        policy_name: Name of the policy for audit trail (defaults to policy class name).
    
    Returns:
        A DecisionResult with the final accept/reject decision and evaluation details.
    """
    if policy_name is None:
        policy_name = policy.__class__.__name__
    
    reasons = []
    evaluations = []
    
    # Check that all required suites are present
    missing = [r for r in policy.required_suites if r not in suite_results]
    if missing:
        if policy.on_missing_suite == "fail":
            return DecisionResult(
                accepted=False,
                policy_name=policy_name,
                target_class=target_class,
                reasons=[f"Missing required suites: {missing}"],
                evaluations=[],
                suite_results_used={r: (suite_results[r].reds, suite_results[r].status.value) for r in suite_results},
                baseline_reds=baseline_reds,
            )
        else:
            reasons.append(f"Warning: missing suites {missing} but on_missing_suite='skip'")
    
    # Evaluate all rules
    for rule in policy.rules:
        if rule.suite in suite_results:
            suite_result = suite_results[rule.suite]
            eval_result = evaluate_rule_check(rule, suite_result, baseline_reds)
            evaluations.append(eval_result)
            reasons.append(eval_result.reason)
        else:
            # Rule references a missing suite
            if policy.on_missing_suite == "fail":
                return DecisionResult(
                    accepted=False,
                    policy_name=policy_name,
                    target_class=target_class,
                    reasons=reasons + [f"Rule references missing suite: {rule.suite}"],
                    evaluations=evaluations,
                    suite_results_used={r: (suite_results[r].reds, suite_results[r].status.value) for r in suite_results},
                    baseline_reds=baseline_reds,
                )
    
    # Combine rule results according to decision_mode
    if policy.decision_mode == "all":
        all_passed = all(e.passed for e in evaluations)
        # Only reject due to missing suites if on_missing_suite is 'fail'
        accepted = all_passed and (not missing or policy.on_missing_suite != "fail")
    elif policy.decision_mode == "any":
        any_passed = any(e.passed for e in evaluations)
        # Only reject due to missing suites if on_missing_suite is 'fail'
        accepted = any_passed and (not missing or policy.on_missing_suite != "fail")
    else:
        accepted = False
        reasons.append(f"Unknown decision_mode: {policy.decision_mode}")
    
    return DecisionResult(
        accepted=accepted,
        policy_name=policy_name,
        target_class=target_class,
        reasons=reasons,
        evaluations=evaluations,
        suite_results_used={r: (suite_results[r].reds, suite_results[r].status.value) for r in suite_results},
        baseline_reds=baseline_reds,
    )
