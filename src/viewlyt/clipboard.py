"""System clipboard shim — the smallest possible, stdlib-only.

Kept out of ``cli`` (which imports Selenium) so light subcommands like
``vl split``/``vl watch`` can copy/read without paying that cost.
"""

from __future__ import annotations


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the system clipboard via the first available OS tool.

    Tries pbcopy (macOS), clip (Windows), then xclip/xsel (Linux/X11). Returns
    True on success, False if no tool is found or the copy fails — callers warn
    but never abort.
    """
    import shutil
    import subprocess

    candidates = (
        ["pbcopy"],
        ["clip"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except Exception:  # tool present but failed (e.g. no DISPLAY) -> try next
            continue
    return False


def read_clipboard() -> str | None:
    """Read the system clipboard's text via the first available OS tool.

    Tries pbpaste (macOS), PowerShell's Get-Clipboard (Windows), then xclip/xsel
    (Linux/X11). Raises ``RuntimeError`` when NO such tool exists on the system at
    all — a poller (``vl watch``) must know up front it will never work, instead
    of looping silently forever. Returns ``None`` when a tool exists but this
    particular read failed (e.g. xclip with no ``$DISPLAY``): a transient hiccup
    the caller just retries on the next tick.
    """
    import shutil
    import subprocess

    candidates = (
        ["pbpaste"],
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    )
    found_tool = False
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        found_tool = True
        try:
            out = subprocess.run(cmd, capture_output=True, check=True)
            return out.stdout.decode("utf-8")
        except Exception:  # tool present but failed (e.g. no DISPLAY) -> try next
            continue
    if not found_tool:
        raise RuntimeError("no clipboard read tool found (pbpaste/powershell/xclip/xsel)")
    return None
