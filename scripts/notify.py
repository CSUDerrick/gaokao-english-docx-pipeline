"""macOS completion notification via osascript.

Nothing to bundle: ``/usr/bin/osascript`` ships with macOS and works from a frozen
PyInstaller app, exactly like this project already shells out to ``open`` and ``security``.
On any non-mac platform, or if osascript is unavailable, ``notify()`` is a quiet no-op —
callers never have to guard the platform themselves.
"""

from __future__ import annotations

import subprocess
import sys


def _escape(text: str) -> str:
    """AppleScript string escaping: backslash and double-quote, newlines flattened, length
    capped. A result path or an error message with a stray ``"`` would otherwise break the
    one-liner handed to osascript (or worse, let text run as script)."""
    escaped = str(text or "").replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").replace("\r", " ")
    return escaped[:240]


def notify(title: str, body: str, *, success: bool = True) -> bool:
    """Best-effort macOS notification. Returns True if it was dispatched."""
    if sys.platform != "darwin":
        return False
    sound = "Glass" if success else "Basso"
    script = (
        f'display notification "{_escape(body)}" '
        f'with title "{_escape(title)}" sound name "{sound}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except Exception:
        return False
