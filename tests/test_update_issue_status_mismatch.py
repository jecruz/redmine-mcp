"""Tests for issue #1560: update_issue_status must verify the persisted status.

Background: Redmine PUT /issues/{id}.json returns 204 No Content. The previous
implementation returned the PUT response body — which is `{}` — so callers got
an empty dict even when the write succeeded. The fix:

1. Discard the PUT response.
2. Re-fetch the issue via GET /issues/{id}.json.
3. Verify the re-fetched status_id matches the requested status_id.
4. Raise a RuntimeError("...status mismatch: requested status_id X, Redmine
   returned status_id Y") when Redmine reports a different status.

These tests cover both the FastMCP server (server.py) and the Synology JSON-RPC
variant (synology-server.py).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

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


# ---------------------------------------------------------------------------
# server.py — FastMCP variant
# ---------------------------------------------------------------------------


def test_server_update_issue_status_verifies_redmine_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: PUT followed by GET; returned status_id matches the request."""
    module = _load_server()
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        if method == "PUT":
            return {}  # 204 No Content — body is empty
        # GET re-fetch + the final get_issue(include="journals") call.
        return {
            "issue": {
                "id": 1059,
                "status": {"id": 5, "name": "Closed"},
                "project": {"id": 20, "name": "Demo"},
                "tracker": {"id": 3, "name": "Task"},
                "journals": [],
            }
        }

    monkeypatch.setattr(module, "_request", fake_request)

    result = module.update_issue_status(issue_id=1059, status_id=5, note="done")

    # PUT goes out, then GET verifies, then get_issue(include="journals") returns.
    assert calls[0] == ("PUT", "/issues/1059.json")
    assert calls[1] == ("GET", "/issues/1059.json")
    # The get_issue call uses the include=journals URL form.
    assert any("/issues/1059.json" in path for _, path in calls[2:])
    assert result["status_id"] == 5


def test_server_update_issue_status_raises_status_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the re-fetched status_id differs from the requested one, raise."""
    module = _load_server()

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            return {}  # pretended the write succeeded
        # Redmine 204'd but the GET says status is still 3 (In Progress).
        return {"issue": {"id": 1059, "status": {"id": 3, "name": "In Progress"}}}

    monkeypatch.setattr(module, "_request", fake_request)

    with pytest.raises(RuntimeError, match="status mismatch"):
        module.update_issue_status(issue_id=1059, status_id=5)


def test_server_update_issue_status_sends_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note argument is forwarded to Redmine as `notes` in the PUT body."""
    module = _load_server()
    captured: dict[str, dict[str, object]] = {}

    def fake_request(method, path, **kwargs):
        if method == "PUT":
            captured["body"] = kwargs.get("json") or {}
            return {}
        return {"issue": {"id": 1059, "status": {"id": 5}}}

    monkeypatch.setattr(module, "_request", fake_request)

    module.update_issue_status(issue_id=1059, status_id=5, note="Closed by bot")

    assert captured["body"] == {"issue": {"status_id": 5, "notes": "Closed by bot"}}


# ---------------------------------------------------------------------------
# synology-server.py — manual JSON-RPC variant
# ---------------------------------------------------------------------------


def test_synology_update_issue_status_verifies_redmine_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: PUT + verify GET + return the cleaned issue."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append((method, path))
        if method == "PUT":
            return {}
        # The verify-GET and the final _get_issue(... journals ...) both hit
        # /issues/{id}.json; the second one asks for include=journals.
        return {
            "issue": {
                "id": 1059,
                "status": {"id": 5, "name": "Closed"},
                "project": {"id": 20, "name": "Demo"},
                "journals": [],
            }
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "update_issue_status",
        {"issue_id": 1059, "status_id": 5, "note": "Ship it"},
    )

    assert calls[0] == ("PUT", "/issues/1059.json")
    assert calls[1] == ("GET", "/issues/1059.json")
    assert result["status_id"] == 5


def test_synology_update_issue_status_raises_status_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the re-fetched status_id differs, surface a STATUS_MISMATCH error."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            return {}
        return {"issue": {"id": 1059, "status": {"id": 3, "name": "In Progress"}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "update_issue_status",
        {"issue_id": 1059, "status_id": 5},
    )

    # The handler surfaces RuntimeError as a JSON-RPC error envelope.
    assert isinstance(result, dict)
    assert "error" in result
    assert "status mismatch" in result["error"]
    assert "status_id 5" in result["error"]
    assert "status_id 3" in result["error"]


def test_synology_update_issue_status_sends_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `note` argument is forwarded as `notes` in the PUT body."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    captured: dict[str, dict[str, object]] = {}

    def fake_request(method, path, api_key, **kwargs):
        if method == "PUT":
            captured["body"] = kwargs.get("json") or {}
            return {}
        return {"issue": {"id": 1059, "status": {"id": 5}}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    handler._call_tool(
        "update_issue_status",
        {"issue_id": 1059, "status_id": 5, "note": "Closed by bot"},
    )

    assert captured["body"] == {"issue": {"status_id": 5, "notes": "Closed by bot"}}


def test_synology_tools_list_still_exposes_update_issue_status() -> None:
    """The DISPATCH wiring still mentions update_issue_status."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}
    assert "update_issue_status" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
