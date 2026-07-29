"""Sanity check that the FastMCP server registers all mutation tools with the
new verify parameter in their schema, and that the MCP tool's JSON schema
advertises verify=True as a boolean property for callers."""
from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path

os.environ.setdefault("REDMINE_URL", "http://example.invalid")
os.environ.setdefault("REDMINE_API_KEY", "stub")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "server_for_inspect", REPO_ROOT / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MUTATION_TOOLS = {
    "create_issue",
    "update_issue_status",
    "update_issue_tracker",
    "move_issue",
    "add_issue_note",
}


def test_fastmcp_mutation_tools_have_verify_parameter() -> None:
    module = _load_server()
    mgr = module.mcp._tool_manager
    tool_names = set(mgr._tools.keys())
    assert MUTATION_TOOLS.issubset(tool_names), (
        f"Missing tools: {MUTATION_TOOLS - tool_names}"
    )
    for tool_name in MUTATION_TOOLS:
        tool_obj = mgr._tools[tool_name]
        fn = getattr(tool_obj, "fn", tool_obj)
        sig = inspect.signature(fn)
        assert "verify" in sig.parameters, (
            f"{tool_name} missing verify parameter"
        )
        param = sig.parameters["verify"]
        assert param.default is False, (
            f"{tool_name}.verify default must be False, got {param.default!r}"
        )


def test_fastmcp_mutation_tools_advertise_verify_in_schema() -> None:
    """FastMCP generates an inputSchema from the type annotations; verify must
    appear as a boolean property for MCP clients to discover it."""
    module = _load_server()
    mgr = module.mcp._tool_manager
    for tool_name in MUTATION_TOOLS:
        tool_obj = mgr._tools[tool_name]
        # FastMCP exposes the generated JSON schema on .parameters or .input_schema
        schema = getattr(tool_obj, "parameters", None) or getattr(
            tool_obj, "input_schema", None
        ) or getattr(tool_obj, "inputSchema", None)
        assert schema is not None, f"{tool_name} has no discoverable schema"
        props = schema.get("properties") if isinstance(schema, dict) else None
        if props is None and hasattr(schema, "get"):
            try:
                props = schema.get("properties")
            except Exception:
                props = None
        if props is None:
            # schema is a pydantic model; iterate annotations instead
            sig = inspect.signature(tool_obj.fn if hasattr(tool_obj, "fn") else tool_obj)
            assert "verify" in sig.parameters
            continue
        assert "verify" in props, (
            f"{tool_name} inputSchema.properties missing 'verify'"
        )
        assert props["verify"].get("type") == "boolean", (
            f"{tool_name}.verify must be typed as boolean"
        )


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])