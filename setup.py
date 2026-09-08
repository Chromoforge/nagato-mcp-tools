"""Build configuration and safety gate for nagato-mcp-tools.

This setup.py acts as a release safety guard to prevent accidental publication
of BSL-licensed Nagato Insight tools in the public MIT package.
"""

import sys
from pathlib import Path

# RELEASE GUARD: Ensure no BSL Insight modules are packaged into MIT release
_pkg_root = Path(__file__).resolve().parent
_tools_dir = _pkg_root / "src" / "nagato_tools"
if not _tools_dir.exists():
    _tools_dir = _pkg_root / "nagato_tools"

if _tools_dir.exists():
    _insight_files = list(_tools_dir.glob("insight_*.py"))
    if _insight_files:
        sys.exit(
            "\n"
            "========================================================================\n"
            "🚨 RELEASE BUILD BLOCKED: BSL INSIGHT FILES DETECTED!\n"
            "========================================================================\n"
            f"Found {len(_insight_files)} Insight tool file(s) in {_tools_dir}:\n"
            + "\n".join(f"  - {f.name}" for f in _insight_files)
            + "\n\n"
            "Insight tools are licensed under BSL 1.1 and must NEVER be packaged into\n"
            "the public MIT 'nagato-mcp-tools' release.\n"
            "Remove them or re-sync with: python sync_tools.py --exclude-insight\n"
            "========================================================================\n"
        )

from setuptools import setup

setup()
