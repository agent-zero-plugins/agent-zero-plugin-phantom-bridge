"""
Observer Manager — manages the lifecycle of all observer layers,
sharing a single CDP connection.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .cdp_client import CDPClient
from .auth_registry import AuthRegistry

logger = logging.getLogger("phantom_bridge")


class ObserverManager:
    """Manages lifecycle of all observer layers. Shares a single CDP connection."""

    def __init__(
        self,
        port: int = 9222,
        data_dir: Path | None = None,
    ):
        if data_dir is None:
            from ..data_paths import DATA_DIR

            data_dir = DATA_DIR

        self._data_dir = data_dir
        self._cdp = CDPClient(port=port)
        self._auth = AuthRegistry(self._cdp, data_dir)

        # Placeholders for Level 2 and Level 3 (imported if available)
        self._sitemap: Any = None
        self._playbook: Any = None
        self._tasks: list[asyncio.Task] = []
        self._started: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect CDP, enable domains, start all observers."""
        if self._started:
            logger.warning(
                "observer_manager: start() called on already-started manager; ignoring"
            )
            return
        await self._cdp.connect()
        self._started = True

        # connect() now starts the background listener task internally,
        # so we no longer need to create it here.

        await self._cdp.enable_domains("Page", "Network", "Runtime")

        # Level 1: Auth Registry (always available)
        await self._auth.start()
        self._auth.set_auth_callback(self._on_auth_detected)

        # Level 2: Sitemap Learner (optional — Stream B)
        try:
            from .sitemap_learner import SitemapLearner

            self._sitemap = SitemapLearner(self._cdp, self._data_dir)
            await self._sitemap.start()
            logger.info("observer_manager: SitemapLearner started")
        except ImportError:
            logger.debug("observer_manager: SitemapLearner not available (Stream B)")

        # Level 3: Playbook Recorder (optional — Stream C)
        try:
            from .playbook_recorder import PlaybookRecorder

            self._playbook = PlaybookRecorder(self._cdp, self._data_dir)
            await self._playbook.start()
            logger.info("observer_manager: PlaybookRecorder started")
        except ImportError:
            logger.debug("observer_manager: PlaybookRecorder not available (Stream C)")
        logger.info("observer_manager: all observers started")

    async def stop(self) -> None:
        """Stop all observers, disconnect CDP."""
        await self._auth.stop()

        if self._sitemap:
            try:
                await self._sitemap.stop()
            except Exception:
                logger.exception("observer_manager: error stopping SitemapLearner")

        if self._playbook:
            try:
                await self._playbook.stop()
            except Exception:
                logger.exception("observer_manager: error stopping PlaybookRecorder")

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        await self._cdp.disconnect()
        self._started = False
        logger.info("observer_manager: all observers stopped")

    # ------------------------------------------------------------------
    # Event callbacks
    # ------------------------------------------------------------------

    async def _on_auth_detected(self, domain: str, entry) -> None:
        """Broadcast a phantom_bridge_auth event when a domain authenticates."""
        try:
            from usr.plugins.phantom_bridge.ws_broadcast import broadcast

            await broadcast(
                "phantom_bridge_auth",
                {
                    "domain": domain,
                    "authenticated": entry.authenticated,
                    "expires_at": entry.expires_at,
                    "cookies_count": entry.cookies_count,
                },
            )
        except Exception as exc:
            logger.debug("observer_manager: ws_broadcast failed: %s", exc)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def auth(self) -> AuthRegistry:
        """Level 1: Auth Registry observer."""
        return self._auth

    @property
    def sitemap(self):
        """Level 2: Sitemap Learner observer (None if not available)."""
        return self._sitemap

    @property
    def playbook(self):
        """Level 3: Playbook Recorder observer (None if not available)."""
        return self._playbook

    @property
    def cdp(self) -> CDPClient:
        """The shared CDP client."""
        return self._cdp
