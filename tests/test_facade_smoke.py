"""Smoke tests for Nagato MCP Tools ToolFacade - broad tool surface coverage.

These tests verify that all major tool categories can be called without errors.
They are not exhaustive functional tests but ensure the facade wiring works.
"""
from pathlib import Path

import pytest

from nagato_tools.facade import ToolFacade


@pytest.fixture
def facade():
    """Create a ToolFacade for testing."""
    return ToolFacade(workspace_root=Path.cwd())


@pytest.mark.asyncio
async def test_list_dir(facade):
    """Test list directory function."""
    result = await facade.acall("nagato_list_dir", dir=".")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_read_file(facade):
    """Test read file function."""
    result = await facade.acall("nagato_read_file", file="README.md")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_search_in_file(facade):
    """Test search in file function."""
    result = await facade.acall("nagato_searchInFile", query="facade", file="src/nagato_tools/facade.py")
    assert isinstance(result, str)
    # Either finds matches or returns "no matches" message
    assert "facade" in result.lower() or "keine treffer" in result.lower() or "treffer" in result.lower()


@pytest.mark.asyncio
async def test_search_ast(facade):
    """Test AST search function."""
    result = await facade.acall("nagato_searchAST", query="ToolFacade", file="src/nagato_tools/facade.py")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_read_signatures(facade):
    """Test read signatures function."""
    result = await facade.acall("nagato_read_signatures", target_symbol="ToolFacade")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_lint(facade):
    """Test lint function."""
    result = await facade.acall("nagato_lint", file="src/nagato_tools/facade.py")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_find_file(facade):
    """Test find file function."""
    result = await facade.acall("nagato_find_file", filename="facade.py")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_rebuild_symbol_db(facade):
    """Test rebuild symbol db function."""
    result = await facade.acall("nagato_rebuild_symbol_db", {})
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_git_status(facade):
    """Test git status function."""
    result = await facade.acall("nagato_git", command="status")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_web_search(facade):
    """Test web search function."""
    result = await facade.acall("nagato_web_search", query="python async")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_is_agent_running(facade):
    """Test is agent running function."""
    result = await facade.acall("nagato_is_agent_running")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_generate_uuid4(facade):
    """Test generate uuid4 function."""
    result = await facade.acall("nagato_generate_uuid4", count=1)
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_execute_snippet(facade):
    """Test execute snippet function."""
    result = await facade.acall("nagato_execute_snippet", code='print("hello")')
    assert isinstance(result, str)
    assert "hello" in result


@pytest.mark.asyncio
async def test_auto_create_with_edit(facade):
    """Test auto-creation of a file during nagato_edit."""
    test_file = "test_smoke_temp.txt"
    # Clean slate
    await facade.acall("nagato_delete", file=test_file)

    result = await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="hello")
    assert isinstance(result, str)
    assert "SUCCESS" in result or "created" in result.lower()
    
    # Verify file exists
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "hello" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=test_file)


@pytest.mark.asyncio
async def test_edit(facade):
    """Test edit function."""
    test_file = "test_smoke_edit.txt"
    # Clean slate
    await facade.acall("nagato_delete", file=test_file)

    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="hello")
    
    result = await facade.acall("nagato_edit", file=test_file, searchstring="hello", replacement="world")
    assert isinstance(result, str)
    assert "SUCCESS" in result or "replaced" in result.lower()
    
    # Verify edit worked
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "world" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=test_file)


@pytest.mark.asyncio
async def test_read_lines(facade):
    """Test read lines function."""
    test_file = "test_smoke_lines.txt"
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="line1\nline2\nline3")
    
    result = await facade.acall("nagato_read_lines", file=test_file, start_line=1, end_line=1)
    assert isinstance(result, str)
    assert "line1" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=test_file)


@pytest.mark.asyncio
async def test_upload(facade):
    """Test upload function."""
    result = await facade.acall("nagato_upload", commit_message="test")
    assert isinstance(result, str)
    assert len(result) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])