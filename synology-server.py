#!/usr/bin/env python3
"""Redmine MCP Server — SSE + direct POST dual-mode (threaded)"""
import json, os, sys, threading, queue, re
from datetime import datetime, timezone
import json, os, sys, threading, queue, re, mimetypes
from pathlib import Path
from html import unescape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
import requests

REDMINE_URL = os.getenv("REDMINE_URL", "http://10.0.0.23:8085").rstrip("/")
DEFAULT_REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "")
PORT = int(os.getenv("PORT", "8095"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
AGENT_TASK_TRACKER_ID = 3
IDEA_TRACKER_ID = 6
REDMINE_AGENT_ID = os.getenv("REDMINE_AGENT_ID", "")
DELETE_AUDIT_LOG_PATH = Path(
    os.getenv(
        "REDMINE_DELETE_AUDIT_LOG",
        str(Path.home() / ".hermes" / "logs" / "redmine-mcp-deletes.log"),
    )
)

REDMINE_WEB_USERNAME = os.getenv("REDMINE_WEB_USERNAME", "")
REDMINE_WEB_PASSWORD = os.getenv("REDMINE_WEB_PASSWORD", "")
REDMINE_WEB_SESSION_COOKIE = os.getenv("REDMINE_WEB_SESSION_COOKIE", "")

_sse_queues = []
_sse_lock = threading.Lock()

def log(msg):
    print(f"[redmine-mcp] {msg}", file=sys.stderr, flush=True)

def _register_sse():
    q = queue.Queue()
    with _sse_lock:
        _sse_queues.append(q)
    log(f"SSE client registered (total: {len(_sse_queues)})")
    return q

def _unregister_sse(q):
    with _sse_lock:
        if q in _sse_queues:
            _sse_queues.remove(q)
    log(f"SSE client unregistered (total: {len(_sse_queues)})")

def _has_sse_clients():
    with _sse_lock:
        return len(_sse_queues) > 0

def _broadcast(msg):
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

def _rm_headers(api_key):
    return {"X-Redmine-API-Key": api_key, "Content-Type": "application/json"}

def _rm_request(method, path, api_key, **kwargs):
    url = f"{REDMINE_URL}{path}"
    r = requests.request(method, url, headers=_rm_headers(api_key), timeout=REQUEST_TIMEOUT, **kwargs)
    try:
        r.raise_for_status()
    except Exception:
        log(f"Redmine error: {r.status_code} {r.text[:300]}")
        raise
    return r.json() if r.content else {}


def _rm_wiki_request(method, path, api_key, **kwargs):
    """Like _rm_request but surfaces HTTP 409 for version-conflict detection."""
    url = f"{REDMINE_URL}{path}"
    r = requests.request(method, url, headers=_rm_headers(api_key), timeout=REQUEST_TIMEOUT, **kwargs)
    if r.status_code == 409:
        raise RuntimeError(
            "Wiki page version conflict: the page has been modified since you last read it. "
            "Re-read the page and retry with the current version."
        )
    try:
        r.raise_for_status()
    except Exception:
        log(f"Redmine error: {r.status_code} {r.text[:300]}")
        raise
    return r.json() if r.content else {}
def _rm_upload_file(file_path, api_key):
    """Upload a file to Redmine and return the upload token.

    POSTs raw file bytes to /uploads.json with Content-Type: application/octet-stream.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    file_size = path.stat().st_size
    if file_size == 0:
        raise ValueError("Cannot upload empty file")

    content_type, _ = mimetypes.guess_type(file_path)
    if content_type is None:
        content_type = "application/octet-stream"

    url = f"{REDMINE_URL}/uploads.json"
    with open(file_path, "rb") as f:
        r = requests.post(
            url,
            headers={
                "X-Redmine-API-Key": api_key,
                "Content-Type": "application/octet-stream",
            },
            data=f.read(),
            timeout=REQUEST_TIMEOUT,
        )
    r.raise_for_status()
    data = r.json()
    upload = data.get("upload", {})
    return {
        "token": upload.get("token"),
        "filename": upload.get("filename", path.name),
        "content_type": upload.get("content_type", content_type),
    }

def _clean_issue(issue):
    return {
        "id": issue.get("id"),
        "project_id": (issue.get("project") or {}).get("id"),
        "project": (issue.get("project") or {}).get("name"),
        "tracker_id": (issue.get("tracker") or {}).get("id"),
        "tracker": (issue.get("tracker") or {}).get("name"),
        "status_id": (issue.get("status") or {}).get("id"),
        "status": (issue.get("status") or {}).get("name"),
        "priority_id": (issue.get("priority") or {}).get("id"),
        "priority": (issue.get("priority") or {}).get("name"),
        "author": (issue.get("author") or {}).get("name"),
        "assigned_to": (issue.get("assigned_to") or {}).get("name"),
        "subject": issue.get("subject"),
        "description": issue.get("description"),
        "start_date": issue.get("start_date"),
        "due_date": issue.get("due_date"),
        "done_ratio": issue.get("done_ratio"),
        "created_on": issue.get("created_on"),
        "updated_on": issue.get("updated_on"),
    }

def _validate_agent_tracker(tracker_id, allow_idea_tracker=False):
    if int(tracker_id) == IDEA_TRACKER_ID and not allow_idea_tracker:
        raise ValueError("Idea tracker is reserved for explicit user idea capture; agents must use tracker_id 3")

def _assert_tracker_honored(issue, requested_tracker_id):
    actual_tracker_id = (issue.get("tracker") or {}).get("id")
    if actual_tracker_id is None or int(actual_tracker_id) == int(requested_tracker_id):
        return
    issue_id = issue.get("id", "unknown")
    raise RuntimeError(
        f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {requested_tracker_id}, "
        f"Redmine returned tracker_id {actual_tracker_id}"
    )

# ── Document creation helpers (HTML form, not JSON API) ──

def _extract_authenticity_token(html):
    m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("Could not find Redmine authenticity token")
    return unescape(m.group(1))

def _extract_selected_category_id(html):
    select_match = re.search(r'name="document\[category_id\]".*?</select>', html, re.S)
    if not select_match:
        return None
    select_html = select_match.group(0)
    selected_match = re.search(r'<option value="(\d+)"[^>]*selected', select_html)
    if selected_match:
        return int(selected_match.group(1))
    first_match = re.search(r'<option value="(\d+)"', select_html)
    return int(first_match.group(1)) if first_match else None

def _web_session():
    session = requests.Session()
    if REDMINE_WEB_SESSION_COOKIE:
        for cookie_pair in REDMINE_WEB_SESSION_COOKIE.split(";"):
            cookie_pair = cookie_pair.strip()
            if not cookie_pair or "=" not in cookie_pair:
                continue
            name, value = cookie_pair.split("=", 1)
            session.cookies.set(name.strip(), value.strip())
        return session

    if REDMINE_WEB_USERNAME and REDMINE_WEB_PASSWORD:
        login_page = session.get(f"{REDMINE_URL}/login", timeout=REQUEST_TIMEOUT)
        login_page.raise_for_status()
        token = _extract_authenticity_token(login_page.text)
        response = session.post(
            f"{REDMINE_URL}/login",
            data={
                "username": REDMINE_WEB_USERNAME,
                "password": REDMINE_WEB_PASSWORD,
                "authenticity_token": token,
                "back_url": "/",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        if "Sign out" not in response.text and "Logged in as" not in response.text:
            raise RuntimeError("Redmine web login failed")
        return session

    raise RuntimeError(
        "Document creation requires REDMINE_WEB_USERNAME/REDMINE_WEB_PASSWORD "
        "or REDMINE_WEB_SESSION_COOKIE"
    )

def _resolve_project_identifier(project_id, api_key):
    data = _rm_request("GET", f"/projects/{project_id}.json", api_key)
    project = data.get("project", {})
    identifier = project.get("identifier")
    if not identifier:
        raise RuntimeError(f"Could not resolve project identifier for project {project_id}")
    return identifier

def _create_document_web(project_identifier, title, description, category_id):
    session = _web_session()
    form_response = session.get(
        f"{REDMINE_URL}/projects/{project_identifier}/documents/new",
        timeout=REQUEST_TIMEOUT,
    )
    form_response.raise_for_status()
    token = _extract_authenticity_token(form_response.text)
    resolved_category_id = category_id or _extract_selected_category_id(form_response.text)
    if resolved_category_id is None:
        raise RuntimeError("Could not determine a Redmine document category")

    response = session.post(
        f"{REDMINE_URL}/projects/{project_identifier}/documents",
        data={
            "utf8": "✓",
            "authenticity_token": token,
            "document[category_id]": str(resolved_category_id),
            "document[title]": title,
            "document[description]": description,
            "commit": "Create",
        },
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    if response.status_code not in (302, 303):
        raise RuntimeError(
            f"Redmine document creation failed with HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    location = response.headers.get("Location", "")
    document_url = location if location.startswith("http") else f"{REDMINE_URL}{location}"
    return {
        "project_identifier": project_identifier,
        "title": title,
        "category_id": resolved_category_id,
        "document_url": document_url or None,
    }

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class MCPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        log(f"GET {self.path}")
        if self.path.startswith("/sse"):
            self._handle_sse_get()
        elif self.path == "/health":
            self._send_json({"status": "ok"})
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        log(f"POST {self.path}")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        log(f"POST body: {body[:200]}")
        if self.path.startswith("/sse"):
            self._handle_sse_post(body)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-MCP-Session-Id")
        self.end_headers()

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _handle_sse_get(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = _register_sse()
        try:
            self.wfile.write(f"event: endpoint\ndata: http://10.0.0.23:{PORT}/sse\n\n".encode())
            self.wfile.flush()
            log("sent endpoint event")
            while True:
                try:
                    msg = q.get(timeout=15)
                    log(f"sending message via SSE: {msg[:100]}")
                    self.wfile.write(f"event: message\ndata: {msg}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(": ping\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            log(f"SSE connection closed: {e}")
        finally:
            _unregister_sse(q)

    def _handle_sse_post(self, body):
        try:
            data = json.loads(body)
            method = data.get("method", "")
            params = data.get("params", {})
            req_id = data.get("id")
            self.api_key = self.headers.get("X-Redmine-API-Key") or DEFAULT_REDMINE_API_KEY
            if method != "initialize" and not self.api_key:
                self._send_json({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "X-Redmine-API-Key header required"}}, 400)
                return
            result = self._handle_method(method, params)
            response = {"jsonrpc": "2.0", "id": req_id, "result": result}
            log(f"response: {json.dumps(response)[:150]}")

            if _has_sse_clients():
                _broadcast(json.dumps(response))
                log("broadcast to SSE clients")

            self._send_json(response)
        except Exception as e:
            err = {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
            self._send_json(err, 500)

    def _handle_method(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "redmine-mcp", "version": "1.1.0"}
            }
        elif method == "tools/list":
            return {"tools": [
                {"name": "get_current_user", "description": "Return the Redmine user associated with the configured API key",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "list_projects", "description": "List visible Redmine projects",
                 "inputSchema": {"type": "object", "properties": {
                     "limit": {"type": "number"}, "offset": {"type": "number"}}}},
                {"name": "list_issue_statuses", "description": "List issue statuses available to the authenticated user for a project",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "is_closed": {"type": "boolean"}}}},
                {"name": "list_issues", "description": "List Redmine issues with optional filters",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "assigned_to_id": {"type": "string"},
                     "status_id": {"type": "string"}, "tracker_id": {"type": "number"},
                     "limit": {"type": "number"}, "offset": {"type": "number"}}}},
                {"name": "get_issue", "description": "Fetch one Redmine issue with journals, attachments, and relations",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"},
                     "include": {"type": "string"}}}},
                {"name": "create_issue", "description": "Create a new Redmine issue",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "subject": {"type": "string"},
                     "description": {"type": "string"}, "tracker_id": {"type": "number"},
                     "assigned_to_id": {"type": "number"}, "priority_id": {"type": "number"},
                     "status_id": {"type": "number"}, "allow_idea_tracker": {"type": "boolean"},
                     "category_id": {"type": "number"}, "due_date": {"type": "string"},
                     "start_date": {"type": "string"}, "estimated_hours": {"type": "number"}}}},
                {"name": "update_issue_status", "description": "Update a Redmine issue status and optionally add a note",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}, "status_id": {"type": "number"},
                     "note": {"type": "string"}}}},
                {"name": "move_issue", "description": "Move a Redmine issue to another visible project and optionally add a note",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}, "project_id": {"type": "number"},
                     "note": {"type": "string"}}}},
                {"name": "update_issue_tracker", "description": "Update a Redmine issue tracker and optionally add a note",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}, "tracker_id": {"type": "number"},
                     "note": {"type": "string"}, "allow_idea_tracker": {"type": "boolean"}}}},
                {"name": "add_issue_note", "description": "Append a journal note to a Redmine issue",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}, "note": {"type": "string"}}}},
                {"name": "list_issue_categories", "description": "List the issue categories defined on a Redmine project (returns id, name, project_id, and assigned_to_id when set)",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}},
                     "required": ["project_id"]}},
                {"name": "create_document", "description": "Create a Redmine project document through the HTML form (requires web session auth — REDMINE_WEB_USERNAME/REDMINE_WEB_PASSWORD or REDMINE_WEB_SESSION_COOKIE env vars)",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "title": {"type": "string"},
                     "description": {"type": "string"}, "category_id": {"type": "number"}}}},
                {"name": "list_trackers", "description": "List Redmine trackers, optionally filtered by project",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}}}},
                {"name": "list_priorities", "description": "List issue priorities configured on the Redmine instance",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "create_project", "description": "Create a new Redmine project",
                 "inputSchema": {"type": "object", "properties": {
                     "name": {"type": "string"}, "identifier": {"type": "string"},
                     "description": {"type": "string"}, "parent_id": {"type": "number"},
                     "inherit_members": {"type": "boolean"}}}},
                {"name": "search_projects", "description": "Search visible Redmine projects by case-insensitive substring of name, identifier, or description",
                 "inputSchema": {"type": "object", "properties": {
                     "query": {"type": "string"}, "limit": {"type": "number"}, "offset": {"type": "number"}}}},
                {"name": "resolve_project", "description": "Resolve a Redmine project by numeric id, identifier, or name. Returns canonical {id, name, identifier}.",
                 "inputSchema": {"type": "object", "properties": {
                     "identifier_or_name": {"type": "string"}}}},
                {"name": "list_wiki_pages", "description": "List all wiki pages in a Redmine project",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}}}},
                {"name": "get_wiki_page", "description": "Get a Redmine wiki page by title",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "title": {"type": "string"},
                     "version": {"type": "number"}}}},
                {"name": "create_wiki_page", "description": "Create a new wiki page in a Redmine project",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "title": {"type": "string"},
                     "text": {"type": "string"}, "parent_title": {"type": "string"},
                     "comments": {"type": "string"}}}},
                {"name": "update_wiki_page", "description": "Update an existing wiki page with optimistic concurrency control",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "title": {"type": "string"},
                     "text": {"type": "string"}, "version": {"type": "number"},
                     "comments": {"type": "string"}}}},
                {"name": "delete_issue", "description": "Delete a Redmine issue. Requires confirm=True or dry_run=True for safety.",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"},
                     "confirm": {"type": "boolean"},
                     "dry_run": {"type": "boolean"}}}},
                {"name": "list_memberships", "description": "List project memberships with roles for permission diagnostics",
                 "inputSchema": {"type": "object", "properties": {
                     "project_id": {"type": "number"}, "user_id": {"type": "number"}}}},
                {"name": "add_issue_attachment", "description": "Upload a file and attach it to an existing Redmine issue",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}, "file_path": {"type": "string"},
                     "description": {"type": "string"}, "content_type": {"type": "string"}}}},
                {"name": "list_issue_attachments", "description": "Return all attachments on a Redmine issue with download URLs",
                 "inputSchema": {"type": "object", "properties": {
                     "issue_id": {"type": "number"}}}}]}
        elif method == "tools/call":
            tool = params.get("name", "")
            args = params.get("arguments", {})
            return {"content": [{"type": "text", "text": json.dumps(self._call_tool(tool, args))}]}
        elif method == "notifications/initialized":
            return {}
        return {}

    def _call_tool(self, name, args):
        api_key = getattr(self, "api_key", DEFAULT_REDMINE_API_KEY)
        try:
            if name == "get_current_user":
                return self._get_current_user(api_key)
            elif name == "list_projects":
                return self._list_projects(args.get("limit", 100), args.get("offset", 0), api_key)
            elif name == "list_issue_statuses":
                return self._list_issue_statuses(
                    args.get("project_id"), args.get("is_closed"), api_key)
            elif name == "list_issues":
                return self._list_issues(
                    args.get("project_id"), args.get("assigned_to_id"),
                    args.get("status_id"), args.get("tracker_id"),
                    args.get("limit", 25), args.get("offset", 0), api_key)
            elif name == "get_issue":
                return self._get_issue(args.get("issue_id"), args.get("include", "journals,attachments,relations"), api_key)
            elif name == "create_issue":
                return self._create_issue(
                    args.get("project_id"), args.get("subject"), args.get("description", ""),
                    args.get("tracker_id", 3), args.get("assigned_to_id"),
                    args.get("priority_id", 2), args.get("status_id", 1),
                    args.get("allow_idea_tracker", False),
                    args.get("category_id"), args.get("due_date"),
                    args.get("start_date"), args.get("estimated_hours"),
                    api_key)
            elif name == "update_issue_status":
                return self._update_issue_status(args.get("issue_id"), args.get("status_id"), args.get("note", ""), api_key)
            elif name == "move_issue":
                return self._move_issue(args.get("issue_id"), args.get("project_id"), args.get("note", ""), api_key)
            elif name == "update_issue_tracker":
                return self._update_issue_tracker(
                    args.get("issue_id"), args.get("tracker_id", AGENT_TASK_TRACKER_ID),
                    args.get("note", ""), args.get("allow_idea_tracker", False), api_key)
            elif name == "add_issue_note":
                return self._add_issue_note(args.get("issue_id"), args.get("note"), api_key)
            elif name == "list_issue_categories":
                return self._list_issue_categories(args.get("project_id"), api_key)
            elif name == "create_document":
                return self._create_document(
                    args.get("project_id"), args.get("title"), args.get("description", ""),
                    args.get("category_id"), api_key)
            elif name == "list_trackers":
                return self._list_trackers(args.get("project_id"), api_key)
            elif name == "list_priorities":
                return self._list_priorities(api_key)
            elif name == "create_project":
                return self._create_project(
                    args.get("name"), args.get("identifier"), args.get("description", ""),
                    args.get("parent_id"), args.get("inherit_members", True), api_key)
            elif name == "search_projects":
                return self._search_projects(args.get("query", ""), args.get("limit", 100), args.get("offset", 0), api_key)
            elif name == "resolve_project":
                return self._resolve_project(args.get("identifier_or_name", ""), api_key)
            elif name == "list_wiki_pages":
                return self._list_wiki_pages(args.get("project_id"), api_key)
            elif name == "get_wiki_page":
                return self._get_wiki_page(args.get("project_id"), args.get("title"), args.get("version"), api_key)
            elif name == "create_wiki_page":
                return self._create_wiki_page(
                    args.get("project_id"), args.get("title"), args.get("text"),
                    args.get("parent_title"), args.get("comments", ""), api_key)
            elif name == "update_wiki_page":
                return self._update_wiki_page(
                    args.get("project_id"), args.get("title"), args.get("text"),
                    args.get("version"), args.get("comments", ""), api_key)
            elif name == "delete_issue":
                return self._delete_issue(
                    args.get("issue_id"), args.get("confirm", False),
                    args.get("dry_run", False), api_key)
            elif name == "list_memberships":
                return self._list_memberships(args.get("project_id"), args.get("user_id"), api_key)
            elif name == "add_issue_attachment":
                return self._add_issue_attachment(
                    args.get("issue_id"), args.get("file_path"),
                    args.get("description", ""), args.get("content_type"), api_key)
            elif name == "list_issue_attachments":
                return self._list_issue_attachments(args.get("issue_id"), api_key)
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            return {"error": str(e)}

    def _get_current_user(self, api_key):
        data = _rm_request("GET", "/users/current.json", api_key)
        user = data.get("user", {})
        return {"id": user.get("id"), "login": user.get("login"),
                "name": " ".join(filter(None, [user.get("firstname"), user.get("lastname")]))}

    def _list_projects(self, limit, offset, api_key):
        data = _rm_request("GET", f"/projects.json?limit={limit}&offset={offset}", api_key)
        projects = data.get("projects", [])
        return {"total_count": data.get("total_count", len(projects)),
                "projects": [{"id": p.get("id"), "identifier": p.get("identifier"),
                              "name": p.get("name"), "parent": (p.get("parent") or {}).get("name"),
                              "description": p.get("description")} for p in projects]}

    def _list_issue_statuses(self, project_id, is_closed, api_key):
        params = {"project_id": project_id} if project_id is not None else {}
        data = _rm_request("GET", "/issue_statuses.json", api_key, params=params)
        statuses = [{"id": status.get("id"), "name": status.get("name"),
                     "is_closed": status.get("is_closed", False)}
                    for status in data.get("issue_statuses", [])]
        if is_closed is not None:
            statuses = [status for status in statuses if status["is_closed"] is is_closed]
        return statuses

    def _list_issues(self, project_id, assigned_to_id, status_id, tracker_id, limit, offset, api_key):
        params = {"limit": limit, "offset": offset}
        if project_id is not None: params["project_id"] = project_id
        if assigned_to_id is not None: params["assigned_to_id"] = assigned_to_id
        if status_id is not None: params["status_id"] = status_id
        if tracker_id is not None: params["tracker_id"] = tracker_id
        data = _rm_request("GET", "/issues.json", api_key, params=params)
        issues = data.get("issues", [])
        return {"total_count": data.get("total_count", len(issues)),
                "issues": [_clean_issue(i) for i in issues]}

    def _get_issue(self, issue_id, include, api_key):
        data = _rm_request("GET", f"/issues/{issue_id}.json?include={include}", api_key)
        issue = data.get("issue", {})
        result = _clean_issue(issue)
        result["journals"] = [{"id": j.get("id"), "user": (j.get("user") or {}).get("name"),
                               "notes": j.get("notes"), "created_on": j.get("created_on")}
                              for j in issue.get("journals", [])]
        result["relations"] = issue.get("relations", [])
        result["attachments"] = issue.get("attachments", [])
        return result

    def _create_issue(self, project_id, subject, description, tracker_id, assigned_to_id, priority_id, status_id, allow_idea_tracker, category_id, due_date, start_date, estimated_hours, api_key):
        _validate_agent_tracker(tracker_id, allow_idea_tracker)
        issue = {"project_id": project_id, "subject": subject, "description": description,
                 "tracker_id": tracker_id, "priority_id": priority_id, "status_id": status_id}
        if assigned_to_id is not None:
            issue["assigned_to_id"] = assigned_to_id
        if category_id is not None:
            issue["category_id"] = category_id
        if due_date is not None:
            issue["due_date"] = due_date
        if start_date is not None:
            issue["start_date"] = start_date
        if estimated_hours is not None:
            issue["estimated_hours"] = estimated_hours
        data = _rm_request("POST", "/issues.json", api_key, json={"issue": issue})
        created_issue = data.get("issue", {})
        _assert_tracker_honored(created_issue, tracker_id)
        return _clean_issue(created_issue)

    def _update_issue_status(self, issue_id, status_id, note, api_key):
        issue = {"status_id": status_id}
        if note: issue["notes"] = note
        _rm_request("PUT", f"/issues/{issue_id}.json", api_key, json={"issue": issue})
        # PUT returns 204 No Content; re-fetch and verify status_id matches.
        updated = _rm_request("GET", f"/issues/{issue_id}.json", api_key)
        issue_body = updated.get("issue", {}) if isinstance(updated.get("issue"), dict) else {}
        actual_status_id = (issue_body.get("status") or {}).get("id")
        if actual_status_id is not None and int(actual_status_id) != int(status_id):
            raise RuntimeError(
                f"Redmine issue {issue_id} status mismatch: requested status_id {status_id}, "
                f"Redmine returned status_id {actual_status_id}"
            )
        return self._get_issue(issue_id, "journals", api_key)

    def _move_issue(self, issue_id, project_id, note, api_key):
        _rm_request("GET", f"/projects/{project_id}.json", api_key)
        issue = {"project_id": project_id}
        if note: issue["notes"] = note
        _rm_request("PUT", f"/issues/{issue_id}.json", api_key, json={"issue": issue})
        updated = self._get_issue(issue_id, "journals", api_key)
        if updated.get("project_id") not in (None, project_id):
            raise RuntimeError(
                f"Redmine issue {issue_id} project mismatch: requested project_id {project_id}, "
                f"Redmine returned project_id {updated.get('project_id')}"
            )
        return updated

    def _update_issue_tracker(self, issue_id, tracker_id, note, allow_idea_tracker, api_key):
        _validate_agent_tracker(tracker_id, allow_idea_tracker)
        issue = {"tracker_id": tracker_id}
        if note: issue["notes"] = note
        _rm_request("PUT", f"/issues/{issue_id}.json", api_key, json={"issue": issue})
        updated = self._get_issue(issue_id, "journals", api_key)
        if updated.get("tracker_id") not in (None, tracker_id):
            raise RuntimeError(
                f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {tracker_id}, "
                f"Redmine returned tracker_id {updated.get('tracker_id')}"
            )
        return updated

    def _add_issue_note(self, issue_id, note, api_key):
        _rm_request("PUT", f"/issues/{issue_id}.json", api_key, json={"issue": {"notes": note}})
        return self._get_issue(issue_id, "journals", api_key)

    def _list_issue_categories(self, project_id, api_key):
        data = _rm_request("GET", f"/projects/{project_id}.json?include=issue_categories", api_key)
        categories = (data.get("project") or {}).get("issue_categories", [])
        return [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "project_id": (c.get("project") or {}).get("id"),
                "assigned_to_id": (c.get("assigned_to") or {}).get("id"),
            }
            for c in categories
        ]

    def _create_document(self, project_id, title, description, category_id, api_key):
        project_identifier = _resolve_project_identifier(project_id, api_key)
        return _create_document_web(project_identifier, title, description, category_id)

    def _list_trackers(self, project_id, api_key):
        data = _rm_request("GET", "/trackers.json", api_key)
        all_trackers = data.get("trackers", [])

        if project_id is None:
            return {
                "total_count": len(all_trackers),
                "trackers": [
                    {"id": t.get("id"), "name": t.get("name")}
                    for t in all_trackers
                ],
            }

        project_data = _rm_request("GET", f"/projects/{project_id}.json?include=trackers", api_key)
        project_trackers = project_data.get("project", {}).get("trackers", [])
        tracker_ids = {t.get("id") for t in project_trackers}

        filtered = [t for t in all_trackers if t.get("id") in tracker_ids]
        return {
            "total_count": len(filtered),
            "trackers": [
                {"id": t.get("id"), "name": t.get("name")}
                for t in filtered
            ],
        }

    def _list_priorities(self, api_key):
        data = _rm_request("GET", "/enumerations/issue_priorities.json", api_key)
        return [
            {
                "id": priority.get("id"),
                "name": priority.get("name"),
                "is_default": priority.get("is_default", False),
            }
            for priority in data.get("issue_priorities", [])
        ]

    def _create_project(self, name, identifier, description, parent_id, inherit_members, api_key):
        project = {
            "name": name,
            "identifier": identifier,
            "description": description,
        }
        if parent_id is not None:
            project["parent_id"] = parent_id
            project["inherit_members"] = True
        data = _rm_request("POST", "/projects.json", api_key, json={"project": project})
        p = data.get("project", {})
        return {
            "id": p.get("id"),
            "name": p.get("name"),
            "identifier": p.get("identifier"),
            "description": p.get("description"),
            "parent_id": p.get("parent", {}).get("id") if p.get("parent") else None,
            "created_on": p.get("created_on"),
        }

    def _search_projects(self, query, limit, offset, api_key):
        """Mirror of server.py search_projects: client-side case-insensitive filter."""
        needle = query.strip().lower()
        data = _rm_request("GET", f"/projects.json?limit={limit}&offset={offset}", api_key)
        projects = data.get("projects", []) or []
        matches = []
        for project in projects:
            haystacks = [
                str(project.get("name") or ""),
                str(project.get("identifier") or ""),
                str(project.get("description") or ""),
            ]
            if any(needle in hay.lower() for hay in haystacks):
                matches.append({
                    "id": project.get("id"),
                    "identifier": project.get("identifier"),
                    "name": project.get("name"),
                    "parent": (project.get("parent") or {}).get("name"),
                    "description": project.get("description"),
                })
        return {
            "total_count": data.get("total_count", len(projects)),
            "query": query,
            "matches": matches,
        }

    def _resolve_project(self, identifier_or_name, api_key):
        """Mirror of server.py resolve_project: resolve by id, identifier, or name."""
        # 1. Numeric → fetch directly by id
        if isinstance(identifier_or_name, int) or (
            isinstance(identifier_or_name, str) and identifier_or_name.isdigit()
        ):
            project_id = int(identifier_or_name)
            try:
                data = _rm_request("GET", f"/projects/{project_id}.json", api_key)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 404:
                    raise RuntimeError(f"Redmine project id {project_id} not found") from exc
                raise
            project = data.get("project", {})
            if project.get("id") is None:
                raise RuntimeError(f"Redmine project id {project_id} not found")
            return {
                "id": project.get("id"),
                "identifier": project.get("identifier"),
                "name": project.get("name"),
                "parent": (project.get("parent") or {}).get("name"),
                "description": project.get("description"),
            }

        needle = str(identifier_or_name).strip().lower()
        if not needle:
            raise RuntimeError("identifier_or_name must not be empty")

        projects = _rm_request("GET", "/projects.json?limit=100", api_key).get("projects", []) or []

        for project in projects:
            identifier = str(project.get("identifier") or "")
            if needle == identifier.lower():
                return {
                    "id": project.get("id"),
                    "identifier": project.get("identifier"),
                    "name": project.get("name"),
                    "parent": (project.get("parent") or {}).get("name"),
                    "description": project.get("description"),
                }
        for project in projects:
            name = str(project.get("name") or "")
            if needle == name.lower():
                return {
                    "id": project.get("id"),
                    "identifier": project.get("identifier"),
                    "name": project.get("name"),
                    "parent": (project.get("parent") or {}).get("name"),
                    "description": project.get("description"),
                }

        raise RuntimeError(
            f"Redmine project '{identifier_or_name}' not found by id, identifier, or name"
        )

    def _list_wiki_pages(self, project_id, api_key):
        data = _rm_request("GET", f"/projects/{project_id}/wiki/index.json", api_key)
        return data.get("wiki_pages", [])

    def _get_wiki_page(self, project_id, title, version, api_key):
        path = f"/projects/{project_id}/wiki/{title}.json"
        params = {}
        if version is not None:
            params["version"] = version
        data = _rm_request("GET", path, api_key, params=params)
        return data.get("wiki_page", {})

    def _create_wiki_page(self, project_id, title, text, parent_title, comments, api_key):
        wiki_page = {"text": text, "comments": comments}
        if parent_title is not None:
            wiki_page["parent_title"] = parent_title
        data = _rm_wiki_request(
            "PUT", f"/projects/{project_id}/wiki/{title}.json",
            api_key, json={"wiki_page": wiki_page},
        )
        return data.get("wiki_page", {})

    def _update_wiki_page(self, project_id, title, text, version, comments, api_key):
        wiki_page = {"text": text, "comments": comments}
        if version is not None:
            wiki_page["version"] = version
        data = _rm_wiki_request(
            "PUT", f"/projects/{project_id}/wiki/{title}.json",
            api_key, json={"wiki_page": wiki_page},
        )
        return data.get("wiki_page", {})

    def _delete_issue(self, issue_id, confirm, dry_run, api_key):
        if confirm == dry_run:
            raise ValueError(
                "Exactly one of confirm=True or dry_run=True must be set. "
                "Set dry_run=True to preview, confirm=True to actually delete."
            )
        if dry_run:
            try:
                issue = self._get_issue(issue_id, "", api_key)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    return {
                        "deleted": False, "id": issue_id, "dry_run": True,
                        "status": "not_found",
                        "message": f"Issue #{issue_id} does not exist — nothing to delete.",
                    }
                raise
            return {
                "deleted": False, "id": issue_id, "dry_run": True,
                "status": "found",
                "message": f"Would delete issue #{issue_id}: {issue.get('subject', '')}",
                "issue": {
                    "id": issue.get("id"), "subject": issue.get("subject"),
                    "project": issue.get("project"), "tracker": issue.get("tracker"),
                    "status": issue.get("status"), "author": issue.get("author"),
                },
            }

        # confirm=True: perform the actual deletion
        delete_url = f"{REDMINE_URL}/issues/{issue_id}.json"
        try:
            response = requests.delete(
                delete_url,
                headers=_rm_headers(api_key),
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            if exc.response is not None:
                if exc.response.status_code == 404:
                    return {
                        "deleted": False, "id": issue_id,
                        "status": "not_found",
                        "message": f"Issue #{issue_id} does not exist.",
                    }
                if exc.response.status_code == 403:
                    return {
                        "deleted": False, "id": issue_id,
                        "status": "forbidden",
                        "message": (
                            f"Permission denied deleting issue #{issue_id}. "
                            "You must be an admin or the issue author with delete permission."
                        ),
                    }
            raise

        # Write audit log
        DELETE_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        audit_line = (
            f"{timestamp} | agent_id={REDMINE_AGENT_ID} | "
            f"DELETED issue_id={issue_id}\n"
        )
        with open(DELETE_AUDIT_LOG_PATH, "a") as f:
            f.write(audit_line)

        return {
            "deleted": True, "id": issue_id,
            "status": "deleted",
            "message": f"Issue #{issue_id} deleted.",
        }
    def _list_memberships(self, project_id, user_id, api_key):
        data = _rm_request("GET", f"/projects/{project_id}/memberships.json", api_key)
        memberships = data.get("memberships", [])
        result = []
        for membership in memberships:
            entry = {
                "id": membership.get("id"),
                "user": {
                    "id": (membership.get("user") or {}).get("id"),
                    "name": (membership.get("user") or {}).get("name"),
                },
                "roles": [
                    {"id": role.get("id"), "name": role.get("name")}
                    for role in (membership.get("roles") or [])
                ],
            }
            if user_id is not None:
                if entry["user"]["id"] == user_id:
                    result.append(entry)
                    break
            else:
                result.append(entry)
        return {
            "project_id": project_id,
            "total_count": len(result),
            "memberships": result,
        }
    def _add_issue_attachment(self, issue_id, file_path, description, content_type, api_key):
        """Upload a file and attach it to an existing Redmine issue."""
        upload = _rm_upload_file(file_path, api_key)

        issue = {
            "uploads": [
                {
                    "token": upload["token"],
                    "filename": upload["filename"],
                    "content_type": content_type or upload["content_type"],
                }
            ],
        }
        if description:
            issue["notes"] = description

        _rm_request("PUT", f"/issues/{issue_id}.json", api_key, json={"issue": issue})

        return {
            "issue_id": issue_id,
            "filename": upload["filename"],
            "content_type": content_type or upload["content_type"],
            "description": description,
        }

    def _list_issue_attachments(self, issue_id, api_key):
        """Return all attachments on a Redmine issue with download URLs."""
        data = _rm_request("GET", f"/issues/{issue_id}.json?include=attachments", api_key)
        attachments = data.get("issue", {}).get("attachments", [])
        return [
            {
                "id": att.get("id"),
                "filename": att.get("filename"),
                "filesize": att.get("filesize"),
                "content_type": att.get("content_type"),
                "description": att.get("description"),
                "content_url": att.get("content_url"),
                "author": att.get("author", {}).get("name"),
                "created_on": att.get("created_on"),
            }
            for att in attachments
        ]

    def log_message(self, f, *args):
        pass

def run():
    log(f"Starting Redmine MCP on port {PORT} (Redmine: {REDMINE_URL})")
    ThreadingHTTPServer(("0.0.0.0", PORT), MCPHandler).serve_forever()

if __name__ == "__main__":
    run()
