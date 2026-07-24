"""Tests for the Redmine MCP host launcher."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from scripts import redmine_mcp_up
from scripts.redmine_mcp_up import compose_env, default_env_file, load_env_file, resolve_redmine_api_key


def test_load_env_file_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / "redmine-mcp.env"
    env_file.write_text(
        "\n".join(
            [
                'REDMINE_URL="http://10.0.0.23:8085"',
                'REDMINE_API_KEY_REF="keychain://caiss/redmine/forge"',
                "REDMINE_AGENT_ID=Forge",
            ]
        ),
        encoding="utf-8",
    )

    env = load_env_file(env_file)

    assert env["REDMINE_URL"] == "http://10.0.0.23:8085"
    assert env["REDMINE_API_KEY_REF"] == "keychain://caiss/redmine/forge"
    assert env["REDMINE_AGENT_ID"] == "Forge"


def test_default_env_file_points_to_instance_root() -> None:
    assert default_env_file("blitz") == Path("/Users/jeffreycruz/Development/agent-zero-data/blitz/redmine-mcp.env")


def test_resolve_redmine_api_key_uses_keyring_cli() -> None:
    seen: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return CompletedProcess(command, 0, stdout="resolved-secret\n", stderr="")

    secret = resolve_redmine_api_key(
        {
            "REDMINE_API_KEY_REF": "keychain://caiss/redmine/forge",
            "REDMINE_AGENT_ID": "Forge",
            "CAISS_KEYRING_CLI": "/usr/local/bin/caiss-keyring",
        },
        runner=fake_run,
    )

    assert secret == "resolved-secret"
    assert seen["command"] == [
        "/usr/local/bin/caiss-keyring",
        "read-secret",
        "--secret-ref",
        "keychain://caiss/redmine/forge",
        "--agent-id",
        "Forge",
        "--actor",
        "redmine-mcp",
    ]


def test_compose_env_sets_container_name_and_project() -> None:
    env = compose_env(
        {"PATH": "/usr/bin"},
        {
            "REDMINE_URL": "http://10.0.0.23:8085",
            "REDMINE_API_KEY_REF": "keychain://caiss/redmine/forge",
            "REDMINE_AGENT_ID": "Forge",
        },
        "resolved-secret",
        "forge",
    )

    assert env["REDMINE_API_KEY"] == "resolved-secret"
    assert env["CONTAINER_NAME"] == "redmine-mcp-forge"
    assert env["COMPOSE_PROJECT_NAME"] == "redmine-mcp-forge"


def test_dry_run_does_not_resolve_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = tmp_path / "redmine-mcp.env"
    env_file.write_text(
        "\n".join(
            [
                "REDMINE_URL=http://10.0.0.23:8085",
                "REDMINE_API_KEY_REF=keychain://caiss/redmine/forge",
                "REDMINE_AGENT_ID=Forge",
            ]
        ),
        encoding="utf-8",
    )

    def fail_resolve(env):
        raise AssertionError("dry-run should not resolve secrets")

    monkeypatch.setattr(redmine_mcp_up, "resolve_redmine_api_key", fail_resolve)

    result = redmine_mcp_up.main(["--instance", "forge", "--env-file", str(env_file), "--dry-run"])

    output = capsys.readouterr().out
    assert result == 0
    assert "container_name=redmine-mcp-forge" in output
    assert "resolved_ref=keychain://caiss/redmine/forge" in output
