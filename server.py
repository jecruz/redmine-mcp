import json
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


# ── Structured exception hierarchy ──────────────────────────────────────────────
# These let callers (and tests) distinguish a Redmine rejection from a transport
# failure. Each carries the parsed Redmine error payload, truncated for safety.

_REDMINE_ERROR_PAYLOAD_LIMIT = 500


class RedmineMCPError(Exception):
    """Base class for any Redmine-side failure surfaced by an MCP mutation tool."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        redmine_payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.redmine_payload = redmine_payload


class RedminePermissionError(RedmineMCPError):
    """401/403 — caller is not allowed to perform this action."""


class RedmineNotFoundError(RedmineMCPError):
    """404 — target resource does not exist or is not visible to the caller."""


class RedmineValidationError(RedmineMCPError):
    """422 — Redmine rejected the request as invalid. Carries parsed error list."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[str] | None = None,
        status_code: int | None = None,
        redmine_payload: Any = None,
    ) -> None:
        super().__init__(
            message, status_code=status_code, redmine_payload=redmine_payload
        )
        self.errors: list[str] = list(errors or [])


class RedmineWorkflowError(RedmineMCPError):
    """State-machine rejection — workflow lock, illegal transition, etc."""


def _truncate_payload(payload: Any, limit: int = _REDMINE_ERROR_PAYLOAD_LIMIT) -> Any:
    """Stringify and truncate the Redmine error payload so we never leak a full HTML
    error page or megabyte of stack text into an exception message."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload[:limit]
    try:
        text = json.dumps(payload)
    except (TypeError, ValueError):
        text = repr(payload)
    return text[:limit]


def _raise_for_redmine_response(response: requests.Response) -> None:
    """Map an HTTP error response to the right typed exception.

    Redmine's REST API uses:
      401/403 — PermissionDenied
      404     — RecordNotFound (the URL or referenced record is wrong)
      409     — Conflict (version mismatches, double-submit, lock conflicts)
      422     — Validation / business-rule rejection (invalid tracker for project,
                illegal status transition, missing required field, etc.)
    Anything else falls back to RedmineMCPError with the truncated payload.
    """
    payload: Any
    parsed: Any = None
    try:
        parsed = response.json() if response.content else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
    else:
        payload = response.text or None

    truncated = _truncate_payload(payload)

    if response.status_code in (401, 403):
        raise RedminePermissionError(
            f"Redmine permission denied (HTTP {response.status_code}): {truncated}",
            status_code=response.status_code,
            redmine_payload=payload,
        )
    if response.status_code == 404:
        raise RedmineNotFoundError(
            f"Redmine resource not found (HTTP 404): {truncated}",
            status_code=404,
            redmine_payload=payload,
        )
    if response.status_code == 409:
        raise RedmineWorkflowError(
            f"Redmine conflict (HTTP 409): {truncated}",
            status_code=409,
            redmine_payload=payload,
        )
    if response.status_code == 422:
        # Redmine puts validation errors in {"errors": ["...", "..."]}
        # or per-attribute {"errors": {"tracker": ["..."]}}. Normalize to a list.
        errors: list[str] = []
        if isinstance(parsed, dict) and "errors" in parsed:
            raw_errors = parsed["errors"]
            if isinstance(raw_errors, list):
                errors = [str(item) for item in raw_errors]
            elif isinstance(raw_errors, dict):
                for field, msgs in raw_errors.items():
                    if isinstance(msgs, list):
                        for msg in msgs:
                            errors.append(f"{field}: {msg}")
                    else:
                        errors.append(f"{field}: {msgs}")
            else:
                errors = [str(raw_errors)]
        raise RedmineValidationError(
            f"Redmine validation failed (HTTP 422): {truncated}",
            errors=errors,
            status_code=422,
            redmine_payload=payload,
        )
    # 4xx/5xx fallthrough
    raise RedmineMCPError(
        f"Redmine request failed (HTTP {response.status_code}): {truncated}",
        status_code=response.status_code,
        redmine_payload=payload,
    )


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
    if response.status_code >= 400:
        _raise_for_redmine_response(response)
    if not response.content:
        return {}
    return response.json()


def _wiki_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Like _request but surfaces HTTP 409 for version-conflict detection."""
    url = f"{REDMINE_URL}{path}"
    response = requests.request(
        method,
        url,
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    if response.status_code == 409:
        # Wiki version conflicts are workflow-style rejections from the agent's
        # perspective. Wrap them in RedmineWorkflowError with a human-readable
        # hint that points to the version-bump recovery pattern.
        payload: Any = None
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = response.text or None
        raise RedmineWorkflowError(
            "Wiki page version conflict: the page has been modified since you "
            "last read it. Re-read the page and retry with the current version.",
            status_code=409,
            redmine_payload=payload,
        )
    if response.status_code >= 400:
        _raise_for_redmine_response(response)
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
    raise RedmineWorkflowError(
        f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {requested_tracker_id}, "
        f"Redmine returned tracker_id {actual_tracker_id}",
        redmine_payload={"issue": issue, "expected_tracker_id": requested_tracker_id},
    )


def _verify_field_matches(
    issue: dict[str, Any],
    *,
    field: str,
    expected: Any,
    issue_id: int | str,
) -> None:
    """Raise RedmineWorkflowError if the post-mutation GET disagrees with what
    we asked Redmine to set. Used by the ``verify=True`` path of every mutation
    tool so the caller sees an exception (not a misleading success) when the
    change silently failed to apply.

    ``issue`` is the cleaned issue shape returned by ``get_issue`` — flat
    ``tracker_id`` / ``status_id`` / ``project_id`` ints, not nested dicts.
    """
    actual_key = f"{field}_id"
    actual = issue.get(actual_key)
    if actual == expected:
        return
    raise RedmineWorkflowError(
        f"Redmine issue {issue_id} {field} mismatch after mutation: "
        f"requested {field}={expected}, Redmine returned {field}={actual}",
        redmine_payload={"issue": issue, "expected": expected, "field": field},
    )


def _verify_issue_state(
    issue_id: int,
    *,
    field: str,
    expected: Any,
    include: str = "journals,attachments,relations",
) -> dict[str, Any]:
    """Re-fetch an issue and assert the requested field matches. Returns the
    cleaned post-change issue on success; raises RedmineWorkflowError on
    mismatch or RedmineNotFoundError if the GET itself fails."""
    fetched = get_issue(issue_id, include=include)
    _verify_field_matches(
        fetched, field=field, expected=expected, issue_id=issue_id
    )
    return fetched


def _verify_journal_added(
    issue_id: int,
    *,
    note_text: str,
    expected_count: int,
) -> dict[str, Any]:
    """Re-fetch an issue and assert the latest journal entry contains the note.
    Returns the post-change issue on success; raises RedmineWorkflowError when
    the journal entry did not land."""
    fetched = get_issue(issue_id, include="journals")
    journals = fetched.get("journals") or []
    if len(journals) < expected_count:
        raise RedmineWorkflowError(
            f"Redmine issue {issue_id} journal count after mutation: "
            f"requested at least {expected_count}, found {len(journals)}",
            redmine_payload={"expected_count": expected_count, "journals": journals},
        )
    latest = journals[-1] if journals else {}
    if note_text and (latest.get("notes") or "") != note_text:
        raise RedmineWorkflowError(
            f"Redmine issue {issue_id} journal note mismatch after mutation: "
            f"requested notes={note_text!r}, Redmine latest notes={latest.get('notes')!r}",
            redmine_payload={
                "expected_note": note_text,
                "latest_note": latest.get("notes"),
            },
        )
    return fetched


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
    verify: bool = False,
) -> dict[str, Any]:
    """Create a Redmine issue.

    Args:
        project_id: Redmine project id
        subject: Issue title
        description: Issue description
        tracker_id: Tracker id (default 3 = Task)
        assigned_to_id: Optional user id to assign
        priority_id: Priority id (default 2 = Normal)
        status_id: Initial status id (default 1 = New)
        allow_idea_tracker: Set True only when the user explicitly asked to file
            an Idea tracker ticket; agents default to False.
        verify: When True, re-fetches the issue after creation and returns
            ``{"ok": true, "issue": {...}}``. Raises RedmineWorkflowError if the
            post-state disagrees with the request (e.g. tracker coercion).
    """
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

    data = _request("POST", "/issues.json", json={"issue": issue})
    created_issue = data.get("issue", {})
    _assert_tracker_honored(created_issue, tracker_id)
    cleaned = _clean_issue(created_issue)
    issue_id = cleaned.get("id")
    if verify and issue_id is not None:
        post_state = _verify_issue_state(
            issue_id, field="tracker", expected=tracker_id
        )
        return {"ok": True, "issue": post_state}
    return cleaned


