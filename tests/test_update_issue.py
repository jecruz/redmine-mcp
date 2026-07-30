"""Tests for the unified mcp__redmine__update_issue tool (Redmine #1547).

Background: previous specialised mutation tools (update_issue_status,
update_issue_tracker, move_issue) had silent-failure modes (Redmine #1559) and
left gaps — there was no single tool to change subject/description/dates/
estimated_hours/priority/assignee etc. update_issue is the unified replacement.

These tests cover both transports (server.py / synology-server.py) by
loading each module via importlib and monkeypatching the HTTP helper.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_server():
    """server.py validates REDMINE_URL/REDMINE_API_KEY at import time — supply
    placeholders so we can exec_module without failing on env validation."""
    os.environ.setdefault("REDMINE_URL", "http://test.invalid")
    os.environ.setdefault("REDMINE_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location(
        "redmine_mcp_server_under_test", REPO_ROOT / "server.py"
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_synology_server():
    spec = importlib.util.spec_from_file_location(
        "synology_server", REPO_ROOT / "synology-server.py"
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_full_issue(**overrides: Any) -> dict[str, Any]:
    """Build the full GET /issues/{id}.json response that _clean_issue
    and get_issue both expect to consume after the PUT."""
    issue: dict[str, Any] = {
        "id": 61,
        "project": {"id": 20, "name": "Demo"},
        "tracker": {"id": 3, "name": "Task"},
        "status": {"id": 1, "name": "New"},
        "priority": {"id": 2, "name": "Normal"},
        "author": {"name": "Agent"},
        "assigned_to": {"name": "Nobody"},
        "subject": "Original subject",
        "description": "",
        "start_date": None,
        "due_date": None,
        "done_ratio": 0,
        "estimated_hours": None,
        "created_on": "2026-07-29T10:00:00Z",
        "updated_on": "2026-07-29T10:00:00Z",
        "journals": [],
    }
    issue.update(overrides)
    return issue


# ---------------------------------------------------------------------------
# Tool registration / schema smoke checks
# ---------------------------------------------------------------------------


def test_server_py_exposes_update_issue() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    assert "def update_issue(" in content
    # All advertised fields
    for field in (
        "subject",
        "description",
        "assigned_to_id",
        "category_id",
        "priority_id",
        "status_id",
        "tracker_id",
        "due_date",
        "start_date",
        "estimated_hours",
        "done_ratio",
        "custom_fields",
        "watcher_user_ids",
        "project_id",
        "notes",
    ):
        assert field in content, f"update_issue missing param: {field}"


def test_synology_tools_list_exposes_update_issue() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    tools = handler._handle_method("tools/list", {})["tools"]
    by_name = {t["name"]: t for t in tools}
    assert "update_issue" in by_name, "tools/list must advertise update_issue"
    schema = by_name["update_issue"]["inputSchema"]
    assert "issue_id" in schema.get("required", []), "issue_id must be required"
    props = schema["properties"]
    for field in (
        "subject",
        "description",
        "assigned_to_id",
        "status_id",
        "tracker_id",
        "project_id",
        "notes",
    ):
        assert field in props, f"update_issue schema missing field: {field}"


def test_synology_handler_has_update_issue() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    assert hasattr(handler, "_update_issue")


# ---------------------------------------------------------------------------
# Empty-update guard
# ---------------------------------------------------------------------------


def test_server_update_issue_no_fields_raises() -> None:
    module = _load_server()
    with pytest.raises(ValueError, match="at least one field"):
        module.update_issue(issue_id=61)


def test_server_update_issue_only_notes_works() -> None:
    """A note-only update is allowed (notes becomes the only key in the body)."""
    module = _load_server()
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "PUT":
            return {}
        return {"issue": _make_full_issue()}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "_request", fake_request)
        module.update_issue(issue_id=61, notes="Just a note")
        assert calls[0][0] == "PUT"
        assert calls[0][2] == {"issue": {"notes": "Just a note"}}
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Field forwarding
# ---------------------------------------------------------------------------


def test_server_update_issue_status_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_issue(issue_id=61, status_id=5) sends only {status_id: 5} and
    verifies the post-state matches."""
    module = _load_server()
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "PUT":
            return {}
        # GET re-fetch (for verify)
        return {"issue": _make_full_issue(status={"id": 5, "name": "Closed"})}

    monkeypatch.setattr(module, "_request", fake_request)
    result = module.update_issue(issue_id=61, status_id=5)
    # PUT body
    assert calls[0][0] == "PUT"
    assert calls[0][2] == {"issue": {"status_id": 5}}
    assert result["status_id"] == 5


