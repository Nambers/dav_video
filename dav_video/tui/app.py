"""Main Textual app: browse WebDAV, manage a queue, drive mpv.

Transport (play/pause/seek/volume) is not here -- the mpv window owns it. This
app is the librarian: servers, browsing, the queue, audio/subtitle track
selection, and the render flags mpv launches with.
"""

from __future__ import annotations

import platform
import shutil
import threading
from functools import partial

from textual import events, on, work
from rich.cells import cell_len
from textual.content import Content
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Label, ListItem, ListView, Static
from textual.worker import get_current_worker

from .. import PROJECT_URL, __version__
from ..core import buildinfo, display, keymap, probe, render
from ..core.credentials import CredentialStore
from ..core.mpv import MpvController, MpvError
from ..core.models import Playlist, Server, Track
from ..core.store import ConfigStore
from ..core import webdav
from ..core.webdav import SORT_MODES, Entry, WebDAVClient, sort_entries
from .modals import (AboutScreen, ConfirmScreen, KeymapScreen, MediaInfoScreen,
                     PickScreen, RenderSettingsScreen, ServerFormScreen,
                     SettingsScreen, TextPromptScreen)


def _row_content(label: str, labels: list[str] | None = None) -> Content:
    """One row's text. Content, never markup: a filename like
    '[SubsPlease] Show [ABC123].mkv' would lose its bracketed parts to the
    markup parser."""
    if labels:
        return Content.assemble(label, ("  " + " ".join(labels), "dim"))
    return Content(label)


def _item(label: str, labels: list[str] | None = None, **data) -> ListItem:
    it = ListItem(Label(_row_content(label, labels)))
    it.data = data  # type: ignore[attr-defined]
    return it


def _dep_version(package: str) -> str:
    """Installed version of a dependency. Never raises."""
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return "unknown"


class ActionCommands(Provider):
    """Every action, by name, in Textual's command palette (Ctrl+P).

    Keys are user data, so an action can end up unbound; searching for it by
    name always works.
    """

    def _hit(self, action: keymap.Action):
        return partial(self.app.run_action, action.id)

    async def discover(self) -> Hits:
        for action in keymap.ACTIONS:
            yield DiscoveryHit(action.title, self._hit(action), help=action.detail)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for action in keymap.ACTIONS:
            score = matcher.match(action.title)
            if score > 0:
                yield Hit(score, matcher.highlight(action.title),
                          self._hit(action), help=action.detail)


def _parent_path(path: str) -> str:
    # NOT a method: textual's DOMNode already owns the instance attribute
    # `_parent` (None on App), which would shadow any `_parent` defined here.
    p = "/" + path.strip("/")
    if p == "/":
        return "/"
    return "/" + p.rsplit("/", 1)[0].strip("/")


def _highlight_first(lv: ListView) -> None:
    """A freshly (re)populated ListView has index=None, so Enter/Space see no
    highlighted child. Highlight row 0."""
    if len(lv) > 0:
        lv.index = 0


