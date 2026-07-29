"""Tests for search_projects and resolve_project discovery tools (Redmine #1554).

Verifies both server.py (FastMCP) and synology-server.py (JSON-RPC) variants.
"""
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Regex for extracting server.py function bodies (handles type-annotated signatures)
_SERVER_FUNC_RE = r"def {func}\([^)]*\).*?:(.*?)(?=\n@mcp\.tool\(\)|\nif __name__|\nclass |\Z)"


def _server_func_body(name):
    content = (REPO_ROOT / "server.py").read_text()
    pattern = _SERVER_FUNC_RE.format(func=name)
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        raise AssertionError(f"function body for '{name}' not found in server.py")
    return m.group(1)


# ── server.py tests ──

def test_server_py_has_search_projects():
    """server.py must define search_projects as an @mcp.tool()."""
    content = (REPO_ROOT / "server.py").read_text()
    assert "def search_projects(" in content, "search_projects function not found in server.py"
    # Verify it's decorated with @mcp.tool()
    idx = content.find("def search_projects(")
    before = content[:idx]
    assert "@mcp.tool()" in before.split("\ndef ")[-1], \
        "search_projects must be decorated with @mcp.tool()"


def test_server_py_search_projects_case_insensitive():
    """search_projects must lower-case the query for case-insensitive matching."""
    body = _server_func_body("search_projects")
    assert ".lower()" in body, "search_projects must lower-case the needle"


def test_server_py_search_projects_matches_name_identifier_description():
    """search_projects must search name, identifier, and description fields."""
    body = _server_func_body("search_projects")
    assert '"name"' in body, "search_projects must check the name field"
    assert '"identifier"' in body, "search_projects must check the identifier field"
    assert '"description"' in body, "search_projects must check the description field"


def test_server_py_search_projects_returns_id_name_identifier():
    """search_projects results must include id, name, identifier at minimum."""
    body = _server_func_body("search_projects")
    assert "_clean_project" in body, "search_projects must use _clean_project"


def test_server_py_search_projects_returns_matches_key():
    """search_projects must return a dict with 'matches' key."""
    body = _server_func_body("search_projects")
    assert '"matches"' in body, "search_projects result must have 'matches' key"


def test_server_py_has_resolve_project():
    """server.py must define resolve_project as an @mcp.tool()."""
    content = (REPO_ROOT / "server.py").read_text()
    assert "def resolve_project(" in content, "resolve_project function not found"
    idx = content.find("def resolve_project(")
    before = content[:idx]
    assert "@mcp.tool()" in before.split("\ndef ")[-1], \
        "resolve_project must be decorated with @mcp.tool()"


def test_server_py_resolve_project_handles_numeric():
    """resolve_project must handle numeric id by fetching /projects/{id}.json."""
    body = _server_func_body("resolve_project")
    assert "isdigit" in body, "resolve_project must check for numeric input with isdigit()"
    assert "/projects/" in body, "resolve_project must fetch by id via /projects/{id}.json"


def test_server_py_resolve_project_identifier_before_name():
    """resolve_project must try identifier match BEFORE name match."""
    body = _server_func_body("resolve_project")
    # Find the non-numeric lookup path
    needle_section = body.split("needle = ")[1] if "needle = " in body else body
    id_pos = needle_section.find('"identifier"')
    name_pos = needle_section.find('"name"')
    assert id_pos >= 0, "resolve_project must look up by identifier"
    assert name_pos >= 0, "resolve_project must look up by name"
    assert id_pos < name_pos, (
        "resolve_project must try identifier match BEFORE name match"
    )


def test_server_py_resolve_project_raises_on_not_found():
    """resolve_project must raise on not-found (not return None or empty dict)."""
    body = _server_func_body("resolve_project")
    assert "not found" in body.lower(), "resolve_project must have a not-found error message"


# ── synology-server.py tests ──

def test_synology_py_has_search_projects():
    """synology-server.py must define _search_projects method."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert "def _search_projects(" in content, "_search_projects method not found in synology-server.py"


def test_synology_py_has_search_projects_in_tools_list():
    """synology-server.py tools/list must advertise search_projects."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert '"search_projects"' in content, "search_projects not in synology-server.py tools/list"


def test_synology_py_has_search_projects_dispatch():
    """synology-server.py _call_tool must dispatch search_projects."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert 'name == "search_projects"' in content, "search_projects dispatch not found"


def test_synology_py_has_resolve_project():
    """synology-server.py must define _resolve_project method."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert "def _resolve_project(" in content, "_resolve_project method not found in synology-server.py"


def test_synology_py_has_resolve_project_in_tools_list():
    """synology-server.py tools/list must advertise resolve_project."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert '"resolve_project"' in content, "resolve_project not in synology-server.py tools/list"


def test_synology_py_has_resolve_project_dispatch():
    """synology-server.py _call_tool must dispatch resolve_project."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert 'name == "resolve_project"' in content, "resolve_project dispatch not found"


def test_synology_py_search_projects_case_insensitive():
    """_search_projects must lower-case the query."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    func_match = re.search(
        r"def _search_projects\(self[^)]*\).*?:(.*?)(?=\n    def |\nclass |\Z)",
        content, re.DOTALL,
    )
    assert func_match, "_search_projects method body not found"
    body = func_match.group(1)
    assert ".lower()" in body, "_search_projects must lower-case the needle"


def test_synology_py_resolve_project_handles_numeric():
    """_resolve_project must handle numeric id."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    func_match = re.search(
        r"def _resolve_project\(self[^)]*\).*?:(.*?)(?=\n    def |\nclass |\Z)",
        content, re.DOTALL,
    )
    assert func_match, "_resolve_project method body not found"
    body = func_match.group(1)
    assert "isdigit" in body, "_resolve_project must check for numeric input"


def test_synology_py_resolve_project_identifier_before_name():
    """_resolve_project must try identifier before name."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    func_match = re.search(
        r"def _resolve_project\(self[^)]*\).*?:(.*?)(?=\n    def |\nclass |\Z)",
        content, re.DOTALL,
    )
    assert func_match
    body = func_match.group(1)
    needle_section = body.split("needle = ")[1] if "needle = " in body else body
    id_pos = needle_section.find('"identifier"')
    name_pos = needle_section.find('"name"')
    assert id_pos >= 0
    assert name_pos >= 0
    assert id_pos < name_pos, "_resolve_project must try identifier before name"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
