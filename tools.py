#!/usr/bin/env python3
"""
tools.py — the Dana4 tools Hermes registers, plus the /dana4 slash command.

Every handler obeys the native-plugin contract: it takes ``(args, **kwargs)``,
always returns a JSON string, and never raises. Failures come back as
``{"error": ...}`` so a tool call can't take the agent turn down with it.

Registration is deliberately NOT a tool — see cli.py.
"""

import json
from typing import Any, Dict, List, Optional

from .dana4_client import Dana4Client, Dana4Error, creds_path, load_creds

# The host process sets this once, in register(). A plugin is a singleton per
# profile, so a module global beats threading ctx through every closure.
_CTX = None

# dana4_watch keeps the ids it has already reported in ctx.state. Trimmed to the
# most recent N so the state stays far below the 10 MiB per-plugin ceiling.
_SEEN_CAP = 500


def bind(ctx) -> None:
    """Give the handlers access to ctx.state / ctx.get_config."""
    global _CTX
    _CTX = ctx


def _client() -> Dana4Client:
    """Build a client. Config host is the weakest source; env and the creds
    file still win inside Dana4Client itself."""
    host = None
    if _CTX is not None:
        host = _CTX.get_config("host", "") or None
    return Dana4Client(host=host)


def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _tool(fn):
    """Wrap a handler so it always returns JSON and never raises."""

    def wrapper(args: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        try:
            return _ok(fn(args or {}))
        except Dana4Error as e:
            out: Dict[str, Any] = {"error": str(e), "status": e.status}
            if e.status == 401:
                out["hint"] = (
                    "401 is almost always a missing workspace invite, not a bad "
                    "password. A human must invite the agent's registered email "
                    "into the workspace from the Dana4 web app."
                )
            return _ok(out)
        except KeyError as e:
            return _ok({"error": f"missing required argument {e.args[0]!r}"})
        except ValueError as e:
            # Dana4Client raises this when no host or no credentials are set.
            return _ok(
                {"error": str(e), "hint": "run `hermes dana4 register` first"}
            )
        except Exception as e:  # never let a tool break the turn
            return _ok({"error": f"{type(e).__name__}: {e}"})

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------- #
# Handlers
# ---------------------------------------------------------------------- #
@_tool
def dana4_status(args):
    """Connection, open tasks and blockers.

    This is the tool to reach for when something is not working, so the local
    configuration is always reported even when the calls themselves fail —
    a bare error here would hide the very thing you need to see.
    """
    stored = load_creds()
    out: Dict[str, Any] = {
        "credentials_file": str(creds_path()),
        "credentials_file_exists": bool(stored),
        "note": (
            "Serverless agents always show Offline in Dana4 — there is no health "
            "endpoint to ping. That is expected and does not need fixing."
        ),
    }
    try:
        client = _client()
    except ValueError as e:
        out["host"] = ""
        out["error"] = str(e)
        out["hint"] = "no host configured — run `hermes dana4 register`"
        return out

    out["host"] = client.host
    out["username"] = client.username
    out["password_set"] = bool(client.password)

    workspace_id = args.get("workspace_id")
    for key, call in (
        ("active_tasks", client.tasks_active),
        ("assigned_tasks", client.tasks_assigned),
        (
            "blockers",
            (lambda: client.blockers(workspace_id)) if workspace_id else None,
        ),
    ):
        if call is None:
            continue
        try:
            out[key] = call()
        except (Dana4Error, ValueError) as e:
            out.setdefault("errors", {})[key] = str(e)
    if "errors" in out and not client.username:
        out["hint"] = "not registered yet — run `hermes dana4 register`"
    elif "errors" in out:
        out["hint"] = (
            "if these are 401s, the agent's email is probably not invited into "
            "the workspace yet; a human has to do that from the Dana4 web app"
        )
    return out


def _new_ids(seen: List[str], candidates: List[str]) -> List[str]:
    known = set(seen)
    return [c for c in candidates if c and c not in known]


@_tool
def dana4_watch(args):
    """One bounded pass over inbox, named channels and the unassigned queue.

    Detection only — it never replies and never claims. Dedup lives in
    ctx.state, so a scheduled run only ever surfaces genuinely new items.
    """
    client = _client()
    mention = args.get("mention")
    workspace_id = args.get("workspace_id")
    channels = args.get("channels") or []
    if isinstance(channels, str):  # tolerate a comma-separated string
        channels = [c.strip() for c in channels.split(",") if c.strip()]

    state = {"msgs": [], "tasks": []}
    if _CTX is not None:
        stored = _CTX.state.get("seen", state)
        if isinstance(stored, dict):
            state = {
                "msgs": list(stored.get("msgs") or []),
                "tasks": list(stored.get("tasks") or []),
            }

    collected: List[Dict[str, Any]] = []

    def absorb(messages, channel_override=None):
        # Both message endpoints answer with a ChatThread — {channel_id, intent,
        # plan, messages} — not a bare list. Iterating that dict would walk its
        # keys and silently collect nothing.
        if isinstance(messages, dict):
            messages = messages.get("messages")
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            # from: null marks system / automated traffic — not worth waking
            # anyone up for.
            if m.get("from") is None and m.get("from_username") is None:
                continue
            mid = m.get("id")
            if not mid or mid in {c["id"] for c in collected}:
                continue
            body = m.get("message", "")
            collected.append(
                {
                    "id": mid,
                    "workspace_id": m.get("workspace_id", workspace_id),
                    "channel_id": channel_override or m.get("channel_id"),
                    "from": m.get("from_username") or m.get("from"),
                    "message": body,
                    "mentions_me": bool(mention and f"@{mention}" in body),
                }
            )

    errors: Dict[str, str] = {}

    # 1. The agent inbox: only chats the server routed to this agent.
    try:
        absorb(client.messages_read_next_chat())
    except Dana4Error as e:
        errors["inbox"] = str(e)

    # 2. Named channels. read_next_chat has a blind spot — a message posted
    #    into a channel the agent was never formally added to never reaches the
    #    inbox — so channels have to be polled directly.
    for channel_id in channels:
        try:
            absorb(
                client.messages_fetch(channel_id), channel_override=channel_id
            )
        except Dana4Error as e:
            errors[f"channel:{channel_id}"] = str(e)

    # 3. Unassigned work. This endpoint returns the same queue on every call,
    #    which is exactly why the dedup below is not optional.
    tasks: List[Any] = []
    try:
        tasks = client.tasks_unassigned() or []
    except Dana4Error as e:
        errors["tasks"] = str(e)

    new_msgs = [c for c in collected if c["id"] not in set(state["msgs"])]
    new_tasks = [
        t
        for t in tasks
        if isinstance(t, dict) and t.get("id") not in set(state["tasks"])
    ]

    state["msgs"] = (state["msgs"] + [c["id"] for c in new_msgs])[-_SEEN_CAP:]
    state["tasks"] = (
        state["tasks"] + [t.get("id") for t in new_tasks if t.get("id")]
    )[-_SEEN_CAP:]
    if _CTX is not None:
        _CTX.state.set("seen", state)

    out: Dict[str, Any] = {
        "new_messages": new_msgs,
        "new_tasks": new_tasks,
        "quiet": not new_msgs and not new_tasks,
    }
    if errors:
        out["errors"] = errors
    return out


@_tool
def dana4_tasks_take(args):
    """Claim the next task assigned to this agent."""
    taken = _client().tasks_take()
    if taken is None:
        return {
            "task": None,
            "note": "nothing queued — stop here, do not poll the unassigned queue as a fallback",
        }
    return taken


@_tool
def dana4_task_report(args):
    """Report progress on, or close out, a task."""
    return {
        "reported": _client().task_report(
            task_id=args["task_id"],
            status=args.get("status"),
            progress=args.get("progress"),
            message=args.get("message"),
            result_json=args.get("result_json"),
        )
    }


@_tool
def dana4_messages(args):
    """Read the agent inbox, and a named channel's history if given."""
    client = _client()
    out: Dict[str, Any] = {"inbox": client.messages_read_next_chat()}
    channel_id = args.get("channel_id")
    if channel_id:
        out["channel"] = client.messages_fetch(channel_id)
    return out


@_tool
def dana4_message_send(args):
    """Post a message into a workspace channel."""
    return {
        "sent": _client().message_send(
            workspace_id=args["workspace_id"],
            message=args["message"],
            channel_id=args.get("channel_id"),
            category=args.get("category"),
        )
    }


@_tool
def dana4_document(args):
    """One entry point for the whole document family, switched on `op`."""
    client = _client()
    op = args["op"]
    task_id = args.get("task_id")
    if op == "list":
        return client.documents_list(args["workspace_id"])
    if op == "get":
        return client.document_get_by_path(args["workspace_id"], args["path"])
    if op == "create":
        return client.document_create(
            workspace_id=args["workspace_id"],
            path=args["path"],
            title=args["title"],
            content=args.get("content", ""),
            task_id=task_id,
            document_type=args.get("document_type", "default"),
        )
    if op == "edit":
        return client.document_edit(
            path=args["path"],
            old_string=args["old_string"],
            new_string=args["new_string"],
            task_id=task_id,
            replace_all=bool(args.get("replace_all", False)),
        )
    if op == "append":
        return client.document_append(
            path=args["path"], content=args["content"], task_id=task_id
        )
    if op == "replace":
        return client.document_replace(
            path=args["path"], content=args["content"], task_id=task_id
        )
    return {"error": f"unknown op {op!r}"}


@_tool
def dana4_search(args):
    """Hybrid keyword + semantic search over a workspace."""
    return _client().search(
        args["workspace_id"], args["query"], limit=args.get("limit")
    )


# ---------------------------------------------------------------------- #
# Schemas
# ---------------------------------------------------------------------- #
_WS = {"type": "string", "description": "Dana4 workspace id."}
_TASK_ID = {
    "type": "string",
    "description": (
        "Task this write belongs to. REQUIRED for edit/append/replace — Dana4 "
        "ties every document write to a task and returns 422 without it, even "
        "though the published OpenAPI schema omits the field."
    ),
}

SCHEMAS = {
    "dana4_status": {
        "name": "dana4_status",
        "description": (
            "Show the Dana4 connection (host, username, whether a password is "
            "stored), the agent's active and assigned tasks, and — with a "
            "workspace_id — that workspace's blockers. Read-only. Start here "
            "when something is not working."
        ),
        "parameters": {
            "type": "object",
            "properties": {"workspace_id": _WS},
            "required": [],
        },
    },
    "dana4_watch": {
        "name": "dana4_watch",
        "description": (
            "Poll Dana4 once for anything new: chat in the agent inbox, chat in "
            "named channels, and unclaimed tasks. Returns only items not seen "
            "on a previous call, so it is safe to run on a schedule. Detection "
            "only — it never replies and never claims. Always pass `channels` "
            "for channels you care about: the inbox only carries chat the "
            "server routed to the agent, so a message posted into a channel the "
            "agent was never formally added to will not show up otherwise."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": _WS,
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Channel ids to poll directly.",
                },
                "mention": {
                    "type": "string",
                    "description": "Agent username; messages containing @username are flagged mentions_me.",
                },
            },
            "required": [],
        },
    },
    "dana4_tasks_take": {
        "name": "dana4_tasks_take",
        "description": (
            "Claim the next task assigned to this agent and get its payload. "
            "Returns task: null when the queue is empty — that means stop, not "
            "fall back to the unassigned queue. Once a task is taken it MUST "
            "get a terminal dana4_task_report or it stays open forever."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "dana4_task_report": {
        "name": "dana4_task_report",
        "description": (
            "Report progress on a task, or close it out. Every claimed task "
            "needs a terminal report: completed with progress 1.0, or failed "
            "with a message saying why."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task to report on.",
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "pending",
                        "assigned",
                        "blocked",
                        "retry",
                        "running",
                        "completed",
                        "failed",
                    ],
                },
                "progress": {
                    "type": "number",
                    "description": "0.0 to 1.0. Send 1.0 with completed.",
                },
                "message": {
                    "type": "string",
                    "description": "Human-readable note; say why on failed.",
                },
                "result_json": {
                    "type": "object",
                    "description": "Structured result for a completed task.",
                },
            },
            "required": ["task_id"],
        },
    },
    "dana4_messages": {
        "name": "dana4_messages",
        "description": (
            "Read chat. Always returns the agent inbox; also returns a "
            "channel's history when channel_id is given. The "
            "inbox is not the channel log — if a user says they tagged the "
            "agent and got no answer, read the channel directly. Messages with "
            "a null sender are system traffic; ignore them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": _WS,
                "channel_id": {
                    "type": "string",
                    "description": "Channel to read in full.",
                },
            },
            "required": [],
        },
    },
    "dana4_message_send": {
        "name": "dana4_message_send",
        "description": (
            "Post a message into a Dana4 channel. These channels carry human "
            "conversation — send only what the user asked you to send."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": _WS,
                "message": {"type": "string", "description": "Message body."},
                "channel_id": {
                    "type": "string",
                    "description": "Target channel.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "default",
                        "dependencies_status",
                        "error",
                        "transient",
                    ],
                },
            },
            "required": ["workspace_id", "message"],
        },
    },
    "dana4_document": {
        "name": "dana4_document",
        "description": (
            "Read and write workspace documents. op=list needs workspace_id; "
            "get needs workspace_id and path; create needs workspace_id, path "
            "and title; edit needs path, old_string and new_string; append and "
            "replace need path and content. Every write needs task_id. An edit "
            "returns 409 when the old_string is not found and 400 when it "
            "matches more than once — re-read the document and retry."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "list",
                        "get",
                        "create",
                        "edit",
                        "append",
                        "replace",
                    ],
                },
                "workspace_id": _WS,
                "path": {
                    "type": "string",
                    "description": "Document path, e.g. /notes/intro.",
                },
                "title": {
                    "type": "string",
                    "description": "Title, for create.",
                },
                "content": {
                    "type": "string",
                    "description": "Body, for create/append/replace.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to replace, for edit.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text, for edit.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every match instead of erroring on several.",
                },
                "document_type": {
                    "type": "string",
                    "enum": [
                        "default",
                        "folder",
                        "project",
                        "html",
                        "image",
                        "audio",
                        "video",
                    ],
                },
                "task_id": _TASK_ID,
            },
            "required": ["op"],
        },
    },
    "dana4_search": {
        "name": "dana4_search",
        "description": "Hybrid keyword and semantic search across a workspace's documents.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": _WS,
                "query": {"type": "string", "description": "What to look for."},
                "limit": {"type": "integer", "description": "Max results."},
            },
            "required": ["workspace_id", "query"],
        },
    },
}

