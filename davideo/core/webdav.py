"""Thin WebDAV client: list a directory + build the info mpv needs to stream.

Design choice: we do NOT embed credentials in the URL (avoids %-encoding the
password and leaking it in process lists). Instead we hand mpv a clean URL plus
an ``Authorization: Basic`` header via mpv's ``http-header-fields`` property.
Same header also authenticates external subtitle fetches.

Compatible with both Alist and generic WebDAV (Nextcloud / nginx dav / ...):
paths returned by the server are normalized, and ``base_url`` may already carry
whatever prefix the backend uses (e.g. Alist's ``/dav``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import quote

from .models import Server

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx"}


@dataclass
class Entry:
    path: str          # server-relative path, e.g. /Movies/a.mkv
    kind: str          # "directory" | "file"
    display: str       # basename for the UI
    size: int | None = None

    @property
    def is_dir(self) -> bool:
        return self.kind == "directory"


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _ext(name: str) -> str:
    base = _basename(name)
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def _stem(name: str) -> str:
    """Basename minus its final extension. 'Show.S01E01.zh.srt' -> 'Show.S01E01.zh'."""
    base = _basename(name)
    return base.rsplit(".", 1)[0] if "." in base else base


# Characters a sidecar subtitle may use to separate the video stem from a
# language/tag suffix: movie.mkv -> movie.zh.srt / movie_eng.srt / movie - fr.ass
_TAG_SEPARATORS = "._- "


def subtitle_matches(video_name: str, sub_name: str) -> bool:
    """True if ``sub_name`` is a sidecar subtitle belonging to ``video_name``.

    The subtitle's stem must either equal the video's stem, or extend it after
    one of ``_TAG_SEPARATORS`` (that's how language tags are written). Anchoring
    on the FULL stem is what keeps episodes apart: 'Show.S01E01.mkv' takes
    'Show.S01E01.zh.srt' but not 'Show.S01E02.srt'. (The old rule compared only
    the first 8 characters, which matched an entire season onto every episode.)
    """
    if _ext(sub_name) not in SUBTITLE_EXTS:
        return False
    video_stem = _stem(video_name).lower()
    sub_stem = _stem(sub_name).lower()
    if not video_stem:
        return False
    if sub_stem == video_stem:
        return True
    return (
        sub_stem.startswith(video_stem)
        and sub_stem[len(video_stem)] in _TAG_SEPARATORS
    )


def parent_of(path: str) -> str:
    """The directory holding ``path``."""
    trimmed = path.strip("/")
    return "/" + trimmed.rsplit("/", 1)[0] if "/" in trimmed else "/"


def sibling_subtitles(video_path: str, entries: list[Entry]) -> list[Entry]:
    """The entries in one directory listing that are sidecar subtitles of
    ``video_path``. Pure, so the caller decides how often to hit the network."""
    video = _basename(video_path)
    return [e for e in entries
            if not e.is_dir and subtitle_matches(video, e.display)]


def subtitle_tag(video_name: str, sub_name: str) -> str:
    """The suffix a sidecar adds to the video's stem, which is where the
    language lives: movie.mkv + movie.chs.ass -> "chs"."""
    video_stem = _stem(video_name).lower()
    sub_stem = _stem(sub_name).lower()
    if not video_stem or not sub_stem.startswith(video_stem):
        return ""
    return sub_stem[len(video_stem):].strip(_TAG_SEPARATORS)


def preferred_subtitle(video_path: str, entries: list[Entry],
                       languages=()) -> Entry | None:
    """The ONE sidecar to hand mpv with this file, or None.

    mpv takes exactly one subtitle per playlist entry, so when a release ships
    .chs + .cht + .en something has to choose. ``languages`` is mpv's own
    ``--slang`` order, read back from the player: the user already stated this
    preference in ~/.config/mpv, and honouring it beats both picking
    alphabetically and inventing a second setting to disagree with it.

    Falls back to the first match, so a file with one sidecar always works and
    an empty preference is simply no preference. Pure.
    """
    found = sibling_subtitles(video_path, entries)
    if not found:
        return None
    video = _basename(video_path)
    for language in languages or ():
        wanted = str(language).strip().lower()
        if not wanted:
            continue
        for entry in found:
            if wanted in subtitle_tag(video, entry.display):
                return entry
    return found[0]


def filter_names(names, query: str) -> list[int]:
    """Indexes of the ``names`` that match ``query``, in the order given.

    Case-insensitive SUBSTRING, not prefix. A library where every file is called
    "[SubsPlease] Show - 01 [1080p].mkv" would make prefix matching useless --
    the prefix is the release group, which is never what you are looking for.
    An empty query matches everything, so "filtering by nothing" is the
    unfiltered list rather than a special case at the call site.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        return list(range(len(names)))
    return [i for i, name in enumerate(names) if wanted in str(name or "").lower()]


# Browser sort modes cycled by the UI: (key, reverse, label). Directories
# always sort ahead of files; the key/direction only orders within each group.
SORT_MODES: list[tuple[str, bool, str]] = [
    ("name", False, "name ↑"),
    ("name", True, "name ↓"),
    ("size", False, "size ↑"),
    ("size", True, "size ↓"),
]


def sort_entries(entries: list[Entry], key: str = "name", reverse: bool = False) -> list[Entry]:
    """Order directory entries for display. Dirs first, then files; within each
    group sort by ``key`` ('name' or 'size'). Pure + UI-free so a Rust/Go port
    reproduces the exact same ordering."""
    def sort_key(e: Entry):
        if key == "size":
            return e.size or 0
        return e.display.lower()

    dirs = sorted((e for e in entries if e.is_dir), key=sort_key, reverse=reverse)
    files = sorted((e for e in entries if not e.is_dir), key=sort_key, reverse=reverse)
    return dirs + files


class WebDAVClient:
    def __init__(self, server: Server, password: str):
        from webdav4.client import Client  # lazy import (keep core dep-light)

        self.server = server
        self._password = password
        self._client = Client(
            server.base_url,
            auth=(server.username, password),
            verify=server.verify_ssl,
        )

    # -- listing ----------------------------------------------------------
    def _normalize(self, raw: dict) -> Entry:
        # webdav4 dict keys vary a little across versions; be defensive.
        path = raw.get("name") or raw.get("href") or ""
        kind = raw.get("type") or ("directory" if path.endswith("/") else "file")
        size = raw.get("content_length", raw.get("size"))
        return Entry(path="/" + path.strip("/"), kind=kind,
                     display=_basename(path), size=size)

    def ls(self, path: str = "/") -> list[Entry]:
        raw = self._client.ls(path, detail=True)
        here = "/" + path.strip("/")
        out: list[Entry] = []
        for item in raw:
            entry = self._normalize(item)
            if entry.path == here:          # servers often echo the dir itself
                continue
            out.append(entry)
        out.sort(key=lambda e: (not e.is_dir, e.display.lower()))
        return out

    # -- streaming helpers ------------------------------------------------
    def url_for(self, path: str) -> str:
        base = self.server.base_url.rstrip("/")
        return base + "/" + quote(path.lstrip("/"), safe="/@:+")

    def auth_header(self) -> str:
        raw = f"{self.server.username}:{self._password}".encode()
        return "Authorization: Basic " + base64.b64encode(raw).decode()

    def find_sibling_subtitles(self, path: str, listing: list[Entry] | None = None
                               ) -> list[str]:
        """Full URLs of the subtitle files next to ``path`` that belong to it
        (e.g. movie.mkv -> movie.zh.srt). Matching rule: ``subtitle_matches``.

        Pass ``listing`` to reuse a directory listing you already have: a whole
        season sits in one directory, and resolving a 12-track queue must not
        mean 12 PROPFINDs of the same folder.
        """
        entries = self.ls(parent_of(path)) if listing is None else listing
        return [self.url_for(e.path) for e in sibling_subtitles(path, entries)]
