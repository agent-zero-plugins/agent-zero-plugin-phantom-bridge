"""
CDP WebSocket Client — shared base for all observer layers.

Connects to Chrome's DevTools Protocol via WebSocket, sends commands,
and dispatches events to registered subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections import defaultdict
from typing import Any, Callable

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger("phantom_bridge")

# Retry configuration for initial connection
_MAX_CONNECT_ATTEMPTS = 10
_INITIAL_BACKOFF = 0.5  # seconds
_MAX_BACKOFF = 5.0


class CDPClient:
    """Chrome DevTools Protocol WebSocket client."""

    def __init__(self, port: int = 9222):
        self._port = port
        self._ws: ClientConnection | None = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._connected = False
        self._ws_url: str | None = None
        self._listen_task: asyncio.Task | None = None
        self._shutdown = False
        self._enabled_domains: set[str] = set()
        self._needs_reenable: bool = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Chrome's CDP WebSocket endpoint.

        1. GET http://127.0.0.1:{port}/json to list debuggable pages
        2. Pick the first 'page' type target
        3. Connect to its webSocketDebuggerUrl
        4. Start the background listener task
        """
        self._shutdown = False
        self._ws_url = await self._discover_ws_url()
        await self._connect_ws()
        # Start the background listener so event dispatch and command
        # responses are processed.  Store the task so disconnect() can
        # cancel it cleanly.
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self._listen())

    async def _discover_ws_url(self) -> str:
        """Discover the WebSocket URL with retry + backoff."""
        backoff = _INITIAL_BACKOFF
        last_error: Exception | None = None

        for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
            try:
                url = f"http://127.0.0.1:{self._port}/json"
                with urllib.request.urlopen(url, timeout=2) as resp:
                    targets = json.loads(resp.read().decode())

                for target in targets:
                    if target.get("type") == "page":
                        ws_url = target.get("webSocketDebuggerUrl")
                        if ws_url:
                            logger.info(
                                "cdp_client: found page target on attempt %d: %s",
                                attempt,
                                target.get("url", ""),
                            )
                            return ws_url

                raise RuntimeError("No debuggable page targets found")

            except Exception as exc:
                last_error = exc
                if attempt < _MAX_CONNECT_ATTEMPTS:
                    logger.debug(
                        "cdp_client: connect attempt %d/%d failed: %s (retry in %.1fs)",
                        attempt,
                        _MAX_CONNECT_ATTEMPTS,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)

        raise RuntimeError(
            f"Failed to discover CDP WebSocket after {_MAX_CONNECT_ATTEMPTS} "
            f"attempts: {last_error}"
        )

    async def _connect_ws(self) -> None:
        """Establish the WebSocket connection."""
        if not self._ws_url:
            raise RuntimeError("No WebSocket URL discovered")

        self._ws = await websockets.connect(
            self._ws_url,
            max_size=16 * 1024 * 1024,  # 16 MB for large payloads
            close_timeout=5,
        )
        self._connected = True
        logger.info("cdp_client: WebSocket connected to %s", self._ws_url)

    async def disconnect(self) -> None:
        """Clean disconnect."""
        self._shutdown = True
        self._connected = False

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Fail any pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("CDP client disconnected"))
        self._pending.clear()

        logger.info("cdp_client: disconnected")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and wait for the response.

        Uses incrementing message IDs. Returns the 'result' dict.
        """
        if not self._ws or not self._connected:
            raise RuntimeError("CDP client is not connected")

        self._msg_id += 1
        msg_id = self._msg_id

        message: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut

        try:
            await self._ws.send(json.dumps(message))
        except Exception:
            self._pending.pop(msg_id, None)
            raise

        try:
            return await asyncio.wait_for(fut, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP command {method} timed out after 30s")

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(self, event: str, callback: Callable) -> None:
        """Register a callback for a CDP event (e.g., 'Page.loadEventFired').

        The callback receives the event params dict. Async callbacks are
        awaited; sync callbacks are called directly.
        """
        self._subscribers[event].append(callback)

    async def enable_domains(self, *domains: str) -> None:
        """Enable CDP domains (Page, Network, Runtime, etc.).

        Calls {domain}.enable for each.  Remembers which domains are active
        so they can be re-enabled after a reconnect.
        """
        for domain in domains:
            await self.send(f"{domain}.enable")
            self._enabled_domains.add(domain)
            logger.debug("cdp_client: enabled domain %s", domain)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def get_cookies(self) -> list[dict]:
        """Get all cookies via Network.getAllCookies."""
        result = await self.send("Network.getAllCookies")
        return result.get("cookies", [])

    # ------------------------------------------------------------------
    # Background listener
    # ------------------------------------------------------------------

    async def _listen(self) -> None:
        """Background listener that dispatches events to subscribers.

        Handles reconnection on WebSocket drop and JSON parse errors.
        """
        while not self._shutdown:
            try:
                # After a reconnect, re-enable domains so Chrome resumes
                # sending events.  This must happen inside _listen (not
                # _reconnect) because send() awaits futures that only
                # _receive_loop can fulfill — and _receive_loop hasn't
                # restarted yet during _reconnect.  We launch a brief
                # concurrent reader so the enable responses are handled.
                if self._needs_reenable and self._ws and self._connected:
                    self._needs_reenable = False
                    await self._reenable_domains()
                await self._receive_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._shutdown:
                    break
                logger.warning("cdp_client: WebSocket dropped: %s", exc)
                self._connected = False
                # Attempt reconnect
                await self._reconnect()

    async def _receive_loop(self) -> None:
        """Read messages from the WebSocket and dispatch them."""
        if not self._ws:
            raise RuntimeError("No WebSocket connection")

        async for raw in self._ws:
            if self._shutdown:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("cdp_client: failed to parse message: %s", raw[:200])
                continue

            # Response to a command we sent
            if "id" in msg:
                msg_id = msg["id"]
                fut = self._pending.pop(msg_id, None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(
                            RuntimeError(
                                f"CDP error: {msg['error'].get('message', msg['error'])}"
                            )
                        )
                    else:
                        fut.set_result(msg.get("result", {}))

            # Event notification
            if "method" in msg:
                event_name = msg["method"]
                params = msg.get("params", {})
                for callback in self._subscribers.get(event_name, []):
                    try:
                        result = callback(params)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception(
                            "cdp_client: error in subscriber for %s", event_name
                        )

    async def _reenable_domains(self) -> None:
        """Re-enable previously active CDP domains after a reconnect.

        Sends {domain}.enable commands and reads responses inline (the
        normal _receive_loop hasn't started yet at this point).  Uses
        ws.recv() directly to avoid iterator issues with ``async for``.
        """
        if not self._ws or not self._enabled_domains:
            return
        for domain in list(self._enabled_domains):
            self._msg_id += 1
            msg_id = self._msg_id
            msg = json.dumps({"id": msg_id, "method": f"{domain}.enable"})
            try:
                await self._ws.send(msg)
                # Read messages until we see the response.  Any CDP
                # events that arrive in the meantime are dispatched so
                # they are not lost.
                for _ in range(200):  # safety cap
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
                    try:
                        resp = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if resp.get("id") == msg_id:
                        logger.debug(
                            "cdp_client: re-enabled domain %s", domain
                        )
                        break
                    # Route replies for other in-flight commands to their futures
                    if "id" in resp:
                        other_id = resp["id"]
                        fut = self._pending.pop(other_id, None)
                        if fut and not fut.done():
                            if "error" in resp:
                                fut.set_exception(
                                    RuntimeError(
                                        f"CDP error: {resp['error'].get('message', resp['error'])}"
                                    )
                                )
                            else:
                                fut.set_result(resp.get("result", {}))
                    # Dispatch any events received while waiting
                    if "method" in resp:
                        event_name = resp["method"]
                        params = resp.get("params", {})
                        for cb in self._subscribers.get(event_name, []):
                            try:
                                result = cb(params)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                pass
            except Exception as exc:
                logger.warning(
                    "cdp_client: failed to re-enable %s: %s", domain, exc
                )

    async def _reconnect(self) -> None:
        """Attempt to reconnect to the WebSocket with backoff."""
        backoff = _INITIAL_BACKOFF
        for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
            if self._shutdown:
                return
            try:
                logger.info(
                    "cdp_client: reconnect attempt %d/%d",
                    attempt,
                    _MAX_CONNECT_ATTEMPTS,
                )
                # Re-discover in case the target changed
                self._ws_url = await self._discover_ws_url()
                await self._connect_ws()
                logger.info("cdp_client: reconnected successfully")
                # Flag that domains need re-enabling.  The actual
                # send() calls happen in _listen after _receive_loop
                # resumes, because send() depends on the listener
                # reading responses from the WebSocket.
                self._needs_reenable = True
                return
            except Exception as exc:
                logger.debug("cdp_client: reconnect failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

        logger.error(
            "cdp_client: failed to reconnect after %d attempts", _MAX_CONNECT_ATTEMPTS
        )
        self._connected = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._connected
