"""Tests for Redmine issue tracker guardrails."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_synology_server():
    spec = importlib.util.spec_from_file_location("synology_server", REPO_ROOT / "synology-server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fastmcp_server_exposes_issue_tracker_update_tool() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    assert "def move_issue(" in content
    assert "def update_issue_tracker(" in content
    assert "allow_idea_tracker" in content
    assert "Idea tracker is reserved for explicit user idea capture" in content


def test_synology_tools_list_exposes_issue_tracker_update_tool() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}

    assert "move_issue" in names
    assert "update_issue_tracker" in names


def test_synology_move_issue_updates_project(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        if path == "/projects/98.json":
            return {"project": {"id": 98}}
        if method == "PUT":
            return {}
        return {"issue": {"id": 1514, "tracker": {"id": 3}, "project": {"id": 98}, "status": {"id": 1}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "move_issue",
        {
            "issue_id": 1514,
            "project_id": 98,
            "note": "Move project",
        },
    )

    assert calls[1] == {
        "method": "PUT",
        "path": "/issues/1514.json",
        "json": {"issue": {"project_id": 98, "notes": "Move project"}},
    }
    assert result["project_id"] == 98


def test_synology_create_issue_rejects_agent_idea_tracker() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    result = handler._call_tool(
        "create_issue",
        {
            "project_id": 20,
            "subject": "Wrong tracker",
            "tracker_id": 6,
        },
    )

    assert result == {"error": "Idea tracker is reserved for explicit user idea capture; agents must use tracker_id 3"}


def test_synology_create_issue_rejects_redmine_tracker_drift(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        assert (method, path) == ("POST", "/issues.json")
        assert kwargs["json"]["issue"]["tracker_id"] == 3
        return {"issue": {"id": 1515, "tracker": {"id": 6}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "create_issue",
        {
            "project_id": 20,
            "subject": "Create as task",
            "tracker_id": 3,
        },
    )

    assert result == {
        "error": "Redmine issue 1515 tracker mismatch: requested tracker_id 3, Redmine returned tracker_id 6"
    }


def test_synology_update_issue_tracker_sets_task(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        if method == "PUT":
            return {}
        return {"issue": {"id": 1514, "tracker": {"id": 3}, "project": {"id": 20}, "status": {"id": 1}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "update_issue_tracker",
        {
            "issue_id": 1514,
            "tracker_id": 3,
            "note": "Correct tracker",
        },
    )

    assert calls[0] == {
        "method": "PUT",
        "path": "/issues/1514.json",
        "json": {"issue": {"tracker_id": 3, "notes": "Correct tracker"}},
    }
    assert result["tracker_id"] == 3
