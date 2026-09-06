"""Tests for nagato_shell denylist behavior.

Locks down the curated DANGEROUS_COMMANDS / DANGEROUS_PATTERNS / EXCLUDED_TEST_*
sets so that adding a new blocked command is intentional and removing one is
visibly caught by CI.
"""
from nagato_tools.shell import _check_command_blocked


def _check(args):
    """Helper: returns (is_blocked, reason) for a single invocation."""
    is_blocked, reason = _check_command_blocked(args)
    return is_blocked, reason


def test_dangerous_system_commands_are_blocked():
    """A representative sample of OS-level destructive commands must be denied."""
    # All names below are members of nagato_tools.shell.DANGEROUS_COMMANDS.
    samples = [
        ["rm", "-rf", "/"],
        ["format", "C:"],
        ["shutdown", "/s"],
        ["del", "/f", "/q", "C:\\Windows"],
        ["dd", "if=/dev/zero", "of=/dev/sda"],
        ["mkfs", "/dev/sda1"],
        ["sudo", "apt-get", "install"],
        ["kill", "-9", "1234"],
    ]
    for args in samples:
        is_blocked, reason = _check(args)
        assert is_blocked, f"Expected {args} to be blocked, got {reason!r}"
        assert reason, f"Blocked {args} should have a non-empty reason"


def test_test_runners_are_blocked_from_shell():
    """nagato_shell is not the place to run pytest."""
    samples = [
        ["pytest", "tests/"],
        ["python", "-m", "pytest"],
        ["py.test"],
    ]
    for args in samples:
        is_blocked, reason = _check(args)
        assert is_blocked, f"Expected {args} to be blocked, got {reason!r}"
        assert "nagato_run_test" in reason, f"Blocked reason should redirect to nagato_run_test, got: {reason!r}"


def test_safe_commands_are_allowed():
    """Read-only / harmless commands must NOT be blocked."""
    samples = [
        ["ls"],
        ["ls", "-la"],
        ["echo", "hello"],
        ["python", "-c", "print(2 + 2)"],
        ["git", "status"],
        ["git", "log", "--oneline", "-5"],
        ["cat", "README.md"],
    ]
    for args in samples:
        is_blocked, reason = _check(args)
        assert not is_blocked, f"Expected {args} to be ALLOWED, got blocked: {reason!r}"


def test_dangerous_pattern_matches_in_full_args():
    """Patterns embedded in args must still trigger the denylist."""
    # "rm -rf" anywhere in the command line must trip the dangerous pattern
    is_blocked, _reason = _check(["ls", "&&", "rm", "-rf", "/tmp"])
    assert is_blocked


def test_powershell_is_not_blocked_by_design():
    """`powershell`, `pwsh`, and `cmd` are intentionally allowed on Windows
    (see comment in DANGEROUS_COMMANDS). This test pins that policy so any
    future change to add them is a deliberate, visible decision.
    """
    for args in (
        ["powershell", "-Command", "Get-Process"],
        ["cmd", "/c", "dir"],
    ):
        is_blocked, _reason = _check(args)
        assert not is_blocked, f"{args} should be allowed by policy, got blocked: {_reason!r}"