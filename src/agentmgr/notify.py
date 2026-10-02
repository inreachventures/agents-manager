"""macOS notifications."""

from __future__ import annotations

import subprocess
import sys


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{_esc(message)}" with title "{_esc(title)}" sound name "Tink"'
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
