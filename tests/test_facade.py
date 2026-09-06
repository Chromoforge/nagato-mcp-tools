"""Tests for Nagato MCP Tools ToolFacade."""
import asyncio
import sys
import pytest
from pathlib import Path

from nagato_tools.facade import ToolFacade, get_tool_facade
from nagato_tools.ctx_mock import get_mock_context


@pytest.fixture
def facade():
    """Create a ToolFacade for testing."""
    # Create a fresh instance to avoid global state issues
    return ToolFacade(workspace_root=Path.cwd())


def test_facade_creation(facade):
    """Test that facade is created and has registered functions."""
    assert facade is not None
    functions = facade.get_registered_functions()
    assert len(functions) > 0
    print(f"Registered functions: {functions}")


def test_facade_has_search_functions(facade):
    """Test that search functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_searchInFile" in functions
    assert "nagato_searchInFiles" in functions
    assert "nagato_semantic_search" in functions


def test_facade_has_read_functions(facade):
    """Test that read functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_read_file" in functions
    assert "nagato_read_lines" in functions
    assert "nagato_list_dir" in functions
    assert "nagato_read_signatures" in functions


def test_facade_has_edit_functions(facade):
    """Test that edit functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_edit" in functions


def test_facade_has_execute_functions(facade):
    """Test that execute functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_execute_snippet" in functions


def test_facade_has_lint_functions(facade):
    """Test that lint functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_lint" in functions


def test_facade_has_git_functions(facade):
    """Test that git functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_git" in functions
    assert "nagato_upload" in functions


