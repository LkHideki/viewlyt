"""System clipboard shim — the smallest possible, stdlib-only.

Kept out of ``cli`` (which imports Selenium) so light subcommands like
``vl split`` can copy without paying that cost.
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
