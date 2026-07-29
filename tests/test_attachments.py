"""Tests for Redmine issue attachment tools."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_synology_server():
    spec = importlib.util.spec_from_file_location("synology_server", REPO_ROOT / "synology-server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fastmcp_server_exposes_attachment_tools() -> None:
    content = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    assert "def add_issue_attachment(" in content
    assert "def list_issue_attachments(" in content
    assert "def _upload_file(" in content
    assert "/uploads.json" in content
    # Verify the upload token flow: uploads key inside issue JSON
    assert '"uploads"' in content
    assert '"token"' in content


def test_synology_tools_list_exposes_attachment_tools() -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    tools = handler._handle_method("tools/list", {})["tools"]
    names = {tool["name"] for tool in tools}

    assert "add_issue_attachment" in names
    assert "list_issue_attachments" in names


def test_synology_add_issue_attachment_dispatch(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)
    calls = []

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as tf:
        tf.write("test content")

    def fake_upload(file_path, api_key):
        calls.append({"fn": "upload", "file_path": file_path})
        return {"token": "abc123", "filename": "test.txt", "content_type": "text/plain"}

    def fake_request(method, path, api_key, **kwargs):
        calls.append({"fn": "request", "method": method, "path": path, "json": kwargs.get("json")})

    monkeypatch.setattr(module, "_rm_upload_file", fake_upload)
    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool(
        "add_issue_attachment",
        {
            "issue_id": 1432,
            "file_path": tf.name,
            "description": "Test attachment",
            "content_type": "text/plain",
        },
    )

    assert calls[0] == {"fn": "upload", "file_path": tf.name}
    assert calls[1]["fn"] == "request"
    assert calls[1]["path"] == "/issues/1432.json"
    assert calls[1]["json"]["issue"]["uploads"][0]["token"] == "abc123"
    assert calls[1]["json"]["issue"]["notes"] == "Test attachment"
    assert result["issue_id"] == 1432
    assert result["filename"] == "test.txt"

    Path(tf.name).unlink(missing_ok=True)


def test_synology_list_issue_attachments_dispatch(monkeypatch) -> None:
    module = _load_synology_server()
    handler = object.__new__(module.MCPHandler)

    def fake_request(method, path, api_key, **kwargs):
        return {
            "issue": {
                "id": 1432,
                "attachments": [
                    {
                        "id": 1,
                        "filename": "screenshot.png",
                        "filesize": 12345,
                        "content_type": "image/png",
                        "description": "Bug screenshot",
                        "content_url": "http://redmine/attachments/1",
                        "author": {"name": "John"},
                        "created_on": "2025-01-15T10:00:00Z",
                    }
                ],
            }
        }

    monkeypatch.setattr(module, "_rm_request", fake_request)

    result = handler._call_tool("list_issue_attachments", {"issue_id": 1432})

    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["filename"] == "screenshot.png"
    assert result[0]["filesize"] == 12345
    assert result[0]["content_url"] == "http://redmine/attachments/1"


def test_upload_file_rejects_missing_file() -> None:
    module = _load_synology_server()
    import pytest

    with pytest.raises(FileNotFoundError):
        module._rm_upload_file("/nonexistent/path/file.txt", "fake-key")


def test_upload_file_rejects_empty_file(monkeypatch) -> None:
    module = _load_synology_server()
    import pytest

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        pass  # create empty file

    try:
        with pytest.raises(ValueError, match="Cannot upload empty file"):
            module._rm_upload_file(tf.name, "fake-key")
    finally:
        Path(tf.name).unlink(missing_ok=True)