def test_server_update_issue_sends_only_supplied_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the kwargs that were supplied show up in the PUT body — the rest
    are omitted so Redmine doesn't get a no-op diff."""
    module = _load_server()
    captured: dict[str, Any] = {}

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            captured["body"] = kwargs.get("json") or {}
            return {}
        return {"issue": _make_full_issue(subject="New subject")}

    monkeypatch.setattr(module, "_request", fake_request)
    module.update_issue(issue_id=61, subject="New subject", priority_id=3)

    body = captured["body"]["issue"]
    assert body == {"subject": "New subject", "priority_id": 3}
    for omitted in (
        "description",
        "assigned_to_id",
        "status_id",
        "tracker_id",
        "due_date",
        "start_date",
        "estimated_hours",
        "done_ratio",
        "custom_fields",
        "watcher_user_ids",
        "project_id",
        "notes",
    ):
        assert omitted not in body, f"{omitted} should not be in PUT body when omitted"


def test_server_update_issue_forwards_complex_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields and watcher_user_ids go through verbatim."""
    module = _load_server()
    captured: dict[str, Any] = {}

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            captured["body"] = kwargs.get("json") or {}
            return {}
        return {"issue": _make_full_issue()}

    monkeypatch.setattr(module, "_request", fake_request)
    module.update_issue(
        issue_id=61,
        custom_fields=[{"id": 1, "value": "abc"}],
        watcher_user_ids=[1, 2, 3],
        done_ratio=50,
        estimated_hours=4.5,
    )
    body = captured["body"]["issue"]
    assert body["custom_fields"] == [{"id": 1, "value": "abc"}]
    assert body["watcher_user_ids"] == [1, 2, 3]
    assert body["done_ratio"] == 50
    assert body["estimated_hours"] == 4.5


def test_server_update_issue_combines_with_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_issue(... fields ..., notes='...') bundles the note into the
    same PUT body — a single atomic change."""
    module = _load_server()
    captured: dict[str, Any] = {}

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            captured["body"] = kwargs.get("json") or {}
            return {}
        return {"issue": _make_full_issue(status={"id": 5, "name": "Closed"})}

    monkeypatch.setattr(module, "_request", fake_request)
    module.update_issue(issue_id=61, status_id=5, done_ratio=100, notes="Shipped")
    assert captured["body"] == {
        "issue": {"status_id": 5, "done_ratio": 100, "notes": "Shipped"}
    }


# ---------------------------------------------------------------------------
# Project move + tracker coercion in a single call (Redmine #1412)
# ---------------------------------------------------------------------------


def test_server_update_issue_project_move_with_tracker_coercion_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When moving to a new project AND requesting a tracker, Redmine silently
    coerces the tracker if the new project doesn't have the requested tracker.
    update_issue must verify and raise so the agent doesn't see a misleading
    success."""
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        # GET shows project moved but tracker got coerced from 3 → 2
        return {
            "issue": _make_full_issue(
                project={"id": 98, "name": "Other"},
                tracker={"id": 2, "name": "Feature"},  # coerced
                status={"id": 5, "name": "Closed"},
            )
        }

    monkeypatch.setattr(module, "_request", fake_request)
    with pytest.raises(RuntimeError, match="tracker mismatch"):
        module.update_issue(
            issue_id=61,
            project_id=98,
            tracker_id=3,
            status_id=5,
            notes="Move + close",
        )


def test_server_update_issue_project_move_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the move AND tracker change both apply, we return the cleaned
    issue."""
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": _make_full_issue(
                project={"id": 98, "name": "Other"},
                tracker={"id": 3, "name": "Task"},
                status={"id": 5, "name": "Closed"},
            )
        }

    monkeypatch.setattr(module, "_request", fake_request)
    result = module.update_issue(
        issue_id=61, project_id=98, tracker_id=3, status_id=5
    )
    assert result["project_id"] == 98
    assert result["tracker_id"] == 3
    assert result["status_id"] == 5


