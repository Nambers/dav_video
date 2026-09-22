"""Non-secret persistence: server profiles, saved playlists, app settings.

Stored as one JSON file under $XDG_CONFIG_HOME/dav_video/config.json.
stdlib-only on purpose -- this module must import cleanly without any third
party dependency, so the core layer can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Server, Playlist, Settings


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "dav_video"


class ConfigStore:
    """Load/save servers, playlists and settings. In-memory + explicit ``save()``."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "config.json")
        self.servers: dict[str, Server] = {}
        self.playlists: dict[str, Playlist] = {}
        self.settings = Settings()
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.servers = {
            s["name"]: Server.from_dict(s) for s in data.get("servers", [])
        }
        self.playlists = {
            p["name"]: Playlist.from_dict(p) for p in data.get("playlists", [])
        }
        self.settings = Settings.from_dict(data.get("settings", {}))

    def save(self) -> None:
        # 0700 on the directory: config.json holds no secrets, but secrets.enc
        # lives here too when the encrypted-file backend is in use.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "servers": [s.to_dict() for s in self.servers.values()],
            "playlists": [p.to_dict() for p in self.playlists.values()],
            "settings": self.settings.to_dict(),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- servers ----------------------------------------------------------
    def upsert_server(self, server: Server) -> None:
        self.servers[server.name] = server
        self.save()

    def remove_server(self, name: str) -> None:
        self.servers.pop(name, None)
        self.save()

    def list_servers(self) -> list[Server]:
        return sorted(self.servers.values(), key=lambda s: s.name.lower())

    # -- playlists --------------------------------------------------------
    def upsert_playlist(self, playlist: Playlist) -> None:
        self.playlists[playlist.name] = playlist
        self.save()

    def remove_playlist(self, name: str) -> None:
        self.playlists.pop(name, None)
        self.save()

    def list_playlists(self) -> list[Playlist]:
        return sorted(self.playlists.values(), key=lambda p: p.name.lower())

    # -- settings ---------------------------------------------------------
    def update_settings(self, **changes) -> Settings:
        """Patch and persist settings. Unknown keys are a programming error, so
        they raise rather than silently vanishing into the JSON."""
        for key, value in changes.items():
            if not hasattr(self.settings, key):
                raise AttributeError(f"unknown setting: {key}")
            setattr(self.settings, key, value)
        self.save()
        return self.settings
