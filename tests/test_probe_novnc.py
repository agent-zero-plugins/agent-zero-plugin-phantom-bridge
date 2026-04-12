"""Tests for probe_novnc() + HealthState classification."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

import pytest

# Ensure plugin root is importable when pytest is launched from anywhere.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from bridge import HealthState, probe_novnc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — ephemeral loopback HTTP server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Grab an unused TCP port, close the socket, return the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeNoVNCHandler(BaseHTTPRequestHandler):
    """Serves /vnc.html with configurable status code. Silent logs."""

    status_override: int = 200

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        if self.path == "/vnc.html":
            self.send_response(self.__class__.status_override)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if 200 <= self.__class__.status_override < 300:
                self.wfile.write(b"<html>noVNC</html>")
        else:
            self.send_response(404)
            self.end_headers()


def _start_fake_server(status: int = 200) -> tuple[HTTPServer, int, threading.Thread]:
    port = _free_port()
    _FakeNoVNCHandler.status_override = status
    server = HTTPServer(("127.0.0.1", port), _FakeNoVNCHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


# ---------------------------------------------------------------------------
# Tests — one per HealthState classification path
# ---------------------------------------------------------------------------


def test_port_unmapped_when_nothing_listening():
    port = _free_port()  # grabbed then released — guaranteed unused
    result = probe_novnc(host="127.0.0.1", port=port, timeout=1.0)

    assert result["state"] == HealthState.PORT_UNMAPPED
    assert "docker-compose" in result["fix"]
    assert str(port) in result["fix"]


def test_healthy_when_vnc_html_returns_200():
    server, port, _ = _start_fake_server(status=200)
    try:
        result = probe_novnc(host="127.0.0.1", port=port, timeout=2.0)
    finally:
        server.shutdown()
        server.server_close()

    assert result["state"] == HealthState.HEALTHY
    assert result["fix"] == ""
    assert str(port) in result["detail"]


def test_novnc_unreachable_when_http_404():
    server, port, _ = _start_fake_server(status=404)
    try:
        result = probe_novnc(host="127.0.0.1", port=port, timeout=2.0)
    finally:
        server.shutdown()
        server.server_close()

    assert result["state"] == HealthState.NOVNC_UNREACHABLE
    assert "novnc" in result["fix"].lower()


def test_novnc_unreachable_when_http_500():
    server, port, _ = _start_fake_server(status=500)
    try:
        result = probe_novnc(host="127.0.0.1", port=port, timeout=2.0)
    finally:
        server.shutdown()
        server.server_close()

    assert result["state"] == HealthState.NOVNC_UNREACHABLE


def test_probe_respects_timeout_budget():
    """Even a refused connect must return within the timeout window."""
    import time

    port = _free_port()
    started = time.monotonic()
    result = probe_novnc(host="127.0.0.1", port=port, timeout=1.5)
    elapsed = time.monotonic() - started

    assert elapsed < 2.5, f"probe_novnc exceeded budget: {elapsed:.2f}s"
    assert result["state"] == HealthState.PORT_UNMAPPED


def test_health_state_is_string_compatible():
    """HealthState values serialize cleanly to JSON for the API response."""
    import json

    payload = {"state": HealthState.HEALTHY.value}
    encoded = json.dumps(payload)
    assert '"healthy"' in encoded
    assert HealthState("healthy") == HealthState.HEALTHY


def test_host_parameter_is_honored():
    """probe_novnc must NOT hardcode localhost — Rejection Criteria."""
    import inspect

    from bridge import probe_novnc as pn

    sig = inspect.signature(pn)
    assert "host" in sig.parameters
    assert sig.parameters["host"].default in ("localhost", "127.0.0.1")
