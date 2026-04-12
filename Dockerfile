# Phantom Bridge — prebuilt Docker image
#
# Extends frdel/agent-zero-run with all Phantom Bridge dependencies
# pre-installed so users skip the apt/pip/execute.py manual steps.
#
# Published to: ghcr.io/notabotchef/phantom-bridge
# Usage:        see docker-compose.override.yml in this repo
#
# Base image pin: update this tag when frdel releases a new version.
# Check: https://hub.docker.com/r/frdel/agent-zero-run/tags
FROM frdel/agent-zero-run:latest

LABEL org.opencontainers.image.title="Phantom Bridge"
LABEL org.opencontainers.image.description="Agent Zero + Phantom Bridge prebuilt image"
LABEL org.opencontainers.image.source="https://github.com/notabotchef/phantom-bridge"
LABEL org.opencontainers.image.licenses="MIT"

# ---------------------------------------------------------------------------
# System dependencies — Phantom Bridge display + browser stack
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    x11vnc \
    novnc \
    xvfb \
    xdotool \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python runtime dependencies
# Use `python3 -m pip` (not bare `pip`) — frdel/agent-zero-run does not
# expose `pip` on PATH. `--break-system-packages` is required on Debian
# Bookworm+ (PEP 668). If A0 ships a venv at /opt/venv, install there too
# so the agent runtime sees the same packages.
# ---------------------------------------------------------------------------
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        "websockets>=13.1,<17.0" \
        "cryptography>=42.0,<45.0" \
        pyyaml \
    && if [ -x /opt/venv/bin/pip ]; then \
        /opt/venv/bin/pip install --no-cache-dir \
            "websockets>=13.1,<17.0" \
            "cryptography>=42.0,<45.0" \
            pyyaml; \
    fi

# ---------------------------------------------------------------------------
# Bake plugin into image — smart entrypoint will only use this copy when
# the user has NOT mounted their own plugin at runtime.
# IMPORTANT: data/ is excluded (see .dockerignore) — never bake user state.
# ---------------------------------------------------------------------------
COPY . /opt/phantom_bridge_baked/

# ---------------------------------------------------------------------------
# Smart entrypoint — presence check before copy, then chains to base image
# ---------------------------------------------------------------------------
COPY docker-entrypoint-phantom.sh /usr/local/bin/docker-entrypoint-phantom.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-phantom.sh

# ---------------------------------------------------------------------------
# Docker HEALTHCHECK — uses bridge_doctor --quiet
# start-period gives A0 + Chromium + noVNC time to fully initialize
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python /a0/usr/plugins/phantom_bridge/tools/bridge_doctor.py --quiet || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-phantom.sh"]