@mcp.tool()
def update_issue_status(
    issue_id: int, status_id: int, note: str = "", verify: bool = False
) -> dict[str, Any]:
    """Update a Redmine issue status and optionally add a note.

    Args:
        issue_id: Redmine issue id
        status_id: Target status id
        note: Optional journal note to record alongside the transition
        verify: When True, re-fetches the issue after the PUT and returns
            ``{"ok": true, "issue": {...}}``. Raises RedmineWorkflowError if the
            post-state status_id does not match (the canonical 2026-07-29 bug:
            PUT accepted silently, status never changed, agent saw {}).
    """
    issue: dict[str, Any] = {"status_id": status_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    fetched = get_issue(issue_id, include="journals")
    if verify:
        _verify_field_matches(
            fetched, field="status", expected=status_id, issue_id=issue_id
        )
        return {"ok": True, "issue": fetched}
    return fetched


@mcp.tool()
def move_issue(
    issue_id: int, project_id: int, note: str = "", verify: bool = False
) -> dict[str, Any]:
    """Move an issue to another Redmine project and optionally add a note.

    Args:
        issue_id: Redmine issue id
        project_id: Destination project id
        note: Optional journal note to record alongside the move
        verify: When True, re-fetches the issue after the PUT and returns
            ``{"ok": true, "issue": {...}}``. Raises RedmineWorkflowError if the
            issue's project_id does not match (the move_issue silent-failure
            pattern from 2026-07-29: journal note written, move rejected, no
            error indication).
    """
    _request("GET", f"/projects/{project_id}.json")
    issue: dict[str, Any] = {"project_id": project_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    updated = get_issue(issue_id, include="journals")
    if verify:
        _verify_field_matches(
            updated, field="project", expected=project_id, issue_id=issue_id
        )
        return {"ok": True, "issue": updated}
    if updated.get("project_id") not in (None, project_id):
        raise RedmineWorkflowError(
            f"Redmine issue {issue_id} project mismatch: requested project_id {project_id}, "
            f"Redmine returned project_id {updated.get('project_id')}",
            redmine_payload={"issue": updated, "expected_project_id": project_id},
        )
    return updated


@mcp.tool()
def update_issue_tracker(
    issue_id: int,
    tracker_id: int = AGENT_TASK_TRACKER_ID,
    note: str = "",
    allow_idea_tracker: bool = False,
    verify: bool = False,
) -> dict[str, Any]:
    """Change an issue tracker and optionally add a note.

    Args:
        issue_id: Redmine issue id
        tracker_id: Target tracker id (default 3 = Task)
        note: Optional journal note to record alongside the change
        allow_idea_tracker: Set True only when the user explicitly asked to use
            the Idea tracker; agents default to False.
        verify: When True, re-fetches the issue after the PUT and returns
            ``{"ok": true, "issue": {...}}``. Raises RedmineWorkflowError if the
            tracker was silently coerced (e.g. destination project lacks tracker
            6).
    """
    _validate_agent_tracker(tracker_id, allow_idea_tracker=allow_idea_tracker)
    issue: dict[str, Any] = {"tracker_id": tracker_id}
    if note:
        issue["notes"] = note
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})
    updated = get_issue(issue_id, include="journals")
    if verify:
        _verify_field_matches(
            updated, field="tracker", expected=tracker_id, issue_id=issue_id
        )
        return {"ok": True, "issue": updated}
    if updated.get("tracker_id") not in (None, tracker_id):
        raise RedmineWorkflowError(
            f"Redmine issue {issue_id} tracker mismatch: requested tracker_id {tracker_id}, "
            f"Redmine returned tracker_id {updated.get('tracker_id')}",
            redmine_payload={"issue": updated, "expected_tracker_id": tracker_id},
        )
    return updated


