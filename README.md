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
