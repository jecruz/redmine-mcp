# Redmine MCP Sidecar

Provides MCP protocol interface to Redmine for Agent Zero.

Launch the sidecar with:

```bash
cd /Users/jeffreycruz/Development/AI_TOOLS/redmine-mcp
python3 scripts/redmine_mcp_up.py --instance forge
```

Instance env files live under `/Users/jeffreycruz/Development/agent-zero-data/<instance>/redmine-mcp.env`.
They carry `REDMINE_API_KEY_REF`, `REDMINE_AGENT_ID`, and `CAISS_KEYRING_CLI`,
not plaintext API keys.

Document creation is supported through Redmine's HTML form when web login
credentials or a session cookie are provided in the environment.

Configuration also supports `REQUEST_TIMEOUT` (default `30`) and
`MAX_ATTACHMENT_BYTES` (default `52428800`). The launcher resolves
`REDMINE_API_KEY_REF` through CAISS Keyring and injects the key only into the
running container.

The container pins the MCP Python SDK to `1.26.0`; the Docker build includes an
import smoke check so an incompatible upstream SDK cannot reach the runtime.

The MCP surface includes issue/project discovery and mutation, unified
`update_issue`, compatibility wrappers (`update_issue_status`, `move_issue`,
and `update_issue_tracker`), notes, memberships, priorities, trackers,
categories, attachments, wiki pages, project search/resolution, documents,
project creation, and guarded issue deletion.

Mutations surface typed Redmine failures rather than misleading success:
permission, not-found, validation, and workflow errors carry the HTTP status
and bounded diagnostic payload. Compatibility mutations accept `verify=true`
for an explicit `{ok, issue}` result; `update_issue` always reads back and
checks requested persisted fields. Issue deletion requires exactly one of
`dry_run=true` or `confirm=true` and records an audit entry on success.