@mcp.tool()
def add_issue_note(
    issue_id: int, note: str, verify: bool = False
) -> dict[str, Any]:
    """Append a journal note to a Redmine issue.

    Args:
        issue_id: Redmine issue id
        note: Journal note text
        verify: When True, re-fetches the issue after the PUT and returns
            ``{"ok": true, "issue": {...}}``. Raises RedmineWorkflowError if the
            latest journal entry does not contain the supplied note (PUT was
            silently rejected).
    """
    _request("PUT", f"/issues/{issue_id}.json", json={"issue": {"notes": note}})
    fetched = get_issue(issue_id, include="journals")
    if verify:
        journals = fetched.get("journals") or []
        latest = journals[-1] if journals else {}
        if (latest.get("notes") or "") != note:
            raise RedmineWorkflowError(
                f"Redmine issue {issue_id} journal note mismatch after mutation: "
                f"requested notes={note!r}, Redmine latest notes={latest.get('notes')!r}",
                redmine_payload={
                    "expected_note": note,
                    "latest_note": latest.get("notes"),
                },
            )
        return {"ok": True, "issue": fetched}
    return fetched


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


@mcp.tool()
def list_wiki_pages(project_id: int) -> list[dict[str, Any]]:
    """List all wiki pages in a Redmine project."""
    data = _request("GET", f"/projects/{project_id}/wiki/index.json")
    return data.get("wiki_pages", [])


