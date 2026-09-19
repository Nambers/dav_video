"""Data models. Plain dataclasses + explicit (de)serialization so the on-disk
format is stable and easy to reproduce in another language later.

Security boundary: NOTHING secret lives here. Passwords are never stored on a
Server object at rest -- they are fetched on demand from the credential store
(keyring / encrypted file) keyed by ``Server.name``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# stdlib-only sibling, so importing it here keeps core import-light
from .probe import media_from_legacy, media_labels


@dataclass
class Server:
    """A saved WebDAV endpoint. ``name`` is the stable id (also the keyring key)."""

    name: str
    base_url: str            # e.g. https://host:5244/dav  (may already include a prefix)
    username: str
    verify_ssl: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Server":
        return cls(
            name=d["name"],
            base_url=d["base_url"],
            username=d.get("username", ""),
            verify_ssl=bool(d.get("verify_ssl", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    """One playable item. Bound to a server by name so a playlist survives
    URL/host changes as long as the server profile keeps its name.

    Two separate label stores, deliberately: ``media`` is probed from the file
    (see core/probe.py) and is replaced wholesale on re-probe, while ``tags`` is
    the user's own and is never touched by probing.

    ``media`` is KEYED ({"duration": "1h30m", "resolution": "4k", ...}), not a
    bag of strings. A field this build knows about is either present -- "" means
    "probed, does not apply" -- or was never attempted, which is how
    ``probe.needs_probe`` spots a track tagged before a field existed without
    any format version to bump.
    """

    server: str              # Server.name
    path: str                # path relative to the server base_url, e.g. /Movies/a.mkv
    title: str = ""          # display label; falls back to basename
    media: dict[str, str] = field(default_factory=dict)  # probed, see MEDIA_FIELDS
    tags: list[str] = field(default_factory=list)        # user's own labels

    def display(self) -> str:
        return self.title or self.path.rstrip("/").rsplit("/", 1)[-1] or self.path

    def labels(self) -> list[str]:
        """Everything to show next to the title, probed first."""
        return media_labels(self.media) + list(self.tags)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        raw = d.get("media") or {}
        # A playlist written before media was keyed holds a plain list. Key it
        # rather than dropping it: the labels stay on screen, and the fields it
        # never had stay absent so the next probe fills them in.
        media = (media_from_legacy(raw) if isinstance(raw, list)
                 else {str(k): str(v) for k, v in dict(raw).items()})
        return cls(
            server=d["server"],
            path=d["path"],
            title=d.get("title", ""),
            media=media,
            tags=[str(x) for x in d.get("tags", [])],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Playlist:
    name: str
    tracks: list[Track] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        return cls(
            name=d["name"],
            tracks=[Track.from_dict(t) for t in d.get("tracks", [])],
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "tracks": [t.to_dict() for t in self.tracks]}


@dataclass
class Settings:
    """App-level preferences. Non-secret, so it rides along in config.json.

    ``render_features`` is a list of core/render.py feature keys -- independent
    toggles, not a preset name. ``last_queue`` is the playlist that was the
    active queue at exit, so the next launch resumes it. ``keybindings`` is a
    sparse patch over core/keymap.py's defaults ({action_id: "key"}), holding
    only what the user actually changed.
    """

    render_features: list[str] = field(default_factory=list)
    mpv_extra_args: list[str] = field(default_factory=list)
    last_queue: str = "untitled"
    keybindings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        features = d.get("render_features")
        if features is None:
            # Migrate a config written before the toggles replaced presets.
            from .render import migrate_preset

            features = migrate_preset(d.get("render_preset", ""))
            if d.get("fullscreen"):
                features = features + ["fullscreen"]
        keys = d.get("keybindings") or {}
        return cls(
            render_features=[str(f) for f in features],
            mpv_extra_args=[str(a) for a in d.get("mpv_extra_args", [])],
            last_queue=d.get("last_queue", "untitled") or "untitled",
            # Validated on use (keymap.resolve), not here: an action that no
            # longer exists must not stop the app from starting.
            keybindings={str(k): str(v) for k, v in dict(keys).items()},
        )

    def to_dict(self) -> dict:
        return asdict(self)
