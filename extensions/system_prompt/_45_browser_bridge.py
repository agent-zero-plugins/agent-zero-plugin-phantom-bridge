"""
Phantom Bridge — System Prompt Extension.

Injects bridge awareness into A0's system prompt (ADDITIVE — never replaces
the core prompt). Teaches A0:
- When to suggest the bridge (auth failures, authenticated services)
- What domains are already authenticated
- What playbooks are available for autonomous replay
- Where cookies/sessions are stored

For small models (detected by context window ≤ 8192 or known small-model name
patterns) we inject a compact ~200-token block instead of the full prompt, so
we don't consume a disproportionate share of their token budget.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from helpers.extension import Extension
from agent import LoopData

logger = logging.getLogger("phantom_bridge")

_plugin_dir = Path(__file__).resolve().parent.parent.parent

# Model name substrings that indicate a small / local model.
# Checked case-insensitively against agent.config.chat_model.name.
_SMALL_MODEL_HINTS = frozenset(
    {
        "small",
        "mini",
        "tiny",
        "nano",
        "phi",
        "gemma",
        "mistral-7b",
        "llama-7b",
        "llama3.2",
        "qwen-7b",
        "deepseek-7b",
    }
)

# Context window threshold below which we treat the model as "small".
_SMALL_CTX_THRESHOLD = 8192

# Canonical tool-call examples embedded in both prompt variants.
# v1.5 guardrails use these for self-correction.
_TOOL_EXAMPLES = """\
Tool call examples:
{"tool":"browser_bridge_open"}
{"tool":"browser_bridge_status"}
{"tool":"bridge_auth"}
{"tool":"bridge_record","action":"start","name":"my_workflow"}
{"tool":"bridge_record","action":"stop"}
{"tool":"bridge_record","action":"list"}
{"tool":"bridge_replay","name":"my_workflow"}
{"tool":"bridge_decrypt_cookies","domain":"github.com"}
{"tool":"bridge_health","domain":"github.com"}"""


def _is_small_model(agent) -> bool:
    """Return True when the agent is using a small / limited-context model."""
    try:
        cfg = agent.config.chat_model
        name_lower = getattr(cfg, "name", "").lower()
        if any(hint in name_lower for hint in _SMALL_MODEL_HINTS):
            return True
        ctx = getattr(cfg, "ctx_length", 0)
        if ctx and ctx <= _SMALL_CTX_THRESHOLD:
            return True
    except Exception:
        pass
    return False


def _compact_prompt(data_dir: Path) -> str:
    """~200-token compact injection for small models.

    Only includes: one-line role statement, tool table, canonical examples,
    and the live authenticated domain list (capped at 3).
    """
    lines = [
        "## Phantom Bridge",
        "Browser auth bridge. Use when a service needs login.",
        "",
        "Tools: browser_bridge_open, browser_bridge_close, browser_bridge_status,",
        "  bridge_auth, bridge_health, bridge_sitemap, bridge_record, bridge_replay,",
        "  bridge_decrypt_cookies",
        "",
        "Recording: user says 'record' → bridge_record action=start name=X →",
        "  user browses in noVNC (you WAIT) → user says 'stop' → bridge_record",
        "  action=stop. Replay: bridge_replay name=X. You do NOT browse during recording.",
        "",
        _TOOL_EXAMPLES,
    ]

    auth_file = data_dir / "auth_registry.json"
    if auth_file.exists():
        try:
            registry = json.loads(auth_file.read_text())
            if registry:
                lines.append("")
                lines.append("Authenticated domains:")
                for domain in list(registry.keys())[:3]:
                    entry = registry[domain]
                    status = "active" if entry.get("authenticated") else "EXPIRED"
                    lines.append(f"  {domain} — {status}")
        except Exception:
            pass

    return "\n".join(lines)


def _full_prompt(data_dir: Path) -> str:
    """Full-detail injection for large models."""
    sections: list[str] = []

    sections.append(
        """\
## Phantom Bridge — Browser Authentication & Automation

You have access to a browser bridge plugin that lets the user authenticate
to any web service from their own browser. The user connects via a remote
browser viewer (noVNC) and logs into services — you then inherit those
authenticated sessions for your browser_agent tool.

### When to suggest the bridge
- When your browser_agent cannot access a service because it requires login
- When the user asks you to do something on an authenticated platform
  (Google, NotebookLM, X/Twitter, Threads, Toast, OpenTable, any web app)
- When you detect a login redirect or "please sign in" page
- When cookies/sessions have expired for a previously authenticated domain

**Suggest it like this:** "I need authenticated access to [domain]. Would you
like to open the Phantom Bridge so you can log in? I'll be able to use that
session afterward."

### How it works
1. You call `browser_bridge_open` — this starts the bridge
2. The user opens the remote viewer from A0's sidebar (Phantom Bridge panel)
3. They browse and log in to services using their real browser
4. The observer watches silently — learning auth patterns, site maps, and workflows
5. The user closes the bridge — you now have their authenticated sessions

### Where sessions are stored
- Browser profile (cookies, localStorage): `data/profile/` in the plugin directory
- Auth registry (tracked domains): `data/auth_registry.json`
- Learned sitemaps: `data/sitemaps/` (per-domain URL patterns)
- Recorded playbooks: `data/playbooks/` (replayable workflows)

Sessions persist across container restarts.

### CRITICAL: Using authenticated sessions
When the bridge is running, Chrome is available on CDP port 9222 with all
authenticated cookies. To use these sessions:

