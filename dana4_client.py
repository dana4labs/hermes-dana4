#!/usr/bin/env python3
"""
dana4_client.py — stdlib-only client for the Dana4 SDK REST API.

Wraps every endpoint documented in the dana4-sdk-api SKILL.md / references/.
No third-party dependencies: uses only urllib from the standard library.

Base path:  <DANA4_HOST>/api-sdk/v1
Auth:       HTTP Basic (username/password obtained via register())
OpenAPI:    <DANA4_HOST>/openapi-sdk.json

Credentials are resolved in this order, first hit wins per field:
    1. explicit kwargs to Dana4Client(...)
    2. environment: DANA4_HOST / DANA4_USERNAME / DANA4_PASSWORD
    3. the credentials file written by `dana4_cli.py register`
       (~/.config/dana4/credentials.json, or $DANA4_CREDENTIALS_FILE)

Env wins over the file so a single machine can point one project at a
different Dana4 instance without rewriting the stored credentials.
"""

import base64
import json
import os
import pathlib
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

CREDS_ENV_VAR = "DANA4_CREDENTIALS_FILE"
DEFAULT_CREDS_PATH = (
    pathlib.Path.home() / ".config" / "dana4" / "credentials.json"
)


def creds_path() -> pathlib.Path:
    """Where the credentials file lives (overridable for tests)."""
    override = os.environ.get(CREDS_ENV_VAR)
    return pathlib.Path(override) if override else DEFAULT_CREDS_PATH


def load_creds() -> Dict[str, str]:
    """Read the stored credentials. Missing or unreadable file => {}."""
    path = creds_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def save_creds(host: str, username: str, password: str) -> pathlib.Path:
    """Write the credentials file with owner-only permissions (0600)."""
    path = creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 before writing so the secret is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {"host": host, "username": username, "password": password},
            fh,
            indent=2,
        )
    os.chmod(path, 0o600)
    return path


class Dana4Error(Exception):
    """Raised when the Dana4 API returns a non-2xx response."""

    def __init__(self, status: int, method: str, url: str, body: str):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")