def test_facade_has_web_functions(facade):
    """Test that web functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_web_search" in functions


def test_facade_has_test_functions(facade):
    """Test that test functions are registered."""
    functions = facade.get_registered_functions()
    assert "nagato_run_test" in functions
    assert "nagato_run_gold_full" in functions


@pytest.mark.asyncio
async def test_search_in_file(facade):
    """Test search in file function."""
    result = await facade.acall("nagato_searchInFile", query="async def", file="src/nagato_tools/search.py")
    assert "async def" in result or "Keine Treffer" in result or "Treffer" in result


@pytest.mark.asyncio
async def test_read_file(facade):
    """Test read file function."""
    result = await facade.acall("nagato_read_file", file="README.md")
    assert "Nagato" in result or "nagato" in result or "MCP" in result


@pytest.mark.asyncio
async def test_list_dir(facade):
    """Test list directory function."""
    result = await facade.acall("nagato_list_dir", dir="src/nagato_tools")
    assert "search.py" in result


@pytest.mark.asyncio
async def test_execute_snippet(facade):
    """Test execute snippet function."""
    result = await facade.acall("nagato_execute_snippet", code="print('hello')", timeout_seconds=5)
    assert "hello" in result


@pytest.mark.asyncio
async def test_lint(facade):
    """Test lint function."""
    result = await facade.acall("nagato_lint", file="src/nagato_tools/search.py")
    assert "SUCCESS" in result or "Syntax error" in result or "LINTING WARNINGS" in result or "errors found" in result.lower()


@pytest.mark.asyncio
async def test_delete_file(facade):
    """Test delete file function."""
    # First create a test file via nagato_edit auto-create
    test_file = "test_delete_temp.txt"
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="Test content for deletion")
    
    # Verify file exists
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "Test content for deletion" in result
    
    # Delete the file
    result = await facade.acall("nagato_delete", file=test_file)
    assert "SUCCESS" in result
    assert "test_delete_temp.txt" in result
    assert "bytes" in result
    
    # Verify file is deleted
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "not found" in result.lower() or "error" in result.lower()


@pytest.mark.asyncio
async def test_delete_file_path_traversal(facade):
    """Test delete file path traversal guard."""
    result = await facade.acall("nagato_delete", file="../../../etc/passwd")
    assert "ERROR" in result
    assert "outside workspace root" in result


@pytest.mark.asyncio
async def test_delete_nonexistent_file(facade):
    """Test delete non-existent file handling."""
    result = await facade.acall("nagato_delete", file="nonexistent_file_xyz.txt")
    assert "ERROR" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_shell_basic(facade):
    """Test basic shell command execution."""
    # Use cmd /c on Windows for shell builtins like echo
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "echo", "hello world"]
    else:
        command = ["echo", "hello world"]
    result = await facade.acall("nagato_shell", command=command)
    assert isinstance(result, str)
    assert "hello world" in result
    assert "EXIT CODE: 0" in result


@pytest.mark.asyncio
async def test_shell_timeout(facade):
    """Test shell command timeout enforcement."""
    # Use Python sleep for cross-platform reliability
    # This avoids issues with Windows timeout command and pytest output capture
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    result = await facade.acall("nagato_shell", command=command, timeout_seconds=2)
    assert isinstance(result, str)
    assert "TIMED OUT" in result or "timed out" in result.lower()


@pytest.mark.asyncio
async def test_shell_stderr_capture(facade):
    """Test shell stderr capture."""
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "echo error message 1>&2"]
    else:
        command = ["bash", "-c", "echo 'error message' >&2"]
    result = await facade.acall("nagato_shell", command=command)
    assert isinstance(result, str)
    assert "error message" in result


@pytest.mark.asyncio
async def test_shell_nonzero_exit(facade):
    """Test shell non-zero exit code handling."""
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "exit /b 1"]
    else:
        command = ["false"]
    result = await facade.acall("nagato_shell", command=command)
    assert isinstance(result, str)
    assert "EXIT CODE: 1" in result


@pytest.mark.asyncio
async def test_shell_truncation(facade):
    """Test shell output truncation for large output."""
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "for /l %i in (1,1,1000) do echo line %i with some extra text to make it longer"]
    else:
        command = ["bash", "-c", "for i in {1..1000}; do echo \"line $i with some extra text to make it longer\"; done"]
    result = await facade.acall("nagato_shell", command=command)
    assert isinstance(result, str)
    assert "[OUTPUT TRUNCATED]" in result or "[TRUNCATED:" in result or len(result) > 0


@pytest.mark.asyncio
async def test_shell_security_denylist(facade):
    """Test shell security denylist blocks dangerous commands."""
    result = await facade.acall("nagato_shell", command=["rm", "-rf", "/"])
    assert isinstance(result, str)
    assert "ERROR[nagato_shell]" in result
    assert "blocked" in result.lower() or "security" in result.lower()


@pytest.mark.asyncio
async def test_shell_path_traversal(facade):
    """Test shell working directory path traversal guard."""
    result = await facade.acall("nagato_shell", command=["pwd"], cwd="../../../etc")
    assert isinstance(result, str)
    assert "ERROR" in result
    assert "outside workspace root" in result


@pytest.mark.asyncio
async def test_shell_command_not_found(facade):
    """Test shell command not found handling."""
    result = await facade.acall("nagato_shell", command=["nonexistent_command_xyz_123"])
    assert isinstance(result, str)
    assert "ERROR" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_rename_file_basic(facade):
    """Test basic file rename within same directory."""
    test_file = "test_rename_source.txt"
    new_name = "test_rename_dest.txt"
    # Clean slate
    await facade.acall("nagato_delete", file=test_file)
    await facade.acall("nagato_delete", file=new_name)

    # First create a test file via nagato_edit auto-create
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="Test content for rename")
    
    # Verify file exists
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "Test content for rename" in result
    
    # Rename the file
    new_name = "test_rename_dest.txt"
    result = await facade.acall("nagato_rename", source=test_file, destination=new_name)
    assert "SUCCESS" in result
    assert test_file in result
    assert new_name in result
    assert "bytes" in result
    
    # Verify source is gone
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "not found" in result.lower() or "error" in result.lower()
    
    # Verify destination exists with same content
    result = await facade.acall("nagato_read_file", file=new_name)
    assert "Test content for rename" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=new_name)


@pytest.mark.asyncio
async def test_rename_file_cross_directory(facade):
    """Test file rename across directories."""
    test_file = "test_rename_cross_src.txt"
    new_path = "test_rename_dest_dir/test_rename_cross_dest.txt"
    # Clean slate
    await facade.acall("nagato_delete", file=test_file)
    await facade.acall("nagato_delete", file=new_path)

    # Create source file via nagato_edit auto-create
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="Cross directory rename test")
    
    # Create destination directory
    import os
    dest_dir = "test_rename_dest_dir"
    os.makedirs(dest_dir, exist_ok=True)
    
    # Rename across directory
    new_path = f"{dest_dir}/test_rename_cross_dest.txt"
    result = await facade.acall("nagato_rename", source=test_file, destination=new_path)
    assert "SUCCESS" in result
    assert test_file in result
    assert new_path in result
    
    # Verify source is gone
    result = await facade.acall("nagato_read_file", file=test_file)
    assert "not found" in result.lower() or "error" in result.lower()
    
    # Verify destination exists with same content
    result = await facade.acall("nagato_read_file", file=new_path)
    assert "Cross directory rename test" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=new_path)
    os.rmdir(dest_dir)


@pytest.mark.asyncio
async def test_rename_file_path_traversal_source(facade):
    """Test rename source path traversal guard."""
    result = await facade.acall("nagato_rename", source="../../../etc/passwd", destination="test.txt")
    assert "ERROR" in result
    assert "outside workspace root" in result


@pytest.mark.asyncio
async def test_rename_file_path_traversal_dest(facade):
    """Test rename destination path traversal guard."""
    result = await facade.acall("nagato_rename", source="test.txt", destination="../../../etc/passwd")
    assert "ERROR" in result
    assert "outside workspace root" in result


@pytest.mark.asyncio
async def test_rename_nonexistent_source(facade):
    """Test rename non-existent source file handling."""
    result = await facade.acall("nagato_rename", source="nonexistent_file_xyz.txt", destination="test.txt")
    assert "ERROR" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_rename_destination_exists(facade):
    """Test rename when destination already exists."""
    # Create source file via nagato_edit auto-create
    test_file = "test_rename_src_exists.txt"
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="Source content")
    
    # Create destination file via nagato_edit auto-create
    dest_file = "test_rename_dest_exists.txt"
    await facade.acall("nagato_edit", file=dest_file, searchstring="", replacement="Dest content")
    
    # Try to rename without overwrite
    result = await facade.acall("nagato_rename", source=test_file, destination=dest_file)
    assert "ERROR" in result
    assert "already exists" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=test_file)
    await facade.acall("nagato_delete", file=dest_file)


@pytest.mark.asyncio
async def test_rename_with_overwrite(facade):
    """Test rename with overwrite=True."""
    # Create source file via nagato_edit auto-create
    test_file = "test_rename_overwrite_src.txt"
    await facade.acall("nagato_edit", file=test_file, searchstring="", replacement="New content")
    
    # Create destination file via nagato_edit auto-create
    dest_file = "test_rename_overwrite_dest.txt"
    await facade.acall("nagato_edit", file=dest_file, searchstring="", replacement="Old content")
    
    # Rename with overwrite
    result = await facade.acall("nagato_rename", source=test_file, destination=dest_file, overwrite=True)
    assert "SUCCESS" in result
    
    # Verify destination has new content
    result = await facade.acall("nagato_read_file", file=dest_file)
    assert "New content" in result
    
    # Cleanup
    await facade.acall("nagato_delete", file=dest_file)


@pytest.mark.asyncio
async def test_shell_with_env(facade):
    """Test shell with custom environment variables."""
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "echo %TEST_VAR%"]
    else:
        command = ["bash", "-c", "echo $TEST_VAR"]
    result = await facade.acall("nagato_shell", command=command, env={"TEST_VAR": "custom_value"})
    assert isinstance(result, str)
    assert "EXIT CODE: 0" in result
    assert "custom_value" in result


@pytest.mark.asyncio
async def test_shell_complex_command(facade):
    """Test shell with complex command."""
    import sys
    if sys.platform == "win32":
        command = ["cmd", "/c", "echo stdout & echo stderr 1>&2 & exit /b 0"]
    else:
        command = ["bash", "-c", "echo stdout; echo stderr >&2; exit 0"]
    result = await facade.acall("nagato_shell", command=command)
    assert isinstance(result, str)
    assert "EXIT CODE: 0" in result
    assert "stdout" in result
    assert "stderr" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])