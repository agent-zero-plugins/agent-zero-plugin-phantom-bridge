"""
bridge_doctor — Phantom Bridge diagnostics tool.

Runs 5 health checks and reports actionable fixes for each failure:
  1. noVNC port reachability (probe_novnc)
  2. Required system binaries (x11vnc, websockify, Xvfb, xdotool, chromium)
  3. DISPLAY environment variable
  4. data/.cookie_key readable
  5. Python runtime dependencies (websockets, cryptography)

Can also be run as a CLI script for Docker HEALTHCHECK use:
    python tools/bridge_doctor.py --quiet    # exit 0 = healthy, 1 = degraded
    python tools/bridge_doctor.py --verbose  # full human-readable report
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Standalone-safe import of probe_novnc — works inside A0 context and as a
# bare `python bridge_doctor.py` call from the container.
# ---------------------------------------------------------------------------

def _import_probe():
    """Import probe_novnc + HealthState, tolerating varied sys.path contexts."""
    # Try direct import first (normal A0 plugin context)
    try:
        from usr.plugins.phantom_bridge.bridge import probe_novnc, HealthState
        return probe_novnc, HealthState
    except ImportError:
        pass

    # Standalone: bridge.py is two directories up from this file
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from bridge import probe_novnc, HealthState  # type: ignore[import]
    return probe_novnc, HealthState


# ---------------------------------------------------------------------------
# Individual check functions — each returns a result dict
# ---------------------------------------------------------------------------

_CHECK_PASS = "pass"
_CHECK_FAIL = "fail"


def _check_novnc(port: int = 6080) -> dict[str, Any]:
    probe_novnc, HealthState = _import_probe()
    result = probe_novnc(host="localhost", port=port, timeout=2.0)
    healthy = result["state"] == HealthState.HEALTHY
    return {
        "name": "noVNC port",
        "status": _CHECK_PASS if healthy else _CHECK_FAIL,
        "detail": result["detail"],
        "fix": result["fix"],
    }


def _check_bins() -> dict[str, Any]:
    required = ["x11vnc", "websockify", "Xvfb", "xdotool", "chromium"]
    missing = [b for b in required if not shutil.which(b)]
    if not missing:
        return {
            "name": "system binaries",
            "status": _CHECK_PASS,
            "detail": f"All required binaries found: {', '.join(required)}",
            "fix": "",
        }
    apt_list = " ".join(missing).replace("Xvfb", "xvfb").replace("chromium", "chromium")
    return {
        "name": "system binaries",
        "status": _CHECK_FAIL,
        "detail": f"Missing binaries: {', '.join(missing)}",
        "fix": (
            f"Install missing packages inside the container:\n"
            f"  docker exec -it a0 apt-get install -y --no-install-recommends {apt_list}\n"
            f"Or use the prebuilt image: ghcr.io/notabotchef/phantom-bridge:latest"
        ),
    }


def _check_display() -> dict[str, Any]:
    display = os.environ.get("DISPLAY", "")
    if display:
        return {
            "name": "DISPLAY env",
            "status": _CHECK_PASS,
            "detail": f"DISPLAY={display}",
            "fix": "",
        }
    return {
        "name": "DISPLAY env",
        "status": _CHECK_FAIL,
        "detail": "DISPLAY environment variable is not set",
        "fix": (
            "Set DISPLAY before starting the bridge. "
            "bridge.py starts Xvfb and exports DISPLAY=:99 automatically — "
            "if missing, Xvfb may have failed to start. "
            "Check: `docker exec a0 ps aux | grep Xvfb`"
        ),
    }


def _check_cookie_key() -> dict[str, Any]:
    # Locate data dir — support PHANTOM_BRIDGE_DATA_DIR env override
    data_env = os.environ.get("PHANTOM_BRIDGE_DATA_DIR", "")
    if data_env:
        key_path = Path(data_env) / ".cookie_key"
    else:
        # Relative to plugin root (two levels up from this file)
        key_path = Path(__file__).resolve().parent.parent / "data" / ".cookie_key"

    if key_path.exists() and key_path.is_file():
        try:
            key_path.read_bytes()  # verify readable
            return {
                "name": "cookie key",
                "status": _CHECK_PASS,
                "detail": f"Cookie key readable at {key_path}",
                "fix": "",
            }
        except OSError as e:
            return {
                "name": "cookie key",
                "status": _CHECK_FAIL,
                "detail": f"Cookie key exists but unreadable: {e}",
                "fix": (
                    f"Fix permissions: `chmod 600 {key_path}`\n"
                    "The key is auto-generated on first cookie export — "
                    "if it's missing, export cookies once via bridge_open."
                ),
            }

    # Not yet generated — this is normal on a fresh container; treat as INFO not FAIL
    return {
        "name": "cookie key",
        "status": _CHECK_PASS,
        "detail": f"Cookie key not yet generated (normal on first run) — will be created at {key_path}",
        "fix": "",
    }


def _check_python_deps() -> dict[str, Any]:
    required_modules = {
        "websockets": "websockets>=13.1,<17.0",
        "cryptography": "cryptography>=42.0,<45.0",
    }
    missing = []
    for mod, spec in required_modules.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(spec)

    if not missing:
        return {
            "name": "python deps",
            "status": _CHECK_PASS,
            "detail": f"All required Python packages importable: {', '.join(required_modules)}",
            "fix": "",
        }
    return {
        "name": "python deps",
        "status": _CHECK_FAIL,
        "detail": f"Missing Python packages: {', '.join(missing)}",
        "fix": (
            f"Install inside the container:\n"
            f"  docker exec -it a0 pip install {' '.join(missing)}\n"
            f"Or run execute.py: `docker exec -it a0 python "
            f"/a0/usr/plugins/phantom_bridge/execute.py`"
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all_checks(novnc_port: int = 6080) -> list[dict[str, Any]]:
    """Run all 5 checks and return list of result dicts."""
    return [
        _check_novnc(port=novnc_port),
        _check_bins(),
        _check_display(),
        _check_cookie_key(),
        _check_python_deps(),
    ]


def _format_report(results: list[dict[str, Any]]) -> str:
    lines = ["Phantom Bridge — Diagnostic Report", "=" * 40]
    all_pass = all(r["status"] == _CHECK_PASS for r in results)
    for r in results:
        icon = "[OK]  " if r["status"] == _CHECK_PASS else "[FAIL]"
        lines.append(f"{icon} {r['name']}: {r['detail']}")
        if r["status"] == _CHECK_FAIL and r["fix"]:
            for fix_line in r["fix"].splitlines():
                lines.append(f"        {fix_line}")
    lines.append("=" * 40)
    lines.append("Overall: HEALTHY" if all_pass else "Overall: DEGRADED — see FAIL items above")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A0 Tool interface
# ---------------------------------------------------------------------------

try:
    from helpers.tool import Tool, Response  # type: ignore[import]

    class BridgeDoctor(Tool):
        """Diagnostic tool — runs 5 health checks and reports actionable fixes."""

        async def execute(self, **kwargs: Any) -> Response:
            novnc_port = int(kwargs.get("port", 6080))
            results = run_all_checks(novnc_port=novnc_port)
            report = _format_report(results)
            all_pass = all(r["status"] == _CHECK_PASS for r in results)
            return Response(message=report, break_loop=not all_pass)

        def get_log_object(self):
            return self.agent.context.log.log(
                type="tool",
                heading=f"icon://stethoscope {self.agent.agent_name}: Bridge Doctor",
                content="",
                kvps=self.args,
            )

except ImportError:
    # Running outside A0 — BridgeDoctor class is unavailable, CLI mode only
    pass


# ---------------------------------------------------------------------------
# CLI entry point — used by Docker HEALTHCHECK and install.sh
# ---------------------------------------------------------------------------

def _cli_main(argv: list[str] | None = None) -> int:
    """CLI entry: exit 0 = healthy, 1 = degraded."""
    if argv is None:
        argv = sys.argv[1:]

    quiet = "--quiet" in argv
    verbose = "--verbose" in argv or (not quiet)

    # Read optional --port=NNNN
    novnc_port = 6080
    for arg in argv:
        if arg.startswith("--port="):
            try:
                novnc_port = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    results = run_all_checks(novnc_port=novnc_port)
    all_pass = all(r["status"] == _CHECK_PASS for r in results)

    if verbose:
        print(_format_report(results))
    elif not quiet:
        # Default: just summary line
        status = "HEALTHY" if all_pass else "DEGRADED"
        fails = [r["name"] for r in results if r["status"] == _CHECK_FAIL]
        print(f"bridge_doctor: {status}" + (f" ({', '.join(fails)} failed)" if fails else ""))

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(_cli_main())
