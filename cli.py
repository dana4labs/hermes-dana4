#!/usr/bin/env python3
"""
cli.py — the `hermes dana4 ...` subcommand tree.

Enrollment lives here rather than in a tool on purpose: a person has to open a
link and approve the agent in the Dana4 web app, which only makes sense at an
interactive terminal.
"""

import json
import pathlib
import sys

from .dana4_client import (
    Dana4Client,
    Dana4Error,
    creds_path,
    load_creds,
    save_creds,
)

_SCHEMA = (
    pathlib.Path(__file__).parent
    / "skills"
    / "dana4"
    / "references"
    / "default-schema.json"
)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _creds(args) -> int:
    stored = load_creds()
    print(
        json.dumps(
            {
                "credentials_file": str(creds_path()),
                "exists": bool(stored),
                "host": stored.get("host", ""),
                "api_key_set": bool(stored.get("api_key")),
            },
            indent=2,
        )
    )
    return 0


def _status(args) -> int:
    try:
        client = Dana4Client(host=getattr(args, "host", None))
        print(
            json.dumps(
                {
                    "host": client.host,
                    "api_key_set": bool(client.api_key),
                    "active_tasks": client.tasks_active(),
                    "assigned_tasks": client.tasks_assigned(),
                },
                indent=2,
                default=str,
            )
        )
    except ValueError as e:
        # No API key: dana4_client's message names the Claude plugin's CLI.
        print(
            f"{e}\nRun `hermes dana4 enroll --username my-agent`, or set "
            "DANA4_API_KEY.",
            file=sys.stderr,
        )
        return 1
    except Dana4Error as e:
        print(f"Dana4 error: {e}", file=sys.stderr)
        if e.status == 401:
            print(
                "A 401 means the API key was rotated or the agent deleted, or "
                "it is not in that workspace. Its owner can check on the "
                "Agents page of the Dana4 web app.",
                file=sys.stderr,
            )
        return 1
    return 0


def _enroll(args) -> int:
    username = args.username or _ask("Agent username (a-z, 0-9, . _ -): ")
    if not username:
        print("A username is required.", file=sys.stderr)
        return 1

    try:
        client = Dana4Client(host=args.host)
        start = client.enroll_start(
            username, bio=args.bio or None, description=args.desc or None
        )
        print(
            f"\nOpen {start['verification_uri_complete']}\n"
            f"sign in, check the code {start['user_code']}, pick the "
            "workspaces, and approve. Waiting...",
            file=sys.stderr,
        )
        result = client.enroll_wait(start)
    except Dana4Error as e:
        if e.status == 409:
            print(
                f"The username '{username}' is taken on {client.host}; pick another.",
                file=sys.stderr,
            )
        else:
            print(f"Enrollment failed: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Enrollment failed: {e}", file=sys.stderr)
        return 1

    path = save_creds(client.host, result["api_key"])
    print(f"API key saved to {path} (mode 0600)", file=sys.stderr)

    try:
        schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
        # bio and description must be strings — the server rejects nulls.
        client.update_agent(
            schema, bio=args.bio or "", description=args.desc or ""
        )
    except (Dana4Error, OSError, ValueError) as e:
        print(
            f"Enrolled, but advertising capabilities failed: {e}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"username": username, "host": client.host}, indent=2))
    return 0


def setup(subparser) -> None:
    """Build the `hermes dana4` subcommand tree."""
    subs = subparser.add_subparsers(dest="dana4_command")

    reg = subs.add_parser(
        "enroll",
        help="Connect this Hermes agent to Dana4 (a person approves it)",
    )
    reg.add_argument("--host", help="Dana4 deployment URL")
    reg.add_argument(
        "--username", help="Agent username — permanent, asked for if omitted"
    )
    reg.add_argument("--bio", default="", help="Short agent bio")
    reg.add_argument("--desc", default="", help="What the agent does")

    st = subs.add_parser("status", help="Connection and open tasks")
    st.add_argument("--host", help="Override the configured host")

    subs.add_parser(
        "creds",
        help="Show configured credentials (offline, never the API key)",
    )

    subparser.set_defaults(func=handle)


def handle(args) -> int:
    sub = getattr(args, "dana4_command", None)
    if sub == "enroll":
        return _enroll(args)
    if sub == "status":
        return _status(args)
    if sub == "creds":
        return _creds(args)
    print("Usage: hermes dana4 {enroll|status|creds}", file=sys.stderr)
    return 2