@mcp.tool()
def get_wiki_page(
    project_id: int,
    title: str,
    version: int | None = None,
) -> dict[str, Any]:
    """Get a Redmine wiki page by title, optionally at a specific version.

    Args:
        project_id: Redmine project id
        title: Wiki page title (e.g. "Home", "ADRs")
        version: Optional version number to retrieve a specific revision
    """
    path = f"/projects/{project_id}/wiki/{title}.json"
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    data = _request("GET", path, params=params)
    return data.get("wiki_page", {})


@mcp.tool()
def create_wiki_page(
    project_id: int,
    title: str,
    text: str,
    parent_title: str | None = None,
    comments: str = "",
) -> dict[str, Any]:
    """Create a new wiki page in a Redmine project.

    Redmine uses PUT for wiki page creation. If a page with the same title
    already exists, this will update it rather than create a new one — use
    the returned version number to distinguish.

    Args:
        project_id: Redmine project id
        title: Wiki page title
        text: Wiki page content (textile or markdown depending on project)
        parent_title: Optional parent wiki page title for hierarchy
        comments: Optional change comment
    """
    wiki_page: dict[str, Any] = {
        "text": text,
        "comments": comments,
    }
    if parent_title is not None:
        wiki_page["parent_title"] = parent_title
    data = _wiki_request(
        "PUT",
        f"/projects/{project_id}/wiki/{title}.json",
        json={"wiki_page": wiki_page},
    )
    return data.get("wiki_page", {})


@mcp.tool()
def update_wiki_page(
    project_id: int,
    title: str,
    text: str,
    version: int | None = None,
    comments: str = "",
) -> dict[str, Any]:
    """Update an existing wiki page with optimistic concurrency control.

    Args:
        project_id: Redmine project id
        title: Wiki page title
        text: New wiki page content
        version: Current version number for optimistic concurrency.
                 If provided and it doesn't match the server's current version,
                 a 409 error is raised.
        comments: Optional change comment
    """
    wiki_page: dict[str, Any] = {
        "text": text,
        "comments": comments,
    }
    if version is not None:
        wiki_page["version"] = version
    data = _wiki_request(
        "PUT",
        f"/projects/{project_id}/wiki/{title}.json",
        json={"wiki_page": wiki_page},
    )
    return data.get("wiki_page", {})


if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
