"""
Browser Bridge — Core bridge logic.

Launches a persistent Chromium instance with --remote-debugging-port so the
host machine can connect via Chrome DevTools Protocol.  The browser profile
is stored in the plugin's data directory and is reused across bridge sessions
AND by A0's browser_agent tool (when patched via the extension).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("browser_bridge")


# ---------------------------------------------------------------------------
# Health probe — standalone helper usable without an A0 context
# ---------------------------------------------------------------------------


class HealthState(str, Enum):
    HEALTHY = "healthy"
    BRIDGE_DOWN = "bridge_down"
    NOVNC_UNREACHABLE = "novnc_unreachable"
    PORT_UNMAPPED = "port_unmapped"
    DEPS_MISSING = "deps_missing"


def probe_novnc(
    host: str = "localhost",
    port: int = 6080,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Probe a noVNC/websockify endpoint and classify its state.

    Two-phase check: raw TCP connect first, then HTTP GET /vnc.html.
    A refused TCP connect points at docker-compose port mapping;
    a successful connect + HTTP failure points at noVNC not installed.

    Returns a dict shaped as:
        {
            "state": HealthState,
            "detail": str,   # human-readable probe result
            "fix":    str,   # copy-pasteable remediation
        }

    Total wall-clock time is bounded by ``timeout`` — tool callers rely on
    this to keep responses interactive. Stdlib only (no ``requests`` dep).
    """
    connect_budget = max(0.2, timeout / 2)
    http_budget = max(0.2, timeout - connect_budget)

    # Phase 1: raw TCP — distinguishes "port not mapped" from "app broken"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_budget)
    try:
        sock.connect((host, port))
    except (ConnectionRefusedError, OSError) as e:
        return {
            "state": HealthState.PORT_UNMAPPED,
            "detail": f"TCP connect to {host}:{port} failed: {e}",
            "fix": (
                f"noVNC port {port} is not reachable. Add "
                f'`"{port}:{port}"` to your docker-compose.yml ports, '
                "then `docker compose up -d`."
            ),
        }
    finally:
        try:
            sock.close()
        except OSError:
            pass

    # Phase 2: HTTP GET /vnc.html — confirms websockify + novnc static files
    url = f"http://{host}:{port}/vnc.html"
    try:
        with urllib.request.urlopen(url, timeout=http_budget) as resp:
            if 200 <= resp.status < 300:
                return {
                    "state": HealthState.HEALTHY,
                    "detail": f"{url} → HTTP {resp.status}",
                    "fix": "",
                }
            return {
                "state": HealthState.NOVNC_UNREACHABLE,
                "detail": f"{url} → HTTP {resp.status}",
                "fix": (
                    "Websockify answered but noVNC static files are missing. "
                    "Reinstall: `apt-get install --reinstall novnc` inside the container."
                ),
            }
    except urllib.error.HTTPError as e:
        return {
            "state": HealthState.NOVNC_UNREACHABLE,
            "detail": f"{url} → HTTP {e.code}",
            "fix": (
                "Port is open but noVNC is not serving vnc.html. "
                "Install novnc: `apt-get install -y novnc`."
            ),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "state": HealthState.NOVNC_UNREACHABLE,
            "detail": f"{url} → {e}",
            "fix": (
                "TCP connected but HTTP failed — websockify likely crashed. "
                "Run `bridge_doctor` or restart the bridge."
            ),
        }


# ---------------------------------------------------------------------------
# Singleton state — one bridge per container
# ---------------------------------------------------------------------------

_bridge: BrowserBridge | None = None


def get_bridge() -> BrowserBridge | None:
    """Return the current bridge instance (if any)."""
    return _bridge