class WebDavTuiApp(App):
    CSS = """
    #main { height: 1fr; }
    #browserpane { width: 2fr; border: round $primary; }
    #queuepane { width: 1fr; border: round $secondary; }
    .panelabel { height: 1; background: $boost; color: $text; padding: 0 1; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    /* Our own footer, because Textual's is a fixed one-line horizontal scroll
       and these bars run past 100 columns. height:auto lets it wrap. */
    #footerbar { height: auto; background: $foreground 8%; color: $text-muted;
                 padding: 0 1; }
    ListView { height: 1fr; }
    #browser .marked { background: $accent 40%; }
    """

    # Ctrl+P finds any action by name, bound or not.
    COMMANDS = App.COMMANDS | {ActionCommands}

    # Empty on purpose: the key table lives in core/keymap.py and is user
    # editable, so bindings are built per instance in _install_bindings().
    BINDINGS: list[Binding] = []

    def __init__(self, store: ConfigStore, creds: CredentialStore):
        super().__init__()
        self.store = store
        self.creds = creds
        self._keys: dict[str, str] = {}     # action id -> key spec, filled below
        self._footer_pane = "servers"       # first screen; kept in step by _sync_footer
        # Snapshot of what Textual bound first: ctrl+q, ctrl+c, and the
        # command-palette key, which App.__init__ adds to the INSTANCE map and
        # not to the class BINDINGS -- rebuilding from those alone loses Ctrl+P.
        self._base_bindings = BindingsMap.from_keys(
            {key: list(value) for key, value in self._bindings.key_to_bindings.items()}
        )
        self._install_bindings()
        self.mpv = MpvController()
        self.server: Server | None = None
        self.client: WebDAVClient | None = None
        self.cwd = "/"
        self.queue = Playlist(name=store.settings.last_queue)
        self._clients: dict[str, WebDAVClient] = {}
        self._clients_lock = threading.Lock()   # _client_for runs on workers too
        self._entries: list[Entry] = []   # current dir listing (kept for re-sort)
        # Marked files are tracked by PATH, not row index: the browser is
        # rebuilt on every sort change, so an index would drift to other files.
        self._sel: set[str] = set()
        self._sort_mode = 0               # index into SORT_MODES
        # Two separate things: `_filter` is the narrowing that is APPLIED (it
        # survives so you can then mark and queue what it found), `_filtering`
        # is whether keystrokes are currently being eaten to edit it.
        self._browser_filter: str = ""
        self._filter_typing = False
        self._editing_server: str | None = None   # original name while editing
        self._pending_delete: str | None = None
        self._tagging: int | None = None          # queue index being tagged
        # Details probes are a network round-trip each, and re-opening the info
        # panel on the same file is the common case. Session-only: nothing here
        # is worth persisting, and a re-probe on relaunch costs one keypress.
        self._info_cache: dict[tuple[str, str], probe.MediaInfo] = {}

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="browserpane"):
                yield Static("servers", id="pathlabel", classes="panelabel")
                yield ListView(id="browser")
            with Vertical(id="queuepane"):
                yield Static("Queue: untitled (0)", id="queuelabel", classes="panelabel")
                yield ListView(id="queue")
        # Short on purpose: this line also carries every status message, and
        # the full shortcut list is behind the settings key.
        yield Static(self._start_hint(), id="status")
        yield Static(id="footerbar")

    def on_mount(self) -> None:
        self.title = "dav_video"
        # The active queue IS a registered, auto-saved playlist. Resume the one
        # that was active at exit (settings.last_queue); otherwise register it
        # now so it is saved from creation and shows up under Open (o).
        existing = self.store.playlists.get(self.queue.name)
        if existing is not None:
            self.queue = Playlist(name=existing.name, tracks=list(existing.tracks))
        else:
            self._autosave()
        # mpv is not running yet, so this only records the launch flags.
        self.mpv.set_render_args(self._render_args())
        self._sync_subtitle()
        self._render_queue()
        self.call_after_refresh(self._render_footer)   # needs its real width
        self._show_servers()
        self.query_one("#browser", ListView).focus()

    def on_unmount(self) -> None:
        # Bounded: quit() asks mpv to write its resume point, then escalates
        # terminate/kill rather than waiting on a wedged player forever.
        self.mpv.quit(timeout=2.0)

    # -- keybindings ------------------------------------------------------
    def _active_pane(self) -> str:
        """Which of the three panes the footer should be describing.

        NOT _context(): Textual's App._context() is the context manager
        run_async()/run_test() enter before there is a screen stack, so
        shadowing it deadlocks startup -- same trap as _parent and _size.
        """
        focused = self.focused
        if focused is not None and focused.id == "queue":
            return "queue"
        return "browser" if self.client is not None else "servers"

    _FOOTER_GAP = "   "          # between entries

    def _footer_items(self) -> list[tuple[str, str]]:
        """(key, label) for the pane in play. A key the user unbound is not
        advertised -- it is still in `?` and Ctrl+P."""
        items = [
            (keymap.format_keys(self._keys[action.id]), action.title)
            for action in keymap.footer_actions(self._footer_pane)
            if self._keys.get(action.id)
        ]
        items.append(("^p", "palette"))
        return items

    def _render_footer(self) -> None:
        """Draw the footer for the pane in play, wrapping it by hand.

        Ours rather than Textual's ``Footer``: that one is a fixed single line
        that scrolls its overflow out of sight, and these bars run past 100
        columns.

        The wrapping is computed here rather than left to the text engine
        because every wrapper -- Textual's included -- treats the space between
        a key and its label as a break opportunity, and a non-breaking space
        does not help: Python counts U+00A0 as whitespace too. Left alone it
        produces "? Settings  i / About", which is worse than truncating.
        """
        bar = self.query_one("#footerbar", Static)
        width = max(20, (bar.size.width or self.size.width) - 2)   # padding: 0 1
        parts: list = []
        used = 0
        for key, label in self._footer_items():
            entry = cell_len(key) + 1 + cell_len(label)
            if used and used + len(self._FOOTER_GAP) + entry > width:
                parts.append("\n")
                used = 0
            elif used:
                parts.append(self._FOOTER_GAP)
                used += len(self._FOOTER_GAP)
            parts.append((key, "bold"))
            parts.append(" " + label)
            used += entry
        bar.update(Content.assemble(*parts))

    def on_resize(self) -> None:
        # The wrap points depend on the width, so they have to be recomputed.
        self._render_footer()

    def _sync_footer(self) -> None:
        """Redraw the footer if the pane in play changed.

        Guarded rather than unconditional: this runs on every focus change.
        """
        pane = self._active_pane()
        if pane == self._footer_pane:
            return
        self._footer_pane = pane
        self._render_footer()

    def on_descendant_focus(self) -> None:
        self._sync_footer()

    def _install_bindings(self) -> None:
        """(Re)build the key bindings from core/keymap.py plus the user's
        overrides.

        Textual freezes a class's BINDINGS into a map when the node is built, so
        an app whose keys are user data has to own that map itself. The footer
        is drawn by _render_footer and not from these, so nothing here depends
        on which pane is in play.
        """
        self._keys = keymap.resolve(self.store.settings.keybindings)
        bindings: list[Binding] = []
        taken: set[str] = set()
        for action in keymap.ACTIONS:
            spec = self._keys.get(action.id, "")
            if not spec:
                continue     # deliberately unbound -- Ctrl+P still finds it
            template = Binding(spec, action.id, action.title,
                               show=False, id=action.id)
            for binding in Binding.make_bindings([template]):
                # Only a hand-edited config.json can collide here; first
                # declaration wins, so a doubled key stays predictable.
                if binding.key in taken:
                    continue
                taken.add(binding.key)
                bindings.append(binding)
        self._bindings = BindingsMap.merge([self._base_bindings, BindingsMap(bindings)])

    def _start_hint(self) -> Content:
        """First thing on the status line. Content, not markup: a rebound key
        can literally be "[".
        """
        spec = self._keys.get("settings", "")
        where = keymap.format_keys(spec) if spec else "Ctrl+P →"
        return Content(f"↑↓ move · Enter open/play · {where} settings, "
                       f"full shortcut list and rebinding")

    # -- small helpers ----------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _render_args(self) -> list[str]:
        s = self.store.settings
        return render.build_args(s.render_features, s.mpv_extra_args)

    def _sync_subtitle(self) -> None:
        s = self.store.settings
        desc = render.describe(s.render_features, s.mpv_extra_args)
        self.sub_title = f"creds: {self.creds.backend_name} · render: {desc}"

    def _client_for(self, name: str) -> WebDAVClient | None:
        """Build (and cache) a client. Blocking -- keyring round-trip plus the
        first webdav4/httpx import. Call this from a worker thread only."""
        with self._clients_lock:
            cached = self._clients.get(name)
        if cached is not None:
            return cached
        server = self.store.servers.get(name)
        if server is None:
            return None
        password = self.creds.get_password(name)
        if password is None:
            return None
        client = WebDAVClient(server, password)
        with self._clients_lock:
            self._clients[name] = client
        return client

    def _drop_client(self, name: str) -> None:
        with self._clients_lock:
            self._clients.pop(name, None)

    # -- browser rendering ------------------------------------------------
    def _show_servers(self) -> None:
        self.client = None
        self._browser_filter, self._filter_typing = "", False
        self.call_after_refresh(self._sync_footer)
        lv = self.query_one("#browser", ListView)
        lv.clear()
        for s in self.store.list_servers():
            lv.append(_item(f"🖥  {s.name}   ({s.base_url})", kind="server", name=s.name))
        lv.append(_item("➕ add server…", kind="add-server"))
        _highlight_first(lv)
        # No key hints here any more: the footer carries the server keys while
        # this pane is showing, which is the whole point of it being per-pane.
        self.query_one("#pathlabel", Static).update(Content("servers"))

    def _show_files(self, path: str, entries: list[Entry]) -> None:
        self.cwd = path
        self._entries = entries
        self._sel.clear()          # selection is per-listing; drop it on navigate
        self._browser_filter, self._filter_typing = "", False   # so is the filter
        self._render_browser()
        self._sync_footer()

    def _sorted_entries(self) -> list[Entry]:
        key, reverse, _ = SORT_MODES[self._sort_mode]
        return sort_entries(self._entries, key, reverse)

    def _visible_entries(self) -> list[Entry]:
        """Sorted entries minus whatever the filter hides."""
        entries = self._sorted_entries()
        keep = webdav.filter_names([e.display for e in entries], self._browser_filter)
        return [entries[i] for i in keep]

    def _render_browser(self) -> None:
        """(Re)populate the browser from self._entries using the current sort
        and filter. Separate from _show_files so re-sorting and re-filtering
        cost no network round-trip."""
        lv = self.query_one("#browser", ListView)
        lv.clear()
        if not self._browser_filter:
            # A filtered list is "your matches" and nothing else; ".." in the
            # middle of it is noise. Escape restores it along with everything.
            lv.append(_item("📁 ..", kind="up"))
        for e in self._visible_entries():
            icon = "📁" if e.is_dir else "🎬"
            lv.append(_item(f"{icon} {e.display}", kind=e.kind, path=e.path, entry=e))
        _highlight_first(lv)
        # clear()/append() finish mounting after this call returns, so painting
        # the marks now would decorate the old rows.
        self.call_after_refresh(self._refresh_marks)
        self._update_path_label()

    def _update_path_label(self) -> None:
        """The browser's header line: where you are, or what you are filtering.

        Content, not markup: paths and the text being typed are both data, and
        either can contain a bracket."""
        name = self.server.name if self.server else "?"
        label = SORT_MODES[self._sort_mode][2]
        if self._browser_filter or self._filter_typing:
            shown = len(self._visible_entries()) if self._entries else 0
            caret = "_" if self._filter_typing else ""
            note = f"{shown} match" if shown != 1 else "1 match"
            if not shown:
                note = "no match"
            text = f"filter: {self._browser_filter}{caret}  ({note})  ·  Esc clears"
        else:
            text = f"{name}:{self.cwd}  [sort: {label}]  ·  / filter"
        self.query_one("#pathlabel", Static).update(Content(text))

    def _render_queue(self, cursor: int | None = None) -> None:
        """Rebuild the queue rows. ``cursor`` is where to leave the highlight:
        None keeps wherever the user was (rows are rebuilt, so it has to be put
        back explicitly), or pass a row to move it there."""
        lv = self.query_one("#queue", ListView)
        target = lv.index if cursor is None else cursor
        lv.clear()
        for i, t in enumerate(self.queue.tracks, 1):
            lv.append(_item(f"{i:>2}. {t.display()}", labels=t.labels(),
                            kind="track", index=i - 1))
        if target is None:
            _highlight_first(lv)
        else:
            # Has to wait for the new rows to mount -- which is also why
            # callers must not set lv.index themselves afterwards.
            self.call_after_refresh(
                self._set_queue_index, max(0, min(target, len(self.queue.tracks) - 1))
            )
        self.query_one("#queuelabel", Static).update(
            f"Queue: {self.queue.name} ({len(self.queue.tracks)})"
        )

    def _set_queue_index(self, index: int) -> None:
        lv = self.query_one("#queue", ListView)
        if len(lv) > index >= 0:
            lv.index = index

    def _update_queue_row(self, index: int) -> None:
        """Repaint one row's text in place.

        Not _render_queue(): the probe worker finishes a track every few
        seconds, and rebuilding the ListView that often drops the cursor."""
        lv = self.query_one("#queue", ListView)
        if not (0 <= index < len(lv.children) and index < len(self.queue.tracks)):
            return
        track = self.queue.tracks[index]
        try:
            label = lv.children[index].query_one(Label)
        except Exception:  # noqa: BLE001 - row was rebuilt underneath us
            return
        label.update(_row_content(f"{index + 1:>2}. {track.display()}", track.labels()))

    # -- workers (blocking network / IPC / keyring off the UI thread) -----
    @work(thread=True, exclusive=True, group="connect")
    def _connect_worker(self, name: str) -> None:
        try:
            client = self._client_for(name)
        except Exception as exc:  # noqa: BLE001 - credential/store errors too
            self.call_from_thread(self._set_status, f"[red]connect failed:[/] {exc}")
            return
        if client is None:
            self.call_from_thread(
                self._set_status,
                f"[red]no saved password for '{name}'. press e to edit it[/]",
            )
            return
        self.call_from_thread(self._on_connected, name, client)

    def _on_connected(self, name: str, client: WebDAVClient) -> None:
        self.server = self.store.servers[name]
        self.client = client
        self._set_status(f"connected: {name}")
        self._browse("/")

    @work(thread=True, exclusive=True, group="net")
    def _browse(self, path: str) -> None:
        if self.client is None:
            return
        try:
            entries = self.client.ls(path)
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            self.call_from_thread(self._set_status, f"[red]list failed:[/] {exc}")
            return
        self.call_from_thread(self._show_files, path, entries)

    @work(thread=True, group="mpv")
    def _play_entry(self, entry: Entry, client: WebDAVClient) -> None:
        try:
            url = client.url_for(entry.path)
            auth = client.auth_header()
            try:
                subs = client.find_sibling_subtitles(entry.path)
            except Exception:  # noqa: BLE001 - subtitles are best-effort
                subs = []
            loaded = self.mpv.play_url(url, auth, subs)
        except MpvError as exc:
            self.call_from_thread(self._set_status, f"[red]mpv:[/] {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"[red]play failed:[/] {exc}")
            return
        note = f"  (+{loaded} sub)" if loaded else ""
        self.call_from_thread(self._set_status, f"▶ {entry.display}{note}")

    @work(thread=True, group="mpv")
    def _play_queue_worker(self, tracks: list[Track]) -> None:
        items: list[tuple[str, str | None, str | None]] = []
        # One listing per directory, not per track: a season lives in one
        # folder, so a 12-episode queue costs a single PROPFIND.
        listings: dict[tuple[str, str], list[Entry] | None] = {}
        # Which subtitle language the user prefers is already answered in their
        # ~/.config/mpv, so start the player and ask it rather than guessing.
        try:
            self.mpv.ensure_started()
            languages = self.mpv.subtitle_languages()
        except Exception:  # noqa: BLE001 - play_playlist reports a dead mpv
            languages = []
        for t in tracks:
            try:
                client = self._client_for(t.server)
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self._set_status, f"[red]{t.server}:[/] {exc}")
                return
            if client is None:
                continue
            items.append((client.url_for(t.path), client.auth_header(),
                          self._subtitle_for(client, t, listings, languages)))
        if not items:
            self.call_from_thread(self._set_status, "[red]queue empty or no credentials[/]")
            return
        try:
            attached = self.mpv.play_playlist(items)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"[red]mpv:[/] {exc}")
            return
        note = f"  ({attached} with subtitles)" if attached else ""
        self.call_from_thread(
            self._set_status, f"▶ queue: {len(items)} track(s){note}")

    def _subtitle_for(self, client: WebDAVClient, track: Track,
                      listings: dict, languages: list[str]) -> str | None:
        """The sidecar subtitle to hand mpv along with ``track``, or None.

        Best effort by design: a directory that will not list must cost the
        track its subtitles, never its playback.
        """
        key = (track.server, webdav.parent_of(track.path))
        if key not in listings:
            try:
                listings[key] = client.ls(key[1])
            except Exception:  # noqa: BLE001
                listings[key] = None
        entries = listings[key]
        if not entries:
            return None
        chosen = webdav.preferred_subtitle(track.path, entries, languages)
        return client.url_for(chosen.path) if chosen is not None else None

    @work(thread=True, group="mpv")
    def _apply_render_worker(self, args: list[str], note: str) -> None:
        try:
            restarted = self.mpv.set_render_args(args)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"[red]mpv:[/] {exc}")
            return
        # vo/gpu-api are launch-time only, so a live player had to be stopped.
        tail = "  (mpv restarted — press p or Enter to resume)" if restarted else ""
        self.call_from_thread(self._set_status, note + tail)

    @work(thread=True, group="creds")
    def _save_server_worker(self, server: Server, password: str, original: str | None) -> None:
        try:
            if not password and original:
                # Editing with the password field left blank keeps the old one.
                password = self.creds.get_password(original) or ""
            if password:
                self.creds.set_password(server.name, password)
            if original and original != server.name:
                self.creds.delete_password(original)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"[red]credential store:[/] {exc}")
            return
        self.call_from_thread(self._finish_save_server, server, original)

    @work(thread=True, group="creds")
    def _delete_server_worker(self, name: str) -> None:
        try:
            self.creds.delete_password(name)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"[red]credential store:[/] {exc}")
            return
        self.call_from_thread(self._finish_delete_server, name)

    # -- selection --------------------------------------------------------
    @on(ListView.Selected, "#browser")
    def _browser_selected(self, event: ListView.Selected) -> None:
        data = getattr(event.item, "data", {})
        kind = data.get("kind")
        if kind == "server":
            self._connect_worker(data["name"])
        elif kind == "add-server":
            self.action_new_server()
        elif kind == "up":
            self._browse(_parent_path(self.cwd))
        elif kind == "directory":
            self._browse(data["path"])
        elif kind == "file":
            entry: Entry = data["entry"]
            if self.client is None:
                return
            self._play_entry(entry, self.client)

    @on(ListView.Selected, "#queue")
    def _queue_selected(self, event: ListView.Selected) -> None:
        data = getattr(event.item, "data", {})
        if data.get("kind") == "track":
            tracks = self.queue.tracks[data["index"]:]
            self._play_queue_worker(tracks)

    # -- multi-select (browser) ------------------------------------------
    def _row_file(self, idx: int | None) -> Entry | None:
        """The file Entry at row ``idx``, or None for '..', a directory, or an
        out-of-range index."""
        lv = self.query_one("#browser", ListView)
        if idx is None or not (0 <= idx < len(lv.children)):
            return None
        entry = getattr(lv.children[idx], "data", {}).get("entry")
        return entry if entry is not None and not entry.is_dir else None

    def _refresh_marks(self) -> None:
        lv = self.query_one("#browser", ListView)
        for child in lv.children:
            entry = getattr(child, "data", {}).get("entry")
            child.set_class(entry is not None and entry.path in self._sel, "marked")

    def _extend(self, delta: int) -> None:
        """Grow a contiguous file selection while moving the cursor (Shift+↑/↓).
        Marks both the row we leave and the one we land on."""
        lv = self.query_one("#browser", ListView)
        idx = lv.index
        if idx is None:
            return
        entry = self._row_file(idx)
        if entry is not None:
            self._sel.add(entry.path)
        new = max(0, min(idx + delta, len(lv.children) - 1))
        lv.index = new
        entry = self._row_file(new)
        if entry is not None:
            self._sel.add(entry.path)
        self._refresh_marks()

    def action_select_down(self) -> None:
        self._extend(1)

    def action_select_up(self) -> None:
        self._extend(-1)

    def action_mark_toggle(self) -> None:
        entry = self._row_file(self.query_one("#browser", ListView).index)
        if entry is None:
            return
        self._sel.discard(entry.path) if entry.path in self._sel else self._sel.add(entry.path)
        self._refresh_marks()

    def _selected_entries(self) -> list[Entry]:
        """Marked files in display order. Read from the model, not the widget
        rows, so it means the same thing no matter how the view is sorted.

        Unfiltered on purpose: a mark is a mark. Narrowing the view to find a
        file must not silently drop something you marked before narrowing."""
        return [e for e in self._sorted_entries() if not e.is_dir and e.path in self._sel]

    # -- actions ----------------------------------------------------------
    def action_add(self) -> None:
        if self.server is None:
            self._set_status("select a server first")
            return
        # multi-select takes precedence: queue everything marked
        entries = self._selected_entries()
        if entries:
            for e in entries:
                self.queue.tracks.append(
                    Track(server=self.server.name, path=e.path, title=e.display)
                )
            self._sel.clear()
            self._refresh_marks()
            self._autosave()
            self._render_queue()
            self._set_status(f"queued {len(entries)} item(s)")
            self._probe_worker(self.queue.tracks[-len(entries):])
            return
        lv = self.query_one("#browser", ListView)
        data = getattr(lv.highlighted_child, "data", {})
        if data.get("kind") != "file":
            self._set_status("only files can be queued")
            return
        entry: Entry = data["entry"]
        self.queue.tracks.append(
            Track(server=self.server.name, path=entry.path, title=entry.display)
        )
        self._autosave()
        self._render_queue()
        self._set_status(f"queued: {entry.display}")
        self._probe_worker(self.queue.tracks[-1:])

    # -- filter -----------------------------------------------------------
    def action_filter(self) -> None:
        """Start narrowing the browser by name."""
        if self.client is None or not self._entries:
            self._set_status("nothing to filter here")
            return
        self._filter_typing = True
        self._update_path_label()
        self._set_status("type to narrow · ↑↓ move · Enter keep it · Esc clear")

    def on_key(self, event: events.Key) -> None:
        """Feed keystrokes to the filter while it is being typed.

        Must be ``on_key`` and not bindings: 17 of the 26 letters already run an
        action, and a key event runs the whole bubble path BEFORE any binding is
        looked up -- so stopping it here is the only way "s" can mean the letter
        s instead of "sort" while you are typing.
        """
        if not self._filter_typing:
            return
        if self.screen is not self.screen_stack[0]:
            return                      # a dialog is up; its keys are its own
        key = event.key
        if key == "escape":
            self._end_filter(clear=True)
        elif key == "enter":
            # Keep the narrowed list: the point of filtering is usually to mark
            # and queue what it found.
            self._end_filter(clear=False)
        elif key == "backspace":
            self._browser_filter = self._browser_filter[:-1]
            self._render_browser()
        elif key in ("up", "down", "pageup", "pagedown", "home", "end"):
            return                      # let the list scroll; keep typing after
        elif event.is_printable and event.character:
            self._browser_filter += event.character
            self._render_browser()
        else:
            return                      # ctrl+…, function keys: not our business
        event.stop()
        event.prevent_default()

    def _end_filter(self, clear: bool) -> None:
        self._filter_typing = False
        if clear:
            self._browser_filter = ""
        self._render_browser()
        self._set_status("filter cleared" if clear
                         else f"filter kept: {self._browser_filter}" if self._browser_filter
                         else "filter cleared")

    def action_clear_sel(self) -> None:
        """Escape: drop the filter first, then the marks -- one undo per press,
        most recent thing first."""
        if self._browser_filter:
            self._end_filter(clear=True)
            return
        if self._sel:
            self._sel.clear()
            self._refresh_marks()
            self._set_status("selection cleared")

    def action_sort(self) -> None:
        if not self._entries:
            return
        self._sort_mode = (self._sort_mode + 1) % len(SORT_MODES)
        self._render_browser()
        self._set_status(f"sort: {SORT_MODES[self._sort_mode][2]}")

    def _move_queue(self, delta: int) -> None:
        lv = self.query_one("#queue", ListView)
        data = getattr(lv.highlighted_child, "data", {})
        if data.get("kind") != "track":
            return
        i = data["index"]
        j = i + delta
        if not (0 <= j < len(self.queue.tracks)):
            return
        self.queue.tracks[i], self.queue.tracks[j] = (
            self.queue.tracks[j],
            self.queue.tracks[i],
        )
        self._autosave()
        self._render_queue(cursor=j)          # follow the moved track
        self._set_status("reordered queue")

    def action_queue_up(self) -> None:
        self._move_queue(-1)

    def action_queue_down(self) -> None:
        self._move_queue(1)

    def action_remove(self) -> None:
        lv = self.query_one("#queue", ListView)
        data = getattr(lv.highlighted_child, "data", {})
        if data.get("kind") != "track":
            return
        idx = data["index"]
        if 0 <= idx < len(self.queue.tracks):
            removed = self.queue.tracks.pop(idx)
            self._autosave()
            self._render_queue()
            self._set_status(f"removed: {removed.display()}")

    def action_play_queue(self) -> None:
        if not self.queue.tracks:
            self._set_status("queue is empty")
            return
        self._play_queue_worker(list(self.queue.tracks))

    def action_up(self) -> None:
        if self.client is not None:
            self._browse(_parent_path(self.cwd))

    def action_focus_browser(self) -> None:
        self.query_one("#browser", ListView).focus()

    def action_focus_queue(self) -> None:
        self.query_one("#queue", ListView).focus()

    def action_servers(self) -> None:
        self._show_servers()

    # -- tags -------------------------------------------------------------
    @work(thread=True, exclusive=True, group="probe")
    def _probe_worker(self, tracks: list[Track], force: bool = False) -> None:
        """Fill in each track's media labels. One subprocess and one HTTP range
        request per track (~3s over WebDAV), so it saves after every track."""
        worker = get_current_worker()
        todo = [t for t in tracks if force or probe.needs_probe(t.media)]
        if not todo:
            self.call_from_thread(self._set_status, "every queued track is already tagged (T to edit)")
            return
        tagged = failed = 0
        verb = "re-probing" if force else "probing"
        for i, track in enumerate(todo, 1):
            if worker.is_cancelled:
                break
            self.call_from_thread(
                self._set_status, f"{verb} {i}/{len(todo)}: {track.display()}"
            )
            try:
                client = self._client_for(track.server)
                if client is None:
                    failed += 1
                    continue
                track.media = probe.probe(client.url_for(track.path), client.auth_header())
                tagged += 1
            except Exception:  # noqa: BLE001 - a bad file must not stop the batch
                failed += 1
                continue
            try:
                position = self.queue.tracks.index(track)
            except ValueError:      # unqueued while we were probing it
                continue
            self.call_from_thread(self._track_tagged, position)
        note = f"tagged {tagged}/{len(todo)}"
        self.call_from_thread(self._set_status, note + (f", {failed} failed" if failed else ""))

    def _track_tagged(self, index: int) -> None:
        """One track finished probing: save, and repaint just its row."""
        self._autosave()
        self._update_queue_row(index)

    # -- media info -------------------------------------------------------
    def _info_target(self) -> tuple[str, str, str, int | None] | None:
        """(server, path, display, size) for whatever the focused pane is on.

        The queue wins when it has focus, so the panel follows the eye rather
        than always reporting on the browser behind it.
        """
        focused = self.focused
        queue_first = focused is not None and focused.id == "queue"
        panes = ("#queue", "#browser") if queue_first else ("#browser", "#queue")
        for pane in panes:
            data = getattr(self.query_one(pane, ListView).highlighted_child, "data", {})
            if pane == "#queue" and data.get("kind") == "track":
                track = self.queue.tracks[data["index"]]
                return track.server, track.path, track.display(), None
            if pane == "#browser" and data.get("kind") == "file" and self.server:
                entry: Entry = data["entry"]
                return self.server.name, entry.path, entry.display, entry.size
        return None

    def action_media_info(self) -> None:
        """Show what is inside the highlighted file -- length, and every audio
        and subtitle track."""
        target = self._info_target()
        if target is None:
            self._set_status("highlight a file or a queued track to inspect it")
            return
        server, path, title, size = target
        screen = MediaInfoScreen(
            title=title, subtitle=f"{server}:{path}", size=size,
            tags=self._known_tags(server, path),
        )
        self.push_screen(screen)
        self._media_info_worker(screen, server, path)

    def _known_tags(self, server: str, path: str) -> list[str]:
        """The user's own tags on this file, if it is queued. Not ``labels()``:
        the probed half is what the panel is about to show in full."""
        for track in self.queue.tracks:
            if track.server == server and track.path == path:
                return list(track.tags)
        return []

    @work(thread=True, group="info")
    def _media_info_worker(self, screen: MediaInfoScreen, server: str, path: str) -> None:
        """Probe (or recall) the stream list, then ask mpv whether it is the
        file playing. Both calls block, so neither belongs on the UI thread."""
        try:
            client = self._client_for(server)
            if client is None:
                raise RuntimeError(f"no saved password for '{server}'")
            url = client.url_for(path)
        except Exception as exc:  # noqa: BLE001 - credential/store errors too
            self.call_from_thread(screen.set_error, str(exc))
            return
        info = self._info_cache.get((server, path))
        if info is None:
            try:
                info = probe.details(url, client.auth_header())
            except Exception as exc:  # noqa: BLE001 - surface it in the panel
                self.call_from_thread(screen.set_error, str(exc))
                return
        live = self._live_selection(url)
        # Deliberately NOT cached with the probe: a subtitle someone dropped on
        # the server five minutes ago is exactly what this panel is asked about.
        sidecars = self._sidecars_for(client, path)
        self.call_from_thread(self._media_info_ready, screen, server, path, info,
                              live, sidecars)

    def _sidecars_for(self, client: WebDAVClient, path: str):
        """[(name, will_be_attached)] for the subtitle files beside ``path``, or
        None if the directory could not be listed.

        A probe only sees inside the container; on a WebDAV share the subtitles
        are usually a separate file, so a panel that answers "does this have
        Chinese subs" has to look here too.
        """
        try:
            entries = client.ls(webdav.parent_of(path))
        except Exception:  # noqa: BLE001 - unknown, which is not the same as none
            return None
        found = webdav.sibling_subtitles(path, entries)
        if not found:
            return []
        chosen = webdav.preferred_subtitle(path, entries,
                                           self.mpv.subtitle_languages())
        return [(e.display, chosen is not None and e.path == chosen.path)
                for e in found]

    def _live_selection(self, url: str) -> set[int] | None:
        """Stream indexes mpv currently has selected, or None if this file is
        not what is playing.

        Asks mpv what it is playing rather than tracking it here: with a queue
        loaded, mpv walks the playlist on its own and the app is not told.
        """
        if not self.mpv.running:
            return None
        try:
            if (self.mpv.get_property("path") or "") != url:
                return None
            return {
                t["ff-index"] for t in self.mpv.track_list()
                if t.get("selected") and t.get("ff-index") is not None
            }
        except Exception:  # noqa: BLE001 - a wedged player must not break the panel
            return None

    def _media_info_ready(self, screen: MediaInfoScreen, server: str, path: str,
                          info: probe.MediaInfo, live: set[int] | None,
                          sidecars) -> None:
        self._info_cache[(server, path)] = info
        screen.set_info(info.with_selection(live) if live else info,
                        live=live is not None, sidecars=sidecars)

    def _tags_changed(self) -> None:
        self._autosave()
        self._render_queue()

    def action_probe_tags(self) -> None:
        """Detect length / resolution / HDR / codec for queued tracks that lack
        it.

        Tracks tagged by an earlier build count as missing (see
        ``probe.needs_probe``), so pressing it once fills in labels that did not
        exist when the queue was first tagged. With genuinely nothing missing it
        re-probes everything rather than refusing, which is the way to refresh a
        file that changed on the server.
        """
        if not self.queue.tracks:
            self._set_status("queue is empty")
            return
        force = not any(probe.needs_probe(t.media) for t in self.queue.tracks)
        self._probe_worker(list(self.queue.tracks), force=force)

    def action_edit_tags(self) -> None:
        lv = self.query_one("#queue", ListView)
        data = getattr(lv.highlighted_child, "data", {})
        if data.get("kind") != "track":
            self._set_status("highlight a queued track to tag it")
            return
        self._tagging = data["index"]
        track = self.queue.tracks[self._tagging]
        self.push_screen(
            TextPromptScreen(f"Tags for {track.display()} (comma separated):",
                             ", ".join(track.tags)),
            self._on_tags_entered,
        )

    def _on_tags_entered(self, text: str | None) -> None:
        index, self._tagging = self._tagging, None
        if index is None or not (0 <= index < len(self.queue.tracks)):
            return
        # None means cancelled; an empty string means "clear them".
        tags = [] if text is None else [p.strip() for p in text.split(",") if p.strip()]
        if text is None:
            return
        self.queue.tracks[index].tags = tags
        self._tags_changed()
        self._set_status(f"tags: {', '.join(tags) if tags else '(none)'}")

    # -- settings ---------------------------------------------------------
    def action_settings(self) -> None:
        """Everything the footer does not carry, one keypress away."""
        self.push_screen(SettingsScreen(self._keys), self._on_settings_choice)

    def _on_settings_choice(self, choice: str | None) -> None:
        if choice == "keys":
            self.push_screen(KeymapScreen(self._keys), self._on_keymap_saved)
        elif choice == "render":
            self.action_render_settings()
        elif choice == "about":
            self.action_about()

    def _on_keymap_saved(self, keys: dict[str, str] | None) -> None:
        if keys is None:          # cancelled
            return
        overrides = keymap.normalize(keys)
        if overrides == keymap.normalize(self.store.settings.keybindings):
            return
        self.store.update_settings(keybindings=overrides)
        self._install_bindings()
        self._render_footer()     # repaint with the new keys
        changed = len(overrides)
        self._set_status(
            f"shortcuts saved — {changed} changed from default" if changed
            else "shortcuts back to their defaults"
        )

    # -- render settings --------------------------------------------------
    def action_render_settings(self) -> None:
        settings = self.store.settings
        self.push_screen(
            RenderSettingsScreen(
                features=settings.render_features,
                extra_args=settings.mpv_extra_args,
                config_path=str(self.store.path),
                mpv_config_path=render.mpv_config_path(),
                display_hint=display.hdr_summary(),
                hdr_display=display.any_hdr(),
            ),
            self._on_render_settings,
        )

    def _on_render_settings(self, features: list[str] | None) -> None:
        if features is None:      # cancelled
            return
        features = render.normalize(features)
        if features == render.normalize(self.store.settings.render_features):
            return
        self.store.update_settings(render_features=features)
        self._sync_subtitle()
        self._apply_render_worker(
            self._render_args(), f"render: {render.describe(features)}"
        )

    # -- about ------------------------------------------------------------
    def action_about(self) -> None:
        """Version, project URL and the backends/paths this run resolved to --
        what a bug report needs. All cheap lookups, so no worker -- the one
        that shells out (git HEAD, for the build date) is cached after the
        first open."""
        self.push_screen(
            AboutScreen(
                version=__version__,
                blurb="Terminal WebDAV browser that hands media to mpv.",
                rows=[
                    # First: it qualifies the version above it. A release and a
                    # -git build can both say 0.1.0.
                    ("built", buildinfo.build_info() or "unknown"),
                    ("github", PROJECT_URL),
                    ("author", "Eritque Arcus <eritque-arcus[at]ikuyo[.]dev>"),
                    ("python", platform.python_version()),
                    ("textual", _dep_version("textual")),
                    ("mpv", shutil.which("mpv") or "not found on PATH"),
                    ("credentials", self.creds.backend_name),
                    ("config", str(self.store.path)),
                    ("mpv config", render.mpv_config_path()),
                    ("license", "MIT"),
                ],
            )
        )

    # -- servers ----------------------------------------------------------
    def _highlighted_server(self) -> str | None:
        lv = self.query_one("#browser", ListView)
        data = getattr(lv.highlighted_child, "data", {})
        return data.get("name") if data.get("kind") == "server" else None

    def action_new_server(self) -> None:
        self._editing_server = None
        self.push_screen(ServerFormScreen(), self._on_server_form)

    def action_edit_server(self) -> None:
        name = self._highlighted_server()
        if name is None:
            self._set_status("highlight a server in the server list (S) to edit it")
            return
        server = self.store.servers[name]
        self._editing_server = name
        self.push_screen(
            ServerFormScreen(
                name=server.name, base_url=server.base_url, username=server.username,
                verify_ssl=server.verify_ssl, editing=True,
            ),
            self._on_server_form,
        )

    def _on_server_form(self, result: dict | None) -> None:
        original, self._editing_server = self._editing_server, None
        if not result:
            return
        server = Server(
            name=result["name"],
            base_url=result["base_url"],
            username=result["username"],
            verify_ssl=result.get("verify_ssl", True),
        )
        self._save_server_worker(server, result.get("password") or "", original)

    def _finish_save_server(self, server: Server, original: str | None) -> None:
        if original and original != server.name:
            # Tracks reference a server by NAME, so a rename has to carry the
            # playlists with it or saved queues lose their source.
            for playlist in list(self.store.playlists.values()):
                if any(t.server == original for t in playlist.tracks):
                    for t in playlist.tracks:
                        if t.server == original:
                            t.server = server.name
                    self.store.upsert_playlist(playlist)
            for t in self.queue.tracks:
                if t.server == original:
                    t.server = server.name
            self.store.remove_server(original)
            self._drop_client(original)
            if self.server is not None and self.server.name == original:
                self.server = server
        self.store.upsert_server(server)
        self._drop_client(server.name)
        self._set_status(f"saved server: {server.name}")
        self._show_servers()

    def action_delete_server(self) -> None:
        name = self._highlighted_server()
        if name is None:
            self._set_status("highlight a server in the server list (S) to delete it")
            return
        self._pending_delete = name
        self.push_screen(
            ConfirmScreen(
                f"Delete server '{name}' and its saved password?\n"
                "Saved playlists keep their tracks but lose their source.",
            ),
            self._on_delete_confirm,
        )

    def _on_delete_confirm(self, confirmed: bool | None) -> None:
        name, self._pending_delete = self._pending_delete, None
        if not confirmed or name is None:
            return
        self._delete_server_worker(name)

    def _finish_delete_server(self, name: str) -> None:
        self.store.remove_server(name)
        self._drop_client(name)
        if self.server is not None and self.server.name == name:
            self.server, self.client = None, None
        self._set_status(f"deleted server: {name}")
        self._show_servers()

    # -- queue / playlists ------------------------------------------------
    def _autosave(self) -> None:
        """Persist the active queue as a named playlist on every change. The
        queue and its saved playlist are one and the same."""
        self.store.upsert_playlist(
            Playlist(name=self.queue.name, tracks=list(self.queue.tracks))
        )
        if self.store.settings.last_queue != self.queue.name:
            self.store.update_settings(last_queue=self.queue.name)

    def action_rename_queue(self) -> None:
        self.push_screen(
            TextPromptScreen("Name this queue:", self.queue.name), self._on_rename
        )

    def _on_rename(self, name: str | None) -> None:
        if not name or name == self.queue.name:
            return
        old = self.queue.name
        self.queue.name = name
        if old in self.store.playlists:   # move the save off the old name
            self.store.remove_playlist(old)
        self._autosave()
        self._render_queue()
        self._set_status(f"queue name: {name}")

    def action_open_playlist(self) -> None:
        playlists = self.store.list_playlists()
        if not playlists:
            self._set_status("no saved playlists")
            return
        choices = [(f"{p.name}  ({len(p.tracks)})", p.name) for p in playlists]
        self.push_screen(PickScreen("Open (replace) queue:", choices), self._on_open_pick)

    def _on_open_pick(self, name: str | None) -> None:
        if not name:
            return
        playlist = self.store.playlists.get(name)
        if playlist is None:
            return
        self.queue = Playlist(name=playlist.name, tracks=list(playlist.tracks))
        self.store.update_settings(last_queue=self.queue.name)   # resume it next launch
        self._render_queue(cursor=0)          # different queue, start at the top
        self._set_status(f"loaded queue: {name}")

    def action_merge_playlist(self) -> None:
        choices = [
            (f"{p.name}  ({len(p.tracks)})", p.name)
            for p in self.store.list_playlists()
            if p.name != self.queue.name
        ]
        if not choices:
            self._set_status("no other saved queue to merge")
            return
        self.push_screen(
            PickScreen("Merge (append) which queue?", choices), self._on_merge_pick
        )

    def _on_merge_pick(self, name: str | None) -> None:
        if not name:
            return
        playlist = self.store.playlists.get(name)
        if playlist is None:
            return
        added = [Track(server=t.server, path=t.path, title=t.title) for t in playlist.tracks]
        self.queue.tracks.extend(added)
        self._autosave()
        self._render_queue()
        self._set_status(f"merged {len(added)} track(s) from '{name}'")