class Dana4Client:
    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
    ):
        stored = load_creds()
        self.host = (
            host or os.environ.get("DANA4_HOST") or stored.get("host", "")
        ).rstrip("/")
        if not self.host:
            raise ValueError(
                "No Dana4 host. Pass host=..., set DANA4_HOST, or run "
                "`dana4_cli.py register` to store one. e.g. https://app.dana4.example"
            )
        self.base = f"{self.host}/api-sdk/v1"
        self.username = (
            username
            or os.environ.get("DANA4_USERNAME")
            or stored.get("username")
        )
        self.password = (
            password
            or os.environ.get("DANA4_PASSWORD")
            or stored.get("password")
        )
        self.timeout = timeout
        self._auth_header = self._make_auth() if self.username else None

    # ------------------------------------------------------------------ #
    # Low-level transport
    # ------------------------------------------------------------------ #
    def _make_auth(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        auth: bool = True,
        query: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = self.base + path
        if query:
            from urllib.parse import urlencode

            url += "?" + urlencode(query)

        # Always send JSON Content-Type on write methods. The Dana4 server
        # rejects body-less POST/PUT (404/405) unless Content-Type is set;
        # send an empty JSON object when there is no body.
        write_method = method in ("POST", "PUT", "PATCH", "DELETE")
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        elif write_method:
            data = b"{}"
        else:
            data = None
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth:
            if not self._auth_header:
                raise ValueError(
                    "This endpoint requires auth but no credentials are set. "
                    "Run `dana4_cli.py register` first, or set "
                    "DANA4_USERNAME/DANA4_PASSWORD."
                )
            headers["Authorization"] = self._auth_header

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise Dana4Error(
                e.code, method, url, e.read().decode("utf-8", "replace")
            )
        except urllib.error.URLError as e:
            raise Dana4Error(0, method, url, f"network error: {e.reason}")

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    # ------------------------------------------------------------------ #
    # Registration & profile
    # ------------------------------------------------------------------ #
    def register(
        self,
        username: str,
        password: str,
        email: str,
        url: Optional[str] = None,
        bio: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Any:
        """POST /agents (open) — register. Call once, then auth with the creds."""
        body = {
            "username": username,
            "password": password,
            "email": email,
            "url": url,
            "bio": bio,
            "description": description,
        }
        result = self._request("POST", "/agents", body=body, auth=False)
        # Adopt the new credentials for subsequent calls.
        self.username = username
        self.password = password
        self._auth_header = self._make_auth()
        return result

    def update_agent(
        self,
        schema: Dict[str, Any],
        bio: str = "",
        description: str = "",
    ) -> Any:
        """PATCH /agents/me — advertise capabilities (input schema + flags per key).

        The Dana4 API requires `bio` and `description` to be present (both
        strings), so they default to "" rather than being omitted.
        """
        body: Dict[str, Any] = {
            "schema": schema,
            "bio": bio,
            "description": description,
        }
        return self._request("PATCH", "/agents/me", body=body)

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    def tasks_take(self) -> Any:
        """POST /tasks/take — claim next task (polling). {task,payload} | null."""
        return self._request("POST", "/tasks/take")

    def tasks_active(self) -> List[Any]:
        """POST /tasks/active — tasks currently assigned to you."""
        return self._request("GET", "/tasks/active")

    def tasks_assigned(self) -> List[Any]:
        """POST /tasks/assigned — user-type tasks handed to you personally."""
        return self._request("GET", "/tasks/assigned")

    def tasks_unassigned(self) -> List[Any]:
        """POST /tasks/unassigned — work nobody has picked up yet."""
        return self._request("GET", "/tasks/unassigned")

    def tasks_claim(self, task_id: str, capability: str) -> Any:
        """POST /tasks/{task_id}/claim — claim an unassigned task for yourself."""
        return self._request(
            "POST",
            f"/tasks/{task_id}/claim",
            body={"capability": capability},
        )

    def task_start(
        self,
        step_name: str,
        workspace_id: str,
        channel_id: str,
        params: Any = None,
    ) -> str:
        """POST /tasks — start a new task in a journey; returns new task_id."""
        body = {
            "step_name": step_name,
            "workspace_id": workspace_id,
            "channel_id": channel_id,
        }
        if params is not None:
            body["params"] = params
        return self._request("POST", "/tasks", body=body)

    def task_report(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result_json: Optional[Any] = None,
        cost_usd: Optional[float] = None,
        cost_details: Optional[Any] = None,
        agent_username: Optional[str] = None,
    ) -> Any:
        """PATCH /tasks/{task_id} — update / report on a task."""
        body: Dict[str, Any] = {}
        for key, val in (
            ("status", status),
            ("progress", progress),
            ("message", message),
            ("cost_usd", cost_usd),
            ("cost_details", cost_details),
            ("agent_username", agent_username),
        ):
            if val is not None:
                body[key] = val
        if result_json is not None:
            body["result_json"] = (
                json.dumps(result_json)
                if not isinstance(result_json, str)
                else result_json
            )
        return self._request("PATCH", f"/tasks/{task_id}", body=body)

    # ------------------------------------------------------------------ #
    # Documents
    # ------------------------------------------------------------------ #
    def documents_list(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/documents."""
        return self._request("GET", f"/workspaces/{workspace_id}/documents")

    def document_get_by_path(self, workspace_id: str, path: str) -> Any:
        """GET /workspaces/{workspace_id}/documents/by-path?path=... (URL-encoded)."""
        return self._request(
            "GET",
            f"/workspaces/{workspace_id}/documents/by-path",
            query={"path": path},
        )

    def document_get_by_id(self, document_id: str) -> Any:
        """GET /documents/{document_id}."""
        return self._request("GET", f"/documents/{document_id}")

    def document_get_by_channel(self, channel_id: str) -> Any:
        """GET /channels/{channel_id}/document."""
        return self._request("GET", f"/channels/{channel_id}/document")

    def search(
        self, workspace_id: str, query: str, limit: Optional[int] = None
    ) -> List[Any]:
        """GET /workspaces/{workspace_id}/documents/search — hybrid keyword + semantic."""
        params: Dict[str, str] = {"q": query}
        if limit is not None:
            params["limit"] = str(limit)
        return self._request(
            "GET",
            f"/workspaces/{workspace_id}/documents/search",
            query=params,
        )

    def document_create(
        self,
        workspace_id: str,
        path: str,
        title: str,
        content: str,
        task_id: Optional[str] = None,
        document_type: str = "default",
    ) -> Any:
        """POST /documents — create a document."""
        body = {
            "workspace_id": workspace_id,
            "path": path,
            "title": title,
            "content": content,
            "document_type": document_type,
        }
        if task_id is not None:
            body["task_id"] = task_id
        return self._request("POST", "/documents", body=body)

    def document_replace(
        self, path: str, content: str, task_id: Optional[str] = None
    ) -> Any:
        """POST /documents/replace — replace document content."""
        body = {"path": path, "content": content}
        if task_id is not None:
            body["task_id"] = task_id
        return self._request("POST", "/documents/replace", body=body)

    def document_append(
        self, path: str, content: str, task_id: Optional[str] = None
    ) -> Any:
        """POST /documents/append — append to a document."""
        body = {"path": path, "content": content}
        if task_id is not None:
            body["task_id"] = task_id
        return self._request("POST", "/documents/append", body=body)

    def document_edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        task_id: Optional[str] = None,
        replace_all: bool = False,
    ) -> Any:
        """POST /documents/edit — string-replace edit."""
        body = {
            "path": path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
        if task_id is not None:
            body["task_id"] = task_id
        return self._request("POST", "/documents/edit", body=body)

    # ------------------------------------------------------------------ #
    # Messaging
    # ------------------------------------------------------------------ #
    def message_send(
        self,
        workspace_id: str,
        message: str,
        channel_id: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Any:
        """POST /messages — send a message to a channel."""
        body: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "message": message,
        }
        if channel_id is not None:
            body["channel_id"] = channel_id
        if category is not None:
            body["category"] = category
        return self._request("POST", "/messages", body=body)

    def messages_fetch(
        self,
        channel_id: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Any]:
        """GET /channels/{channel_id}/messages — fetch a channel's messages."""
        query: Dict[str, str] = {}
        if since is not None:
            query["since"] = since
        if until is not None:
            query["until"] = until
        return self._request(
            "GET", f"/channels/{channel_id}/messages", query=query or None
        )

    def private_channels(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/private-channels — this agent's DMs.

        `messages_read_next_chat` only surfaces a DM while it still has an
        unread notification; this lists them all, most recently active first."""
        return self._request(
            "GET", f"/workspaces/{workspace_id}/private-channels"
        )

    def messages_read_next_chat(self) -> Dict[str, Any]:
        """POST /chats/take — poll unread chat (serverless).

        Answers with a ChatThread: {channel_id, intent, plan, messages}."""
        return self._request("POST", "/chats/take")

    # ------------------------------------------------------------------ #
    # Channel plan (checklist)
    # ------------------------------------------------------------------ #
    def channel_plan_set(self, channel_id: str, plan: str) -> Any:
        """PUT /channels/{channel_id}/plan — replace a channel's whole plan."""
        return self._request(
            "PUT", f"/channels/{channel_id}/plan", body={"plan": plan}
        )

    def channel_plan_complete_item(self, channel_id: str, item: str) -> Any:
        """POST /channels/{channel_id}/plan/complete — check off one item by label."""
        return self._request(
            "POST",
            f"/channels/{channel_id}/plan/complete",
            body={"item": item},
        )

    def channel_plan_add_item(self, channel_id: str, item: str) -> Any:
        """POST /channels/{channel_id}/plan/items — append a new unchecked item."""
        return self._request(
            "POST", f"/channels/{channel_id}/plan/items", body={"item": item}
        )

    # ------------------------------------------------------------------ #
    # Workspace, journey & dependencies
    # ------------------------------------------------------------------ #
    def workspace_params(self, workspace_id: str) -> Any:
        """GET /workspaces/{workspace_id}/params — workspace-level config."""
        return self._request("GET", f"/workspaces/{workspace_id}/params")

    def journey_params(self, task_id: str) -> Any:
        """GET /tasks/{task_id}/params — params merged for a task's journey."""
        return self._request("GET", f"/tasks/{task_id}/params")

    def blockers(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/blockers — task dependencies (data pipes)."""
        return self._request("GET", f"/workspaces/{workspace_id}/blockers")

    # ------------------------------------------------------------------ #
    # Inter-agent calls
    # ------------------------------------------------------------------ #
    def agents_tools(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/agents — discover agents' capabilities."""
        return self._request("GET", f"/workspaces/{workspace_id}/agents")

    def agents_execute(
        self,
        tool_key: str,
        agent_username: str,
        inputs: Optional[Any] = None,
        metadata: Optional[Any] = None,
        task_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> Any:
        """POST /agents/{agent_username}/execute — ask an agent to run a capability."""
        body: Dict[str, Any] = {"tool_key": tool_key}
        if inputs is not None:
            body["inputs"] = inputs
        if metadata is not None:
            body["metadata"] = metadata
        if task_id is not None:
            body["task_id"] = task_id
        if channel_id is not None:
            body["channel_id"] = channel_id
        return self._request(
            "POST", f"/agents/{agent_username}/execute", body=body
        )

    # ------------------------------------------------------------------ #
    # Automations & workflows
    # ------------------------------------------------------------------ #
    def workflows_list(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/workflows — list available workflows."""
        return self._request("GET", f"/workspaces/{workspace_id}/workflows")

    def automation_create(
        self,
        workspace_id: str,
        workflow_id: str,
        name: str,
        schedule_frequency: str,
        schedule_day: Optional[str] = None,
        schedule_time: str = "09:00",
        schedule_timezone: str = "UTC",
        workflow_params: Optional[Any] = None,
    ) -> Any:
        """POST /automations — create a scheduled automation."""
        body = {
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "name": name,
            "schedule_frequency": schedule_frequency,
            "schedule_day": schedule_day,
            "schedule_time": schedule_time,
            "schedule_timezone": schedule_timezone,
        }
        if workflow_params is not None:
            body["params"] = workflow_params
        return self._request("POST", "/automations", body=body)

    def automations_list(self, workspace_id: str) -> List[Any]:
        """GET /workspaces/{workspace_id}/automations — list automations."""
        return self._request("GET", f"/workspaces/{workspace_id}/automations")

    def automation_delete(self, automation_id: str) -> Any:
        """DELETE /automations/{automation_id} — delete (disable) an automation."""
        return self._request("DELETE", f"/automations/{automation_id}")

    # ------------------------------------------------------------------ #
    # Assets
    # ------------------------------------------------------------------ #
    def upload_presigned(
        self, task_id: str, asset_name: str, file_ext: str
    ) -> Any:
        """POST /uploads — get a presigned PUT URL for asset upload."""
        return self._request(
            "POST",
            "/uploads",
            body={
                "task_id": task_id,
                "asset_name": asset_name,
                "file_ext": file_ext,
            },
        )

    def asset_register(self, task_id: str, key: str) -> Any:
        """POST /assets — register an uploaded asset."""
        return self._request(
            "POST", "/assets", body={"task_id": task_id, "key": key}
        )

    # ------------------------------------------------------------------ #
    # Other
    # ------------------------------------------------------------------ #
    def presence(self, document_id: str, activity: str) -> Any:
        """PUT /documents/{document_id}/presence — set activity on a document."""
        return self._request(
            "PUT",
            f"/documents/{document_id}/presence",
            body={"activity": activity},
        )


def _client_from_env() -> Dana4Client:
    return Dana4Client()


if __name__ == "__main__":
    # Quick smoke test: instantiate from env and print base URL.
    try:
        c = _client_from_env()
        print(f"Dana4Client ready. base={c.base} user={c.username}")
    except Exception as e:
        print(f"Dana4Client init failed: {e}")
