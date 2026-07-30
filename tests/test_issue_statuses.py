"""Tests for issue status discovery."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(filename: str, module_name: str):
    os.environ.setdefault("REDMINE_URL", "http://redmine.test")
    os.environ.setdefault("REDMINE_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / filename)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fastmcp_list_issue_statuses_filters_by_project_and_closed_state(monkeypatch) -> None:
    module = _load_module("server.py", "redmine_server")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "issue_statuses": [
                {"id": 1, "name": "New", "is_closed": False},
                {"id": 2, "name": "In Progress", "is_closed": False},
                {"id": 5, "name": "Closed", "is_closed": True},
            ]
        }

    monkeypatch.setattr(module, "_request", fake_request)

    result = module.list_issue_statuses(project_id=103, is_closed=False)

    assert calls == [("GET", "/issue_statuses.json", {"params": {"project_id": 103}})]
    assert result == [
        {"id": 1, "name": "New", "is_closed": False},
        {"id": 2, "name": "In Progress", "is_closed": False},
    ]


def test_synology_list_issue_statuses_is_exposed_and_filters_terminal_statuses(monkeypatch) -> None:
    module = _load_module("synology-server.py", "synology_server_statuses")
    handler = object.__new__(module.MCPHandler)
    handler.api_key = "test-key"
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append((method, path, api_key, kwargs))
        return {
            "issue_statuses": [
                {"id": 1, "name": "New", "is_closed": False},
                {"id": 3, "name": "Resolved", "is_closed": True},
                {"id": 5, "name": "Closed", "is_closed": True},
            ]
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)

    tools = handler._handle_method("tools/list", {})["tools"]
    status_tool = next(tool for tool in tools if tool["name"] == "list_issue_statuses")
    result = handler._call_tool("list_issue_statuses", {"project_id": 103, "is_closed": True})

    assert status_tool["inputSchema"]["properties"] == {
        "project_id": {"type": "number"},
        "is_closed": {"type": "boolean"},
    }
    assert calls == [("GET", "/issue_statuses.json", "test-key", {"params": {"project_id": 103}})]
    assert result == [
        {"id": 3, "name": "Resolved", "is_closed": True},
        {"id": 5, "name": "Closed", "is_closed": True},
    ]
