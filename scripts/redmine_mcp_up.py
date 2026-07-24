#!/usr/bin/env python3
"""Launch the Redmine MCP sidecar after resolving its API key from CAISS Keyring."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = Path("/Users/jeffreycruz/Development/agent-zero-data")
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEFAULT_KEYRING_CLI = "/Users/jeffreycruz/.caiss/hatch/scripts/caiss-keyring-host"


def load_env_file(path: Path) -> dict[str, str]:
    """Load a simple KEY=VALUE env file."""
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        env[key.strip()] = value
    return env


def default_env_file(instance: str) -> Path:
    """Return the instance-specific env file path."""
    return DEFAULT_DATA_ROOT / instance / "redmine-mcp.env"


def resolve_redmine_api_key(env: dict[str, str], *, runner=subprocess.run) -> str:
    """Resolve REDMINE_API_KEY_REF through CAISS Keyring."""
    secret_ref = env.get("REDMINE_API_KEY_REF", "").strip()
    if not secret_ref:
        raise RuntimeError("REDMINE_API_KEY_REF is required")

    agent_id = env.get("REDMINE_AGENT_ID", "").strip()
    if not agent_id:
        raise RuntimeError("REDMINE_AGENT_ID is required")

    keyring_cli = env.get("CAISS_KEYRING_CLI", "").strip()
    if not keyring_cli:
        keyring_cli = DEFAULT_KEYRING_CLI if Path(DEFAULT_KEYRING_CLI).exists() else ""
    if not keyring_cli:
        keyring_cli = shutil.which("caiss-keyring-host") or shutil.which("caiss-keyring") or ""
    if not keyring_cli:
        raise RuntimeError("CAISS_KEYRING_CLI or caiss-keyring on PATH is required")

    result = runner(
        [
            keyring_cli,
            "read-secret",
            "--secret-ref",
            secret_ref,
            "--agent-id",
            agent_id,
            "--actor",
            "redmine-mcp",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"failed to resolve REDMINE_API_KEY_REF {secret_ref}"
            + (f": {detail}" if detail else "")
        )

    secret = (result.stdout or "").strip()
    if not secret:
        raise RuntimeError(f"failed to resolve REDMINE_API_KEY_REF {secret_ref}: empty secret")
    return secret


def compose_env(base_env: dict[str, str], redmine_env: dict[str, str], api_key: str, instance: str) -> dict[str, str]:
    """Build the environment used for docker compose."""
    env = base_env.copy()
    env.update(redmine_env)
    env["REDMINE_API_KEY"] = api_key
    env["CONTAINER_NAME"] = f"redmine-mcp-{instance}"
    env["COMPOSE_PROJECT_NAME"] = f"redmine-mcp-{instance}"
    return env


def run_compose(*, compose_file: Path, compose_env: dict[str, str], runner=subprocess.run) -> subprocess.CompletedProcess[Any]:
    """Run docker compose with the resolved environment."""
    return runner(
        [
            "docker",
            "--context",
            compose_env.get("DOCKER_CONTEXT", "orbstack"),
            "compose",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--build",
            "--force-recreate",
        ],
        cwd=str(compose_file.parent),
        env=compose_env,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    """Launch the sidecar for a specific instance."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="forge", help="Agent Zero instance name")
    parser.add_argument("--env-file", type=Path, help="Override the instance env file path")
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    env_file = args.env_file or default_env_file(args.instance)
    if not env_file.exists():
        print(f"error: env file not found: {env_file}", file=sys.stderr)
        return 1

    redmine_env = load_env_file(env_file)

    if args.dry_run:
        print(f"instance={args.instance}")
        print(f"env_file={env_file}")
        print(f"container_name=redmine-mcp-{args.instance}")
        print(f"compose_project=redmine-mcp-{args.instance}")
        print(f"resolved_ref={redmine_env.get('REDMINE_API_KEY_REF', '')}")
        return 0

    api_key = resolve_redmine_api_key(redmine_env)
    env = compose_env(os.environ, redmine_env, api_key, args.instance)

    result = run_compose(compose_file=args.compose_file, compose_env=env)
    if result.returncode != 0:
        print("error: docker compose up failed", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
