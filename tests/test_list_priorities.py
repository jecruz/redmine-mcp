"""Tests for list_priorities enumeration tool.

Verifies that:
- server.py has list_priorities() tool using /enumerations/issue_priorities.json
- synology-server.py has _list_priorities method using the same endpoint
"""
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_server_py_has_list_priorities():
    """Verify server.py: list_priorities function exists with correct endpoint."""
    content = (REPO_ROOT / "server.py").read_text()

    func_match = re.search(
        r"def list_priorities\(\)[^:]*:(.*?)(?=\n@mcp\.tool\(\)|\nclass |\Z)",
        content, re.DOTALL,
    )
    assert func_match, "list_priorities function not found in server.py"
    func_body = func_match.group(1)

    assert "/enumerations/issue_priorities.json" in func_body, \
        "list_priorities must call /enumerations/issue_priorities.json"
    assert "is_default" in func_body, \
        "list_priorities must include is_default field"
    assert "issue_priorities" in func_body, \
        "list_priorities must extract issue_priorities from response"


def test_synology_py_has_list_priorities():
    """Verify synology-server.py: _list_priorities method exists with correct endpoint."""
    content = (REPO_ROOT / "synology-server.py").read_text()

    func_match = re.search(
        r"def _list_priorities\(self[^)]*\):(.*?)(?=\n    def |\nclass |\Z)",
        content, re.DOTALL,
    )
    assert func_match, "_list_priorities method not found in synology-server.py"
    func_body = func_match.group(1)

    assert "/enumerations/issue_priorities.json" in func_body, \
        "_list_priorities must call /enumerations/issue_priorities.json"
    assert "is_default" in func_body, \
        "_list_priorities must include is_default field"
    assert "issue_priorities" in func_body, \
        "_list_priorities must extract issue_priorities from response"


def test_synology_py_routes_list_priorities():
    """Verify synology-server.py _call_tool routes 'list_priorities' to _list_priorities."""
    content = (REPO_ROOT / "synology-server.py").read_text()

    assert 'elif name == "list_priorities":' in content, \
        "synology-server.py _call_tool must route 'list_priorities'"
    assert "self._list_priorities(" in content, \
        "synology-server.py must call self._list_priorities for list_priorities route"


def test_synology_py_declares_list_priorities_tool():
    """Verify synology-server.py tools/list includes list_priorities."""
    content = (REPO_ROOT / "synology-server.py").read_text()

    assert '"name": "list_priorities"' in content, \
        "synology-server.py tools/list must declare list_priorities"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
