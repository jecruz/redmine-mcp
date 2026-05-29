"""Tests for create_project inherit_members fix.

Verifies that:
- Top-level projects (no parent_id) do NOT include inherit_members
- Subprojects (with parent_id) include inherit_members: True
"""
import os
import re
import pytest


def test_server_py_code_has_conditional_inherit():
    """Verify server.py: inherit_members = True only set inside 'if parent_id is not None' block."""
    path = "/Users/jeffreycruz/Development/AI_TOOLS/redmine-mcp"
    with open(os.path.join(path, "server.py")) as f:
        content = f.read()

    func_match = re.search(
        r"def create_project\([^)]+\)[^:]*:(.*?)(?=\n@mcp\.tool\(\)|\nclass |\Z)",
        content, re.DOTALL)
    assert func_match, "create_project function not found in server.py"
    func_body = func_match.group(1)

    # inherit_members must be inside the parent_id conditional
    assert "if parent_id is not None" in func_body, \
        "create_project must check 'if parent_id is not None'"

    lines = func_body.split("\n")
    found_if = -1
    for i, line in enumerate(lines):
        if "if parent_id is not None" in line:
            found_if = i
            break

    # Check that inherit_members appears AFTER the if line, before dedent
    inherit_line = -1
    base_indent = len(lines[found_if]) - len(lines[found_if].lstrip()) if found_if >= 0 else 0
    for i in range(found_if + 1, len(lines)):
        line = lines[i]
        if "inherit_members" in line and "True" in line:
            inherit_line = i
            break
        # If line is non-blank and has less/equal indent than if (left the if block)
        if line.strip() and (len(line) - len(line.lstrip())) <= base_indent:
            break

    assert inherit_line > found_if, \
        "inherit_members = True must be inside 'if parent_id is not None' block in server.py"


def test_synology_py_code_has_conditional_inherit():
    """Verify synology-server.py: inherit_members = True only set inside 'if parent_id is not None' block."""
    path = "/Users/jeffreycruz/Development/AI_TOOLS/redmine-mcp"
    with open(os.path.join(path, "synology-server.py")) as f:
        content = f.read()

    func_match = re.search(
        r"def _create_project\(self[^)]*\):(.*?)(?=\n    def |\nclass |\Z)",
        content, re.DOTALL)
    assert func_match, "_create_project method not found in synology-server.py"
    func_body = func_match.group(1)

    assert "if parent_id is not None" in func_body, \
        "_create_project must check 'if parent_id is not None'"

    lines = func_body.split("\n")
    found_if = -1
    for i, line in enumerate(lines):
        if "if parent_id is not None" in line:
            found_if = i
            break

    base_indent = len(lines[found_if]) - len(lines[found_if].lstrip()) if found_if >= 0 else 0
    inherit_line = -1
    for i in range(found_if + 1, len(lines)):
        line = lines[i]
        if "inherit_members" in line and "True" in line:
            inherit_line = i
            break
        if line.strip() and (len(line) - len(line.lstrip())) <= base_indent:
            break

    assert inherit_line > found_if, \
        "inherit_members = True must be inside 'if parent_id is not None' block in synology-server.py"


def test_both_files_have_create_project():
    """Both server.py and synology-server.py must have create_project functionality."""
    path = "/Users/jeffreycruz/Development/AI_TOOLS/redmine-mcp"
    with open(os.path.join(path, "server.py")) as f:
        assert "def create_project" in f.read()
    with open(os.path.join(path, "synology-server.py")) as f:
        assert "def _create_project" in f.read()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
