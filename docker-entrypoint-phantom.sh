#!/usr/bin/env bash
# Phantom Bridge — smart entrypoint
#
# Decision logic:
#   1. If the user has mounted their own plugin (bridge.py exists at PLUGIN_DIR),
#      leave it alone — they control updates via `git pull`.
#   2. Otherwise, copy the baked plugin from BAKED_DIR into PLUGIN_DIR so a
#      fresh container works without any manual install step.
#
# Safety nets:
#   - Existence check on bridge.py before any copy.
#   - `cp -rn` (no-clobber) as second safety net — never overwrites existing files.
#   - Never copies data/ — user state (cookies, playbooks, profile) is never baked.
#   - Never `rm -rf` anything.
#
# Base image entrypoint path:
#   /exe/run_a0.sh — verified against frdel/agent-zero-run:latest as of 2026-04.
#   If the base image changes this path, update BASE_ENTRYPOINT below and pin
#   the FROM tag in Dockerfile to the last known-good version.

set -euo pipefail

PLUGIN_DIR="/a0/usr/plugins/phantom_bridge"
BAKED_DIR="/opt/phantom_bridge_baked"
BASE_ENTRYPOINT="/exe/run_a0.sh"

# ---------------------------------------------------------------------------
# Smart copy — only when user has not mounted their own plugin
# ---------------------------------------------------------------------------
if [[ -f "${PLUGIN_DIR}/bridge.py" ]]; then
    echo "[phantom-bridge] user-mounted plugin detected at ${PLUGIN_DIR} — skipping baked copy"
else
    echo "[phantom-bridge] no mounted plugin found — installing baked plugin"
    mkdir -p "${PLUGIN_DIR}"

    # cp -rn = no-clobber: safe to run on a partially-populated dir
    # Exclude data/ — never copy user state directories
    for item in "${BAKED_DIR}"/*; do
        basename_item="$(basename "${item}")"
        # Skip data/ — user state must never be sourced from baked image
        if [[ "${basename_item}" == "data" ]]; then
            continue
        fi
        cp -rn "${item}" "${PLUGIN_DIR}/" 2>/dev/null || true
    done

    echo "[phantom-bridge] baked plugin installed (version: $(grep '^version:' "${PLUGIN_DIR}/plugin.yaml" 2>/dev/null | awk '{print $2}' || echo 'unknown'))"
fi

# ---------------------------------------------------------------------------
# Ensure data directory structure exists (idempotent)
# ---------------------------------------------------------------------------
mkdir -p \
    "${PLUGIN_DIR}/data/cookies" \
    "${PLUGIN_DIR}/data/sitemaps" \
    "${PLUGIN_DIR}/data/playbooks" \
    "${PLUGIN_DIR}/data/profile"

# ---------------------------------------------------------------------------
# Chain to Agent Zero base entrypoint
# ---------------------------------------------------------------------------
if [[ -x "${BASE_ENTRYPOINT}" ]]; then
    exec "${BASE_ENTRYPOINT}" "$@"
else
    echo "[phantom-bridge] ERROR: Base entrypoint not found at ${BASE_ENTRYPOINT}"
    echo "[phantom-bridge] This image may need updating — check frdel/agent-zero-run release notes."
    exit 1
fi
