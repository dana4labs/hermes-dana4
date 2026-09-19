#!/usr/bin/env python3
"""
cli.py — the `hermes dana4 ...` subcommand tree.

Registration lives here rather than in a tool on purpose. The email is set
once, at register time, is never returned by any GET, and is the identity a
human types when inviting the agent into a workspace. Getting it wrong means
deleting the agent record server-side and starting over, so it is asked for,
never inferred.
"""

import json
import pathlib
import secrets
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
                "username": stored.get("username", ""),
                "password_set": bool(stored.get("password")),
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
                    "username": client.username,
                    "password_set": bool(client.password),
                    "active_tasks": client.tasks_active(),
                    "assigned_tasks": client.tasks_assigned(),
                },
                indent=2,
                default=str,
            )
        )
    except ValueError:
        # dana4_client is a byte-identical copy of the Claude plugin's, so its
        # "no host" message names that plugin's CLI. Say the Hermes thing.
        print(
            "No Dana4 host configured. Run `hermes dana4 register --host "
            "https://app.dana4.example --email you@example.com`, or set "
            "DANA4_HOST.",
            file=sys.stderr,
        )
        return 1
    except Dana4Error as e:
        print(f"Dana4 error: {e}", file=sys.stderr)
        if e.status == 401:
            print(
                "A 401 is usually a missing workspace invite, not a bad "
                "password — a human has to invite the agent's email from the "
                "Dana4 web app.",
                file=sys.stderr,
            )
        return 1
    return 0


def _register(args) -> int:
    host = args.host or _ask("Dana4 host (e.g. https://app.dana4.example): ")
    if not host:
        print("A host is required.", file=sys.stderr)
        return 1

    email = args.email or _ask(
        "Agent email (permanent — you invite this address): "
    )
    if not email or "@" not in email:
        print(
            "A real email address is required. It cannot be changed later, and "
            "it is the address a human invites into the workspace.",
            file=sys.stderr,
        )
        return 1

    username = args.username or email.split("@")[0]

    # Re-registering the same agent must not fail. The server treats
    # register(username, password) as an update when the password matches an
    # existing agent, so reuse the password we stored last time rather than
    # minting a fresh one that would collide with the existing record.
    stored = load_creds()
    reuse = (
        bool(stored.get("password"))
        and stored.get("username") == username
        and stored.get("host", "").rstrip("/") == host.rstrip("/")
    )
    password = stored["password"] if reuse else secrets.token_urlsafe(24)

    try:
        client = Dana4Client(host=host)
        # url=None is what makes this agent serverless: Dana4 has nothing to
        # call, so the agent pulls its own work.
        result = client.register(
            username=username,
            password=password,
            email=email,
            url=None,
            bio=args.bio or "",
            description=args.desc or "",
        )
    except Dana4Error as e:
        if e.status == 409:
            print(
                f"An agent '{username}' already exists on {host}, but the "
                f"stored credentials in {creds_path()} don't match it. Recover "
                "the original credentials.json, or have a Dana4 root user delete "
                "the agent record, then re-register.",
                file=sys.stderr,
            )
        else:
            print(f"Registration failed: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Registration failed: {e}", file=sys.stderr)
        return 1

    if isinstance(result, dict) and result.get("url") not in (None, ""):
        print(
            f"Warning: server returned url={result['url']!r}; expected null for "
            "a serverless agent.",
            file=sys.stderr,
        )

    path = save_creds(host, username, password)
    print(f"credentials saved to {path} (mode 0600)", file=sys.stderr)

    try:
        schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
        # bio and description must be strings — the server rejects nulls.
        client.update_agent(
            schema, bio=args.bio or "", description=args.desc or ""
        )
    except (Dana4Error, OSError, ValueError) as e:
        print(
            f"Registered, but advertising capabilities failed: {e}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {"username": username, "email": email, "host": host}, indent=2
        )
    )
    print(
        "\nNext: invite "
        f"{email} into your workspace from the Dana4 web app. Until a human does "
        "that, every workspace call returns 401. The agent showing Offline "
        "afterwards is expected — serverless agents have no health endpoint.",
        file=sys.stderr,
    )
    return 0


def setup(subparser) -> None:
    """Build the `hermes dana4` subcommand tree."""
    subs = subparser.add_subparsers(dest="dana4_command")

    reg = subs.add_parser(
        "register", help="Register this Hermes agent with Dana4"
    )
    reg.add_argument("--host", help="Dana4 deployment URL")
    reg.add_argument(
        "--email", help="Agent email — permanent, asked for if omitted"
    )
    reg.add_argument(
        "--username", help="Agent username (default: <email-local>)"
    )
    reg.add_argument("--bio", default="", help="Short agent bio")
    reg.add_argument("--desc", default="", help="What the agent does")

    st = subs.add_parser("status", help="Connection and open tasks")
    st.add_argument("--host", help="Override the configured host")

    subs.add_parser(
        "creds",
        help="Show configured credentials (offline, never the password)",
    )

    subparser.set_defaults(func=handle)


def handle(args) -> int:
    sub = getattr(args, "dana4_command", None)
    if sub == "register":
        return _register(args)
    if sub == "status":
        return _status(args)
    if sub == "creds":
        return _creds(args)
    print("Usage: hermes dana4 {register|status|creds}", file=sys.stderr)
    return 2
