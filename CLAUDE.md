# Redmine MCP

Agnostic MCP server providing a Model Context Protocol interface to Redmine's REST API.

## Quick Start

```bash
cd /Users/jeffreycruz/Development/AI_TOOLS/redmine-mcp
docker --context orbstack compose up -d
```

## Configuration

Environment file at `/Users/jeffreycruz/Development/agent-zero-data/redmine-mcp.env`:
- `REDMINE_URL` — Redmine instance URL (e.g. http://10.0.0.23:8085)
- `REDMINE_API_KEY` — Redmine API key
- `REDMINE_WEB_USERNAME` — Optional Redmine web login for document creation
- `REDMINE_WEB_PASSWORD` — Optional Redmine web password for document creation
- `REDMINE_WEB_SESSION_COOKIE` — Optional semicolon-delimited cookie string for document creation
- `MCP_HOST` — Host to bind (default: 0.0.0.0)
- `MCP_PORT` — Port to bind (default: 8080)
- `MCP_PATH` — SSE path (default: /sse)
- `REQUEST_TIMEOUT` — HTTP timeout in seconds (default: 30)

## Available Tools

- `get_current_user` — Return the Redmine user for the configured API key
- `list_projects` — List visible Redmine projects
- `list_issues` — List issues with optional filters (project_id, assigned_to_id, status_id, tracker_id)
- `get_issue` — Fetch a single issue with journals, attachments, relations
- `create_issue` — Create a new Redmine issue
- `update_issue_status` — Update issue status and optionally add a note
- `add_issue_note` — Append a journal note to an issue
- `create_document` — Create a project document through the Redmine HTML form (not available via JSON REST API — uses web session auth with REDMINE_WEB_USERNAME/REDMINE_WEB_PASSWORD or REDMINE_WEB_SESSION_COOKIE)

## Networks

Connects to `memory_default` and `redmine-mcp_default` for container-to-container communication.

## Document Creation

The Redmine JSON REST API does not expose documents (`/documents.json` returns 403). The `create_document` tool works around this by automating the HTML form flow:

1. Resolves numeric `project_id` → project `identifier` via the JSON API
2. Authenticates a web session (login form or session cookie)
3. Fetches the document form page to extract the CSRF authenticity token
4. Detects the default document category from the form if `category_id` is not supplied
5. POSTs the form fields to create the document

Requires web session auth configured via env vars:
- `REDMINE_WEB_USERNAME` + `REDMINE_WEB_PASSWORD` — login credentials
- `REDMINE_WEB_SESSION_COOKIE` — semicolon-delimited cookie string (e.g., `_redmine_session=...`)
