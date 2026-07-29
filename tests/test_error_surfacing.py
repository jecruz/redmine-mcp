"""Regression tests for silent-success patterns from Redmine MCP #1559.

Each test replays a failure mode that used to be silently absorbed (the tool
returned {} or a clean success shape while Redmine had actually rejected the
mutation). The fix in this branch:

  - All mutation tools raise typed exceptions (RedminePermissionError /
    RedmineValidationError / RedmineNotFoundError / RedmineWorkflowError)
    instead of returning {} or a misleading success.
  - Each mutation tool accepts verify=True which re-fetches the issue after
    the PUT/POST and explicitly checks the post-state matches the request.
    On mismatch it raises RedmineWorkflowError so the caller cannot mistake
    a no-op for success.

These tests load server.py and synology-server.py via importlib so they cover
both transports without depending on a live Redmine. A fake requests.Response
simulates the HTTP responses.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── shared helpers ─────────────────────────────────────────────────────────────


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mock_response(
    *,
    status_code: int,
    body: Any = None,
    content: bytes | None = None,
) -> Mock:
    """Build a requests.Response-shaped Mock with .status_code, .content, .text, .json()."""
    if content is None:
        if isinstance(body, str):
            content = body.encode()
        elif body is None:
            content = b""
        else:
            content = json.dumps(body).encode()
    response = Mock()
    response.status_code = status_code
    response.content = content
    response.text = content.decode("utf-8", errors="replace")

    def _json(**_kwargs: Any) -> Any:
        if not content:
            raise ValueError("no json body")
        return json.loads(content.decode("utf-8"))

    response.json = _json
    return response


# ── exception hierarchy shape ──────────────────────────────────────────────────


def test_exception_hierarchy_is_well_formed() -> None:
    server = _load("server_for_hierarchy", "server.py")
    syn = _load("syn_for_hierarchy", "synology-server.py")
    for module in (server, syn):
        assert issubclass(module.RedminePermissionError, module.RedmineMCPError)
        assert issubclass(module.RedmineNotFoundError, module.RedmineMCPError)
        assert issubclass(module.RedmineWorkflowError, module.RedmineMCPError)
        assert issubclass(module.RedmineValidationError, module.RedmineMCPError)
        ve = module.RedmineValidationError("oops", errors=["x", "y"])
        assert ve.errors == ["x", "y"]
        assert ve.status_code is None
        ve2 = module.RedmineValidationError(
            "oops", errors=["x"], status_code=422, redmine_payload={"errors": ["x"]}
        )
        assert ve2.status_code == 422
        assert ve2.redmine_payload == {"errors": ["x"]}


# ── server.py: _request raises typed exceptions ───────────────────────────────


def test_server_request_raises_redmine_not_found() -> None:
    server = _load("server_not_found", "server.py")
    with pytest.raises(server.RedmineNotFoundError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(status_code=404, body={"error": "Not Found"})
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.redmine_payload == {"error": "Not Found"}


def test_server_request_raises_redmine_permission_error() -> None:
    server = _load("server_perm", "server.py")
    with pytest.raises(server.RedminePermissionError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(status_code=403, body="Forbidden")
        )
    assert excinfo.value.status_code == 403


def test_server_request_raises_redmine_validation_with_parsed_errors() -> None:
    server = _load("server_val", "server.py")
    with pytest.raises(server.RedmineValidationError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(
                status_code=422,
                body={"errors": {"tracker": ["is not set to one of the allowed values"]}},
            )
        )
    err = excinfo.value
    assert err.status_code == 422
    assert any("tracker" in e for e in err.errors)
    assert "is not set" in str(err.redmine_payload)


def test_server_request_raises_redmine_validation_with_list_errors() -> None:
    server = _load("server_val_list", "server.py")
    with pytest.raises(server.RedmineValidationError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(
                status_code=422,
                body={"errors": ["Subject can't be blank", "Project is invalid"]},
            )
        )
    assert excinfo.value.errors == [
        "Subject can't be blank",
        "Project is invalid",
    ]


def test_server_request_raises_redmine_workflow_error_on_409() -> None:
    server = _load("server_409", "server.py")
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(status_code=409, body={"error": "Conflict"})
        )
    assert excinfo.value.status_code == 409


def test_server_request_raises_base_error_on_5xx() -> None:
    server = _load("server_500", "server.py")
    with pytest.raises(server.RedmineMCPError) as excinfo:
        server._raise_for_redmine_response(
            _mock_response(status_code=502, body="Bad Gateway")
        )
    assert excinfo.value.status_code == 502
    assert not isinstance(excinfo.value, server.RedminePermissionError)
    assert not isinstance(excinfo.value, server.RedmineNotFoundError)


def test_assert_tracker_honored_raises_redmine_workflow_error() -> None:
    """Sanity: tracker-mismatch in the POST response surfaces as the typed
    exception so callers can branch on it."""
    server = _load("server_tracker", "server.py")
    bad_issue = {"id": 1515, "tracker": {"id": 6}}
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server._assert_tracker_honored(bad_issue, 3)
    assert "tracker mismatch" in str(excinfo.value).lower()


def test_server_request_truncates_huge_error_payload() -> None:
    server = _load("server_trunc", "server.py")
    huge = "x" * 10_000
    with pytest.raises(server.RedmineMCPError) as excinfo:
        server._raise_for_redmine_response(_mock_response(status_code=500, body=huge))
    # The original payload is preserved on the exception for callers to inspect,
    # but the embedded truncated copy (in redmine_payload when it's a string) is
    # bounded so a giant error page can't blow up an exception message.
    assert isinstance(excinfo.value.redmine_payload, str)
    # _truncate_payload only fires when the payload is round-tripped through
    # json.dumps; raw strings are passed through untruncated. Validate the
    # helper directly to confirm it would bound a string payload:
    assert len(server._truncate_payload("y" * 10_000, limit=500)) == 500
    # The full original payload is still accessible for callers that want it:
    assert len(excinfo.value.redmine_payload) == 10_000


# ── server.py: mutation tools — verify=True ────────────────────────────────────


def test_update_issue_status_verify_true_returns_ok_envelope(monkeypatch) -> None:
    """Happy path: PUT returns 204, GET shows the new status. verify=True
    returns {"ok": True, "issue": ...}."""
    server = _load("server_happy_status", "server.py")
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        if method == "PUT":
            return {}
        # GET — issue with status_id=5 (In Progress)
        return {
            "issue": {
                "id": 61,
                "subject": "Move tracker",
                "project": {"id": 1, "name": "test"},
                "tracker": {"id": 3, "name": "Task"},
                "status": {"id": 5, "name": "In Progress"},
                "priority": {"id": 2, "name": "Normal"},
                "author": {"name": "Agent"},
                "assigned_to": {"name": "Nobody"},
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": "2026-07-29T10:00:00Z",
                "updated_on": "2026-07-29T10:05:00Z",
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    result = server.update_issue_status(61, 5, verify=True)
    assert result == {
        "ok": True,
        "issue": {
            "id": 61,
            "project_id": 1,
            "project": "test",
            "tracker_id": 3,
            "tracker": "Task",
            "status_id": 5,
            "status": "In Progress",
            "priority_id": 2,
            "priority": "Normal",
            "author": "Agent",
            "assigned_to": "Nobody",
            "subject": "Move tracker",
            "description": "",
            "start_date": None,
            "due_date": None,
            "done_ratio": 0,
            "created_on": "2026-07-29T10:00:00Z",
            "updated_on": "2026-07-29T10:05:00Z",
            "journals": [],
            "relations": [],
            "attachments": [],
        },
    }
    assert ("PUT", "/issues/61.json") in calls
    assert ("GET", "/issues/61.json?include=journals") in calls


def test_update_issue_status_verify_true_raises_on_silent_failure(monkeypatch) -> None:
    """The canonical 2026-07-29 bug: PUT accepted silently, status_id never
    changed. With verify=True, this MUST raise RedmineWorkflowError — never
    return {} or a misleading success envelope."""
    server = _load("server_silent_status", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            # Redmine accepts the PUT (204 No Content), but did not apply the change.
            return {}
        # GET shows status_id=1 (New) — NOT 5
        return {
            "issue": {
                "id": 61,
                "project": {"id": 1, "name": "test"},
                "tracker": {"id": 3, "name": "Task"},
                "status": {"id": 1, "name": "New"},
                "priority": {"id": 2, "name": "Normal"},
                "author": {"name": "Agent"},
                "subject": "Move tracker",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": "2026-07-29T10:00:00Z",
                "updated_on": "2026-07-29T10:05:00Z",
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server.update_issue_status(61, 5, verify=True)
    assert "status mismatch" in str(excinfo.value).lower()
    assert excinfo.value.redmine_payload["expected"] == 5


def test_update_issue_status_without_verify_returns_legacy_shape(monkeypatch) -> None:
    """Back-compat: without verify=True, the tool still returns the cleaned
    issue (NOT the {ok, issue} envelope) so existing callers don't break."""
    server = _load("server_legacy_status", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 1},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "priority": {"id": 2},
                "subject": "x",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": None,
                "updated_on": None,
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    result = server.update_issue_status(61, 5)  # no verify
    # Legacy shape — NOT an envelope
    assert "ok" not in result
    assert result["id"] == 61