HANDLERS = {
    "dana4_status": dana4_status,
    "dana4_watch": dana4_watch,
    "dana4_tasks_take": dana4_tasks_take,
    "dana4_task_report": dana4_task_report,
    "dana4_messages": dana4_messages,
    "dana4_message_send": dana4_message_send,
    "dana4_document": dana4_document,
    "dana4_search": dana4_search,
}


# ---------------------------------------------------------------------- #
# /dana4 slash command
# ---------------------------------------------------------------------- #
def slash_dana4(raw_args: str) -> str:
    """`/dana4 [status|help]` — a quick read-only look at the connection."""
    parts = (raw_args or "").split()
    sub = parts[0] if parts else "status"
    if sub in ("help", "-h", "--help"):
        return (
            "/dana4 status [workspace_id] — connection, open tasks, blockers\n"
            "hermes dana4 register       — register this agent (once, at setup)\n"
            "hermes dana4 creds          — show what is configured, offline\n"
            "Ask in plain language for anything else: 'check my Dana4 tasks'."
        )
    if sub != "status":
        return f"Unknown subcommand {sub!r}. Try /dana4 help."

    payload = json.loads(
        dana4_status({"workspace_id": parts[1]} if len(parts) > 1 else {})
    )
    lines = [
        f"host:     {payload.get('host') or '(not set)'}",
        f"username: {payload.get('username') or '(not registered)'}",
        f"password: {'stored' if payload.get('password_set') else 'MISSING'}",
    ]
    for label, key in (
        ("active", "active_tasks"),
        ("assigned", "assigned_tasks"),
        ("blockers", "blockers"),
    ):
        if key in payload:
            lines.append(f"{label + ':':9} {len(payload[key] or [])}")
    for key, err in (payload.get("errors") or {}).items():
        lines.append(f"! {key}: {err}")
    if payload.get("error"):
        lines.append(f"! {payload['error']}")
    if payload.get("hint"):
        lines.append(payload["hint"])
    return "\n".join(lines)


# Shown beside the tool call in the Hermes UI.
EMOJI = {
    "dana4_status": "🔌",
    "dana4_watch": "👀",
    "dana4_tasks_take": "📥",
    "dana4_task_report": "✅",
    "dana4_messages": "💬",
    "dana4_message_send": "📤",
    "dana4_document": "📄",
    "dana4_search": "🔍",
}
