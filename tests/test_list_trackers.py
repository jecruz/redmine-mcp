"""Tests for Redmine list_trackers tool."""

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


def test_fastmcp_server_exposes_list_trackers_tool() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    assert "def list_trackers(" in content
    assert '"trackers"' in content
    assert "/trackers.json" in content


def test_synology_tools_list_exposes_list_trackers_tool() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}

    assert "list_trackers" in names


def test_synology_list_trackers_returns_all_when_no_project_id(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if path == "/trackers.json":
            return {
                "trackers": [
                    {"id": 1, "name": "Bug"},
                    {"id": 2, "name": "Feature"},
                    {"id": 3, "name": "Task"},
                    {"id": 6, "name": "Idea"},
                ]
            }
        raise RuntimeError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_trackers", {})

    assert result["total_count"] == 4
    assert result["trackers"] == [
        {"id": 1, "name": "Bug"},
        {"id": 2, "name": "Feature"},
        {"id": 3, "name": "Task"},
        {"id": 6, "name": "Idea"},
    ]


def test_synology_list_trackers_filters_by_project_id(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"method": method, "path": path})
        if path == "/trackers.json":
            return {
                "trackers": [
                    {"id": 1, "name": "Bug"},
                    {"id": 2, "name": "Feature"},
                    {"id": 3, "name": "Task"},
                    {"id": 6, "name": "Idea"},
                ]
            }
        if path == "/projects/70.json?include=trackers":
            return {
                "project": {
                    "id": 70,
                    "trackers": [
                        {"id": 1, "name": "Bug"},
                        {"id": 2, "name": "Feature"},
                    ],
                }
            }
        raise RuntimeError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_trackers", {"project_id": 70})

    # Verify both API calls were made
    assert {"method": "GET", "path": "/trackers.json"} in calls
    assert {"method": "GET", "path": "/projects/70.json?include=trackers"} in calls

    # Verify filtered result (only Bug and Feature, no Task or Idea)
    assert result["total_count"] == 2
    assert result["trackers"] == [
        {"id": 1, "name": "Bug"},
        {"id": 2, "name": "Feature"},
    ]


def test_synology_list_trackers_project_with_few_trackers(monkeypatch) -> None:
    """Verify that a project that only has [Bug, Feature] excludes Idea (tracker 6)."""
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        if path == "/trackers.json":
            return {
                "trackers": [
                    {"id": 1, "name": "Bug"},
                    {"id": 2, "name": "Feature"},
                    {"id": 3, "name": "Task"},
                    {"id": 6, "name": "Idea"},
                ]
            }
        if path == "/projects/70.json?include=trackers":
            return {
                "project": {
                    "id": 70,
                    "trackers": [
                        {"id": 1, "name": "Bug"},
                        {"id": 2, "name": "Feature"},
                    ],
                }
            }
        raise RuntimeError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_trackers", {"project_id": 70})

    tracker_ids = {t["id"] for t in result["trackers"]}
    assert 6 not in tracker_ids, "Idea tracker should be excluded for project 70"
    assert tracker_ids == {1, 2}
