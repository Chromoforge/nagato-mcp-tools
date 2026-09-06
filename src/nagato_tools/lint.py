import asyncio
import json
from pathlib import Path

from nagato_tools.config import get_lint_config, get_workspace_root
from nagato_tools.errors import _nagato_error as nagato_error


def validate_content_syntax(content: str, file_path: str = "") -> str | None:
    """
    Validates syntax in-memory for Python, JSON, and YAML content.

    Args:
        content: The text content to validate.
        file_path: Optional file path or filename to determine file type.

    Returns:
        Error message string if syntax validation fails, or None if valid / unsupported file type.
    """
    suffix = Path(file_path).suffix.lower() if file_path else ".py"

    if suffix == ".py":
        try:
            compile(content, file_path or "<string>", "exec")
        except SyntaxError as e:
            line_text = e.text.strip() if e.text else 'N/A'
            return f"Syntax error: {e.msg}\nAt: {file_path or '<string>'}:{e.lineno}:{e.offset}\nCode: {line_text}"
        except Exception as e:
            return f"Syntax check failed: {str(e)}"
    elif suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return f"JSON syntax error: {e.msg} at line {e.lineno}, column {e.colno}"
        except Exception as e:
            return f"JSON validation failed: {str(e)}"
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml
            yaml.safe_load(content)
        except ImportError:
            pass
        except Exception as e:
            return f"YAML syntax error: {str(e)}"

    return None


async def nagato_lint(file: str, _ctx=None) -> str:
    """Runs a token-efficient syntax check and a configurable Ruff linter."""
    workspace_root = get_workspace_root()
    target_file = workspace_root / file

    # Guard: prevent path traversal outside workspace
    resolved = target_file.resolve()
    workspace_root_resolved = workspace_root.resolve()
    if not resolved.is_relative_to(workspace_root_resolved):
        return nagato_error(f"Path {file} resolves outside workspace root.", tool="nagato_lint")

    # Load lint configuration
    lint_config = get_lint_config()

    # Check if linting is enabled
    if not lint_config.enabled:
        return f"SUCCESS: {file} linting disabled by configuration."

    if target_file.suffix != ".py":
        return f"SUCCESS: {file} is not a Python file, skipping syntax check."

    # 1. Hard Syntax Check (Token-Efficient & RAM-only)
    try:
        content = target_file.read_text(encoding="utf-8", errors="replace")
        syntax_err = validate_content_syntax(content, file)
        if syntax_err:
            return nagato_error(syntax_err, tool="nagato_lint")
    except Exception as e:
        return nagato_error(f"Syntax check failed: {str(e)}", tool="nagato_lint")

    # 2. Optional Linter (Ruff - Fully Asynchronous)
    try:
        # Build ruff command with config options
        cmd = [lint_config.ruff_path, "check"]

        # Add auto-fix if enabled
        if lint_config.auto_fix:
            cmd.append("--fix")

        # Add select rules if specified
        if lint_config.select:
            cmd.extend(["--select", ",".join(lint_config.select)])

        # Add ignore rules if specified
        if lint_config.ignore:
            cmd.extend(["--ignore", ",".join(lint_config.ignore)])

        cmd.append(str(target_file))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            return f"SUCCESS: {file} passed all syntax and linting checks."
        else:
            # Decode Ruff output for the LLM
            warnings = stdout.decode("utf-8", errors="replace").strip()

            if lint_config.soft_mode:
                # Soft mode: return warnings but don't fail
                return f"LINTING WARNINGS found in {file} (soft mode - not blocking):\n{warnings}"
            else:
                # Hard mode: treat warnings as errors
                return f"LINTING ERRORS found in {file}:\n{warnings}"

    except FileNotFoundError:
        return f"SUCCESS: {file} passed syntax check (ruff not found)."
    except Exception as e:
        return f"SUCCESS: {file} passed syntax check (linter skipped: {str(e)})."


# Non-prefixed aliases for MCP server compatibility
lint = nagato_lint
__all__ = ['nagato_lint']
