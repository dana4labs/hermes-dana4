---
name: dana4
description: "Work inside a Dana4 workspace as a serverless agent — poll and report tasks, read and answer channel chat, and read/write/search workspace documents. Use when the user mentions Dana4 — a Dana4 workspace, channel, task or document — or asks to register a Dana4 agent, check or claim Dana4 tasks, watch for new Dana4 activity, post to a Dana4 channel, or read/edit/search a Dana4 document."
---

# Dana4

Drive the Dana4 collaborative platform (humans + agents in shared workspaces) from this
session. The agent is registered as **serverless**: Dana4 never calls in, so everything
happens by pulling.

Everything below is done with the plugin's tools — `dana4_status`, `dana4_watch`,
`dana4_tasks_take`, `dana4_task_report`, `dana4_messages`, `dana4_message_send`,
`dana4_document` and `dana4_search`. There is nothing to shell out to and nothing to
install; the underlying client is Python standard library only.

For request and response shapes beyond what a tool schema says, read
`references/endpoints.md` before calling something you have not used before.

## Credentials

Resolved per field, first hit wins: the plugin's `host` config → `DANA4_HOST` /
`DANA4_USERNAME` / `DANA4_PASSWORD` env vars → `~/.config/dana4/credentials.json` (written by
`hermes dana4 register`, mode 0600). `dana4_status` shows what is configured and never
returns the password.

If nothing is configured, tell the user to run `hermes dana4 register` — **do not guess a
host**, and do not try to register from a tool. Registration is a terminal command on
purpose (see below).

## Registering (once, by a human)

```bash
hermes dana4 register --host https://app.dana4.example --email you@example.com
```

It generates the password, registers with `url: null` — which is what makes the agent
serverless — saves the credentials at mode 0600, and advertises the default `run_task`
capability.

**The email must come from the user. Never infer one** — not from git config, not from an
OpenAPI `info.contact` block, not from anything else. It is write-only, settable only at
register time, and a re-register does not change it; fixing a wrong one means deleting the
agent record server-side. It is also the identity a human uses to invite the agent into a
workspace, so a wrong address means the agent can never be invited at all.

## Working a task

`dana4_tasks_take` claims the next task assigned to this agent and returns
`{"task": ..., "payload": ...}`, or `task: null` when the queue is empty. Empty means
stop — do not fall back to the unassigned queue. The payload carries the capability's
declared inputs plus `workspace_id`, `task_id` and `channel_id`.

Then do the work and report:

- `dana4_task_report` with `status: running`, `progress: 0.1` on pickup
- `dana4_task_report` with `status: completed`, `progress: 1.0` and a `result_json`, or
  `status: failed` with a `message` saying why

**Always send a terminal report.** A task with no terminal update stays open forever.
Progress-only updates with a tiny delta may be throttled server-side, so never rely on one
to close a task.

Run one bounded pass at a time. Claim a task, work it, close it, stop — do not loop
unprompted. For continuous operation, put `dana4_watch` on a schedule (below).

## Documents

Every document write is tied to a task. `dana4_document` with `op: create`, `edit`,
`append` or `replace` needs a `task_id`; if you are not already inside a task, work one
that you claimed, or ask the user rather than inventing one.

`op: list`, `op: get` and `dana4_search` are the read side and need no task.

An `edit` returns `409` when the old text is not found and `400` when it matches more than
once — re-read the document and retry with more surrounding context.

## Chat

`dana4_messages` returns the agent inbox, plus a channel's full history when given a
`channel_id`.

The inbox (`POST /chats/take`) is **not the channel log**, and it has a blind
spot: it only returns chats the server routed to this agent. A message posted in a channel
the agent was never formally added to will never appear there. When a user says "I tagged
the agent on Dana4 but it didn't get it", suspect this first and read the channel directly.
Messages with a null sender are system/automated noise — filter them out.

**Do not reply on your own initiative.** These channels carry human conversation. Propose
the reply, and send it with `dana4_message_send` only once the user has approved it — or
when sending it is the task you claimed.

## Watching for new work

`dana4_watch` does one bounded pass over the inbox, any channels you name, and the
unassigned queue, and returns only what it has not reported before. It is detection only:
it never replies and never claims.

Always pass `channels` for the channels that matter, for the blind-spot
reason above, and pass `mention` with the agent's username so direct tags come back
flagged `mentions_me`.

It is safe to run on a schedule — a `cronjob` every couple of minutes is the intended use.
Deduplication is handled inside the tool, so a scheduled run stays quiet until something
genuinely new happens. If it returns `quiet: true`, there is nothing to report; say
nothing rather than narrating the absence.

## Gotchas

These are field-verified against a live deployment; the OpenAPI spec is wrong about
several of them.

- **Workspace membership is required and cannot be self-served.** There is no API to join
  a workspace. A human invites the agent by **email** from the Dana4 web app. Until then
  every workspace-scoped call returns `401`. A `401` on a workspace you expected to have
  is almost always a missing invite, not bad credentials — check `dana4_status` and then
  ask the user to invite the email.
- **Serverless agents always show `Offline` in the UI.** The orchestrator marks any agent
  with no `url` offline because there is no health endpoint to ping. This is by design and
  does not stop task polling. Do not "fix" it.
- **Nothing is pushed.** Tasks are assigned and then sit until `dana4_tasks_take` claims
  them; chat notifications are skipped entirely for serverless agents. Polling is the only
  path.
- **Capability input schemas must declare `workspace_id` and `task_id` as required
  strings.** The orchestrator validates the assembled payload against the schema and turns
  a missing required property into a workspace blocker instead of dispatching the task.
- Capabilities are advertised with `PATCH /agents/me`. `bio` and `description` are
  required strings on that call.
- `documents/edit` requires `task_id` in the body — it is what authorizes the write, and a
  missing one surfaces as `422 missing field task_id`.
- `GET /tasks/unassigned` returns the same queue on every call. `dana4_watch` dedupes for
  you; anything else polling it must track reported ids itself.

## Scope

Serverless (polling) only. Server mode — where Dana4 pushes to a webhook you host — needs a
publicly reachable HTTPS endpoint and is out of scope here; see
`references/serverless-mode.md` for the mode this plugin implements.
