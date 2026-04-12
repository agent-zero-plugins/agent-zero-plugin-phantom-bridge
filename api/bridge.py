"""Bridge API handler — start/stop bridge, observer data queries."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from helpers.api import ApiHandler, Request, Response


class BridgeHandler(ApiHandler):
    async def handle_request(self, request: Request) -> Response:
        """Override to add Cache-Control: no-store on all responses.

        Bridge status, cookies, and auth data are always live — caching any of
        them would cause the UI to show stale state (e.g. a crashed bridge that
        appears running, or auth domains that haven't refreshed yet).
        A0 v1.5 enables API/WS caching by default, so we opt out explicitly.
        """
        response: Response = await super().handle_request(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "status")

        if action == "status":
            return self._status()
        elif action == "start":
            return await self._start(input)
        elif action == "stop":
            return await self._stop()
        elif action == "auth_registry":
            return self._get_auth_registry()
        elif action == "sitemaps":
            return self._get_sitemaps()
        elif action == "playbooks":
            return self._get_playbooks()
        elif action == "record_start":
            return await self._record_start(input)
        elif action == "record_stop":
            return await self._record_stop()
        elif action == "cookies":
            return self._get_cookies()
        elif action == "export_cookies":
            return await self._export_cookies()
        elif action == "delete_cookies":
            domain = input.get("domain", "")
            return await self._delete_cookies(domain)
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    def _status(self) -> dict:
        from usr.plugins.phantom_bridge.bridge import get_bridge, probe_novnc, HealthState

        bridge = get_bridge()
        if bridge and bridge.is_running():
            status = bridge.status()
            # Add live noVNC health state for state-aware WebUI
            novnc_port = status.get("novnc_port", 6080)
            try:
                probe = probe_novnc(host="localhost", port=novnc_port, timeout=2.0)
                health_state = probe["state"].value
                health_fix = probe["fix"]
            except Exception:
                health_state = HealthState.NOVNC_UNREACHABLE.value
                health_fix = "probe_novnc raised an unexpected error — run bridge_doctor for details."
            return {
                "ok": True,
                "running": True,
                "health_state": health_state,
                "health_fix": health_fix,
                **status,
            }
        return {"ok": True, "running": False, "health_state": HealthState.BRIDGE_DOWN.value, "health_fix": "Run bridge_open to start the bridge."}

    async def _start(self, input: dict) -> dict:
        from usr.plugins.phantom_bridge.bridge import (
            get_bridge,
            create_bridge_from_config,
        )

        bridge = get_bridge()
        if bridge and bridge.is_running():
            return {
                "ok": True,
                "running": True,
                "message": "Already running",
                **bridge.status(),
            }

        config = input.get("config", {})
        bridge = create_bridge_from_config(config)
        try:
            status = await bridge.start()
            return {"ok": True, "running": True, **status}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _stop(self) -> dict:
        from usr.plugins.phantom_bridge.bridge import get_bridge

        bridge = get_bridge()
        if not bridge or not bridge.is_running():
            return {"ok": True, "running": False, "message": "Not running"}
        result = await bridge.stop()
        return {"ok": True, **result}

    def _get_auth_registry(self) -> dict:
        from usr.plugins.phantom_bridge.data_paths import get_auth_registry_file

        auth_file = get_auth_registry_file()
        if auth_file.exists():
            try:
                registry = json.loads(auth_file.read_text())
                return {"ok": True, "registry": registry}
            except Exception:
                pass
        return {"ok": True, "registry": {}}

    def _get_sitemaps(self) -> dict:
        from usr.plugins.phantom_bridge.data_paths import get_sitemaps_dir

        sitemaps_dir = get_sitemaps_dir()
        result = {}
        if sitemaps_dir.exists():
            for f in sitemaps_dir.glob("*.json"):
                try:
                    result[f.stem] = json.loads(f.read_text())
                except Exception:
                    pass
        return {"ok": True, "sitemaps": result}

    def _get_playbooks(self) -> dict:
        from usr.plugins.phantom_bridge.data_paths import get_playbooks_dir

        playbooks_dir = get_playbooks_dir()
        result = []
        if playbooks_dir.exists():
            for f in playbooks_dir.glob("*.json"):
                try:
                    pb = json.loads(f.read_text())
                    result.append(
                        {
                            "name": pb.get("name", f.stem),
                            "domain": pb.get("domain", ""),
                            "steps": len(pb.get("steps", [])),
                            "recorded_at": pb.get("recorded_at", ""),
                        }
                    )
                except Exception:
                    pass
        return {"ok": True, "playbooks": result}

    def _get_cookies(self) -> dict:
        """Return cookie counts per domain from encrypted per-domain files."""
        from usr.plugins.phantom_bridge.cookie_crypt import get_cookie_summary

        summary = get_cookie_summary()
        return {
            "ok": True,
            "cookies": {d: {"count": info["count"]} for d, info in summary.items()},
            "total_domains": len(summary),
        }

    async def _export_cookies(self) -> dict:
        """Fetch all cookies from Chrome via CDP and save as encrypted per-domain files."""
        try:
            import asyncio
            import websockets

            with urllib.request.urlopen(
                "http://127.0.0.1:9222/json", timeout=3
            ) as resp:
                targets = json.loads(resp.read().decode())
            ws_url = next(
                (t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"),
                None,
            )
            if not ws_url:
                return {"ok": False, "error": "No page target"}

            async with websockets.connect(ws_url) as ws:
                responses = {}

                async def recv():
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "id" in msg:
                            responses[msg["id"]] = msg

                listener = asyncio.create_task(recv())
                await ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
                for _ in range(50):
                    if 1 in responses:
                        break
                    await asyncio.sleep(0.1)
                listener.cancel()
                cookies = responses.get(1, {}).get("result", {}).get("cookies", [])

            by_domain: dict[str, list[dict]] = {}
            for c in cookies:
                d = c.get("domain", "").lstrip(".")
                if d:
                    by_domain.setdefault(d, []).append(
                        {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain"),
                            "path": c.get("path", "/"),
                            "expires": c.get("expires", -1),
                            "httpOnly": c.get("httpOnly", False),
                            "secure": c.get("secure", False),
                        }
                    )

            # Only save if we actually got cookies — don't overwrite
            # existing data when Chrome just restarted and cookies aren't loaded yet
            if by_domain:
                from usr.plugins.phantom_bridge.cookie_crypt import save_domain_cookies

                for domain, domain_cookies in by_domain.items():
                    save_domain_cookies(domain, domain_cookies)

            return {
                "ok": True,
                "domains": len(by_domain),
                "total": sum(len(v) for v in by_domain.values()),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _delete_cookies(self, domain: str) -> dict:
        """Delete cookies — all if domain is empty, or for a specific domain."""
        try:
            import asyncio
            import websockets
            from usr.plugins.phantom_bridge.cookie_crypt import (
                delete_domain_cookies,
                delete_all_cookies,
            )

            url = "http://127.0.0.1:9222/json"
            with urllib.request.urlopen(url, timeout=3) as resp:
                targets = json.loads(resp.read().decode())

            ws_url = None
            for t in targets:
                if t.get("type") == "page":
                    ws_url = t.get("webSocketDebuggerUrl")
                    break

            if not ws_url:
                return {"ok": False, "error": "No page target found"}

            async with websockets.connect(ws_url) as ws:
                responses = {}

                async def recv_loop():
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "id" in msg:
                            responses[msg["id"]] = msg

                listener = asyncio.create_task(recv_loop())

                if domain:
                    # Delete cookies for a specific domain
                    await ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "method": "Network.deleteCookies",
                                "params": {"domain": domain, "name": "*"},
                            }
                        )
                    )
                    # Also try with dot prefix
                    await ws.send(
                        json.dumps(
                            {
                                "id": 2,
                                "method": "Network.deleteCookies",
                                "params": {
                                    "domain": "." + domain.lstrip("."),
                                    "name": "*",
                                },
                            }
                        )
                    )
                else:
                    # Get all cookies then delete each
                    await ws.send(
                        json.dumps({"id": 1, "method": "Network.getAllCookies"})
                    )
                    for _ in range(50):
                        if 1 in responses:
                            break
                        await asyncio.sleep(0.1)

                    cookies = responses.get(1, {}).get("result", {}).get("cookies", [])
                    msg_id = 10
                    for c in cookies:
                        await ws.send(
                            json.dumps(
                                {
                                    "id": msg_id,
                                    "method": "Network.deleteCookies",
                                    "params": {
                                        "name": c["name"],
                                        "domain": c.get("domain", ""),
                                        "path": c.get("path", "/"),
                                    },
                                }
                            )
                        )
                        msg_id += 1

                await asyncio.sleep(0.5)
                listener.cancel()

            # Clear encrypted cookie files on disk
            if domain:
                delete_domain_cookies(domain)
                return {"ok": True, "deleted": domain}
            else:
                delete_all_cookies()
                from usr.plugins.phantom_bridge.data_paths import get_auth_registry_file

                auth_file = get_auth_registry_file()
                if auth_file.exists():
                    auth_file.write_text("{}")
                return {"ok": True, "deleted": "all"}

        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _record_start(self, input: dict) -> dict:
        from usr.plugins.phantom_bridge.bridge import get_bridge

        name = input.get("name", "")
        if not name:
            return {"ok": False, "error": "Missing 'name'"}
        bridge = get_bridge()
        if not bridge:
            return {"ok": False, "error": "Bridge not running"}
        om = getattr(bridge, "_observer_manager", None)
        recorder = getattr(om, "_playbook", None) if om else None
        if not recorder:
            return {"ok": False, "error": "Recorder unavailable"}
        try:
            await recorder.start_recording(name)
            return {"ok": True, "name": name}
        except (RuntimeError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    async def _record_stop(self) -> dict:
        from usr.plugins.phantom_bridge.bridge import get_bridge

        bridge = get_bridge()
        if not bridge:
            return {"ok": False, "error": "Bridge not running"}
        om = getattr(bridge, "_observer_manager", None)
        recorder = getattr(om, "_playbook", None) if om else None
        if not recorder:
            return {"ok": False, "error": "Recorder unavailable"}
        try:
            playbook = await recorder.stop_recording()
            return {
                "ok": True,
                "name": playbook.name,
                "domain": playbook.domain,
                "steps": len(playbook.steps),
                "duration_ms": playbook.duration_ms,
            }
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