class BrowserBridge:
    """Manages a Chromium process with remote debugging enabled."""

    def __init__(
        self,
        profile_dir: str | Path,
        remote_debug_port: int = 9222,
        bind_address: str = "0.0.0.0",
        headless: bool = False,
        window_width: int = 1280,
        window_height: int = 900,
        default_url: str = "about:blank",
        executable_path: str | None = None,
        novnc_port: int = 6080,
    ):
        self.profile_dir = Path(profile_dir)
        self.remote_debug_port = remote_debug_port
        self.bind_address = bind_address
        self.headless = headless
        self.window_width = window_width
        self.window_height = window_height
        self.default_url = default_url
        self.executable_path = executable_path
        self.novnc_port = novnc_port

        self._process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._started_at: float | None = None
        self._observer_manager = None
        self._xvfb_process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._vnc_process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._websockify_process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._screencast = None
        self._display: str = os.environ.get("DISPLAY", "")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Launch Chromium with remote debugging.  Returns connection info."""
        global _bridge

        if self.is_running():
            return self.status()

        # Check if Chrome is already running on our debug port (started externally)
        if self._detect_existing_chrome():
            _bridge = self
            self._started_at = time.time()
            # Start supporting services on top of the existing Chrome
            self._start_novnc()
            try:
                from usr.plugins.phantom_bridge.screencast import ScreencastManager

                self._screencast = ScreencastManager(port=self.remote_debug_port)
                await self._screencast.start()
            except Exception:
                pass
            try:
                from usr.plugins.phantom_bridge.data_paths import DATA_DIR, ensure_dirs
                from usr.plugins.phantom_bridge.observer.manager import ObserverManager

                ensure_dirs()
                self._observer_manager = ObserverManager(
                    port=self.remote_debug_port, data_dir=DATA_DIR
                )
                await self._observer_manager.start()
            except Exception:
                pass
            return self.status()

        # Ensure profile directory exists and clean stale locks (symlinks!)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = self.profile_dir / lock_file
            # These are symlinks — exists() follows the link and returns False
            # Use is_symlink() or lexists() instead
            if lock_path.is_symlink() or lock_path.exists():
                lock_path.unlink(missing_ok=True)
                logger.info("browser_bridge: removed stale %s", lock_file)

        # Ensure a display is available (start Xvfb if needed)
        self._ensure_display()

        # Resolve Chromium binary
        chrome_bin = self._resolve_chromium()

        args = [
            str(chrome_bin),
            f"--remote-debugging-port={self.remote_debug_port}",
            f"--remote-debugging-address={self.bind_address}",
            f"--user-data-dir={self.profile_dir}",
            f"--window-size={self.window_width},{self.window_height}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
        ]

        if self.headless:
            args.append("--headless=new")

        # Open default page
        args.append(self.default_url)

        logger.info(
            "browser_bridge: launching Chromium on port %d (profile: %s)",
            self.remote_debug_port,
            self.profile_dir,
        )

        # Set DISPLAY for Chromium to render to Xvfb
        env = os.environ.copy()
        if self._display:
            env["DISPLAY"] = self._display

        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Chromium binary not found at {chrome_bin}. "
                "Make sure Playwright is installed: playwright install chromium"
            )

        self._started_at = time.time()
        _bridge = self

        # Wait for the debug port to be ready
        await self._wait_for_devtools()

        # Force Chrome window to fill the entire Xvfb display
        self._maximize_window(env)

        # Start observer layers (auth registry, sitemap learner, playbook recorder)
        try:
            from usr.plugins.phantom_bridge.data_paths import DATA_DIR, ensure_dirs
            from usr.plugins.phantom_bridge.observer.manager import ObserverManager

            ensure_dirs()
            self._observer_manager = ObserverManager(
                port=self.remote_debug_port,
                data_dir=DATA_DIR,
            )
            await self._observer_manager.start()
            logger.info("browser_bridge: observer layers started")
        except Exception as e:
            logger.warning("browser_bridge: observer layers failed to start: %s", e)
            self._observer_manager = None

        # Start noVNC (x11vnc + websockify) for remote browser control
        self._start_novnc()

        # Notify connected UI clients that the bridge is now running.
        try:
            from usr.plugins.phantom_bridge.ws_broadcast import (
                broadcast as _ws_broadcast,
            )

            await _ws_broadcast("phantom_bridge_status", {"running": True})
        except Exception:
            pass

        # Start screencast manager (zero-config fallback when noVNC port isn't exposed)
        try:
            from usr.plugins.phantom_bridge.screencast import ScreencastManager

            self._screencast = ScreencastManager(port=self.remote_debug_port)
            await self._screencast.start()
            logger.info("browser_bridge: screencast manager started")
        except Exception as e:
            logger.warning("browser_bridge: screencast failed to start: %s", e)
            self._screencast = None

        return self.status()

    async def stop(self) -> dict[str, Any]:
        """Stop the bridge Chromium process."""
        global _bridge

        if not self.is_running():
            return {"running": False, "message": "Bridge was not running."}

        # Stop observer layers first
        if self._observer_manager:
            try:
                await self._observer_manager.stop()
                logger.info("browser_bridge: observer layers stopped")
            except Exception as e:
                logger.warning("browser_bridge: error stopping observers: %s", e)
            self._observer_manager = None

        # Stop screencast
        if self._screencast:
            try:
                await self._screencast.stop()
            except Exception:
                pass
            self._screencast = None

        # Stop noVNC processes
        self._stop_novnc()

        pid = self._process.pid if self._process else None
        try:
            if self._process:
                self._process.terminate()
                self._process.wait(timeout=5)
        except Exception as e:
            logger.warning("browser_bridge: error stopping process: %s", e)
            if self._process:
                self._process.kill()
        finally:
            self._process = None
            self._started_at = None
            _bridge = None

        # Notify connected UI clients that the bridge has stopped.
        try:
            from usr.plugins.phantom_bridge.ws_broadcast import (
                broadcast as _ws_broadcast,
            )

            await _ws_broadcast("phantom_bridge_status", {"running": False})
        except Exception:
            pass

        return {"running": False, "message": f"Bridge stopped (pid {pid})."}

    def is_running(self) -> bool:
        """Check if the Chromium process is still alive."""
        # Check our managed process
        if self._process is not None and self._process.poll() is None:
            return True
        # Check if we adopted an external Chrome (no subprocess but bridge is set)
        if self._process is None and self._started_at is not None:
            return self._detect_existing_chrome()
        return False

    def status(self) -> dict[str, Any]:
        """Return current bridge status."""
        running = self.is_running()
        info: dict[str, Any] = {
            "running": running,
            "port": self.remote_debug_port,
            "profile_dir": str(self.profile_dir),
        }

        if running and self._started_at:
            info["uptime_seconds"] = int(time.time() - self._started_at)
            info["connect_url"] = f"http://localhost:{self.remote_debug_port}"
            info["novnc_url"] = (
                f"http://localhost:{self.novnc_port}/vnc.html?autoconnect=true&resize=scale"
            )
            info["novnc_port"] = self.novnc_port
            info["novnc_running"] = (
                self._vnc_process is not None and self._vnc_process.poll() is None
            )
            info["pid"] = self._process.pid if self._process else None

        # List active pages via DevTools JSON endpoint
        if running:
            try:
                pages = self._get_devtools_pages()
                info["pages"] = pages
                info["page_count"] = len(pages)

                # Extract domains with active sessions
                domains = set()
                for page in pages:
                    url = page.get("url", "")
                    if url and "://" in url:
                        domain = url.split("://", 1)[1].split("/", 1)[0]
                        if domain and domain not in ("blank", "newtab"):
                            domains.add(domain)
                info["authenticated_domains"] = sorted(domains)
            except Exception:
                info["pages"] = []
                info["page_count"] = 0
                info["authenticated_domains"] = []

        return info

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def get_profile_dir(self) -> Path:
        """Return the persistent profile directory path."""
        return self.profile_dir

    def profile_exists(self) -> bool:
        """Check if a browser profile already exists."""
        return self.profile_dir.exists() and any(self.profile_dir.iterdir())

    def clear_profile(self) -> None:
        """Delete the browser profile (all cookies, sessions, localStorage)."""
        if self.is_running():
            raise RuntimeError(
                "Cannot clear profile while bridge is running. Stop the bridge first."
            )
        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info("browser_bridge: profile cleared at %s", self.profile_dir)

    def _maximize_window(self, env: dict | None = None) -> None:
        """Force the Chrome window to fill the Xvfb display using xdotool."""
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        run_env = env or os.environ.copy()
        if self._display:
            run_env["DISPLAY"] = self._display
        try:
            # Wait for the window to appear, then resize it
            subprocess.run(
                [
                    xdotool,
                    "search",
                    "--sync",
                    "--onlyvisible",
                    "--name",
                    "",
                    "windowsize",
                    "--sync",
                    "%@",
                    str(self.window_width),
                    str(self.window_height),
                ],
                env=run_env,
                timeout=5,
                capture_output=True,
            )
            subprocess.run(
                [
                    xdotool,
                    "search",
                    "--onlyvisible",
                    "--name",
                    "",
                    "windowmove",
                    "%@",
                    "0",
                    "0",
                ],
                env=run_env,
                timeout=3,
                capture_output=True,
            )
            logger.info(
                "browser_bridge: window resized to %dx%d",
                self.window_width,
                self.window_height,
            )
        except Exception as e:
            logger.debug("browser_bridge: xdotool resize failed (non-critical): %s", e)

    def _detect_existing_chrome(self) -> bool:
        """Check if Chrome is already running with CDP on our port."""
        import urllib.request

        for host in ["127.0.0.1", "[::1]"]:
            try:
                url = f"http://{host}:{self.remote_debug_port}/json/version"
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        logger.info(
                            "browser_bridge: detected existing Chrome on port %d",
                            self.remote_debug_port,
                        )
                        return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # Display (Xvfb)
    # ------------------------------------------------------------------

    def _ensure_display(self) -> None:
        """Start Xvfb if no DISPLAY is set or if the display isn't active."""
        display = ":99"

        # Check if Xvfb is actually running (not just a stale lock)
        lock_file = Path("/tmp/.X99-lock")
        xvfb_alive = False
        if lock_file.exists():
            try:
                pid = int(lock_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                xvfb_alive = True
            except (ValueError, ProcessLookupError, PermissionError):
                # Stale lock — remove it
                lock_file.unlink(missing_ok=True)
                logger.info("browser_bridge: removed stale Xvfb lock")

        if xvfb_alive:
            self._display = display
            os.environ["DISPLAY"] = display
            logger.info("browser_bridge: using existing Xvfb on %s", display)
            return

        xvfb_bin = shutil.which("Xvfb")
        if not xvfb_bin:
            logger.warning("browser_bridge: Xvfb not found — display may not work")
            return

        try:
            self._xvfb_process = subprocess.Popen(
                [
                    xvfb_bin,
                    display,
                    "-screen",
                    "0",
                    f"{self.window_width}x{self.window_height}x24",
                    "-ac",
                    "-nolisten",
                    "tcp",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._display = display
            os.environ["DISPLAY"] = display
            logger.info("browser_bridge: Xvfb started on display %s", display)
            time.sleep(0.5)
        except Exception as e:
            logger.warning("browser_bridge: failed to start Xvfb: %s", e)

    # ------------------------------------------------------------------
    # noVNC (x11vnc + websockify)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_port_in_use(port: int) -> bool:
        """Check if a TCP port is already in use."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _start_novnc(self) -> None:
        """Launch x11vnc and websockify for noVNC browser control."""
        display = self._display or os.environ.get("DISPLAY", ":99")
        vnc_port = 5900

        # Skip if VNC is already running on port 5900
        if self._is_port_in_use(vnc_port):
            logger.info("browser_bridge: x11vnc already running on port %d", vnc_port)
            # Check if websockify is also running
            if self._is_port_in_use(self.novnc_port):
                logger.info(
                    "browser_bridge: websockify already running on port %d",
                    self.novnc_port,
                )
                return
            # Only need websockify
            self._start_websockify(vnc_port)
            return

        # Start x11vnc — captures the Xvfb display
        x11vnc_bin = shutil.which("x11vnc")
        if not x11vnc_bin:
            logger.warning(
                "browser_bridge: x11vnc not found — noVNC disabled. "
                "Run the plugin's execute.py to install dependencies."
            )
            return

        try:
            self._vnc_process = subprocess.Popen(
                [
                    x11vnc_bin,
                    "-display",
                    display,
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-rfbport",
                    str(vnc_port),
                    "-quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "browser_bridge: x11vnc started on :%d (display %s)", vnc_port, display
            )
        except Exception as e:
            logger.warning("browser_bridge: failed to start x11vnc: %s", e)
            return

        self._start_websockify(vnc_port)

    def _start_websockify(self, vnc_port: int) -> None:
        """Start websockify to bridge VNC over WebSocket."""
        if self._is_port_in_use(self.novnc_port):
            logger.info(
                "browser_bridge: websockify already running on port %d", self.novnc_port
            )
            return

        websockify_bin = shutil.which("websockify")
        novnc_web = self._find_novnc_web_dir()

        if not websockify_bin:
            logger.warning("browser_bridge: websockify not found — noVNC disabled")
            return

        try:
            self._websockify_process = subprocess.Popen(
                [
                    websockify_bin,
                    "--web",
                    novnc_web,
                    f"0.0.0.0:{self.novnc_port}",
                    f"localhost:{vnc_port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "browser_bridge: noVNC ready at http://localhost:%d/vnc.html",
                self.novnc_port,
            )
        except Exception as e:
            logger.warning("browser_bridge: failed to start websockify: %s", e)

    def _stop_novnc(self) -> None:
        """Stop noVNC and Xvfb processes."""
        for name, proc_attr in [
            ("websockify", "_websockify_process"),
            ("x11vnc", "_vnc_process"),
            ("Xvfb", "_xvfb_process"),
        ]:
            proc = getattr(self, proc_attr, None)
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            setattr(self, proc_attr, None)
            logger.info("browser_bridge: %s stopped", name)

    @staticmethod
    def _find_novnc_web_dir() -> str:
        """Find the noVNC static files directory."""
        candidates = [
            "/usr/share/novnc",  # Debian/Ubuntu apt package
            "/usr/share/novnc/utils/../",  # Alternate layout
            "/opt/novnc",  # Manual install
        ]
        for path in candidates:
            check = Path(path)
            if check.exists() and (check / "vnc.html").exists():
                return str(check)
        # Fallback — websockify will fail gracefully if path is wrong
        return "/usr/share/novnc"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_chromium(self) -> str | Path:
        """Find a usable Chromium binary."""
        if self.executable_path:
            return self.executable_path

        # Prefer system Chromium — it works reliably from Python subprocess
        # (Playwright's ARM64 build crashes with SIGTRAP from Python Popen)
        for name in [
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ]:
            path = shutil.which(name)
            if path:
                logger.info("browser_bridge: using system Chromium at %s", path)
                return path

        # Try A0's Playwright-installed Chromium as fallback
        try:
            from helpers.playwright import (
                get_playwright_binary,
                ensure_playwright_binary,
            )

            binary = get_playwright_binary()
            if binary:
                # The headless_shell binary can't do headed mode with DevTools.
                # We need the full chromium binary instead.
                full_chrome = self._find_full_chromium_from_playwright(binary)
                if full_chrome:
                    return full_chrome
                # Fall back to headless shell if that's all we have
                return str(binary)

            # Install if not present
            binary = ensure_playwright_binary()
            full_chrome = self._find_full_chromium_from_playwright(binary)
            if full_chrome:
                return full_chrome
            return str(binary)
        except ImportError:
            pass

        # Try Playwright cache directly (common Docker paths)
        for cache_root in [
            Path.home() / ".cache" / "ms-playwright",
            Path("/a0/tmp/playwright"),
            Path("/tmp/playwright"),
        ]:
            if cache_root.exists():
                for pattern in (
                    "chromium-*/chrome-linux/chrome",
                    "chromium-*/chrome-*/chrome",
                    "chromium-*/chrome-*/Chromium.app/Contents/MacOS/Chromium",
                ):
                    match = next(cache_root.glob(pattern), None)
                    if match and match.exists():
                        logger.info("browser_bridge: found Chromium at %s", match)
                        return str(match)

        # Try system Chromium / Chrome
        for name in [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
        ]:
            path = shutil.which(name)
            if path:
                return path

        raise RuntimeError(
            "No Chromium binary found. Install via: playwright install chromium"
        )

    def _find_full_chromium_from_playwright(
        self, headless_binary: str | Path
    ) -> str | None:
        """Given a headless_shell binary path, find the full Chromium binary
        in the same Playwright cache (needed for headed + DevTools mode)."""
        pw_path = Path(headless_binary)
        # Walk up to find the playwright cache root
        # Typical: .../chromium_headless_shell-XXXX/chrome-linux/headless_shell
        # Full:    .../chromium-XXXX/chrome-linux/chrome
        cache_root = pw_path.parent.parent.parent
        if not cache_root.exists():
            return None

        for pattern in (
            "chromium-*/chrome-*/chrome",
            "chromium-*/chrome-*/chromium",
            "chromium-*/chrome-*/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        ):
            match = next(cache_root.glob(pattern), None)
            if match and match.exists():
                return str(match)
        return None

    def _get_devtools_pages(self) -> list[dict[str, Any]]:
        """Query the DevTools HTTP JSON API for open pages."""
        import urllib.request

        url = f"http://127.0.0.1:{self.remote_debug_port}/json"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                return [
                    {
                        "title": p.get("title", ""),
                        "url": p.get("url", ""),
                        "type": p.get("type", ""),
                    }
                    for p in data
                    if p.get("type") == "page"
                ]
        except Exception:
            return []

    async def _wait_for_devtools(self, timeout: float = 30.0) -> None:
        """Poll the DevTools endpoint until it responds or timeout."""
        import urllib.request

        urls = [
            f"http://127.0.0.1:{self.remote_debug_port}/json/version",
            f"http://[::1]:{self.remote_debug_port}/json/version",
        ]
        deadline = time.time() + timeout

        while time.time() < deadline:
            for url in urls:
                try:
                    with urllib.request.urlopen(url, timeout=1) as resp:
                        if resp.status == 200:
                            logger.info(
                                "browser_bridge: DevTools ready on port %d",
                                self.remote_debug_port,
                            )
                            return
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        logger.warning(
            "browser_bridge: DevTools did not respond within %.0fs (may still be starting)",
            timeout,
        )


# ---------------------------------------------------------------------------
# Factory — creates a BrowserBridge from plugin config
# ---------------------------------------------------------------------------


def create_bridge_from_config(config: dict[str, Any] | None = None) -> BrowserBridge:
    """Create a BrowserBridge instance from A0 plugin config dict."""
    try:
        from usr.plugins.phantom_bridge.data_paths import get_profile_dir, ensure_dirs
    except ImportError:
        from data_paths import get_profile_dir, ensure_dirs

    plugin_dir = Path(__file__).resolve().parent

    # Load defaults from YAML if no config provided
    if not config:
        try:
            import yaml

            yaml_path = plugin_dir / "default_config.yaml"
            if yaml_path.exists():
                with open(yaml_path) as f:
                    config = yaml.safe_load(f) or {}
        except ImportError:
            config = {}
        except Exception:
            config = {}

    config = config or {}

    env_data = os.environ.get("PHANTOM_BRIDGE_DATA_DIR", "").strip()
    if env_data:
        profile_dir = Path(env_data) / "profile"
    else:
        profile_dir = plugin_dir / config.get("profile_dir", "data/profile")

    ensure_dirs()

    return BrowserBridge(
        profile_dir=profile_dir,
        remote_debug_port=config.get("remote_debug_port", 9222),
        bind_address=config.get("bind_address", "0.0.0.0"),
        headless=config.get("headless", False),
        window_width=config.get("window_width", 1280),
        window_height=config.get("window_height", 900),
        default_url=config.get("default_url", "about:blank"),
        executable_path=config.get("executable_path"),
        novnc_port=config.get("novnc_port", 6080),
    )
