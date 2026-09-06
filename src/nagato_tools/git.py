import subprocess
from pathlib import Path
from typing import Any, Optional

from nagato_tools.config import get_workspace_root
try:
    from fsm.events import Event  # host-only
except ImportError:
    Event = None  # type: ignore[misc]  # fsm-only; standalone gets None
from nagato_tools.errors import _nagato_error as nagato_error
try:
    from fsm.transition_apply import apply_event  # host-only
except ImportError:
    apply_event = None  # type: ignore[misc]  # fsm-only; standalone gets None
def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to config."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return get_workspace_root()


async def nagato_git(command: str, args: str = "", _ctx: Optional[Any] = None) -> str:
    """
    Secure git tool for the revert workflow.
    Allowed commands: log, status, diff, revert, checkout_file, reset_file
    Args:
        command: One of: log | status | diff | revert | checkout_file | reset_file
        args:
          log           → Number of commits (default: 15), e.g., "20"
          status        → (empty)
          diff          → e.g., "HEAD~3" or "HEAD~1..HEAD"
          revert        → Commit hash, e.g., "a1b2c3d" (--no-edit, no auto-commit)
          checkout_file → "<commit> -- <path>", e.g., "HEAD~2 -- StateEngine/DiTE/TitanStateEngine.py"
          reset_file    → "<path>" (git checkout HEAD -- <path>, discards local changes)
    """
    def _run(git_args: list[str]) -> str:
        workspace_root = _get_workspace_root(_ctx)
        result = subprocess.run(
            ["git"] + git_args,
            cwd=str(workspace_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout.strip() or "(no output)"
        status = "OK" if result.returncode == 0 else f"ERROR (exit {result.returncode})"
        return f"{status}:\n{output}"

    try:
        if command == "log":
            n = int(args.strip()) if args.strip().isdigit() else 15
            return _run(["log", "--oneline", f"-{n}"])

        if command == "status":
            return _run(["status", "--short"])

        if command == "diff":
            ref = args.strip() or "HEAD~1"
            return _run(["diff", "--name-only", ref])

        if command == "revert":
            commit = args.strip()
            if not commit:
                return nagato_error("args must contain the commit hash.", tool="nagato_git")
            # Defense in depth: validate the commit reference shape before
            # passing it to git. List-form + subprocess prevents shell
            # injection, but a malformed ref still produces confusing errors.
            if not re.match(
                r"^(?:[0-9a-fA-F]{4,40}|\^[0-9a-fA-F]{4,40}"
                r"|HEAD(?:~[0-9]+|\^[0-9]+)?"
                r"|[a-zA-Z0-9_./-]+(?:~[0-9]+)?"
                r"|[0-9a-fA-F]{4,40}\^[0-9]*)$",
                commit,
            ):
                return nagato_error(
                    f"Invalid commit reference: {commit!r}. "
                    "Expected a hash (e.g. 'a1b2c3d'), HEAD, HEAD~N, or <ref>~N.",
                    tool="nagato_git",
                )
            # --no-commit, no auto-commit: agent sees the result and commits itself via nagato_upload
            return _run(["revert", "--no-commit", commit])

        if command == "checkout_file":
            # Format: "<commit> -- <path>"
            parts = args.strip().split(" -- ", 1)
            if len(parts) != 2:
                return nagato_error(
                    "Format: '<commit> -- <path>', e.g. 'HEAD~2 -- StateEngine/DiTE/TitanStateEngine.py'",
                    tool="nagato_git",
                )
            ref, path = parts[0].strip(), parts[1].strip()
            workspace_root = _get_workspace_root(_ctx)
            target_path = workspace_root / path
            resolved = target_path.resolve()
            if not resolved.is_relative_to(workspace_root.resolve()):
                return nagato_error(f"Path {path} resolves outside workspace root.", tool="nagato_git")
            return _run(["checkout", ref, "--", path])

        if command == "reset_file":
            path = args.strip()
            if not path:
                return nagato_error("args must contain the file path.", tool="nagato_git")
            workspace_root = _get_workspace_root(_ctx)
            target_path = workspace_root / path
            resolved = target_path.resolve()
            if not resolved.is_relative_to(workspace_root.resolve()):
                return nagato_error(f"Path {path} resolves outside workspace root.", tool="nagato_git")
            return _run(["checkout", "HEAD", "--", path])

        return nagato_error(
            f"Unknown command '{command}'. Allowed: log, status, diff, revert, checkout_file, reset_file",
            tool="nagato_git",
        )

    except Exception as e:
        return nagato_error(str(e), tool="nagato_git")

async def nagato_upload(commit_message: str, _ctx: Optional[Any] = None) -> str:
    """
    Executes git add -A + git commit — when allowed in standalone mode.
    Args:
        commit_message: The commit message.
        _ctx: Optional session context (injected by facade).
    """
    has_fsm = False
    fsm = None
    Event = None
    apply_event = None
    try:
        try:
            from fsm.nagato_fsm import NagatoFSM  # host-only
        except ImportError:
            NagatoFSM = None  # type: ignore[misc]  # fsm-only; standalone gets None
        try:
            from fsm.events import Event  # host-only
        except ImportError:
            Event = None  # type: ignore[misc]  # fsm-only; standalone gets None
        try:
            from fsm.transition_apply import apply_event  # host-only
        except ImportError:
            apply_event = None  # type: ignore[misc]  # fsm-only; standalone gets None
        if NagatoFSM is not None:
            fsm = NagatoFSM()
            has_fsm = True
    except (ImportError, Exception):
        NagatoFSM = None
        Event = None
        apply_event = None
        has_fsm = False

    try:
        if has_fsm and fsm is not None and Event is not None and apply_event is not None:
            reds_after, anchor_after, gold_state, run_id, summary_line = fsm.get_gold_status()

            if gold_state == "running":
                return nagato_error("Upload blocked: Gold Full is still running. Wait for completion.", tool="nagato_upload")
            if gold_state == "missing" or reds_after == -1:
                return nagato_error(
                    "Upload blocked: No Gold Full result available. Run nagato_run_gold_full first.",
                    tool="nagato_upload",
                )

            reds_before = fsm.Context.red_count

            # ── CATASTROPHIC REGRESSION GUARD (all States) ──────────────────────
            if reds_after > reds_before:
                fsm.Context.push_action("")
                apply_event(fsm, Event(name="regression_guard_catastrophic", source="git"))
                return nagato_error(
                    f"Upload blocked — catastrophic regression: "
                    f"Reds increased from {reds_before} → {reds_after}. "
                    f"Session reset to BUGFIX. Undo the fix immediately or debug the cause.",
                    tool="nagato_upload",
                )

            # ── STATE-SPECIFIC GUARD ──────────────────────────────────────────
            strict_states = {"REGRESSION_FIX", "BUGFIX"}
            xfail_fix_states = {"XFAIL_FIX"}
            fixture_fix_states = {"FIXTURE_FIX"}
            relaxed_states = {"NEW_SUBSYSTEM"}
            current_state = fsm.Context.GetState()

            if current_state in strict_states and reds_after >= reds_before:
                return nagato_error(
                    f"Upload blocked: State={current_state} requires reds_after < reds_before. "
                    f"Current: {reds_before} → {reds_after}. No progress yet.",
                    tool="nagato_upload",
                )
            if current_state in xfail_fix_states and reds_after > reds_before:
                user_approved = getattr(fsm.Context, 'xfail_fix_user_approved', False)
                if not user_approved:
                    return nagato_error(
                        f"Upload blocked: State=XFAIL_FIX detected new failures (reds {reds_before} → {reds_after}). "
                        f"Use ask_user to decide how to proceed before uploading.",
                        tool="nagato_upload",
                    )
            if current_state in fixture_fix_states and reds_after > reds_before:
                user_approved = getattr(fsm.Context, 'fixture_fix_user_approved', False)
                if not user_approved:
                    return nagato_error(
                        f"Upload blocked: State=FIXTURE_FIX detected new failures (reds {reds_before} → {reds_after}). "
                        f"Use ask_user to decide how to proceed before uploading.",
                        tool="nagato_upload",
                    )
            if current_state in relaxed_states and reds_after > reds_before:
                return nagato_error(
                    f"Upload blocked: State=NEW_SUBSYSTEM, but reds increased ({reds_before} → {reds_after}).",
                    tool="nagato_upload",
                )
            if current_state not in (strict_states | fixture_fix_states | relaxed_states | {"DONE"}):
                return nagato_error(
                    f"Upload blocked: Unexpected Session State '{current_state}'. No upload without clear state.",
                    tool="nagato_upload",
                )

        # ── GIT COMMIT ───────────────────────────────────────────────────────
        # Pre-flight: confirm git identity is configured so we don't fail with
        # the cryptic raw "Please tell me who you are" stderr mid-commit.
        ws = str(_get_workspace_root(_ctx))
        for cfg_key in ("user.email", "user.name"):
            check = subprocess.run(
                ["git", "config", "--get", cfg_key],
                cwd=ws,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if not check.stdout.strip():
                return nagato_error(
                    f"Git {cfg_key} not configured. Run: git config {cfg_key} <value>",
                    tool="nagato_upload",
                )

        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=ws,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if add_result.returncode != 0:
            return nagato_error(f"Upload failed (git add): {add_result.stdout}", tool="nagato_upload")

        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=ws,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if commit_result.returncode != 0:
            return nagato_error(f"Upload failed (git commit): {commit_result.stdout}", tool="nagato_upload")

        if has_fsm:
            # State aktualisieren
            fsm.Context.red_count = reds_after
            fsm.Context.anchor = anchor_after
            fsm.Context.push_action("")
            event_name = "upload_success_reds_positive" if reds_after > 0 else "upload_success_reds_zero"
            apply_event(fsm, Event(name=event_name, source="git"))

            return (
                f"UPLOAD OK: {commit_result.stdout.strip()}\n"
                f"Reds: {reds_before} → {reds_after}. Session state: {fsm.Context.GetState()}."
            )
        else:
            return f"UPLOAD OK: {commit_result.stdout.strip()} (Standalone Mode)"

    except Exception as e:
        return nagato_error(str(e), tool="nagato_upload")


# Non-prefixed aliases for MCP server compatibility
git = nagato_git
upload = nagato_upload
__all__ = ['nagato_git', 'nagato_upload']
