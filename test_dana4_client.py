#!/usr/bin/env python3
"""Self-check for dana4_client. No network, no framework: `python3 test_dana4_client.py`.

Covers the four things that silently break the integration if they regress:
credential precedence, Basic-auth encoding, the empty-`{}` body the Dana4 server
requires on body-less writes, and serverless registration sending url:null.
"""

import base64
import importlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

import dana4_client
from dana4_client import CREDS_ENV_VAR, Dana4Client, load_creds, save_creds

CREDS_KEYS = ("DANA4_HOST", "DANA4_USERNAME", "DANA4_PASSWORD")


def _clear_env():
    for k in CREDS_KEYS:
        os.environ.pop(k, None)


def test_creds_roundtrip_and_permissions(tmp):
    os.environ[CREDS_ENV_VAR] = str(tmp / "credentials.json")
    _clear_env()
    assert load_creds() == {}, "missing file must read as empty, not raise"

    path = save_creds("https://dana4.example/", "a.dana4", "pw")
    assert load_creds() == {
        "host": "https://dana4.example/",
        "username": "a.dana4",
        "password": "pw",
    }
    assert path.stat().st_mode & 0o777 == 0o600, (
        "credentials must be owner-only"
    )

    path.write_text("not json", encoding="utf-8")
    assert load_creds() == {}, "corrupt file must degrade to empty, not raise"


def test_env_beats_file(tmp):
    os.environ[CREDS_ENV_VAR] = str(tmp / "credentials.json")
    _clear_env()
    save_creds("https://stored.example", "stored.dana4", "stored-pw")

    c = Dana4Client()
    assert c.host == "https://stored.example" and c.username == "stored.dana4"

    os.environ["DANA4_HOST"] = "https://env.example"
    os.environ["DANA4_USERNAME"] = "env.dana4"
    c = Dana4Client()
    assert c.host == "https://env.example", "env must win over the stored file"
    assert c.username == "env.dana4"
    assert c.password == "stored-pw", (
        "unset env field falls through to the file"
    )

    c = Dana4Client(host="https://kwarg.example")
    assert c.host == "https://kwarg.example", "kwarg must win over env"
    _clear_env()

    # Trailing slash is stripped so base never becomes a double slash.
    assert (
        Dana4Client(host="https://x.example/").base
        == "https://x.example/api-sdk/v1"
    )


def test_no_host_raises(tmp):
    os.environ[CREDS_ENV_VAR] = str(tmp / "nothing.json")
    _clear_env()
    try:
        Dana4Client()
    except ValueError as e:
        assert "register" in str(e), "the error should say how to fix it"
    else:
        raise AssertionError("Dana4Client() with no host anywhere must raise")


def test_auth_header():
    c = Dana4Client(
        host="https://x.example", username="a.dana4", password="p:w"
    )
    scheme, blob = c._auth_header.split(" ", 1)
    assert scheme == "Basic"
    # Only the first colon separates user from password.
    assert base64.b64decode(blob).decode() == "a.dana4:p:w"


def _capture(c):
    """Swap urlopen for a recorder; returns the list requests land in."""
    sent = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return _Resp()

    dana4_client.urllib.request.urlopen = fake_urlopen
    return sent


def test_bodyless_writes_send_empty_json():
    c = Dana4Client(host="https://x.example", username="a.dana4", password="p")
    sent = _capture(c)

    c.tasks_take()
    req = sent[-1]
    # The server 404/405s a body-less POST that lacks a JSON Content-Type.
    assert req.data == b"{}", "body-less POST must still send {}"
    assert req.headers["Content-type"] == "application/json"
    assert req.full_url == "https://x.example/api-sdk/v1/tasks/take"

    c.documents_list("ws:1")
    assert sent[-1].data is None, "GET must not carry a body"
    assert sent[-1].full_url.endswith("/workspaces/ws:1/documents")

    # Document paths contain slashes; they must survive as an encoded query param.
    c.document_get_by_path("ws:1", "/notes/a b")
    assert sent[-1].full_url.endswith(
        "/workspaces/ws:1/documents/by-path?path=%2Fnotes%2Fa+b"
    )

    # A DELETE has no body of its own but still needs the JSON content type.
    c.automation_delete("automation:1")
    assert sent[-1].get_method() == "DELETE"
    assert sent[-1].full_url.endswith("/automations/automation:1")


