"""Console-script entry point for ``nagato-mcp-tools``.

Exposed via ``[project.scripts]`` in ``pyproject.toml``. This indirection
exists so that ``nagato-mcp-tools`` (the console script installed by
``pip install``) loads the server module as ``__main__`` instead of as a
package submodule. That avoids CPython's ``runpy`` ``RuntimeWarning``
("found in sys.modules after import of package ...") that would otherwise
appear whenever the server is launched via ``python -m nagato_mcp_tools``
from a process that has already imported it (e.g. the MCP Inspector or a
test runner).
"""

from nagato_mcp_tools.server import main

if __name__ == "__main__":
    main()