def test_server_update_issue_status_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical 2026-07-29 bug from the parent task: PUT 204'd but the
    status didn't actually change. update_issue must raise."""
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        # GET shows status is still 1 (New), NOT 5
        return {"issue": _make_full_issue(status={"id": 1, "name": "New"})}

    monkeypatch.setattr(module, "_request", fake_request)
    with pytest.raises(RuntimeError, match="status mismatch"):
        module.update_issue(issue_id=61, status_id=5, notes="Trying to close")


def test_server_update_issue_project_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same pattern for project moves: PUT 204'd but the issue stayed in the
    old project. update_issue must raise."""
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": _make_full_issue(
                project={"id": 20, "name": "Old"},
                tracker={"id": 3},
                status={"id": 1},
            )
        }

    monkeypatch.setattr(module, "_request", fake_request)
    with pytest.raises(RuntimeError, match="project mismatch"):
        module.update_issue(issue_id=61, project_id=98, notes="Try to move")


def test_server_update_issue_rejects_idea_tracker_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent-track guard must apply to update_issue just like
    update_issue_tracker / create_issue."""
    module = _load_server()
    with pytest.raises(ValueError, match="Idea tracker is reserved"):
        module.update_issue(issue_id=61, tracker_id=6)


def test_server_update_issue_allows_idea_tracker_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        return {"issue": _make_full_issue(tracker={"id": 6, "name": "Idea"})}

    monkeypatch.setattr(module, "_request", fake_request)
    result = module.update_issue(
        issue_id=61, tracker_id=6, allow_idea_tracker=True, notes="Filing idea"
    )
    assert result["tracker_id"] == 6


# ---------------------------------------------------------------------------
# synology-server.py — JSON-RPC dispatch
# ---------------------------------------------------------------------------


def test_synology_update_issue_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP tools/call wrapper routes 'update_issue' to _update_issue."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "PUT":
            return {}
        return {"issue": _make_full_issue(status={"id": 5, "name": "Closed"})}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "update_issue",
        {"issue_id": 61, "status_id": 5, "notes": "Closing"},
    )
    # First call: PUT
    assert calls[0][0] == "PUT"
    assert calls[0][1] == "/issues/61.json"
    assert calls[0][2] == {"issue": {"status_id": 5, "notes": "Closing"}}
    # The result is the cleaned issue
    assert result["status_id"] == 5


def test_synology_update_issue_status_mismatch_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeError surfaces as JSON-RPC error envelope."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {"issue": _make_full_issue(status={"id": 1})}

    monkeypatch.setattr(module, "_rm_request", fake_request)
    result = handler._call_tool(
        "update_issue", {"issue_id": 61, "status_id": 5}
    )
    assert "error" in result
    assert "status mismatch" in result["error"]


def test_synology_update_issue_project_move_with_coercion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": _make_full_issue(
                project={"id": 98},
                tracker={"id": 2},  # coerced
                status={"id": 5},
            )
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)
    result = handler._call_tool(
        "update_issue",
        {
            "issue_id": 61,
            "project_id": 98,
            "tracker_id": 3,
            "status_id": 5,
            "notes": "Move + close",
        },
    )
    assert "error" in result
    assert "tracker mismatch" in result["error"]


def test_synology_update_issue_empty_raises() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    result = handler._call_tool("update_issue", {"issue_id": 61})
    assert "error" in result
    assert "at least one field" in result["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])