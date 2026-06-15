import { createStore } from "/js/AlpineStore.js";

async function api(endpoint, body = {}) {
    const { callJsonApi } = await import("/js/api.js");
    return await callJsonApi(`plugins/phantom_bridge/${endpoint}`, body);
}

export const store = createStore("phantomBridge", {
    running: false,
    novncUrl: "",
    novncReady: false,
    novncPort: 6080,
    health_state: "healthy",
    health_fix: "",
    authEntries: [],
    authCount: 0,
    cookieDomains: [],
    cookieTotal: 0,
    sitemapEntries: [],
    sitemapCount: 0,
    playbooks: [],
    playbookCount: 0,

    // Internal — not part of the public store shape
    _pollInterval: null,
    _ws: null,

    init() {},

    async onOpen() {
        await this.fetchStatus();
        this._subscribeToWsEvents();
        // 30-second fallback poll catches anything WS might miss (e.g. tab hidden,
        // missed events during reconnection).
        this._pollInterval = setInterval(() => this.fetchStatus(), 30000);
    },

    cleanup() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
        if (this._ws) {
            try {
                this._ws.off("phantom_bridge_status");
                this._ws.off("phantom_bridge_auth");
                this._ws.off("phantom_bridge_playbook");
            } catch (_) {}
            this._ws = null;
        }
    },

    // Subscribe to real-time push events emitted by ws_broadcast.py.
    // Falls back gracefully if A0's WebSocket system is unavailable.
    _subscribeToWsEvents() {
        import("/js/websocket.js").then(({ websocket }) => {
            this._ws = websocket;
            // Both event types trigger a full status refresh so the UI is always
            // consistent — the event payload is not used directly.
            websocket.on("phantom_bridge_status", () => this.fetchStatus());
            // A new login was detected: export cookies to disk first so the
            // on-disk encrypted files are current before fetchStatus() reads them.
            websocket.on("phantom_bridge_auth", () => this._exportAndRefresh());
            // A new playbook was saved — show a toast, then refresh.
            websocket.on("phantom_bridge_playbook", (payload) => this._onPlaybookSaved(payload));
        }).catch(() => {
            // WS not available — the 30s fallback poll handles updates.
        });
    },

    async _onPlaybookSaved(payload) {
        try {
            const { toastFrontendSuccess } = await import("/components/notifications/notification-store.js");
            toastFrontendSuccess(
                `Playbook '${payload.name}' saved — ${payload.steps} steps, ${(payload.duration_ms / 1000).toFixed(1)}s`,
                "Phantom Bridge"
            );
        } catch (_) {}
        await this.fetchStatus();
    },

    // Called on phantom_bridge_auth events: flush new cookies to disk before
    // reading them. Swallows errors so a failed export doesn't block the UI update.
    async _exportAndRefresh() {
        try {
            await api("bridge", { action: "export_cookies" });
        } catch (_) {}
        await this.fetchStatus();
    },

    // Returns { cls, text, hint } for the state banner.
    // cls is a CSS class name defined in main.html.
    statusBanner() {
        const state = this.health_state || "healthy";
        const hint = this.health_fix || "";
        if (state === "healthy") {
            return { cls: "pb-banner-healthy", text: "noVNC healthy", hint: "" };
        }
        if (state === "bridge_down") {
            return { cls: "pb-banner-warn", text: "Bridge offline", hint: hint || "Run bridge_open to start the bridge." };
        }
        if (state === "port_unmapped") {
            return { cls: "pb-banner-error", text: "Port not mapped", hint: hint || "Add 6080:6080 to your docker-compose.yml ports and restart." };
        }
        if (state === "novnc_unreachable") {
            return { cls: "pb-banner-error", text: "noVNC unreachable", hint: hint || "Run bridge_doctor for a fix command." };
        }
        if (state === "deps_missing") {
            return { cls: "pb-banner-error", text: "Dependencies missing", hint: hint || "Run bridge_doctor to see which packages need installing." };
        }
        return { cls: "pb-banner-warn", text: `Health: ${state}`, hint };
    },

    async fetchStatus() {
        try {
            const status = await api("bridge", { action: "status" });
            this.running = status.running || false;
            this.novncReady = status.novnc_running || false;
            this.novncUrl = status.novnc_url || "";
            this.novncPort = status.novnc_port || 6080;
            this.health_state = status.health_state || (this.running ? "healthy" : "bridge_down");
            this.health_fix = status.health_fix || "";

            // Cookie domains — read from encrypted on-disk files; no CDP roundtrip.
            const cookieData = await api("bridge", { action: "cookies" });
            const cookies = cookieData.cookies || {};
            this.cookieDomains = Object.entries(cookies).map(([domain, info]) => ({
                domain,
                count: info.count || 0,
            })).sort((a, b) => b.count - a.count);
            this.cookieTotal = this.cookieDomains.reduce((sum, d) => sum + d.count, 0);

            const auth = await api("bridge", { action: "auth_registry" });
            const registry = auth.registry || {};
            this.authEntries = Object.entries(registry).map(([domain, entry]) => ({
                domain,
                authenticated: entry.authenticated,
                expires: entry.expires_at ? `expires ${new Date(entry.expires_at).toLocaleDateString()}` : "no expiry",
            }));
            this.authCount = this.authEntries.length;

            const sm = await api("bridge", { action: "sitemaps" });
            const sitemaps = sm.sitemaps || {};
            this.sitemapEntries = Object.entries(sitemaps).map(([domain, s]) => ({
                domain,
                features: Object.keys(s.features || {}).length,
            }));
            this.sitemapCount = this.sitemapEntries.length;

            const pb = await api("bridge", { action: "playbooks" });
            this.playbooks = pb.playbooks || [];
            this.playbookCount = this.playbooks.length;
        } catch (e) {
            // API not ready yet
        }
    },

    async startBridge() {
        try {
            const { toastFrontendInfo } = await import("/components/notifications/notification-store.js");
            toastFrontendInfo("Starting bridge...", "Phantom Bridge");
            await api("bridge", { action: "start" });
            await this.fetchStatus();
        } catch (e) {
            const { toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendError("Failed to start bridge: " + e.message, "Phantom Bridge");
        }
    },

    async stopBridge() {
        try {
            await api("bridge", { action: "stop" });
            this.running = false;
            this.novncReady = false;
            await this.fetchStatus();
        } catch (e) {
            const { toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendError("Failed to stop bridge: " + e.message, "Phantom Bridge");
        }
    },

    async deleteAllCookies() {
        try {
            const { toastFrontendInfo, toastFrontendSuccess } = await import("/components/notifications/notification-store.js");
            toastFrontendInfo("Deleting all cookies...", "Phantom Bridge");
            await api("bridge", { action: "delete_cookies" });
            toastFrontendSuccess("All cookies deleted", "Phantom Bridge");
            await this.fetchStatus();
        } catch (e) {
            const { toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendError("Failed to delete cookies: " + e.message, "Phantom Bridge");
        }
    },

    async replayPlaybook(name) {
        try {
            const { toastFrontendInfo, toastFrontendSuccess, toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendInfo(`Replaying '${name}'...`, "Phantom Bridge");
            const result = await api("bridge", { action: "replay", name });
            if (result.ok) {
                toastFrontendSuccess(`Replay '${name}' started (${result.steps} steps)`, "Phantom Bridge");
            } else {
                toastFrontendError(`Replay failed: ${result.error}`, "Phantom Bridge");
            }
        } catch (e) {
            const { toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendError("Replay failed: " + e.message, "Phantom Bridge");
        }
    },

    async deletePlaybook(name) {
        if (!confirm(`Delete playbook '${name}'?`)) return;
        try {
            const { toastFrontendSuccess, toastFrontendError } = await import("/components/notifications/notification-store.js");
            const result = await api("bridge", { action: "delete_playbook", name });
            if (result.ok) {
                toastFrontendSuccess(`Deleted '${name}'`, "Phantom Bridge");
                await this.fetchStatus();
            } else {
                toastFrontendError(`Delete failed: ${result.error}`, "Phantom Bridge");
            }
        } catch (e) {
            const { toastFrontendError } = await import("/components/notifications/notification-store.js");
            toastFrontendError("Delete failed: " + e.message, "Phantom Bridge");
        }
    },

    openBridge() {
        // Same-origin standalone viewer (RFB over wss:///vnc_proxy) — no mixed content.
        const url = `${location.origin}/plugins/phantom_bridge/webui/bridge.html`;
        window.open(url, "phantom-bridge");
    },
});
