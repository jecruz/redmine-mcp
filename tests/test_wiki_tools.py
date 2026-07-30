"""Tests for Redmine wiki page tools."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_synology_server():
    spec = importlib.util.spec_from_file_location("synology_server", REPO_ROOT / "synology-server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── server.py static analysis ──


def test_fastmcp_server_exposes_wiki_tools() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    assert "def list_wiki_pages(" in content
    assert "def get_wiki_page(" in content
    assert "def create_wiki_page(" in content
    assert "def update_wiki_page(" in content
    assert "def _wiki_request(" in content
    assert "status_code == 409" in content
    assert "version conflict" in content.lower()


def test_fastmcp_wiki_tools_are_decorated() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    for tool_name in ("list_wiki_pages", "get_wiki_page", "create_wiki_page", "update_wiki_page"):
        pattern = f"@mcp.tool()\ndef {tool_name}("
        assert pattern in content, f"{tool_name} must be decorated with @mcp.tool()"


# ── synology-server.py static analysis ──


def test_synology_server_exposes_wiki_request_helper() -> None:
    content = (REPO_ROOT / "synology-server.py").read_text(encoding="utf-8")

    assert "def _rm_wiki_request(" in content
    assert "status_code == 409" in content


def test_synology_tools_list_exposes_wiki_tools() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}

    assert "list_wiki_pages" in names
    assert "get_wiki_page" in names
    assert "create_wiki_page" in names
    assert "update_wiki_page" in names


def test_synology_server_has_wiki_method_implementations() -> None:
    content = (REPO_ROOT / "synology-server.py").read_text(encoding="utf-8")

    assert "def _list_wiki_pages(" in content
    assert "def _get_wiki_page(" in content
    assert "def _create_wiki_page(" in content
    assert "def _update_wiki_page(" in content


# ── list_wiki_pages ──


def test_synology_list_wiki_pages_calls_correct_endpoint(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path})
        return {"wiki_pages": [{"title": "Home", "version": 1}]}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_wiki_pages", {"project_id": 97})

    assert calls == [{"method": "GET", "path": "/projects/97/wiki/index.json"}]
    assert result == [{"title": "Home", "version": 1}]


# ── get_wiki_page ──


def test_synology_get_wiki_page_no_version(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params", {})})
        return {"wiki_page": {"title": "Home", "text": "Welcome", "version": 1}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "get_wiki_page", {"project_id": 97, "title": "Home"}
    )

    assert calls[0]["params"] == {}
    assert result["title"] == "Home"
    assert result["version"] == 1


def test_synology_get_wiki_page_with_version(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params", {})})
        return {"wiki_page": {"title": "Home", "text": "Old", "version": 1}}

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "get_wiki_page", {"project_id": 97, "title": "Home", "version": 1}
    )

    assert calls[0]["params"] == {"version": 1}
    assert result["version"] == 1


# ── create_wiki_page ──


def test_synology_create_wiki_page_simple(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return {"wiki_page": {"title": "ADRs", "text": "# ADRs", "version": 1}}

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    result = handler._call_tool(
        "create_wiki_page",
        {"project_id": 97, "title": "ADRs", "text": "# ADRs"},
    )

    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"] == "/projects/97/wiki/ADRs.json"
    assert calls[0]["json"]["wiki_page"] == {"text": "# ADRs", "comments": ""}
    assert result["version"] == 1


def test_synology_create_wiki_page_with_parent(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return {"wiki_page": {"title": "ADRs", "text": "# ADRs", "version": 1, "parent": {"title": "Home"}}}

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    handler._call_tool(
        "create_wiki_page",
        {"project_id": 97, "title": "ADRs", "text": "# ADRs", "parent_title": "Home", "comments": "Initial"},
    )

    assert calls[0]["json"]["wiki_page"] == {
        "text": "# ADRs",
        "comments": "Initial",
        "parent_title": "Home",
    }


def test_synology_create_wiki_page_409_returns_error(monkeypatch) -> None:
    """When Redmine returns 409, the wiki_request wrapper should raise a RuntimeError."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        from unittest.mock import Mock
        response = Mock()
        response.status_code = 409
        raise RuntimeError(
            "Wiki page version conflict: the page has been modified since you last read it. "
            "Re-read the page and retry with the current version."
        )

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    result = handler._call_tool(
        "create_wiki_page",
        {"project_id": 97, "title": "Conflict", "text": "test"},
    )

    assert "version conflict" in result["error"]


# ── update_wiki_page ──


def test_synology_update_wiki_page_no_version(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return {"wiki_page": {"title": "ADRs", "text": "Updated", "version": 2}}

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    result = handler._call_tool(
        "update_wiki_page",
        {"project_id": 97, "title": "ADRs", "text": "Updated", "comments": "fix typo"},
    )

    assert calls[0]["json"]["wiki_page"] == {"text": "Updated", "comments": "fix typo"}
    assert result["version"] == 2


def test_synology_update_wiki_page_with_version(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return {"wiki_page": {"title": "ADRs", "text": "Updated", "version": 3}}

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    handler._call_tool(
        "update_wiki_page",
        {"project_id": 97, "title": "ADRs", "text": "Updated", "version": 2, "comments": "v3"},
    )

    assert calls[0]["json"]["wiki_page"] == {
        "text": "Updated",
        "comments": "v3",
        "version": 2,
    }


def test_synology_update_wiki_page_409_returns_error(monkeypatch) -> None:
    """When Redmine returns 409 on update, it should surface as a RuntimeError."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        raise RuntimeError(
            "Wiki page version conflict: the page has been modified since you last read it. "
            "Re-read the page and retry with the current version."
        )

    monkeypatch.setattr(module, "_rm_wiki_request", fake_request)

    result = handler._call_tool(
        "update_wiki_page",
        {"project_id": 97, "title": "Conflict", "text": "test", "version": 1},
    )

    assert "version conflict" in result["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
