"""Generate launchd plists for localmem on macOS.

Quick start (recommended — also loads + kickstarts the services):
    python3 deploy/setup-launchd.py --load

Or write-only (review plists before loading manually):
    python3 deploy/setup-launchd.py

Writes three LaunchAgent plists to ~/Library/LaunchAgents/:
  - com.localmem.serve     — MCP server on port 8781
  - com.localmem.dashboard — dashboard backend on port 8782
  - com.localmem.frontend  — static frontend on port 8785

With --load, also runs the modern launchctl sequence:
    launchctl bootout   gui/$(id -u)/com.localmem.<svc>     # idempotent unload
    launchctl bootstrap gui/$(id -u) <plist>                # modern load
    launchctl kickstart -k gui/$(id -u)/com.localmem.<svc>  # force start now

(The old `launchctl load` is legacy and gets stuck in the "submitted but
won't run" state on modern macOS; bootstrap+kickstart is the canonical
sequence for Catalina and later.)

Assumes the canonical layout from the README's PyPI install path:
  - venv at  ~/.venvs/localmem/
  - data at  ~/localmem-data/
  - source clone at  ~/localmem-source/  (for the dashboard frontend dist)

Override any of these via environment variables before running:
  LOCALMEM_VENV=...  LOCALMEM_DATA=...  LOCALMEM_FRONTEND=...
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys


def _launchctl(args: list[str]) -> tuple[int, str]:
    """Run launchctl with the given args, returning (returncode, combined output)."""
    proc = subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _load_service(label: str, plist_path: pathlib.Path) -> None:
    """Bootout (idempotent) + bootstrap + kickstart the service.

    Uses the modern domain-aware commands. `bootout` is run with 2>/dev/null
    equivalent — failures are expected when the service isn't loaded yet.
    """
    uid = os.getuid()
    domain = f"gui/{uid}"
    target = f"{domain}/{label}"

    # bootout — clears any prior state; non-zero is expected and OK
    _launchctl(["bootout", target])

    rc, out = _launchctl(["bootstrap", domain, str(plist_path)])
    if rc != 0:
        print(f"  WARN bootstrap {label}: {out}", file=sys.stderr)
        return

    rc, out = _launchctl(["kickstart", "-k", target])
    if rc != 0:
        print(f"  WARN kickstart {label}: {out}", file=sys.stderr)
        return

    print(f"  loaded {label}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate (and optionally load) localmem LaunchAgents on macOS."
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="Also run launchctl bootstrap + kickstart for each service. "
             "Without this, only the .plist files are written.",
    )
    args = parser.parse_args()

    HOME = pathlib.Path.home()

    VENV = pathlib.Path(os.environ.get("LOCALMEM_VENV", HOME / ".venvs/localmem"))
    DATA = pathlib.Path(os.environ.get("LOCALMEM_DATA", HOME / "localmem-data"))
    FRONTEND = pathlib.Path(
        os.environ.get("LOCALMEM_FRONTEND", HOME / "localmem-source/dashboard/dist")
    )

    VENV_BIN = VENV / "bin" / "localmem"
    AGENTS = HOME / "Library" / "LaunchAgents"

    # Existence checks — clearer error than launchd's silent failure later.
    for required, label in (
        (VENV_BIN, "venv localmem binary"),
        (DATA / "localmem.yaml", "localmem.yaml config"),
        (FRONTEND / "index.html", "dashboard frontend bundle"),
    ):
        if not required.exists():
            print(f"ERROR: missing {label} at {required}", file=sys.stderr)
            print(
                "Set LOCALMEM_VENV / LOCALMEM_DATA / LOCALMEM_FRONTEND env vars "
                "if your layout differs.",
                file=sys.stderr,
            )
            return 1

    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)

    def plist_xml(label: str, args: list[str], workdir: pathlib.Path) -> str:
        args_xml = "".join(f"<string>{a}</string>" for a in args)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            f"<key>Label</key><string>{label}</string>"
            f"<key>ProgramArguments</key><array>{args_xml}</array>"
            f"<key>WorkingDirectory</key><string>{workdir}</string>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            f"<key>StandardOutPath</key><string>{DATA}/logs/{label}.out.log</string>"
            f"<key>StandardErrorPath</key><string>{DATA}/logs/{label}.err.log</string>"
            "</dict></plist>\n"
        )

    services = [
        (
            "com.localmem.serve",
            [str(VENV_BIN), "-c", str(DATA / "localmem.yaml"), "serve"],
            DATA,
        ),
        (
            "com.localmem.dashboard",
            [str(VENV_BIN), "-c", str(DATA / "localmem.yaml"), "dashboard"],
            DATA,
        ),
        (
            "com.localmem.frontend",
            ["/usr/bin/python3", "-m", "http.server", "8785"],
            FRONTEND,
        ),
    ]

    written: list[tuple[str, pathlib.Path]] = []
    for label, prog_args, workdir in services:
        path = AGENTS / f"{label}.plist"
        path.write_text(plist_xml(label, prog_args, workdir))
        print(f"  wrote {path}")
        written.append((label, path))

    if args.load:
        print()
        print("Loading services (bootout → bootstrap → kickstart) ...")
        if shutil.which("launchctl") is None:
            print("  ERROR: launchctl not found in PATH — are you on macOS?",
                  file=sys.stderr)
            return 2
        for label, path in written:
            _load_service(label, path)
        print()
        print("Check status: launchctl list | grep localmem")
        print("Tail logs:    tail -f ~/localmem-data/logs/com.localmem.*.log")
    else:
        print()
        print("Plists written but NOT loaded. To load them now, re-run with --load:")
        print("  python3 deploy/setup-launchd.py --load")
        print("Or load manually (modern sequence):")
        for label, path in written:
            print(f"  launchctl bootout   gui/$(id -u)/{label} 2>/dev/null")
            print(f"  launchctl bootstrap gui/$(id -u) {path}")
            print(f"  launchctl kickstart -k gui/$(id -u)/{label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
