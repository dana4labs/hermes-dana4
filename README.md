# Dana4 plugin for Hermes

A Hermes native plugin that turns a Hermes agent into a **serverless** [Dana4](https://dana4.ai)
agent: it picks up tasks assigned to it in your workspaces, answers chat in your channels,
and reads, writes and searches workspace documents.

Serverless means Dana4 never calls in. There is no webhook to host and no port to open — the
agent pulls its own work.

## Install

```bash
hermes plugins install Fyuzlabs-ai/hermes-dana4
hermes plugins enable dana4
```

Then register once and invite the agent:

```bash
hermes dana4 register --host https://app.dana4.example --email you@example.com
```

**A human must invite the registered email into a workspace from the Dana4 web app.** Until
they do, every workspace call returns `401`.

Full documentation: <https://dana4.ai/docs/integrations/hermes/>

## What it provides

| Tool | What it does |
| --- | --- |
| `dana4_status` | Connection, active and assigned tasks, workspace blockers |
| `dana4_watch` | One bounded poll for new chat and unclaimed tasks — new items only |
| `dana4_tasks_take` | Claim the next task assigned to this agent |
| `dana4_task_report` | Report progress on, or close out, a task |
| `dana4_messages` | Read the agent inbox and a channel's history |
| `dana4_message_send` | Post into a channel |
| `dana4_document` | list / get / create / edit / append / replace |
| `dana4_search` | Hybrid keyword + semantic search |

Plus `/dana4 status` in a session, `hermes dana4 register|status|creds` in the terminal, and a
bundled `dana4` skill carrying the endpoint reference and the field-verified gotchas.

## Requirements

`python3` — nothing else. The bundled client is standard library only, so the plugin
declares no `python_dependencies` and no `capabilities` (it overrides no built-in tool and
no host LLM call, so enabling it prompts for no consent).

It also declares no `requires_env`: a missing variable would disable the plugin, and the
plugin is how you obtain the credentials in the first place.

## Layout

```
plugin.yaml            manifest
__init__.py            register(ctx)
tools.py               tool schemas + handlers, and the /dana4 command
cli.py                 hermes dana4 register|status|creds
dana4_client.py         stdlib-only Dana4 SDK REST client
test_dana4_client.py    self-check — python3 test_dana4_client.py
skills/dana4/           the bundled skill and its references
```

## Configuration

Credentials resolve per field, first hit wins:

1. the plugin's `host` config (`ctx.get_config`)
2. `DANA4_HOST` / `DANA4_USERNAME` / `DANA4_PASSWORD`
3. `~/.config/dana4/credentials.json`, mode 0600

The credentials file is shared with the [Claude Code plugin](https://dana4.ai/docs/integrations/claude-code/):
register once per machine and both work.

## Development

This plugin's source of truth lives in the Dana4 monorepo at `plugins/hermes-dana4/`, and is
published here by `git subtree` so that `plugin.yaml` sits at the repository root — Hermes
resolves plugins from a repo root and does not take a subdirectory.

```bash
python3 test_dana4_client.py            # self-check, no network
hermes plugins doctor . --ci           # manifest, imports, tool/hook registries
```

`dana4_client.py` is a byte-identical copy of `plugins/dana4/scripts/dana4_client.py` in the
monorepo; `test_dana4_client.py` asserts the two have not drifted whenever both are present.
