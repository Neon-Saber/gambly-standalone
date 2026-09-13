"""
Auto-derived version string for the bot status embed (cogs/bot_status.py).

Format: v<commit-count>, e.g. v128 - just the digits, no hash suffix, so it
reads like a plain version number instead of a git artifact. Pulled straight
from git at import time so it just moves on its own every real deploy -
nothing to remember to bump by hand, and it can't drift out of sync with
what's actually running the way a hardcoded string could.

Falls back to a static placeholder if git metadata isn't available (e.g.
a deploy that ships a tarball without a .git folder, or git itself isn't
on PATH) so a missing .git dir never crashes the bot over something this
cosmetic.
"""
import os
import subprocess

_FALLBACK_VERSION = "v0"

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _git(*args):
    return subprocess.check_output(
        ["git", *args],
        cwd=_REPO_DIR,
        stderr=subprocess.DEVNULL,
    ).decode().strip()


def _compute_version():
    try:
        commit_count = _git("rev-list", "--count", "HEAD")
        return f"v{commit_count}"
    except Exception:
        return _FALLBACK_VERSION


VERSION = _compute_version()
