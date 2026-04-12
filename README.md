<p align="center">
  <img src="docs/banner.png" alt="Phantom Bridge" width="700" />
</p>

<p align="center">
  <strong>Log into any service once. A0 uses it forever.</strong>
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
  <a href="https://github.com/frdel/agent-zero"><img src="https://img.shields.io/badge/Agent_Zero-plugin-orange.svg" alt="A0 Compatible"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-1.4.1-purple.svg" alt="Version 1.4.1"></a>
</p>

<p align="center">
  An Agent Zero plugin that opens a remote browser so you can authenticate to any web service from your own browser.<br>
  A0 inherits those sessions, learns site patterns, and replays recorded workflows autonomously.
</p>

---

## What's New in v1.4.1

- **One-liner installer** — `install.sh` with security warning, 5-second countdown, SHA256 verification, A0 dir auto-detection, Docker and manual modes, post-install `bridge_doctor` check. Shellcheck clean.
- **Release workflow** — `.github/workflows/release.yml` publishes install.sh SHA256 to GitHub Release notes so users can verify before running.
- README "Install via script" section placed after Quick Start with explicit security warning.

### v1.4.0

- **Prebuilt Docker image** — `ghcr.io/notabotchef/phantom-bridge:latest` has x11vnc, novnc, xvfb, xdotool, chromium, websockets, and cryptography pre-installed. No apt or pip steps.
- **Smart entrypoint** — `docker-entrypoint-phantom.sh` detects whether you have a git-cloned plugin mounted and skips the baked copy if so. Your `git pull` workflow is preserved.
- **Drop-in compose override** — `docker-compose.override.yml` drops next to your existing compose file and auto-merges. No edits to your original file needed.
- **GitHub Actions publish pipeline** — `.github/workflows/docker-publish.yml` builds multi-arch (amd64 + arm64) on tag push and publishes to ghcr.io with `packages: write`.
- **Quick Start section** — README now leads with the 3-command Docker path, with the manual install preserved below as a fallback.

### v1.3.0

- **Diagnostics & pre-flight** — `bridge_doctor` tool runs 5 health checks (noVNC port, system binaries, DISPLAY env, cookie key, Python deps) and prints copy-paste fix commands for every failure.
- **Pre-flight in `bridge_open`** — After the bridge starts, `probe_novnc` checks the noVNC endpoint and prepends an actionable hint to the response if the viewer is unreachable — no more silent blank screens.
- **State-aware WebUI** — The sidebar panel shows an amber/red banner with the exact fix hint when `health_state != healthy`.
- **Verified `execute.py`** — After `apt-get install`, the installer now re-checks that `x11vnc` and `websockify` actually landed on PATH and exits 1 with a clear error if they didn't.

### v1.2.1

- **Centralized data path resolution** — All persistent data paths (cookies, playbooks, sitemaps, auth registry, profile) now resolve through a single `data_paths.py` module. Supports `PHANTOM_BRIDGE_DATA_DIR` environment variable to relocate the entire data tree (e.g., to a mounted volume).

### v1.2.0

- **Robust playbook replay** — Recorded playbooks now capture multiple locator strategies per step (CSS selector, visible text, ARIA role, aria-label, placeholder, label text, input type). During replay, if an exact selector breaks (dynamic class names, hashed IDs, SPA re-renders), the engine falls back through alternative strategies automatically.
- **Agent-guided instructions** — New `Playbook.to_agent_instructions()` generates natural-language workflow descriptions with prioritized locator hints, letting A0 reason about element location instead of blindly executing brittle scripts.
- **Recording UI** — Start and stop playbook recordings directly from the sidebar panel with real-time notifications and an A0 progress bar.

### v1.1.1

- **WebSocket push events** — Bridge status and auth events are pushed to the UI in real time. No more 5-second polling.
- **Self-correction error messages** — All tool error paths now include canonical JSON call examples so A0 v1.5 agents can fix malformed calls automatically.
- **Small-model prompt** — Compact ~200-token system prompt variant for models with ≤ 8192 context window, preserving their token budget.
- **Unicode sanitization** — Page titles and DOM events are sanitized before storage so lone surrogates from malformed web pages never crash JSON serialization.
- **Cache-Control: no-store** — Bridge API responses opt out of A0 v1.5's new API caching so status and cookie data are always live.
- **Task lifecycle safety** — `ObserverManager.start()` is idempotent; `stop()` cancels and awaits all background tasks.
- **Review fixes** — Cookie files are now flushed to disk when a new login is detected (not just on poll), and a failed CDP connect no longer permanently blocks retries.

