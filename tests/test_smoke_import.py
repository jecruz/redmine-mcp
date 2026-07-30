"""Smoke test: both server modules import cleanly with required env vars."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("REDMINE_URL", "http://example.invalid")
os.environ.setdefault("REDMINE_API_KEY", "dummy-key-for-import-test")


def test_server_py_imports() -> None:
    spec = importlib.util.spec_from_file_location(
        "server", REPO_ROOT / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Sanity check the new exception hierarchy is exposed
    assert module.RedmineMCPError is not None
    assert module.RedminePermissionError.__bases__ == (module.RedmineMCPError,)
    assert module.RedmineNotFoundError.__bases__ == (module.RedmineMCPError,)
    assert module.RedmineWorkflowError.__bases__ == (module.RedmineMCPError,)
    assert module.RedmineValidationError.__bases__ == (module.RedmineMCPError,)
    # Validation error carries an errors list attribute
    ve = module.RedmineValidationError("oops", errors=["a", "b"])
    assert ve.errors == ["a", "b"]
    assert ve.status_code is None
    ve2 = module.RedmineValidationError("oops", errors=["a"], status_code=422)
    assert ve2.status_code == 422
    # Base error carries status_code and redmine_payload
    base = module.RedmineMCPError("oops", status_code=500, redmine_payload={"x": 1})
    assert base.status_code == 500
    assert base.redmine_payload == {"x": 1}


def test_synology_server_imports() -> None:
    spec = importlib.util.spec_from_file_location(
        "synology_server", REPO_ROOT / "synology-server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.RedmineMCPError is not None
    assert module.RedmineValidationError.__bases__ == (module.RedmineMCPError,)