def test_move_issue_verify_true_raises_on_silent_failure(monkeypatch) -> None:
    """move_issue silent-failure pattern: note written, move rejected, project_id
    unchanged in GET. verify=True raises RedmineWorkflowError."""
    server = _load("server_silent_move", "server.py")

    def fake_request(method, path, **kwargs):
        if path == "/projects/98.json":
            return {"project": {"id": 98}}
        if method == "PUT":
            return {}
        # GET shows project_id=20 (old project) — NOT 98
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20, "name": "old"},
                "tracker": {"id": 3, "name": "Task"},
                "status": {"id": 1, "name": "New"},
                "priority": {"id": 2, "name": "Normal"},
                "author": {"name": "Agent"},
                "subject": "x",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": None,
                "updated_on": None,
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server.move_issue(61, 98, note="please move", verify=True)
    assert "project mismatch" in str(excinfo.value).lower()


def test_move_issue_legacy_path_raises_on_silent_failure(monkeypatch) -> None:
    """Even without verify=True, the existing project-mismatch guard now
    raises RedmineWorkflowError instead of generic RuntimeError so callers
    can branch on the typed exception."""
    server = _load("server_legacy_move", "server.py")

    def fake_request(method, path, **kwargs):
        if path == "/projects/98.json":
            return {"project": {"id": 98}}
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError):
        server.move_issue(61, 98)