**v1.1.0** — Cookies encrypted at rest (Fernet), per-domain cookie files, `bridge_decrypt_cookies` tool.

---

## The Problem

A0 runs inside a Docker container with its own Chromium. When it needs to access authenticated services, the traditional options are:

- **Export cookies** from your host browser → import into the container → watch them get invalidated because fingerprints don't match
- **Build OAuth integrations** for every service → weeks of work, partner approvals, API waitlists
- **Hard-code credentials** → security nightmare, breaks on 2FA

None of these scale. Every new service is another integration project.

## The Solution

Phantom Bridge flips the model. Instead of moving credentials *into* the container, you use the container's browser *directly*:

```
1. Tell A0: "open the browser bridge"
2. A remote browser viewer appears — you're looking at A0's Chromium
3. Log into anything. Google, NotebookLM, X, GitHub, AWS, Jira.
4. Close the bridge. A0's browser agent inherits every session.
5. Show A0 a workflow once — it replays it forever.
```

Sessions persist across container restarts. No export/import. No fingerprint mismatch. No API keys.

---

## Features

### Remote Browser Control

Full native browser control via [noVNC](https://novnc.com) — not screenshots, not iframes, real VNC.

- **Keyboard, mouse, clipboard** — everything works, including captchas
- **Draggable modal** inside A0's UI with minimize, resize, and pop-out
- **Screencast fallback** — works through A0's port when noVNC port isn't exposed

### Session Inheritance

The bridge and A0's `browser_agent` share the same Chromium profile directory. When you log into Google via the bridge, A0's browser agent has those cookies immediately. No transfer step.

```
Bridge Chromium ──→ data/profile/ ←── A0's browser_agent
                   (shared cookies, localStorage, sessions)
```

### Cookie Management

Cookies are stored as **encrypted per-domain files** at `data/cookies/<domain>.json`. Cookie values are encrypted at rest using [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption — names and metadata stay in plaintext so A0 can inspect structure without decrypting:

```json
[
  { "name": "SID", "encrypted_value": "gAAAAABn...", "domain": ".google.com", "httpOnly": true, "secure": true, "expires": 1756684800 },
  { "name": "HSID", "encrypted_value": "gAAAAABn...", "domain": ".google.com", "httpOnly": true, "secure": false }
]
```

- **Encrypted at rest** — session tokens are never stored in plaintext on disk
- **Per-domain files** — A0 only loads cookies for the domain it needs (cheaper token calls)
- **On-demand decryption** — A0 uses the `bridge_decrypt_cookies` tool to get plaintext cookies in memory when needed for HTTP requests
- Live cookie counts per domain in the sidebar panel
- **Delete All** button to wipe every session instantly

### Intelligent Observer

Three observation layers watch silently while you browse:

| Layer | What it does | Data file |
|-------|-------------|-----------|
| **Auth Registry** | Detects logins via cookie diffing + auth URL patterns | `data/auth_registry.json` |
| **Sitemap Learner** | Maps URL patterns and features per domain | `data/sitemaps/*.json` |
| **Playbook Recorder** | Records replayable navigation sequences | `data/playbooks/*.json` |

### A0 Intelligence Layer

The plugin injects context into A0's system prompt (additive — never replaces the core prompt):

- **When to suggest the bridge** — A0 proactively suggests opening the bridge when it detects authentication failures, login redirects, or requests for authenticated services
- **Live session state** — A0 knows which domains are authenticated and when sessions expire
- **Playbook awareness** — A0 knows what workflows have been recorded and suggests replay when appropriate

```
"I need authenticated access to NotebookLM. Would you like to
open the Phantom Bridge so you can log in? I'll be able to use
that session afterward."
```

### Pattern Learning

Teach A0 once, it does it forever:

1. Open the bridge
2. Tell A0: *"Record this — I'll show you how to generate images on Gemini"* (or click **Start Recording** in the sidebar)
3. Walk through the workflow in the remote viewer
4. Tell A0: *"Stop recording"* (or click **Stop Recording** in the sidebar)
5. From now on: *"Generate images on Gemini like I showed you"*

A0 replays the recorded workflow autonomously using Playwright with the shared browser profile. Each step captures **6 locator strategies** — so when CSS selectors break (dynamic classes, hashed IDs, SPA re-renders), replay falls back through text, ARIA role, aria-label, placeholder, and label matches automatically.

---

## Architecture

```
Your Browser ──→ A0 Web UI (:5050)
                    │
                    ├── Sidebar Panel ── status, cookies, sitemaps, playbooks
                    │
                    └── Phantom Bridge Modal (draggable, resizable)
                            │
                      noVNC iframe (:6080)
                            │
                      websockify ──→ x11vnc ──→ Xvfb display
                                                    │
                                               Chromium (system)
                                                    │
                                              CDP WebSocket
                                                    │
                                        ┌───────────┴───────────┐
                                        │    Observer Layers     │
                                        │  ┌─ Auth Registry      │
                                        │  ├─ Sitemap Learner    │
                                        │  └─ Playbook Recorder  │
                                        └────────────────────────┘
                                                    │
                                          data/profile/ (shared)
                                                    │
                                          A0's browser_agent
```

### How Profile Sharing Works

The `_30_browser_bridge_profile.py` extension runs at `message_loop_start` and patches `browser_agent.State.get_user_data_dir()` to return the bridge's profile directory instead of an ephemeral one. It also patches `__del__` to prevent profile deletion. This means A0's browser agent uses the exact same cookies, localStorage, and sessions you created via the bridge.

---

## Quick Start (Docker — recommended)

The fastest path: one file download, one command, done. No apt, no pip, no execute.py.

```bash
# 1. Download the drop-in compose override
curl -O https://raw.githubusercontent.com/notabotchef/phantom-bridge/main/docker-compose.override.yml

# 2. Start (or restart) your A0 stack — Compose auto-merges the override
docker compose up -d

# 3. Open A0 in your browser
open http://localhost:5050   # then click the Phantom Bridge icon in the sidebar
```

The prebuilt image (`ghcr.io/notabotchef/phantom-bridge:latest`) has all system dependencies pre-installed.
If you already have A0 running with a `git clone` of this plugin, the smart entrypoint detects your mounted directory and skips the baked copy — your `git pull` workflow is preserved.

> **Different noVNC port?** Set `PHANTOM_NOVNC_PORT=6081` in your `.env` file before running `docker compose up -d`.

---

## Install via script (advanced, opt-in)

> **Read the script before running.** Piping curl to bash runs code you haven't reviewed. Prefer the Quick Start above unless you have a specific reason.
> Script source: [`install.sh`](install.sh) in this repo. Pinned SHA published per release.

Two equivalent forms — the second lets you inspect before executing:

```bash
# Form 1: pipe directly (shows a 5-second countdown + Y/n confirm)
bash <(curl -fsSL https://raw.githubusercontent.com/notabotchef/phantom-bridge/main/install.sh)

# Form 2: download, inspect, then run (recommended)
curl -fsSL https://raw.githubusercontent.com/notabotchef/phantom-bridge/main/install.sh -o install.sh
less install.sh   # read it
bash install.sh
```

Options:

```bash
bash install.sh --dry-run --yes       # preview what would happen, no changes
bash install.sh --mode=manual         # git clone + execute.py instead of compose
bash install.sh --path=/your/a0/dir  # override auto-detected A0 directory
```

Verify the script SHA before running (SHA is published in each [GitHub Release](https://github.com/notabotchef/phantom-bridge/releases)):

```bash
echo "<sha-from-release>  install.sh" | sha256sum --check
```

---

## Manual Install

The traditional install path — use this if you prefer full control, already have a custom A0 setup, or cannot use Docker Compose overrides.

### 1. Install the plugin

```bash
# Clone into A0's plugin directory
git clone https://github.com/notabotchef/phantom-bridge.git /path/to/a0/usr/plugins/phantom_bridge

# Or copy if you already have the files
cp -r phantom_bridge /path/to/a0/usr/plugins/
```

### 2. Install dependencies

From A0's Plugins UI, click **Execute** on Phantom Bridge. Or run manually inside the container:

```bash
# Replace "a0" with your container name if different (see step 3)
docker exec -it a0 python /a0/usr/plugins/phantom_bridge/execute.py
```

This installs: `x11vnc`, `novnc`, `xvfb`, `xdotool`, `chromium`

### 3. Expose port 6080 and mount the data volume

Port 6080 is the noVNC remote viewer — it's what lets you see and control A0's browser.
The `data/` volume mount ensures cookies, sessions, and recorded playbooks survive container rebuilds.

#### Option A — docker-compose (recommended)

A ready-to-use `docker-compose.yml` is included at the repo root. Copy it and adjust paths:

```bash
cp docker-compose.yml /path/to/your/a0/docker-compose.yml
```

Or add these two lines to your existing compose file:

```yaml
services:
  agent-zero:          # ← your container's service name; change if different
    ports:
      - "5050:5000"
      - "6080:6080"    # Phantom Bridge remote viewer
    volumes:
      - ./a0-data/usr:/a0/usr   # persists plugins, sessions, cookies
```

Then restart: `docker compose up -d`

#### Option B — docker run

```bash
docker run -d \
  --name a0 \
  -p 5050:5000 \
  -p 6080:6080 \
  -v "$(pwd)/a0-data/usr:/a0/usr" \
  frdel/agent-zero-run:latest
```

> **Container name:** `docker exec` targets the **container name**, not the Compose service name. The container name is set by `container_name:` in `docker-compose.yml` (defaulting to something like `a0-agent-zero-1` if omitted) or by `--name` in `docker run`. The included `docker-compose.yml` sets `container_name: a0`, so `docker exec -it a0 ...` works as shown. If you omit `container_name:` or use a different value, either update the exec commands to match, or use `docker compose exec <service> ...` (e.g. `docker compose exec agent-zero ...`) which targets the service name directly and always works.

> **No port 6080?** The plugin still works — it falls back to a screencast mode that streams through A0's existing port (5050). No extra ports needed. The sidebar panel automatically detects which mode is available.

### 4. Use it

Just talk to A0:

- *"Open the browser bridge"*
- *"I need to log into Google"*
- *"Record this workflow"*
- *"Replay the export I showed you"*

Or click the phantom icon in A0's chat bar.

---

## Tools

| Tool | Description |
|------|-------------|
| `browser_bridge_open` | Start the bridge + remote viewer |
| `browser_bridge_close` | Stop the bridge (sessions persist) |
| `browser_bridge_status` | Check status, pages, authenticated domains |
| `bridge_auth` | Query authenticated domains + session expiry |
| `bridge_health` | Test if a session is still valid |
| `bridge_sitemap` | Learned URL patterns per domain |
| `bridge_record` | Start/stop recording a workflow |
| `bridge_replay` | Replay a saved workflow autonomously |
| `bridge_decrypt_cookies` | Decrypt stored cookies for a domain (for HTTP requests) |

---

## Configuration

Edit `default_config.yaml` or configure via A0's plugin settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Enable/disable the plugin |
| `remote_debug_port` | `9222` | CDP port (internal) |
| `novnc_port` | `6080` | Remote viewer port |
| `profile_dir` | `data/profile` | Browser profile location |
| `headless` | `false` | Set `true` to disable display rendering |
| `window_width` | `1280` | Browser viewport width |
| `window_height` | `900` | Browser viewport height |

---

## Data Storage

All persistent data lives in `data/` (survives container restarts when `/usr` is volume-mounted):

```
data/
├── profile/              # Chromium user data (cookies, localStorage, sessions)
├── .cookie_key           # Fernet symmetric encryption key (auto-generated)
├── cookies/              # Per-domain encrypted cookie files
│   ├── google.com.json
│   ├── github.com.json
│   └── ...
├── auth_registry.json    # Authenticated domains with expiry metadata
├── sitemaps/             # Learned URL patterns per domain
└── playbooks/            # Recorded workflows for autonomous replay
```

### Security

- **Cookie values are encrypted at rest** using Fernet symmetric encryption. The key is auto-generated at `data/.cookie_key` on first export. Cookie names and metadata remain in plaintext for structure inspection.
- **On-demand decryption only.** Plaintext cookie values are never written to disk — the `bridge_decrypt_cookies` tool returns them in memory.
- **Port 6080 gives full browser control.** Only expose on trusted networks.
- **The entire `data/` directory is gitignored.** Never commit it.
- All cookie data stays inside the container. Nothing is sent externally.

---

## Use Cases

| Service | What A0 Can Do After You Log In |
|---------|-------------------------------|
| **Google** | Access Gmail, Drive, Calendar, any Google service |
| **NotebookLM** | Query knowledge bases, generate content |
| **X / Twitter** | Post content, monitor mentions, engage |
| **Threads** | Publish posts, read feeds |
| **GitHub** | Manage repos, review PRs, triage issues |
| **AWS Console** | Monitor resources, check billing, manage services |
| **Jira / Linear** | Track sprints, update tickets, manage backlogs |
| **Vercel / Netlify** | Deploy previews, check build logs, manage domains |
| **Any web app** | If you can log into it, A0 can use it |

---

## How It Compares

| Approach | Setup | Captchas | Session Persistence | Fingerprint Match |
|----------|-------|----------|--------------------|--------------------|
| Cookie export/import | Manual | Fails | Fragile | No |
| OAuth integration | Weeks per service | N/A | Depends | N/A |
| Credential injection | Security risk | Fails on 2FA | Fragile | No |
| **Phantom Bridge** | **5 minutes** | **Works** | **Persistent** | **Perfect** |

---

## Plugin Structure

```
phantom_bridge/
├── plugin.yaml            # A0 plugin manifest
├── bridge.py              # Core BrowserBridge singleton — Chromium + noVNC lifecycle
├── cookie_crypt.py        # Fernet encryption for cookie values at rest
├── screencast.py          # CDP screencast manager (zero-config fallback)
├── execute.py             # Dependency installer
├── hooks.py               # A0 framework lifecycle hooks
├── default_config.yaml    # Plugin defaults
├── observer/              # Three-tier CDP observation system
│   ├── cdp_client.py      # WebSocket client with pub/sub + auto-reconnect
│   ├── auth_registry.py   # L1: cookie-based auth detection
│   ├── sitemap_learner.py # L2: URL pattern learning
│   ├── playbook_recorder.py # L3: workflow recording
│   └── manager.py         # Orchestrates all observer layers
├── tools/                 # A0 tool implementations (one per file)
├── api/                   # HTTP API handlers
├── extensions/            # A0 extension hooks
│   ├── system_prompt/     # Injects bridge awareness into A0's prompt
│   ├── python/            # Profile sharing patch
│   ├── prompts/           # Tool usage examples for A0
│   └── webui/             # Chat bar button + modal injection
└── webui/                 # Alpine.js sidebar panel + bridge viewer
```

---

## Troubleshooting

### Blank viewer / noVNC not loading

Run the diagnostic tool from inside the container:

```bash
docker exec -it a0 python /a0/usr/plugins/phantom_bridge/tools/bridge_doctor.py
```

Or ask A0 directly: *"Run bridge_doctor"*

`bridge_doctor` checks 5 things and prints a copy-paste fix for each failure:

| Check | What it detects | Fix |
|-------|----------------|-----|
| **noVNC port** | Port 6080 not mapped in compose | Add `"6080:6080"` to `ports:`, then `docker compose up -d` |
| **System binaries** | `x11vnc`, `websockify`, `Xvfb`, etc. missing | `apt-get install -y x11vnc novnc xvfb xdotool chromium` |
| **DISPLAY env** | Xvfb not running | Check `ps aux \| grep Xvfb`; run `bridge_open` to restart |
| **Cookie key** | `data/.cookie_key` unreadable | `chmod 600 data/.cookie_key` |
| **Python deps** | `websockets` or `cryptography` not installed | `pip install -r requirements.txt` |

### Quick exit codes (for scripting)

```bash
# exit 0 = healthy, exit 1 = something is wrong
docker exec a0 python /a0/usr/plugins/phantom_bridge/tools/bridge_doctor.py --quiet
echo "bridge health: $?"
```

---

## License

MIT

---

<p align="center">
  <sub>Built for <a href="https://github.com/frdel/agent-zero">Agent Zero</a> by <a href="https://github.com/notabotchef">@notabotchef</a></sub>
</p>
