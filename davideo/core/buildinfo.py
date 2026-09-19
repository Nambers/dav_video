"""When this copy of davideo was built -- one line for the about box.

The version string alone can't answer "how old is what I'm running": a release
and an AUR ``-git`` package both say 0.1.0, and the -git one is rebuilt from
whatever HEAD was that day. Three sources, first hit wins:

  1. ``davideo/_build.py`` -- a stamp written at build time (generated, never
     committed; see ``scripts/stamp_build.py``). The only source that survives
     installation, so this is the one a packager sets.
  2. git HEAD -- running from a checkout: the dev tree, and the -git package's
     build tree before it turns into site-packages.
  3. nothing -- "unknown" is a normal answer here, not an error.

Pure stdlib, and every failure path returns empty rather than raising: this is
a diagnostic line in a dialog, never something playback depends on. A wrong
date is worse than no date, so anything unparseable is dropped, not guessed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

# The about box opens synchronously, so git gets a short leash: on a slow or
# networked checkout we'd rather say "unknown" than stall the UI.
GIT_TIMEOUT = 2.0
_STAMP_MODULE = "davideo._build"


@dataclass(frozen=True)
class Build:
    time: str = ""          # "2026-09-18 12:34", local time; "" if unknown
    commit: str = ""        # short sha; "" if unknown
    modified: bool = False  # checkout had uncommitted changes to tracked files


def format_time(value: str | int | float | None) -> str:
    """ISO-8601 (what ``git log --format=%cI`` prints) or epoch seconds (what
    SOURCE_DATE_EPOCH holds) -> "YYYY-MM-DD HH:MM" in local time. Anything else
    is ""."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        try:
            stamp = datetime.fromtimestamp(float(text))
        except (ValueError, OverflowError, OSError):
            return ""
    try:
        # A naive stamp is taken as local time (astimezone's own rule), which
        # is what a packager writing datetime.now().isoformat() meant.
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return ""


def describe(build: Build) -> str:
    """The about-box value: "2026-09-18 12:34 (1a2b3c4, modified)". "" when we
    know nothing at all."""
    extra = ", ".join(
        part for part in (build.commit, "modified" if build.modified else "") if part
    )
    if build.time and extra:
        return f"{build.time} ({extra})"
    return build.time or extra


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess | None:
    """Run git in ``cwd``. None if git isn't installed, or took too long."""
    try:
        return subprocess.run(
            ("git", "-C", str(cwd), *args),
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def checkout_root() -> Path | None:
    """The git checkout this file lives in, or None. An installed copy has no
    .git above it -- which is exactly the "not a checkout" answer we want."""
    for folder in Path(__file__).resolve().parents:
        # Worktrees and submodules put a *file* there, so don't test is_dir().
        if (folder / ".git").exists():
            return folder
    return None


def git_head() -> tuple[str, str]:
    """(ISO-8601 commit time, short sha) of HEAD, raw as git prints them.
    ("", "") when there's no checkout, no git binary, or no commits yet."""
    root = checkout_root()
    if root is None:
        return "", ""
    # \0 as the separator: a commit time can't contain one, and %h can't either.
    done = _git("log", "-1", "--format=%cI%x00%h", cwd=root)
    if done is None or done.returncode != 0:
        return "", ""
    iso, _, short = done.stdout.strip().partition("\0")
    return iso, short


def _git_modified(root: Path) -> bool:
    """Tracked files differ from HEAD. Untracked files deliberately don't
    count: a scratch file lying in the tree didn't change what got built."""
    done = _git("diff", "--quiet", "HEAD", "--", cwd=root)
    # 0 = clean, 1 = differences, anything else = git couldn't tell us.
    return done is not None and done.returncode == 1


def _from_stamp() -> Build:
    try:
        from importlib import import_module
        module = import_module(_STAMP_MODULE)
    except Exception:  # noqa: BLE001 - no stamp is the normal case
        return Build()
    return Build(
        time=format_time(getattr(module, "BUILD_TIME", "")),
        commit=str(getattr(module, "COMMIT", "") or "")[:12],
    )


def _from_git() -> Build:
    iso, short = git_head()
    if not iso and not short:
        return Build()
    root = checkout_root()
    return Build(
        time=format_time(iso),
        commit=short,
        modified=root is not None and _git_modified(root),
    )


@lru_cache(maxsize=1)
def current() -> Build:
    """Where this copy came from. Cached: a build is a build, and the about box
    shouldn't spawn two git processes every time it's opened. (The ``modified``
    flag can go stale in a dev tree -- not worth a subprocess per keypress.)"""
    try:
        stamp = _from_stamp()
        if stamp.time or stamp.commit:
            return stamp
        return _from_git()
    except Exception:  # noqa: BLE001 - a diagnostic line must never break the box
        return Build()


def build_info() -> str:
    """One line for the about box; "" when we can't tell."""
    return describe(current())
