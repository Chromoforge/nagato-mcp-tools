"""Smoke tests ensuring all nagato packages can be imported without error."""
import pytest


def test_core_package_imports():
    """Verify core tool package and MCP server import cleanly without optional dependencies."""
    import nagato_tools
    from nagato_tools.facade import ToolFacade, create_standalone_facade
    from nagato_mcp_tools.server import NagatoMCPServer

    facade = create_standalone_facade(".")
    assert facade is not None
    assert len(facade.get_registered_functions()) >= 30


def test_ui_package_imports():
    """Verify UI server imports cleanly when optional [ui] extra is installed."""
    pytest.importorskip("fastapi")
    from nagato_mcp_tools.ui_server import create_app
    app = create_app()
    assert app is not None
