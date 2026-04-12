"""
Playbook Recorder — records user browser actions as replayable playbooks.

Subscribes to CDP events (Page navigation, Network requests, DOM interactions)
and captures them as a sequence of PlaybookSteps.  Pure observation — never
interferes with user browsing.

DOM interactions (clicks, typing, selects, form submits) are captured by
injecting a lightweight JavaScript hook via Runtime.addBinding + Runtime.evaluate.
The hook builds CSS selectors for target elements, masks passwords, debounces
rapid input events, and reports back via the __phantomBridge binding.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cdp_client import CDPClient
from .playbook import Playbook, PlaybookStep, slugify

logger = logging.getLogger("phantom_bridge")


def _safe_str(s: str) -> str:
    """Strip lone Unicode surrogates that break JSON serialisation.

    DOM event payloads (click text, typed values, selectors) arrive as raw
    strings from web pages and can contain malformed UTF-16 sequences.
    Round-tripping through UTF-8 with errors='replace' eliminates them so
    playbook JSON files are always valid.
    """
    return s.encode("utf-8", errors="replace").decode("utf-8")


# File extensions to ignore when recording network requests
_STATIC_EXTENSIONS = frozenset(
    {
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".map",
        ".webp",
        ".avif",
    }
)

# HTTP methods worth recording (skip GET — too noisy)
_RECORDED_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# ---------------------------------------------------------------------------
# JavaScript hook injected into every page during recording.
# Captures click, input, change, submit events and sends them back to
# Python via the __phantomBridge CDP binding.
# ---------------------------------------------------------------------------
_DOM_HOOK_JS = r"""
(function() {
    if (window.__phantomBridgeInstalled) return;
    window.__phantomBridgeInstalled = true;

    // ---- selector builder: id > name > class > nth-child ----
    function buildSelector(el) {
        if (!el || el === document || el === document.documentElement) return 'html';
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';

        var tag = el.tagName.toLowerCase();
        // class-based (first non-empty class)
        if (el.classList && el.classList.length > 0) {
            for (var i = 0; i < el.classList.length; i++) {
                var cls = el.classList[i];
                if (cls && document.querySelectorAll(tag + '.' + CSS.escape(cls)).length === 1) {
                    return tag + '.' + CSS.escape(cls);
                }
            }
        }

        // nth-child fallback
        var parent = el.parentElement;
        if (!parent) return tag;
        var children = Array.from(parent.children).filter(function(c) {
            return c.tagName === el.tagName;
        });
        var idx = children.indexOf(el) + 1;
        var parentSel = buildSelector(parent);
        return parentSel + ' > ' + tag + ':nth-child(' + idx + ')';
    }

    // ---- interactive element check ----
    var INTERACTIVE = ['A','BUTTON','INPUT','SELECT','TEXTAREA'];
    function isInteractive(el) {
        if (!el || !el.tagName) return false;
        if (INTERACTIVE.indexOf(el.tagName) !== -1) return true;
        if (el.getAttribute('role') === 'button') return true;
        if (el.hasAttribute('onclick')) return true;
        return false;
    }

    // Find closest interactive ancestor (for clicks on spans inside buttons)
    function closestInteractive(el) {
        var cur = el;
        while (cur && cur !== document) {
            if (isInteractive(cur)) return cur;
            cur = cur.parentElement;
        }
        return null;
    }

    function send(data) {
        try { window.__phantomBridge(JSON.stringify(data)); } catch(e) {}
    }

// ---- locator metadata: captures multiple strategies for robust replay ----
function getLocatorInfo(el) {
    var info = {};
    info.tag = el.tagName ? el.tagName.toLowerCase() : '';
    info.role = el.getAttribute('role') || '';
    info.ariaLabel = el.getAttribute('aria-label') || '';
    info.placeholder = el.getAttribute('placeholder') || '';
    // For labels, look at associated <label> element
    if (el.id) {
        var label = document.querySelector('label[for="' + el.id + '"]');
        if (label) info.labelText = (label.innerText || '').trim().substring(0, 50);
    }
    // For inputs without id, check parent label
    if (!info.labelText && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
        var parentLabel = el.closest('label');
        if (parentLabel) info.labelText = (parentLabel.innerText || '').trim().substring(0, 50);
    }
    return info;
}

    // ---- click handler ----
    document.addEventListener('click', function(e) {
        var target = closestInteractive(e.target);
        if (!target) return;
        var text = (target.innerText || '').trim().substring(0, 50);
        var loc = getLocatorInfo(target);
        send({
            type: 'click',
            selector: buildSelector(target),
            text: text,
            url: location.href,
            tag: loc.tag,
            role: loc.role,
            ariaLabel: loc.ariaLabel,
            placeholder: loc.placeholder,
            labelText: loc.labelText || null
        });
    }, true);

    // ---- input handler (debounced 500ms) ----
    var inputTimers = {};
    document.addEventListener('input', function(e) {
        var target = e.target;
        if (!target || !target.tagName) return;
        var tag = target.tagName;
        if (tag !== 'INPUT' && tag !== 'TEXTAREA') return;

        var sel = buildSelector(target);
        if (inputTimers[sel]) clearTimeout(inputTimers[sel]);

        inputTimers[sel] = setTimeout(function() {
            delete inputTimers[sel];
            var val = target.value || '';
            // Mask passwords — never record actual password values
            if (target.type === 'password') val = '***';
            var loc = getLocatorInfo(target);
            send({
                type: 'type',
                selector: sel,
                value: val,
                url: location.href,
                tag: loc.tag,
                role: loc.role,
                ariaLabel: loc.ariaLabel,
                placeholder: target.getAttribute('placeholder') || '',
                inputType: target.type || '',
                labelText: loc.labelText || null
            });
        }, 500);
    }, true);

    // ---- select change handler ----
    document.addEventListener('change', function(e) {
        var target = e.target;
        if (!target || target.tagName !== 'SELECT') return;
        var loc = getLocatorInfo(target);
        send({
            type: 'select',
            selector: buildSelector(target),
            value: target.value || '',
            url: location.href,
            tag: loc.tag,
            role: loc.role,
            ariaLabel: loc.ariaLabel,
            labelText: loc.labelText || null
        });
    }, true);

    // ---- form submit handler ----
    document.addEventListener('submit', function(e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        // Find the submit button (if any) for the selector
        var btn = form.querySelector('[type="submit"]') || form.querySelector('button');
        var sel = btn ? buildSelector(btn) : buildSelector(form);
        var text = btn ? (btn.innerText || '').trim().substring(0, 50) : '';
        var loc = btn ? getLocatorInfo(btn) : {};
        send({
            type: 'submit',
            selector: sel,
            text: text,
            value: form.action || '',
            url: location.href,
            tag: loc.tag || 'form',
            role: loc.role || '',
            ariaLabel: loc.ariaLabel || '',
            labelText: loc.labelText || null
        });
    }, true);
})();
"""


class PlaybookRecorder:
    """Records user browser actions as replayable playbooks."""

    def __init__(self, cdp: CDPClient, data_dir: Path):
        self._cdp = cdp
        self._data_dir = data_dir / "playbooks"
        self._recording: bool = False
        self._current_steps: list[PlaybookStep] = []
        self._current_name: str | None = None
        self._record_start: float | None = None
        self._last_step_time: float | None = None
        self._current_domain: str = ""
        self._playbooks: dict[str, Playbook] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize — load existing playbooks from disk.

        Does NOT subscribe to CDP events yet.  Recording only begins
        when start_recording() is called.
        """
        self._load_all()
        logger.info(
            "playbook_recorder: initialized with %d saved playbooks",
            len(self._playbooks),
        )

    async def stop(self) -> None:
        """Stop recording if active, persist everything."""
        if self._recording:
            await self.stop_recording()
        self._save_all()

    # ------------------------------------------------------------------
    # Recording controls
    # ------------------------------------------------------------------

    async def start_recording(self, name: str) -> None:
        """Start recording a new playbook.

        Subscribes to CDP events for navigation, network activity, and
        DOM interactions (clicks, typing, selects, form submits).
        """
        if self._recording:
            raise RuntimeError(
                f"Already recording playbook '{self._current_name}'. "
                "Stop current recording first."
            )

        slug = slugify(name)
        if not slug:
            raise ValueError("Playbook name cannot be empty")

        self._current_name = slug
        self._current_steps = []
        self._record_start = time.time()
        self._last_step_time = self._record_start
        self._current_domain = ""
        self._recording = True

        # Ensure CDP domains are enabled (and tracked for reconnect)
        await self._cdp.enable_domains("Page", "Network", "Runtime")

        # Register the __phantomBridge binding for DOM interaction capture
        try:
            await self._cdp.send("Runtime.addBinding", {"name": "__phantomBridge"})
        except RuntimeError as exc:
            # Binding may already exist from a previous recording
            if "bindingCalled" not in str(exc).lower():
                logger.debug("playbook_recorder: addBinding note: %s", exc)

        # Inject the DOM hook into the current page
        await self._inject_dom_hook()

        # Subscribe to events
        await self._cdp.subscribe("Page.frameNavigated", self._on_navigated)
        await self._cdp.subscribe(
            "Page.navigatedWithinDocument", self._on_spa_navigation
        )
        await self._cdp.subscribe("Network.requestWillBeSent", self._on_network_request)
        await self._cdp.subscribe("Network.responseReceived", self._on_network_response)
        # Re-inject DOM hook after every page load (navigations unload scripts)
        await self._cdp.subscribe("Page.loadEventFired", self._on_page_load_reinject)
        # Receive DOM interaction events from the injected JS hook
        await self._cdp.subscribe("Runtime.bindingCalled", self._on_binding_called)

        logger.info("playbook_recorder: recording started — '%s'", slug)

    async def stop_recording(self, description: str = "") -> Playbook:
        """Stop recording, finalize the playbook, and persist to disk.

        Returns the completed Playbook.
        """
        if not self._recording:
            raise RuntimeError("No recording in progress")

        self._recording = False
        now = time.time()
        duration_ms = int((now - (self._record_start or now)) * 1000)

        playbook = Playbook(
            name=self._current_name or "unnamed",
            domain=self._current_domain,
            description=description,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            steps=list(self._current_steps),
            duration_ms=duration_ms,
        )

        self._playbooks[playbook.name] = playbook
        self._save_playbook(playbook)

        step_count = len(playbook.steps)
        logger.info(
            "playbook_recorder: recording stopped — '%s' (%d steps, %dms)",
            playbook.name,
            step_count,
            duration_ms,
        )

        # Push notification to UI via WebSocket
        try:
            from usr.plugins.phantom_bridge.ws_broadcast import (
                broadcast as _ws_broadcast,
            )

            await _ws_broadcast(
                "phantom_bridge_playbook",
                {
                    "name": playbook.name,
                    "domain": playbook.domain,
                    "steps": step_count,
                    "duration_ms": duration_ms,
                    "description": playbook.description or "",
                },
            )
        except Exception as exc:
            logger.debug("playbook_recorder: ws_broadcast failed: %s", exc)

        # Reset state
        self._current_steps = []
        self._current_name = None
        self._record_start = None
        self._last_step_time = None

        return playbook

    # ------------------------------------------------------------------
    # CDP event handlers
    # ------------------------------------------------------------------

    async def _on_navigated(self, params: dict[str, Any]) -> None:
        """Record a navigate step from Page.frameNavigated."""
        if not self._recording:
            return

        frame = params.get("frame", {})
        # Only record top-level frame navigations
        if frame.get("parentId"):
            return

        url = frame.get("url", "")
        if not url or url in ("about:blank", "about:srcdoc"):
            return

        # Extract domain from first navigation
        if not self._current_domain and "://" in url:
            self._current_domain = url.split("://", 1)[1].split("/", 1)[0]

        self._add_step(
            PlaybookStep(
                action="navigate",
                timestamp=datetime.now(timezone.utc).isoformat(),
                url=url,
            )
        )

    async def _on_spa_navigation(self, params: dict[str, Any]) -> None:
        """Record SPA (single-page app) navigation."""
        if not self._recording:
            return

        url = params.get("url", "")
        if not url:
            return

        self._add_step(
            PlaybookStep(
                action="navigate",
                timestamp=datetime.now(timezone.utc).isoformat(),
                url=url,
            )
        )

    async def _on_network_request(self, params: dict[str, Any]) -> None:
        """Record significant network requests (POST/PUT/DELETE/PATCH only).

        Skips GET requests and static assets to reduce noise.
        """
        if not self._recording:
            return

        request = params.get("request", {})
        method = request.get("method", "GET").upper()

        if method not in _RECORDED_METHODS:
            return

        url = request.get("url", "")
        if not url:
            return

        # Skip static assets
        path = url.split("?", 1)[0]
        if any(path.endswith(ext) for ext in _STATIC_EXTENSIONS):
            return

        content_type = ""
        headers = request.get("headers", {})
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v
                break

        self._add_step(
            PlaybookStep(
                action="request",
                timestamp=datetime.now(timezone.utc).isoformat(),
                url=url,
                method=method,
                content_type=content_type or None,
            )
        )

    async def _on_network_response(self, params: dict[str, Any]) -> None:
        """Detect file downloads via Content-Disposition header."""
        if not self._recording:
            return

        response = params.get("response", {})
        headers = response.get("headers", {})

        # Check for Content-Disposition to detect downloads
        disposition = ""
        for k, v in headers.items():
            if k.lower() == "content-disposition":
                disposition = v
                break

        if not disposition or "attachment" not in disposition.lower():
            return

        # Extract filename from Content-Disposition
        filename = "unknown"
        if "filename=" in disposition:
            # Handle both filename="foo.csv" and filename=foo.csv
            parts = disposition.split("filename=", 1)[1]
            filename = parts.strip().strip('"').strip("'").split(";")[0].strip()

        url = response.get("url", "")

        self._add_step(
            PlaybookStep(
                action="download",
                timestamp=datetime.now(timezone.utc).isoformat(),
                url=url,
                value=filename,
            )
        )

    # ------------------------------------------------------------------
    # DOM interaction handlers
    # ------------------------------------------------------------------

    async def _inject_dom_hook(self) -> None:
        """Inject the DOM interaction capture script into the current page.

        Uses Runtime.evaluate (not Page.addScriptTag) to bypass CSP
        restrictions. Only injects into the main frame.
        """
        try:
            await self._cdp.send(
                "Runtime.evaluate",
                {"expression": _DOM_HOOK_JS, "awaitPromise": False},
            )
            logger.debug("playbook_recorder: DOM hook injected")
        except Exception as exc:
            logger.warning("playbook_recorder: failed to inject DOM hook: %s", exc)

    async def _on_page_load_reinject(self, params: dict[str, Any]) -> None:
        """Re-register the CDP binding and re-inject the DOM hook after every
        page load.

        Pages unload all scripts on navigation, so we must re-inject.
        The __phantomBridge binding must also be re-added because
        Chrome can drop it when the execution context is destroyed
        during navigation.
        """
        if not self._recording:
            return
        try:
            await self._cdp.send(
                "Runtime.addBinding", {"name": "__phantomBridge"}
            )
        except Exception:
            # Binding may still exist — that's fine
            pass
        await self._inject_dom_hook()

    async def _on_binding_called(self, params: dict[str, Any]) -> None:
        """Handle DOM interaction events from the injected JS hook.

        The JS hook calls window.__phantomBridge(jsonString) which triggers
        the Runtime.bindingCalled CDP event.
        """
        if not self._recording:
            return

        if params.get("name") != "__phantomBridge":
            return

        payload_str = params.get("payload", "")
        if not payload_str:
            return

        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.debug(
                "playbook_recorder: invalid DOM event payload: %s",
                payload_str[:200],
            )
            return

        event_type = data.get("type", "")
        selector = data.get("selector", "")

        if not event_type or not selector:
            return

        # Sanitize all string fields from the DOM — pages can return lone
        # Unicode surrogates that break JSON serialization.
        selector = _safe_str(selector)
        raw_text = data.get("text")
        raw_value = data.get("value")
        raw_url = data.get("url")
        text = _safe_str(raw_text) if raw_text else None
        value = _safe_str(raw_value) if raw_value is not None else None
        url = _safe_str(raw_url) if raw_url else None

        # Capture locator metadata for robust replay
        tag = _safe_str(data.get("tag", "")) or None
        role = _safe_str(data.get("role", "")) or None
        aria_label = _safe_str(data.get("ariaLabel", "")) or None
        placeholder = _safe_str(data.get("placeholder", "")) or None
        label_text = _safe_str(data["labelText"]) if data.get("labelText") else None

        now_iso = datetime.now(timezone.utc).isoformat()

        if event_type == "click":
            self._add_step(
                PlaybookStep(
                    action="click",
                    timestamp=now_iso,
                    selector=selector,
                    text=text,
                    url=url,
                    tag=tag,
                    role=role,
                    aria_label=aria_label,
                    label_text=label_text,
                )
            )
        elif event_type == "type":
            input_type = _safe_str(data.get("inputType", "")) or None
            self._add_step(
                PlaybookStep(
                    action="type",
                    timestamp=now_iso,
                    selector=selector,
                    value=value or "",
                    url=url,
                    tag=tag,
                    role=role,
                    aria_label=aria_label,
                    placeholder=placeholder,
                    label_text=label_text,
                    input_type=input_type,
                )
            )
        elif event_type == "select":
            self._add_step(
                PlaybookStep(
                    action="select",
                    timestamp=now_iso,
                    selector=selector,
                    value=value or "",
                    url=url,
                    tag=tag,
                    role=role,
                    aria_label=aria_label,
                    label_text=label_text,
                )
            )
        elif event_type == "submit":
            self._add_step(
                PlaybookStep(
                    action="submit",
                    timestamp=now_iso,
                    selector=selector,
                    text=text,
                    value=value,
                    url=url,
                    tag=tag,
                    role=role,
                    aria_label=aria_label,
                    label_text=label_text,
                )
            )

    # ------------------------------------------------------------------
    # Playbook management
    # ------------------------------------------------------------------

    def get_playbook(self, name: str) -> Playbook | None:
        """Get a saved playbook by name."""
        slug = slugify(name)
        return self._playbooks.get(slug)

    def list_playbooks(self) -> list[dict[str, Any]]:
        """Return summary of all saved playbooks."""
        return [
            {
                "name": pb.name,
                "domain": pb.domain,
                "description": pb.description,
                "step_count": len(pb.steps),
                "duration_ms": pb.duration_ms,
                "recorded_at": pb.recorded_at,
            }
            for pb in sorted(
                self._playbooks.values(),
                key=lambda p: p.recorded_at,
                reverse=True,
            )
        ]

    def delete_playbook(self, name: str) -> bool:
        """Delete a saved playbook by name."""
        slug = slugify(name)
        if slug not in self._playbooks:
            return False

        del self._playbooks[slug]

        # Remove file
        path = self._data_dir / f"{slug}.json"
        if path.exists():
            path.unlink()
            logger.info("playbook_recorder: deleted playbook '%s'", slug)

        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_step(self, step: PlaybookStep) -> None:
        """Add a step and compute wait_ms from the previous step."""
        now = time.time()
        if self._last_step_time is not None and self._current_steps:
            # Assign wait_ms to the *previous* step (time until this step)
            prev = self._current_steps[-1]
            prev.wait_ms = int((now - self._last_step_time) * 1000)
        self._last_step_time = now
        self._current_steps.append(step)

    def _save_playbook(self, playbook: Playbook) -> None:
        """Persist a single playbook to disk."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / f"{playbook.name}.json"
        path.write_text(json.dumps(playbook.to_dict(), indent=2))

    def _save_all(self) -> None:
        """Persist all playbooks to data/playbooks/{name}.json."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        for playbook in self._playbooks.values():
            self._save_playbook(playbook)

    def _load_all(self) -> None:
        """Load existing playbooks from data/playbooks/ on startup."""
        if not self._data_dir.exists():
            return

        for path in self._data_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                pb = Playbook.from_dict(data)
                self._playbooks[pb.name] = pb
                logger.debug("playbook_recorder: loaded playbook '%s'", pb.name)
            except Exception:
                logger.warning(
                    "playbook_recorder: failed to load %s", path, exc_info=True
                )
