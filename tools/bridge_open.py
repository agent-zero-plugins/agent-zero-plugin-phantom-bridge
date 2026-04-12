"""
browser_bridge_open — A0 tool to start the Browser Bridge.

Launches Chromium with remote debugging enabled so the user can connect
from their host browser to log into services inside the container.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from helpers.tool import Tool, Response

logger = logging.getLogger("browser_bridge")


class BrowserBridgeOpen(Tool):

    async def execute(self, **kwargs: Any) -> Response:
        from usr.plugins.phantom_bridge.bridge import (
            get_bridge,
            create_bridge_from_config,
        )

        # Load config
        config = self._load_config()

        # Check if already running
        bridge = get_bridge()
        if bridge and bridge.is_running():
            status = bridge.status()
            return Response(
                message=(
                    f"Browser bridge is already running.\n"
                    f"Connect URL: {status.get('connect_url', 'http://localhost:9222')}\n"
                    f"Uptime: {status.get('uptime_seconds', 0)}s\n"
                    f"Open pages: {status.get('page_count', 0)}\n\n"
                    f"Tell the user to open the Connect URL in their host Chrome browser."
                ),
                break_loop=False,
            )

        # Create and start
        bridge = create_bridge_from_config(config)

        try:
            status = await bridge.start()
        except RuntimeError as e:
            return Response(
                message=f"Failed to start browser bridge: {e}",
                break_loop=False,
            )

        novnc_url = status.get("novnc_url", "")
        novnc_running = status.get("novnc_running", False)
        novnc_port = status.get("novnc_port", 6080)

        # Pre-flight probe — advisory only, never blocks startup
        preflight_hint = ""
        try:
            from usr.plugins.phantom_bridge.bridge import probe_novnc, HealthState
            probe = probe_novnc(host="localhost", port=novnc_port, timeout=2.0)
            if probe["state"] != HealthState.HEALTHY:
                preflight_hint = (
                    f"\n[WARNING] noVNC health check: {probe['state'].value}\n"
                    f"Detail: {probe['detail']}\n"
                    f"Fix: {probe['fix']}\n"
                    f"Run bridge_doctor for a full diagnostic report.\n"
                )
        except Exception:
            pass  # probe failure must never break bridge_open

        if novnc_running and not preflight_hint:
            viewer_msg = (
                f"Remote browser viewer: {novnc_url}\n"
                f"Or use the Phantom Bridge panel in A0's sidebar.\n\n"
                f"The user can control the container's browser directly — "
                f"full keyboard, mouse, and clipboard support.\n"
            )
        elif novnc_running and preflight_hint:
            viewer_msg = (
                f"Remote browser viewer: {novnc_url}\n"
                f"Or use the Phantom Bridge panel in A0's sidebar.\n\n"
                f"The user can control the container's browser directly — "
                f"full keyboard, mouse, and clipboard support.\n"
                f"{preflight_hint}"
            )
        else:
            viewer_msg = (
                f"noVNC is not running (dependencies may not be installed).\n"
                f"The user can still connect via Chrome DevTools at "
                f"http://localhost:{status.get('port', 9222)}\n"
                f"{preflight_hint}"
            )

        return Response(
            message=(
                f"Browser bridge is live!\n\n"
                f"{viewer_msg}\n"
                f"Instructions for the user:\n"
                f"1. Open the bridge viewer from the sidebar or the URL above\n"
                f"2. Navigate to any service and log in (Google, NotebookLM, X, etc.)\n"
                f"3. All cookies and sessions persist in the container\n"
                f"4. The observer is recording — it learns auth patterns and site maps\n"
                f"5. When done, tell me to close the bridge\n\n"
                f"After the user logs in, my browser_agent tool will have access "
                f"to those authenticated sessions automatically."
            ),
            break_loop=False,
        )

    def _load_config(self) -> dict[str, Any]:
        """Load plugin configuration."""
        try:
            from helpers.plugins import get_plugin_config
            return get_plugin_config("phantom_bridge", agent=self.agent) or {}
        except ImportError:
            pass

        # Fallback: load default_config.yaml directly
        try:
            import yaml

            config_path = Path(__file__).resolve().parent.parent / "default_config.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    return yaml.safe_load(f) or {}
        except ImportError:
            pass

        return {}

    def get_log_object(self):
        return self.agent.context.log.log(
            type="tool",
            heading=f"icon://language {self.agent.agent_name}: Opening Browser Bridge",
            content="",
            kvps=self.args,
        )