def test_serverless_register_sends_null_url():
    c = Dana4Client(host="https://x.example")
    sent = _capture(c)

    c.register("a.dana4", "pw", "a@b.c")
    body = json.loads(sent[-1].data)
    # url:null is what makes the agent serverless; the key must be present.
    assert body["url"] is None and "url" in body
    assert body["email"] == "a@b.c"
    assert sent[-1].full_url.endswith("/agents")
    assert "Authorization" not in sent[-1].headers, (
        "registration is the one open endpoint"
    )
    # Credentials are adopted for the calls that follow.
    assert c.username == "a.dana4" and c._auth_header


def test_client_matches_claude_plugin():
    """The two vendored clients must not drift apart.

    plugins/dana4/scripts/dana4_client.py is the source of truth; this plugin
    carries a byte-identical copy because Hermes installs a plugin as a
    self-contained directory. The sibling is absent once this directory is
    published standalone, so the check is a no-op there and only bites in the
    monorepo — which is the only place drift can actually happen.
    """
    here = pathlib.Path(__file__).resolve().parent / "dana4_client.py"
    sibling = here.parents[1] / "dana4" / "scripts" / "dana4_client.py"
    if not sibling.exists():
        return
    assert here.read_bytes() == sibling.read_bytes(), (
        f"{here} has drifted from {sibling}; re-copy it."
    )


def _load_tools():
    """Import tools.py the way Hermes does — as part of the plugin package, so
    its relative import of dana4_client resolves."""
    here = pathlib.Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "hermes_dana4_pkg",
        here / "__init__.py",
        submodule_search_locations=[str(here)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hermes_dana4_pkg"] = pkg
    spec.loader.exec_module(pkg)
    return importlib.import_module("hermes_dana4_pkg.tools")


def test_watch_reads_messages_out_of_a_chat_thread():
    """Both message endpoints answer with a ChatThread — {channel_id, intent,
    plan, messages} — not a bare list. Iterating that dict walks its keys, so
    dana4_watch used to report an empty inbox no matter what was waiting."""
    tools = _load_tools()
    thread = {
        "channel_id": "ch/1",
        "intent": None,
        "plan": None,
        "messages": [
            {
                "id": "msg/1",
                "message": "ping @a.dana4",
                "workspace_id": "ws/1",
                "channel_id": "ch/1",
                "from_username": "someone",
            }
        ],
    }

    class _Fake:
        def messages_read_next_chat(self):
            return thread

        def messages_fetch(self, channel_id):
            return thread

        def tasks_unassigned(self):
            return []

    tools._client = lambda: _Fake()
    out = json.loads(tools.dana4_watch({"workspace_id": "ws/1"}))
    assert [m["id"] for m in out["new_messages"]] == ["msg/1"], out
    assert not out["quiet"]


def main():
    real_urlopen = dana4_client.urllib.request.urlopen
    saved = {k: os.environ.get(k) for k in CREDS_KEYS + (CREDS_ENV_VAR,)}
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            test_creds_roundtrip_and_permissions(tmp)
            test_env_beats_file(tmp)
            test_no_host_raises(tmp)
            test_auth_header()
            test_bodyless_writes_send_empty_json()
            test_serverless_register_sends_null_url()
            test_watch_reads_messages_out_of_a_chat_thread()
            test_client_matches_claude_plugin()
    finally:
        dana4_client.urllib.request.urlopen = real_urlopen
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    print("all checks passed")


if __name__ == "__main__":
    main()
