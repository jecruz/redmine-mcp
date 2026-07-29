import os
import re
from html import unescape
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


REDMINE_URL = os.getenv("REDMINE_URL", "").rstrip("/")
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "")
REDMINE_WEB_USERNAME = os.getenv("REDMINE_WEB_USERNAME", "")
REDMINE_WEB_PASSWORD = os.getenv("REDMINE_WEB_PASSWORD", "")
REDMINE_WEB_SESSION_COOKIE = os.getenv("REDMINE_WEB_SESSION_COOKIE", "")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH = os.getenv("MCP_PATH", "/sse")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "sse")
AGENT_TASK_TRACKER_ID = 3
IDEA_TRACKER_ID = 6


if not REDMINE_URL:
    raise RuntimeError("REDMINE_URL is required")
if not REDMINE_API_KEY:
    raise RuntimeError("REDMINE_API_KEY is required")


mcp = FastMCP(
    name="Redmine",
    instructions=(
        "Use these tools to interact with Redmine through its JSON API. "
        "Prefer these tools over raw HTTP code when working with Redmine issues and projects."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    sse_path=MCP_PATH,
)


def _headers() -> dict[str, str]:
    return {
        "X-Redmine-API-Key": REDMINE_API_KEY,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = f"{REDMINE_URL}{path}"
    response = requests.request(
        method,
        url,
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def _extract_authenticity_token(html: str) -> str:
    match = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
    if not match:
        raise RuntimeError("Could not find Redmine authenticity token")
    return unescape(match.group(1))


def _extract_selected_category_id(html: str) -> int | None:
    select_match = re.search(
        r'name="document\[category_id\]".*?</select>',
        html,
        re.S,
    )
    if not select_match:
        return None

    select_html = select_match.group(0)
    selected_match = re.search(r'<option value="(\d+)"[^>]*selected', select_html)
    if selected_match:
        return int(selected_match.group(1))

    first_match = re.search(r'<option value="(\d+)"', select_html)
    return int(first_match.group(1)) if first_match else None


def _web_session() -> requests.Session:
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


def _resolve_project_identifier(project_id: int) -> str:
    data = _request("GET", f"/projects/{project_id}.json")
    project = data.get("project", {})
    identifier = project.get("identifier")
    if not identifier:
        raise RuntimeError(f"Could not resolve project identifier for project {project_id}")
    return identifier


def _create_document_in_browser(
    project_identifier: str,
    title: str,
    description: str,
    category_id: int | None,
) -> dict[str, Any]:
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


def _clean_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": issue.get("id"),
        "project_id": issue.get("project", {}).get("id"),
        "project": issue.get("project", {}).get("name"),
        "tracker_id": issue.get("tracker", {}).get("id"),
        "tracker": issue.get("tracker", {}).get("name"),
        "status_id": issue.get("status", {}).get("id"),
        "status": issue.get("status", {}).get("name"),
        "priority_id": issue.get("priority", {}).get("id"),
        "priority": issue.get("priority", {}).get("name"),
        "author": issue.get("author", {}).get("name"),
        "assigned_to": issue.get("assigned_to", {}).get("name"),
        "subject": issue.get("subject"),
        "description": issue.get("description"),
        "start_date": issue.get("start_date"),
        "due_date": issue.get("due_date"),
        "done_ratio": issue.get("done_ratio"),
        "created_on": issue.get("created_on"),
        "updated_on": issue.get("updated_on"),
    }


def _validate_agent_tracker(tracker_id: int, *, allow_idea_tracker: bool) -> None:
    if tracker_id == IDEA_TRACKER_ID and not allow_idea_tracker:
        raise ValueError("Idea tracker is reserved for explicit user idea capture; agents must use tracker_id 3")


def _assert_tracker_honored(issue: dict[str, Any], requested_tracker_id: int) -> None:
    actual_tracker_id = issue.get("tracker", {}).get("id")
    if actual_tracker_id is None or actual_tracker_id == requested_tracker_id:
        return
    issue_id = issue.get("id", "unknown")
    raise RuntimeError(
        f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {requested_tracker_id}, "
        f"Redmine returned tracker_id {actual_tracker_id}"
    )


@mcp.tool()
def get_current_user() -> dict[str, Any]:
    """Return the Redmine user associated with the configured API key."""
    data = _request("GET", "/users/current.json")
    user = data.get("user", {})
    return {
        "id": user.get("id"),
        "login": user.get("login"),
        "name": " ".join(filter(None, [user.get("firstname"), user.get("lastname")])),
    }


@mcp.tool()
def list_projects(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List visible Redmine projects."""
    data = _request("GET", f"/projects.json?limit={limit}&offset={offset}")
    projects = data.get("projects", [])
    return {
        "total_count": data.get("total_count", len(projects)),
        "projects": [
            {
                "id": project.get("id"),
                "identifier": project.get("identifier"),
                "name": project.get("name"),
                "parent": project.get("parent", {}).get("name"),
                "description": project.get("description"),
            }
            for project in projects
        ],
    }


@mcp.tool()
def list_issues(
    project_id: int | None = None,
    assigned_to_id: str | int | None = None,
    status_id: str | int | None = None,
    tracker_id: int | None = None,
    subject_contains: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List Redmine issues with optional filters."""
    params: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
    }
    if project_id is not None:
        params["project_id"] = project_id
    if assigned_to_id is not None:
        params["assigned_to_id"] = assigned_to_id
    if status_id is not None:
        params["status_id"] = status_id
    if tracker_id is not None:
        params["tracker_id"] = tracker_id
    if subject_contains:
        params["subject"] = f"~{subject_contains}"

    data = _request("GET", "/issues.json", params=params)
    issues = data.get("issues", [])
    return {
        "total_count": data.get("total_count", len(issues)),
        "issues": [_clean_issue(issue) for issue in issues],
    }


@mcp.tool()
def get_issue(issue_id: int, include: str = "journals,attachments,relations") -> dict[str, Any]:
    """Fetch one Redmine issue with optional include expansions."""
    data = _request("GET", f"/issues/{issue_id}.json?include={include}")
    issue = data.get("issue", {})
    result = _clean_issue(issue)
    result["journals"] = [
        {
            "id": journal.get("id"),
            "user": journal.get("user", {}).get("name"),
            "notes": journal.get("notes"),
            "created_on": journal.get("created_on"),
        }
        for journal in issue.get("journals", [])
    ]
    result["relations"] = issue.get("relations", [])
    result["attachments"] = issue.get("attachments", [])
    return result


@mcp.tool()
def create_issue(
    project_id: int,
    subject: str,
    description: str = "",
    tracker_id: int = 3,
    assigned_to_id: int | None = None,
    priority_id: int = 2,
    status_id: int = 1,
    allow_idea_tracker: bool = False,
    category_id: int | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    estimated_hours: float | None = None,
) -> dict[str, Any]:
    """Create a Redmine issue."""
    _validate_agent_tracker(tracker_id, allow_idea_tracker=allow_idea_tracker)
    issue: dict[str, Any] = {
        "project_id": project_id,
        "subject": subject,
        "description": description,
        "tracker_id": tracker_id,
        "priority_id": priority_id,
        "status_id": status_id,
    }
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

    data = _request("POST", "/issues.json", json={"issue": issue})
    created_issue = data.get("issue", {})
    _assert_tracker_honored(created_issue, tracker_id)
    return _clean_issue(created_issue)


@mcp.tool()
def update_issue_status(issue_id: int, status_id: int, note: str = "") -> dict[str, Any]:
    """Update a Redmine issue status and optionally add a note."""
    issue: dict[str, Any] = {"status_id": status_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    return get_issue(issue_id, include="journals")


@mcp.tool()
def move_issue(issue_id: int, project_id: int, note: str = "") -> dict[str, Any]:
    """Move an issue to another Redmine project and optionally add a note."""
    _request("GET", f"/projects/{project_id}.json")
    issue: dict[str, Any] = {"project_id": project_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    updated = get_issue(issue_id, include="journals")
    if updated.get("project_id") not in (None, project_id):
        raise RuntimeError(
            f"Redmine issue {issue_id} project mismatch: requested project_id {project_id}, "
            f"Redmine returned project_id {updated.get('project_id')}"
        )
    return updated


@mcp.tool()
def update_issue_tracker(
    issue_id: int,
    tracker_id: int = AGENT_TASK_TRACKER_ID,
    note: str = "",
    allow_idea_tracker: bool = False,
) -> dict[str, Any]:
    """Change an issue tracker and optionally add a note."""
    _validate_agent_tracker(tracker_id, allow_idea_tracker=allow_idea_tracker)
    issue: dict[str, Any] = {"tracker_id": tracker_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    updated = get_issue(issue_id, include="journals")
    if updated.get("tracker_id") not in (None, tracker_id):
        raise RuntimeError(
            f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {tracker_id}, "
            f"Redmine returned tracker_id {updated.get('tracker_id')}"
        )
    return updated


@mcp.tool()
def add_issue_note(issue_id: int, note: str) -> dict[str, Any]:
    """Append a journal note to a Redmine issue."""
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": {"notes": note}})
    return get_issue(issue_id, include="journals")


@mcp.tool()
def create_document(
    project_id: int,
    title: str,
    description: str = "",
    category_id: int | None = None,
) -> dict[str, Any]:
    """Create a Redmine project document through the HTML form."""
    project_identifier = _resolve_project_identifier(project_id)
    return _create_document_in_browser(project_identifier, title, description, category_id)


@mcp.tool()
def create_project(
    name: str,
    identifier: str,
    description: str = "",
    parent_id: int | None = None,
    inherit_members: bool = True,
) -> dict[str, Any]:
    """Create a Redmine project.

    Args:
        name: Display name for the project
        identifier: URL-safe slug (alphanumeric + hyphens, max 100 chars)
        description: Project description
        parent_id: Optional parent project ID for nesting
        inherit_members: Inherit parent members if parent_id is set
    """
    project: dict[str, Any] = {
        "name": name,
        "identifier": identifier,
        "description": description,
    }
    if parent_id is not None:
        project["parent_id"] = parent_id
        project["inherit_members"] = True

    data = _request("POST", "/projects.json", json={"project": project})
    p = data.get("project", {})
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "identifier": p.get("identifier"),
        "description": p.get("description"),
        "parent_id": p.get("parent", {}).get("id") if p.get("parent") else None,
        "created_on": p.get("created_on"),
    }


if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