1. **browser_use / browser_agent**: Connect to the bridge's Chrome via CDP
   instead of launching a new browser. Use cdp_url="http://127.0.0.1:9222"
   or connect_over_cdp. This gives you access to all cookies from the bridge.
2. **HTTP requests**: Cookies are stored **encrypted** in per-domain files at
   `data/cookies/<domain>.json`. Use the **bridge_decrypt_cookies** tool to
   decrypt cookies for a specific domain on demand. The tool returns a
   ready-to-use Cookie header string. Never write decrypted cookies to disk.
3. **CLI tools** (like nlm): Use --cdp-url http://127.0.0.1:9222 to
   authenticate via the bridge's running Chrome session.

DO NOT launch a fresh browser when authenticated sessions exist in the bridge.
Always check bridge_auth first, and if the domain is authenticated, connect
to the bridge's Chrome on port 9222 instead.

### Cookie encryption
Cookie values are encrypted at rest using Fernet symmetric encryption.
The key is stored at `data/.cookie_key` (auto-generated on first export).
Cookie names and metadata are in plaintext — only values are encrypted.
To read decrypted cookies, always use the **bridge_decrypt_cookies** tool.

### Recording & replaying workflows
The bridge can record what the USER does in the noVNC remote viewer and
save it as a replayable playbook. This is NOT about recording what YOU
(the agent) do — it records the HUMAN's actions via CDP observation.

**When the user says "record this", "record what I do", or "start recording":**
1. Make sure the bridge is open (call `browser_bridge_open` if not)
2. Call `bridge_record` with `action="start"` and `name="<descriptive_name>"`
3. Tell the user: "Recording started. Go ahead and perform the workflow in
   the Phantom Bridge viewer. Tell me when you're done and I'll stop recording."
4. WAIT. Do not perform browser actions yourself. The user is browsing.
5. When the user says "stop", "done", or "stop recording", call `bridge_record`
   with `action="stop"`
6. Confirm: "Recorded! I saved that as '<name>'. Say 'replay <name>' any time."

**When the user says "replay", "do what I showed you", or "repeat that":**
1. Call `bridge_replay` with the playbook name
2. The bridge replays the recorded steps autonomously using the shared
   browser profile (with all authenticated sessions intact)

**To list saved playbooks:** call `bridge_record` with `action="list"`

IMPORTANT: Recording captures the USER's actions, not yours. When recording
is active, do NOT use browser_agent — just wait for the user to finish.

"""
        + _TOOL_EXAMPLES
    )

    # ----- Live auth state -----
    auth_file = data_dir / "auth_registry.json"
    if auth_file.exists():
        try:
            registry = json.loads(auth_file.read_text())
            if registry:
                sections.append("### Currently Authenticated Domains")
                for domain, entry in registry.items():
                    status = "active" if entry.get("authenticated") else "EXPIRED"
                    expires = entry.get("expires_at", "unknown")
                    sections.append(f"- **{domain}** — {status} (expires: {expires})")
                sections.append("")
                sections.append(
                    "Use these sessions with browser_agent. If a session shows "
                    "EXPIRED, suggest the user re-authenticate via the bridge."
                )
                sections.append("")
        except Exception:
            pass

    # ----- Available playbooks (cap at 5 for full prompt, 3 for compact) -----
    playbooks_dir = data_dir / "playbooks"
    if playbooks_dir.exists():
        playbook_files = list(playbooks_dir.glob("*.json"))
        if playbook_files:
            sections.append("### Saved Playbooks (replayable workflows)")
            sections.append(
                "These are workflows the user demonstrated via the bridge. "
                "You can replay them autonomously using `bridge_replay`."
            )
            for pf in playbook_files[:5]:
                try:
                    pb = json.loads(pf.read_text())
                    name = pb.get("name", pf.stem)
                    domain = pb.get("domain", "unknown")
                    steps = len(pb.get("steps", []))
                    desc = pb.get("description", "")
                    desc_str = f" — {desc}" if desc else ""
                    sections.append(f"- **{name}** ({domain}, {steps} steps){desc_str}")
                except Exception:
                    pass
            sections.append("")
            sections.append(
                "When the user asks you to repeat a task that matches a saved "
                "playbook, use `bridge_replay` instead of navigating manually."
            )
            sections.append("")

    # ----- Sitemaps summary (cap at 3) -----
    sitemaps_dir = data_dir / "sitemaps"
    if sitemaps_dir.exists():
        sitemap_files = list(sitemaps_dir.glob("*.json"))
        if sitemap_files:
            sections.append("### Learned Site Maps")
            for sf in sitemap_files[:3]:
                try:
                    sm = json.loads(sf.read_text())
                    domain = sm.get("domain", sf.stem)
                    features = sm.get("features", {})
                    sections.append(f"- **{domain}** — {len(features)} features mapped")
                except Exception:
                    pass
            sections.append("")

    return "\n".join(sections)


class BrowserBridgeContext(Extension):
    async def execute(
        self,
        system_prompt: list[str] = [],
        loop_data: LoopData = LoopData(),
        **kwargs: Any,
    ) -> None:
        try:
            from usr.plugins.phantom_bridge.data_paths import DATA_DIR
        except ImportError:
            from data_paths import DATA_DIR

        if _is_small_model(self.agent):
            system_prompt.append(_compact_prompt(DATA_DIR))
        else:
            system_prompt.append(_full_prompt(DATA_DIR))
