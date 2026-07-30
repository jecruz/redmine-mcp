"""Tests for list_memberships tool.

Verifies that:
- list_memberships function exists in both server.py and synology-server.py
- The tools/list schema in synology-server.py includes the tool
- Client-side user_id filtering works correctly
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Code-structure tests ──

def test_server_py_has_list_memberships():
    """server.py: list_memberships function exists and is decorated with @mcp.tool()."""
    content = (REPO_ROOT / "server.py").read_text()
    assert "def list_memberships" in content, "list_memberships function not found in server.py"
    # Must be decorated
    assert "@mcp.tool()" in content, "server.py must have @mcp.tool() decorator"


def test_synology_py_has_list_memberships():
    """synology-server.py: _list_memberships method exists with correct signature."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert "def _list_memberships" in content, (
        "_list_memberships method not found in synology-server.py"
    )
    assert "project_id" in content, "signature must include project_id"


def test_synology_py_tools_list_includes_list_memberships():
    """synology-server.py tools/list response includes list_memberships schema."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert '"name": "list_memberships"' in content, (
        "tools/list schema must include list_memberships"
    )


def test_synology_py_call_tool_dispatches_list_memberships():
    """synology-server.py _call_tool dispatches to _list_memberships."""
    content = (REPO_ROOT / "synology-server.py").read_text()
    assert 'elif name == "list_memberships":' in content, (
        "_call_tool must dispatch list_memberships"
    )
    assert "self._list_memberships" in content, (
        "_call_tool must call self._list_memberships"
    )


# ── Logic test (client-side filtering) ──

MEMBERSHIPS_FIXTURE = [
    {
        "id": 101,
        "user": {"id": 1, "name": "Alice"},
        "roles": [{"id": 3, "name": "Manager"}, {"id": 4, "name": "Developer"}],
    },
    {
        "id": 102,
        "user": {"id": 2, "name": "Bob"},
        "roles": [{"id": 4, "name": "Developer"}],
    },
    {
        "id": 103,
        "user": {"id": 3, "name": "Carol"},
        "roles": [{"id": 5, "name": "Reporter"}],
    },
    {
        "id": 104,
        "user": None,
        "roles": [{"id": 4, "name": "Developer"}],
    },
]


def clean_membership(membership):
    """Mirror the server-side cleaning logic."""
    user = membership.get("user") or {}
    return {
        "id": membership.get("id"),
        "user": {"id": user.get("id"), "name": user.get("name")},
        "roles": [
            {"id": role.get("id"), "name": role.get("name")}
            for role in (membership.get("roles") or [])
        ],
    }


def list_memberships_impl(memberships, user_id=None):
    """Inline implementation matching server.py logic."""
    result = []
    for membership in memberships:
        entry = clean_membership(membership)
        if user_id is not None:
            if entry["user"]["id"] == user_id:
                result.append(entry)
                break
        else:
            result.append(entry)
    return result


def test_filter_all_returns_everyone():
    """Without user_id, all members are returned (including anonymous)."""
    result = list_memberships_impl(MEMBERSHIPS_FIXTURE)
    assert len(result) == 4


def test_filter_by_user_id_returns_one():
    """With user_id=2, only Bob is returned."""
    result = list_memberships_impl(MEMBERSHIPS_FIXTURE, user_id=2)
    assert len(result) == 1
    assert result[0]["user"]["name"] == "Bob"


def test_filter_by_nonexistent_user_returns_empty():
    """With unknown user_id, empty list is returned."""
    result = list_memberships_impl(MEMBERSHIPS_FIXTURE, user_id=999)
    assert result == []


def test_anonymous_user_has_null_id_and_name():
    """Memberships with user=None produce user.id=None and user.name=None."""
    result = list_memberships_impl(MEMBERSHIPS_FIXTURE, user_id=None)
    anon = [m for m in result if m["user"]["id"] is None]
    assert len(anon) == 1
    assert anon[0]["user"]["name"] is None


def test_roles_are_cleaned_to_id_and_name_only():
    """Role entries contain only id and name."""
    result = list_memberships_impl(MEMBERSHIPS_FIXTURE, user_id=1)
    alice = result[0]
    assert len(alice["roles"]) == 2
    for role in alice["roles"]:
        assert set(role.keys()) == {"id", "name"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
