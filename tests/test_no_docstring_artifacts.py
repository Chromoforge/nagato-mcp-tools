"""Verify no docstring escape artifacts (\\1) exist in source files."""
import pathlib
import pytest


def test_no_backslash1_in_docstrings():
    """Ensure no \\1 escape artifacts leak into synced python modules."""
    src_dir = pathlib.Path(__file__).resolve().parent.parent / "src"
    assert src_dir.exists(), f"Source directory {src_dir} does not exist"

    for p in src_dir.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "\\1" not in text, f"Found \\1 artifact in {p}"
        assert "\\2" not in text, f"Found \\2 artifact in {p}"
