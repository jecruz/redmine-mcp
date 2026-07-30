"""Tests for Redmine issue tracker guardrails."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import requests


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


def test_create_issue_forwards_optional_fields(monkeypatch) -> None:
    """create_issue forwards assigned_to_id, category_id, due_date, start_date, estimated_hours to Redmine."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    captured_body: dict[str, dict[str, Any]] = {}

    def fake_request(method, path, api_key, **kwargs):
        captured_body["issue"] = kwargs["json"]["issue"]
        return {"issue": {"id": 1600, "tracker": {"id": 3}, "project": {"id": 20}, "status": {"id": 1}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    handler._call_tool(
        "create_issue",
        {
            "project_id": 20,
            "subject": "Forward optional fields",
            "assigned_to_id": 1,
            "category_id": 5,
            "due_date": "2026-08-15",
            "start_date": "2026-07-29",
            "estimated_hours": 3.5,
        },
    )

    assert captured_body["issue"]["assigned_to_id"] == 1
    assert captured_body["issue"]["category_id"] == 5
    assert captured_body["issue"]["due_date"] == "2026-08-15"
    assert captured_body["issue"]["start_date"] == "2026-07-29"
    assert captured_body["issue"]["estimated_hours"] == 3.5


def test_create_issue_omits_fields_when_none(monkeypatch) -> None:
    """create_issue does not include optional fields when they are None or omitted."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    captured_body: dict[str, dict[str, Any]] = {}

    def fake_request(method, path, api_key, **kwargs):
        captured_body["issue"] = kwargs["json"]["issue"]
        return {"issue": {"id": 1601, "tracker": {"id": 3}, "project": {"id": 20}, "status": {"id": 1}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    handler._call_tool(
        "create_issue",
        {
            "project_id": 20,
            "subject": "No optional fields",
        },
    )

    body = captured_body["issue"]
    for key in ("assigned_to_id", "category_id", "due_date", "start_date", "estimated_hours"):
        assert key not in body, f"{key} should not be present when omitted"


def test_fastmcp_server_exposes_delete_issue_tool() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    assert "def delete_issue(" in content
    assert "confirm" in content
    assert "dry_run" in content
    assert "Exactly one of confirm=True or dry_run=True must be set" in content


def test_synology_tools_list_exposes_delete_issue() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}

    assert "delete_issue" in names


def test_synology_delete_issue_missing_flags_raises() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    result = handler._call_tool("delete_issue", {"issue_id": 1234})

    assert result == {
        "error": (
            "Exactly one of confirm=True or dry_run=True must be set. "
            "Set dry_run=True to preview, confirm=True to actually delete."
        )
    }


def test_synology_delete_issue_both_flags_raises() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    result = handler._call_tool(
        "delete_issue",
        {"issue_id": 1234, "confirm": True, "dry_run": True},
    )

    assert "Exactly one of confirm=True or dry_run=True must be set" in result["error"]


def test_synology_delete_issue_dry_run_exists(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        return {
            "issue": {
                "id": 1234,
                "subject": "Test issue",
                "project": {"id": 1, "name": "Test Project"},
                "tracker": {"id": 3, "name": "Task"},
                "status": {"id": 1, "name": "New"},
                "author": {"id": 1, "name": "Test User"},
            }
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "delete_issue",
        {"issue_id": 1234, "dry_run": True},
    )

    assert result["deleted"] is False
    assert result["dry_run"] is True
    assert result["status"] == "found"
    assert result["id"] == 1234
    assert result["issue"]["subject"] == "Test issue"
    assert "Would delete" in result["message"]


def test_synology_delete_issue_dry_run_not_found(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("Not Found", response=resp)

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "delete_issue",
        {"issue_id": 9999, "dry_run": True},
    )

    assert result["deleted"] is False
    assert result["status"] == "not_found"
    assert result["id"] == 9999
    assert "does not exist" in result["message"]


def test_synology_delete_issue_confirm_performs_delete(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    delete_calls = []

    def fake_delete(url, **kwargs):
        delete_calls.append({"url": url})
        resp = requests.Response()
        resp.status_code = 204
        return resp

    monkeypatch.setattr(module.requests, "delete", fake_delete)

    # Prevent audit log write from failing (path is under ~/.hermes)
    monkeypatch.setattr(module, "DELETE_AUDIT_LOG_PATH", module.Path("/tmp/test_delete_audit.log"))
    monkeypatch.setattr(module, "REDMINE_AGENT_ID", "test-agent")

    result = handler._call_tool(
        "delete_issue",
        {"issue_id": 1234, "confirm": True},
    )

    assert len(delete_calls) == 1
    assert "/issues/1234.json" in delete_calls[0]["url"]
    assert result["deleted"] is True
    assert result["status"] == "deleted"
    assert result["id"] == 1234
