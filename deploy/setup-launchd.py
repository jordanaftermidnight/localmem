"""Generate launchd plists for localmem on macOS.

Run as:  python3 deploy/setup-launchd.py
   or:   curl -sL https://raw.githubusercontent.com/jordanaftermidnight/localmem/main/deploy/setup-launchd.py | python3 -

Writes three LaunchAgent plists to ~/Library/LaunchAgents/:
  - com.localmem.serve     — MCP server on port 8781
  - com.localmem.dashboard — dashboard backend on port 8782
  - com.localmem.frontend  — static frontend on port 8785

Then load each one:
  launchctl load -w ~/Library/LaunchAgents/com.localmem.serve.plist
  launchctl load -w ~/Library/LaunchAgents/com.localmem.dashboard.plist
  launchctl load -w ~/Library/LaunchAgents/com.localmem.frontend.plist

Assumes the canonical layout from the README's PyPI install path:
  - venv at  ~/.venvs/localmem/
  - data at  ~/localmem-data/
  - source clone at  ~/localmem-source/  (for the dashboard frontend dist)

Override any of these via environment variables before running:
  LOCALMEM_VENV=...  LOCALMEM_DATA=...  LOCALMEM_FRONTEND=...
"""

from __future__ import annotations

import os
import pathlib
import sys


def main() -> int:
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

    for label, args, workdir in services:
        path = AGENTS / f"{label}.plist"
        path.write_text(plist_xml(label, args, workdir))
        print(f"  wrote {path}")

    print()
    print("Next: load each one with launchctl —")
    for label, _, _ in services:
        print(f"  launchctl load -w {AGENTS}/{label}.plist")
    print()
    print("Then check status:")
    print("  launchctl list | grep localmem")
    return 0


if __name__ == "__main__":
    sys.exit(main())
