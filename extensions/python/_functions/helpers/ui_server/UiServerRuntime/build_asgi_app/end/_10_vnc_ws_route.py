"""Phantom Bridge — same-origin VNC WebSocket proxy.

Wraps A0's built ASGI app with a thin pure-ASGI middleware that intercepts
WebSocket connections to ``/vnc_proxy`` and bidirectionally relays them to the
internal websockify endpoint (``ws://localhost:<novnc_port>/websockify``).

Why this exists
---------------
The noVNC client previously connected straight to ``http://<host>:6080/vnc.html``.
When A0 is served over HTTPS (e.g. a tailscale ``*.ts.net`` origin) the browser
blocks that insecure port-6080 resource as **Mixed Content**, leaving a black
screen. By relaying the VNC WebSocket through A0's own origin the client can use
a same-origin ``wss://<host>/vnc_proxy`` URL — no mixed content, and no extra
exposed port.

This is registered as an ``@extensible`` *end* hook on
``UiServerRuntime.build_asgi_app``: it receives the freshly-built ASGI app in
``data['result']`` and replaces it with the wrapped app.
"""

from __future__ import annotations

import asyncio
import logging

from helpers.extension import Extension

logger = logging.getLogger("phantom_bridge")

_PROXY_PATH = "/vnc_proxy"
_DEFAULT_NOVNC_PORT = 6080
_MAX_MSG_BYTES = 16 * 1024 * 1024  # 16 MiB — VNC framebuffer updates can be large


def _novnc_port() -> int:
    """Resolve the live websockify port from the running bridge, else default."""
    try:
        from usr.plugins.phantom_bridge.bridge import get_bridge

        bridge = get_bridge()
        if bridge is not None and getattr(bridge, "novnc_port", None):
            return int(bridge.novnc_port)
    except Exception:  # pragma: no cover - defensive
        pass
    return _DEFAULT_NOVNC_PORT


class _VncProxyASGI:
    """Pure-ASGI middleware: relay ``/vnc_proxy`` websockets, pass everything else."""

    def __init__(self, inner_app):
        self._inner = inner_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket" and scope.get("path") == _PROXY_PATH:
            await self._relay(scope, receive, send)
            return
        await self._inner(scope, receive, send)

    async def _relay(self, scope, receive, send):
        import websockets

        # Wait for the client's connect message before accepting.
        msg = await receive()
        if msg.get("type") != "websocket.connect":
            return

        upstream_url = f"ws://localhost:{_novnc_port()}/websockify"
        try:
            upstream = await websockets.connect(
                upstream_url,
                subprotocols=["binary"],
                max_size=_MAX_MSG_BYTES,
                open_timeout=10,
                ping_interval=None,  # VNC drives its own keepalive
            )
        except Exception as e:
            logger.warning("phantom_bridge: vnc_proxy upstream connect failed: %s", e)
            # 1011 = internal error / upstream unavailable
            await send({"type": "websocket.close", "code": 1011})
            return

        # Accept the browser side, echoing the negotiated subprotocol if any.
        accept: dict = {"type": "websocket.accept"}
        sub = getattr(upstream, "subprotocol", None)
        if sub:
            accept["subprotocol"] = sub
        await send(accept)

        async def client_to_upstream():
            while True:
                event = await receive()
                etype = event.get("type")
                if etype == "websocket.receive":
                    data = event.get("bytes")
                    if data is None:
                        text = event.get("text")
                        if text is None:
                            continue
                        data = text.encode("utf-8")
                    await upstream.send(data)
                elif etype == "websocket.disconnect":
                    return

        async def upstream_to_client():
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    await send({"type": "websocket.send", "bytes": bytes(frame)})
                else:
                    await send({"type": "websocket.send", "text": frame})

        t_up = asyncio.ensure_future(client_to_upstream())
        t_down = asyncio.ensure_future(upstream_to_client())
        try:
            await asyncio.wait(
                {t_up, t_down}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for t in (t_up, t_down):
                if not t.done():
                    t.cancel()
            try:
                await upstream.close()
            except Exception:
                pass
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:
                pass


class VncProxyRoute(Extension):
    """Wrap the built ASGI app with the /vnc_proxy relay (idempotent)."""

    async def execute(self, data: dict | None = None, **kwargs):
        if not isinstance(data, dict):
            return
        app = data.get("result")
        if app is None:
            return
        if isinstance(app, _VncProxyASGI):
            return  # already wrapped
        data["result"] = _VncProxyASGI(app)
        logger.info("phantom_bridge: /vnc_proxy same-origin VNC websocket route installed")
