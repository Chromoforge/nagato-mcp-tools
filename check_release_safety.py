#!/usr/bin/env python3
"""Standalone pre-release safety checker for nagato-mcp-tools.

Run this script before building or uploading to verify that no BSL-licensed
Insight tools are present in the package.
"""

import sys
from pathlib import Path


def main() -> int:
    pkg_root = Path(__file__).resolve().parent
    tools_dir = pkg_root / "src" / "nagato_tools"
    if not tools_dir.exists():
        tools_dir = pkg_root / "nagato_tools"

    if not tools_dir.exists():
        print(f"ERROR: Cannot find nagato_tools directory in {pkg_root}")
        return 1

    insight_files = list(tools_dir.glob("insight_*.py"))
    if insight_files:
        print(
            "\n"
            "========================================================================\n"
            "🚨 RELEASE CHECK FAILED: BSL INSIGHT FILES DETECTED!\n"
            "========================================================================\n"
            f"Found {len(insight_files)} Insight tool file(s) in {tools_dir}:\n"
            + "\n".join(f"  - {f.name}" for f in insight_files)
            + "\n\n"
            "Insight tools are licensed under BSL 1.1 and must NEVER be published\n"
            "in the public MIT 'nagato-mcp-tools' package!\n"
            "========================================================================\n"
        )
        return 1

    print("✓ Clean public release package: No Insight files detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
