#!/usr/bin/env python3
"""Self-check for dana4_client. No network, no framework: `python3 test_dana4_client.py`.

Covers the four things that silently break the integration if they regress:
credential precedence, the Bearer header, the empty-`{}` body the Dana4 server
requires on body-less writes, and the enrollment (device) flow.
"""

import importlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

import dana4_client
from dana4_client import CREDS_ENV_VAR, Dana4Client, load_creds, save_creds

CREDS_KEYS = ("DANA4_HOST", "DANA4_API_KEY")


def _clear_env():
    for k in CREDS_KEYS:
        os.environ.pop(k, None)


def test_creds_roundtrip_and_permissions(tmp):
    os.environ[CREDS_ENV_VAR] = str(tmp / "credentials.json")
    _clear_env()
    assert load_creds() == {}, "missing file must read as empty, not raise"

    path = save_creds("https://dana4.example/", "d4a_key")
    assert load_creds() == {
        "host": "https://dana4.example/",
        "api_key": "d4a_key",
    }
    assert path.stat().st_mode & 0o777 == 0o600, (
        "credentials must be owner-only"
    )

    path.write_text("not json", encoding="utf-8")
    assert load_creds() == {}, "corrupt file must degrade to empty, not raise"


def test_env_beats_file(tmp):
    os.environ[CREDS_ENV_VAR] = str(tmp / "credentials.json")
    _clear_env()
    save_creds("https://stored.example", "d4a_stored")

    c = Dana4Client()
    assert c.host == "https://stored.example" and c.api_key == "d4a_stored"

    os.environ["DANA4_HOST"] = "https://env.example"
    c = Dana4Client()
    assert c.host == "https://env.example", "env must win over the stored file"
    assert c.api_key == "d4a_stored", (
        "unset env field falls through to the file"
    )
    os.environ["DANA4_API_KEY"] = "d4a_env"
    assert Dana4Client().api_key == "d4a_env"

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
        assert "enroll" in str(e), "the error should say how to fix it"
    else:
        raise AssertionError("Dana4Client() with no host anywhere must raise")


def test_auth_header():
    c = Dana4Client(host="https://x.example", api_key="d4a_k")
    sent = _capture(c)
    c.tasks_take()
    assert sent[-1].headers["Authorization"] == "Bearer d4a_k"


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
    c = Dana4Client(host="https://x.example", api_key="d4a_k")
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


def test_enroll_polls_until_approved():
    c = Dana4Client(host="https://x.example")
    replies = [
        (
            200,
            {
                "device_code": "dc",
                "user_code": "BCDF-GHJK",
                "expires_in": 60,
                "interval": 0,
                "verification_uri_complete": "https://x/activate",
            },
        ),
        (400, {"error": "authorization_pending"}),
        (200, {"api_key": "d4a_new", "agent": {"username": "a.dana4"}}),
    ]
    sent = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        status, body = replies.pop(0)
        if status != 200:
            import io
            import urllib.error

            raise urllib.error.HTTPError(
                req.full_url,
                status,
                "",
                {},
                io.BytesIO(json.dumps(body).encode()),
            )
        return _Resp(body)

    dana4_client.urllib.request.urlopen = fake_urlopen
    start = c.enroll_start("a.dana4")
    assert "Authorization" not in sent[-1].headers, "enrollment is open"
    assert sent[-1].full_url.endswith("/agents/enroll")
    result = c.enroll_wait(start)
    assert result["api_key"] == "d4a_new" and c.api_key == "d4a_new"
    assert json.loads(sent[-1].data) == {"device_code": "dc"}


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
            test_enroll_polls_until_approved()
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
