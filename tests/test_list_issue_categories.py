"""Tests for the list_issue_categories discovery tool.

Covers:
- server.py exposes list_issue_categories wired to /projects/{id}.json?include=issue_categories
- synology-server.py exposes list_issue_categories in tools/list and dispatches to _list_issue_categories
- _list_issue_categories flattens Redmine's nested project.issue_categories into the contract shape
- Empty category list returns empty result
- assigned_to_id surfaces only when the category has a default assignee
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_server():
    """Load server.py without triggering its module-level env validation.

    server.py raises at import time if REDMINE_URL / REDMINE_API_KEY are
    missing, so provide placeholders before exec_module. The values are not
    used by the tests under coverage (every external call is monkeypatched).
    """
    os.environ.setdefault("REDMINE_URL", "http://test.invalid")
    os.environ.setdefault("REDMINE_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("redmine_mcp_server_under_test", REPO_ROOT / "server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_synology_server():
    spec = importlib.util.spec_from_file_location("synology_server", REPO_ROOT / "synology-server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# server.py — FastMCP tool surface
# ---------------------------------------------------------------------------


def test_fastmcp_server_exposes_list_issue_categories_tool() -> None:
    module = _load_server()
    tool = module.mcp._tool_manager._tools["list_issue_categories"]
    params = tool.parameters
    assert params["properties"]["project_id"]["type"] == "integer"
    assert "project_id" in params["required"]


def test_server_list_issue_categories_hits_project_include(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_server()
    captured: dict[str, object] = {}

    def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        return {
            "project": {
                "id": 43,
                "issue_categories": [
                    {"id": 100, "name": "JAISOC OS", "project": {"id": 43}, "assigned_to": {"id": 7}},
                    {"id": 101, "name": "Stargate", "project": {"id": 43}},
                ],
            }
        }

    monkeypatch.setattr(module, "_request", fake_request)

    result = module.list_issue_categories(project_id=43)

    assert captured == {"method": "GET", "path": "/projects/43.json?include=issue_categories"}
    assert result == [
        {"id": 100, "name": "JAISOC OS", "project_id": 43, "assigned_to_id": 7},
        {"id": 101, "name": "Stargate", "project_id": 43, "assigned_to_id": None},
    ]


def test_server_list_issue_categories_returns_empty_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_server()

    def fake_request(method, path, **kwargs):
        return {"project": {"id": 43, "issue_categories": []}}

    monkeypatch.setattr(module, "_request", fake_request)
    assert module.list_issue_categories(project_id=43) == []


# ---------------------------------------------------------------------------
# synology-server.py — manual JSON-RPC variant
# ---------------------------------------------------------------------------


def test_synology_tools_list_exposes_list_issue_categories() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}
    assert "list_issue_categories" in names

    cat_tool = next(t for t in tools if t["name"] == "list_issue_categories")
    assert cat_tool["inputSchema"]["properties"]["project_id"]["type"] == "number"
    assert "project_id" in cat_tool["inputSchema"].get("required", [])


def test_synology_list_issue_categories_flattens_response(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    captured: dict[str, object] = {}

    def fake_request(method, path, api_key, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["api_key"] = api_key
        return {
            "project": {
                "id": 43,
                "issue_categories": [
                    {"id": 100, "name": "JAISOC OS", "project": {"id": 43}, "assigned_to": {"id": 7}},
                    {"id": 101, "name": "Stargate", "project": {"id": 43}},
                ],
            }
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_issue_categories", {"project_id": 43})

    assert captured["method"] == "GET"
    assert captured["path"] == "/projects/43.json?include=issue_categories"
    assert captured["api_key"] == module.DEFAULT_REDMINE_API_KEY
    assert result == [
        {"id": 100, "name": "JAISOC OS", "project_id": 43, "assigned_to_id": 7},
        {"id": 101, "name": "Stargate", "project_id": 43, "assigned_to_id": None},
    ]


def test_synology_list_issue_categories_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        return {"project": {"id": 43, "issue_categories": []}}

    monkeypatch.setattr(module, "_rm_request", fake_request)
    assert handler._call_tool("list_issue_categories", {"project_id": 43}) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
