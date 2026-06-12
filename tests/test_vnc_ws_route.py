"""Tests for the same-origin /vnc_proxy ASGI WebSocket relay.

Covers the mixed-content fix: a pure-ASGI middleware that intercepts WebSocket
connections to ``/vnc_proxy`` and relays them to the internal websockify
endpoint, while passing every other request straight through to the inner app.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Load the hook module directly from its deep @extensible path.
_HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "extensions/python/_functions/helpers/ui_server/UiServerRuntime/"
    / "build_asgi_app/end/_10_vnc_ws_route.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("pb_vnc_ws_route", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pb_vnc_ws_route"] = mod
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


def _make_route():
    """Build the extension without coupling to the Extension __init__ signature.

    The real ``helpers.extension.Extension`` and the various test stubs disagree
    on constructor args, so bypass __init__ and set the one attribute we need.
    """
    obj = hook.VncProxyRoute.__new__(hook.VncProxyRoute)
    obj.agent = None
    return obj


class _RecordingApp:
    """Inner ASGI app that records whether it was called."""

    def __init__(self):
        self.called_with = None

    async def __call__(self, scope, receive, send):
        self.called_with = scope
        await send({"type": "dummy.passthrough"})


def test_hook_file_lives_at_extensible_path():
    """The relay MUST be a build_asgi_app *end* hook so it wraps the built app."""
    assert _HOOK_PATH.is_file(), f"missing hook at {_HOOK_PATH}"
    parts = _HOOK_PATH.parts
    assert "_functions" in parts and parts[-2] == "end"
    assert parts[-3] == "build_asgi_app"
    assert parts[-4] == "UiServerRuntime"


@pytest.mark.asyncio
async def test_non_proxy_requests_pass_through():
    """Any scope that is not a /vnc_proxy websocket reaches the inner app."""
    inner = _RecordingApp()
    app = hook._VncProxyASGI(inner)

    sent = []
    async def send(msg):
        sent.append(msg)
    async def receive():
        return {"type": "http.request"}

    # Plain HTTP request
    await app({"type": "http", "path": "/"}, receive, send)
    assert inner.called_with == {"type": "http", "path": "/"}
    assert sent == [{"type": "dummy.passthrough"}]

    # A websocket to a DIFFERENT path also passes through
    inner2 = _RecordingApp()
    app2 = hook._VncProxyASGI(inner2)
    await app2({"type": "websocket", "path": "/some/other"}, receive, send)
    assert inner2.called_with == {"type": "websocket", "path": "/some/other"}


@pytest.mark.asyncio
async def test_proxy_path_is_intercepted_not_passed_through(monkeypatch):
    """A /vnc_proxy websocket MUST be handled by the relay, never the inner app."""
    inner = _RecordingApp()
    app = hook._VncProxyASGI(inner)

    # Force the upstream connect to fail fast so _relay closes cleanly.
    async def _boom(*a, **k):
        raise OSError("no upstream in test")

    import websockets
    monkeypatch.setattr(websockets, "connect", _boom)

    sent = []
    async def send(msg):
        sent.append(msg)
    async def receive():
        return {"type": "websocket.connect"}

    await app({"type": "websocket", "path": "/vnc_proxy"}, receive, send)

    # Inner app must NOT have been called for the proxy path.
    assert inner.called_with is None
    # On upstream failure we close with 1011 (internal error / upstream down).
    assert {"type": "websocket.close", "code": 1011} in sent


@pytest.mark.asyncio
async def test_route_extension_wraps_result_idempotently():
    """VncProxyRoute.execute wraps the built app once and is idempotent."""
    inner = _RecordingApp()
    ext = _make_route()

    data = {"args": (object(),), "kwargs": {}, "result": inner}
    await ext.execute(data)
    wrapped = data["result"]
    assert isinstance(wrapped, hook._VncProxyASGI)
    assert wrapped._inner is inner

    # Running again must NOT double-wrap.
    await ext.execute(data)
    assert data["result"] is wrapped


@pytest.mark.asyncio
async def test_route_extension_noop_without_result():
    """No result key → nothing to wrap, no crash."""
    ext = _make_route()
    data = {"args": (), "kwargs": {}}
    await ext.execute(data)  # must not raise
    assert "result" not in data
