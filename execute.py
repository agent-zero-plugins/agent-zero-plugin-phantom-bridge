"""Phantom Bridge — Setup script.

Installs x11vnc and noVNC packages, then prints setup instructions.
Run from A0's Plugins UI or manually: python execute.py
"""

import shutil
import subprocess
import sys


def main():
    print("=" * 50)
    print("  Phantom Bridge — Setup")
    print("=" * 50)
    print()

    # Check what's already installed
    has_x11vnc = shutil.which("x11vnc") is not None
    has_websockify = shutil.which("websockify") is not None

    if has_x11vnc and has_websockify:
        print("[OK] x11vnc and websockify already installed.")
    else:
        print("Installing x11vnc and noVNC...")
        result = subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends",
         "x11vnc", "novnc", "xvfb", "xdotool", "chromium"],
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            print(f"[ERROR] apt-get install failed:\n{result.stderr}")
            print("\nManual install: sudo apt-get install x11vnc novnc")
            return 1

        print("[OK] Installed x11vnc and noVNC (with websockify)")

        # Verify binaries actually landed on PATH — apt can report success while
        # the binary is absent (dpkg desync, PATH issue, partial install).
        post_x11vnc = shutil.which("x11vnc")
        post_websockify = shutil.which("websockify")
        if not post_x11vnc or not post_websockify:
            missing_after = [b for b, found in [("x11vnc", post_x11vnc), ("websockify", post_websockify)] if not found]
            print(
                f"[ERROR] apt reported success but these binaries are not on PATH: "
                f"{', '.join(missing_after)}\n"
                "Check apt-get output above for silent errors. "
                "Try: apt-get install -y --fix-broken"
            )
            return 1

    # Install Python dependencies
    # Check what's already importable, install only what's missing.
    needed = []
    try:
        import websockets  # noqa: F401
        print("[OK] websockets already installed.")
    except ImportError:
        needed.append("websockets>=13.1,<17.0")

    try:
        import cryptography  # noqa: F401
        print("[OK] cryptography already installed.")
    except ImportError:
        needed.append("cryptography>=42.0")

    if needed:
        # Make sure pip exists in the same Python that runs this script.
        # Some A0 base images ship python3 without the pip module bundled.
        try:
            import pip  # noqa: F401
        except ImportError:
            print("[INFO] pip module missing — bootstrapping via apt...")
            apt_result = subprocess.run(
                ["apt-get", "install", "-y", "--no-install-recommends", "python3-pip"],
                text=True,
                capture_output=True,
            )
            if apt_result.returncode != 0:
                print(f"[ERROR] Could not install python3-pip: {apt_result.stderr[:200]}")
                print("Manual fix: apt-get install -y python3-pip")
                return 1

        # Use --break-system-packages for PEP 668 (Debian Bookworm+).
        # Use --ignore-installed to avoid fighting with apt-managed packages.
        result = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--break-system-packages", "--ignore-installed", "--no-cache-dir",
                *needed,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            print(f"[OK] Installed Python deps: {', '.join(needed)}")
        else:
            print(f"[ERROR] pip install failed: {result.stderr[:300]}")
            print("Manual fix: python3 -m pip install --break-system-packages " + " ".join(needed))
            return 1

    print()
    print("=" * 60)
    print("  Phantom Bridge — Setup complete!")
    print("=" * 60)
    print()
    print("  Next: make sure port 6080 is exposed on your Docker host.")
    print("  (This script runs inside the container and cannot detect")
    print("  host-side port mappings — check your docker run / compose.)")
    print()
    print("  If you used 'docker run', restart with -p 6080:6080:")
    print()
    print("    docker stop agent-zero && docker rm agent-zero")
    print("    docker run -d --name agent-zero \\")
    print('      -p 5080:80 -p 6080:6080 \\')
    print('      -v "$(pwd)/agent-zero/usr:/a0/usr" \\')
    print("      agent0ai/agent-zero:latest")
    print()
    print("  If you use docker-compose, add to your ports section:")
    print('    - "6080:6080"    # Phantom Bridge viewer')
    print("  Then: docker compose up -d")
    print()
    print("  If port 6080 is already mapped, you're good to go.")
    print("  Open A0's sidebar > Phantom Bridge to start browsing.")
    print()
    print("  After the port is exposed, open A0's sidebar and click")
    print("  the Phantom Bridge icon, or tell A0: 'open the browser bridge'")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