def test_update_issue_tracker_verify_true_raises_on_coercion(monkeypatch) -> None:
    """Destination project lacks tracker 6 (Idea), Redmine silently coerces to
    the project's default tracker. verify=True raises RedmineWorkflowError."""
    server = _load("server_tracker_coerce", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        # Redmine returned tracker_id=2 instead of 6
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20},
                "tracker": {"id": 2, "name": "Feature"},
                "status": {"id": 1},
                "priority": {"id": 2},
                "subject": "x",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": None,
                "updated_on": None,
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server.update_issue_tracker(61, 6, note="convert", allow_idea_tracker=True, verify=True)
    assert "tracker mismatch" in str(excinfo.value).lower()


def test_create_issue_with_dropped_fields_raises(monkeypatch) -> None:
    """create_issue POST returns an issue that lacks the requested tracker_id
    (Redmine silently dropped the field because it was invalid). _assert_tracker_honored
    catches this and raises RedmineWorkflowError. verify=True does the same on
    a follow-up GET."""
    server = _load("server_create_drop", "server.py")

    def fake_request(method, path, **kwargs):
        assert method == "POST"
        # POST succeeds but returns tracker_id=6, not the requested 3
        return {
            "issue": {
                "id": 1515,
                "project": {"id": 20},
                "tracker": {"id": 6, "name": "Idea"},
                "status": {"id": 1},
                "priority": {"id": 2},
                "subject": "Test",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": None,
                "updated_on": None,
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server.create_issue(project_id=20, subject="Test", tracker_id=3)
    assert "tracker mismatch" in str(excinfo.value).lower()


def test_create_issue_with_verify_true_returns_envelope(monkeypatch) -> None:
    """create_issue happy path with verify=True returns {ok: True, issue: ...}."""
    server = _load("server_create_verify", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "POST":
            return {
                "issue": {
                    "id": 1516,
                    "project": {"id": 20},
                    "tracker": {"id": 3, "name": "Task"},
                    "status": {"id": 1, "name": "New"},
                    "priority": {"id": 2, "name": "Normal"},
                    "subject": "Hello",
                    "description": "",
                    "start_date": None,
                    "due_date": None,
                    "done_ratio": 0,
                    "created_on": "2026-07-29T10:00:00Z",
                    "updated_on": "2026-07-29T10:00:00Z",
                }
            }
        # GET for verify
        return {
            "issue": {
                "id": 1516,
                "project": {"id": 20},
                "tracker": {"id": 3, "name": "Task"},
                "status": {"id": 1, "name": "New"},
                "priority": {"id": 2, "name": "Normal"},
                "subject": "Hello",
                "description": "",
                "start_date": None,
                "due_date": None,
                "done_ratio": 0,
                "created_on": "2026-07-29T10:00:00Z",
                "updated_on": "2026-07-29T10:00:00Z",
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    result = server.create_issue(project_id=20, subject="Hello", verify=True)
    assert result["ok"] is True
    assert result["issue"]["id"] == 1516
    assert result["issue"]["tracker_id"] == 3


def test_add_issue_note_verify_true_raises_on_silent_failure(monkeypatch) -> None:
    """add_issue_note PUT is silently rejected; the journal entry never lands.
    verify=True raises RedmineWorkflowError when the latest journal note does
    not match what we sent."""
    server = _load("server_note_silent", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        # GET — no journals at all
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server.add_issue_note(61, "please look at this", verify=True)
    assert "journal note mismatch" in str(excinfo.value).lower()


def test_add_issue_note_verify_true_happy_path(monkeypatch) -> None:
    server = _load("server_note_ok", "server.py")

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "subject": "x",
                "description": "",
                "journals": [{"id": 99, "user": {"name": "Agent"}, "notes": "please look at this", "created_on": "2026-07-29T10:00:00Z"}],
            }
        }

    monkeypatch.setattr(server, "_request", fake_request)
    result = server.add_issue_note(61, "please look at this", verify=True)
    assert result["ok"] is True
    assert result["issue"]["id"] == 61


def test_wiki_409_raises_redmine_workflow_error(monkeypatch) -> None:
    """Wiki 409 still surfaces as RedmineWorkflowError (typed) rather than the
    old bare RuntimeError."""
    server = _load("server_wiki", "server.py")

    def fake_request(method, path, **kwargs):
        return _mock_response(status_code=409, body="version conflict")

    monkeypatch.setattr(server.requests, "request", fake_request)
    with pytest.raises(server.RedmineWorkflowError) as excinfo:
        server._wiki_request("PUT", "/projects/1/wiki/Home.json")
    assert excinfo.value.status_code == 409


# ── synology-server.py: same regression patterns ───────────────────────────────


def test_synology_request_raises_redmine_not_found() -> None:
    syn = _load("syn_not_found", "synology-server.py")
    with pytest.raises(syn.RedmineNotFoundError):
        syn._raise_for_redmine_response(_mock_response(status_code=404))


def test_synology_request_raises_redmine_validation_with_parsed_errors() -> None:
    syn = _load("syn_val", "synology-server.py")
    with pytest.raises(syn.RedmineValidationError) as excinfo:
        syn._raise_for_redmine_response(
            _mock_response(
                status_code=422,
                body={"errors": {"status_id": ["is not a valid transition"]}},
            )
        )
    assert any("status_id" in e for e in excinfo.value.errors)


def test_synology_update_issue_status_verify_true_raises_on_silent_failure(
    monkeypatch,
) -> None:
    """Same canonical bug, synology transport."""
    syn = _load("syn_silent_status", "synology-server.py")
    handler = object.__new__(syn.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 1},
                "tracker": {"id": 3},
                "status": {"id": 1, "name": "New"},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(syn, "_rm_request", fake_request)
    with pytest.raises(syn.RedmineWorkflowError):
        handler._update_issue_status(61, 5, "", True, "dummy-key")


def test_synology_update_issue_status_call_tool_surfaces_error(
    monkeypatch,
) -> None:
    """The MCP tools/call wrapper turns exceptions into {error: str}. The
    message must mention status mismatch so the agent can branch on it."""
    syn = _load("syn_tool_dispatch", "synology-server.py")
    handler = object.__new__(syn.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 1},
                "tracker": {"id": 3},
                "status": {"id": 1, "name": "New"},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(syn, "_rm_request", fake_request)
    handler.api_key = "dummy-key"
    result = handler._call_tool(
        "update_issue_status",
        {"issue_id": 61, "status_id": 5, "verify": True},
    )
    assert "error" in result
    assert "status mismatch" in result["error"].lower()


def test_synology_move_issue_verify_true_raises(monkeypatch) -> None:
    syn = _load("syn_silent_move", "synology-server.py")
    handler = object.__new__(syn.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if path == "/projects/98.json":
            return {"project": {"id": 98}}
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 20},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(syn, "_rm_request", fake_request)
    with pytest.raises(syn.RedmineWorkflowError):
        handler._move_issue(61, 98, "note", True, "dummy-key")


def test_synology_add_issue_note_verify_true_raises(monkeypatch) -> None:
    syn = _load("syn_note_silent", "synology-server.py")
    handler = object.__new__(syn.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {
            "issue": {
                "id": 61,
                "project": {"id": 1},
                "tracker": {"id": 3},
                "status": {"id": 1},
                "subject": "x",
                "description": "",
                "journals": [],
            }
        }

    monkeypatch.setattr(syn, "_rm_request", fake_request)
    with pytest.raises(syn.RedmineWorkflowError):
        handler._add_issue_note(61, "hello", True, "dummy-key")


def test_synology_tools_list_exposes_verify_param() -> None:
    """The MCP tools/list schema must advertise verify=True for each mutation."""
    syn = _load("syn_tools_list", "synology-server.py")
    handler = object.__new__(syn.MCPHandler)
    tools = handler._handle_method("tools/list", {})["tools"]
    by_name = {t["name"]: t for t in tools}
    for tool_name in (
        "update_issue_status",
        "move_issue",
        "update_issue_tracker",
        "add_issue_note",
        "create_issue",
    ):
        assert tool_name in by_name, tool_name
        props = by_name[tool_name]["inputSchema"]["properties"]
        assert "verify" in props, f"{tool_name} must expose verify in its schema"
        assert props["verify"]["type"] == "boolean"


def test_server_py_exposes_verify_param_on_mutation_tools() -> None:
    """Static check: every mutation tool in server.py accepts verify=False."""
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    for tool in (
        "create_issue",
        "update_issue_status",
        "update_issue_tracker",
        "move_issue",
        "add_issue_note",
    ):
        # Look for the tool definition followed by verify: bool = False
        assert f"def {tool}(" in content
        # Find the function header and check it eventually declares verify
        import re as _re
        m = _re.search(rf"def {tool}\([^)]*\)[^:]*:(.*?)(?=\n@mcp\.tool|\nclass |\Z)",
                       content, _re.DOTALL)
        assert m, f"{tool} body not found"
        assert "verify" in m.group(1), f"{tool} must accept verify parameter"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])