"""Modal dialogs: settings, keybindings, media info, add/edit server, prompt,
pick, confirm.

Every dialog answers to Escape (cancel) and Enter (accept), and is drivable
from the arrow keys (see ``_ArrowNav``).
"""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.screen import ModalScreen
from textual.content import Content
from textual.widgets import Button, Checkbox, Input, Label, ListItem, ListView, Static

from ..core import keymap, probe, render


class _ArrowNav:
    """Up/down (and optionally left/right) move focus between a dialog's controls.

    Must be ``on_key`` rather than BINDINGS: key events run the whole bubble
    path before any binding is looked up, so this wins over the scroll bindings
    of whatever scrollable container the control sits in. Screens whose focus
    lives in a ListView must not mix this in -- the list needs those keys.
    """

    ARROWS: tuple[str, ...] = ("up", "down")

    def on_key(self, event: events.Key) -> None:
        if event.key not in self.ARROWS:
            return
        if event.key in ("down", "right"):
            self.focus_next()
        else:
            self.focus_previous()
        event.stop()
        event.prevent_default()


class ServerFormScreen(_ArrowNav, ModalScreen[dict | None]):
    """Collect name / url / username / password / TLS verification.

    Returns a dict or None. Pass the existing values to edit a saved server;
    leaving the password field blank then keeps the stored password.
    """

    CSS = """
    ServerFormScreen { align: center middle; }
    #dialog { width: 70; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #dialog Input { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str = "", base_url: str = "", username: str = "",
                 verify_ssl: bool = True, editing: bool = False):
        super().__init__()
        self._name, self._url, self._user = name, base_url, username
        self._verify, self._editing = verify_ssl, editing

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Edit WebDAV server" if self._editing else "WebDAV server")
            yield Input(value=self._name, placeholder="name (e.g. home-alist)", id="name")
            yield Input(value=self._url, placeholder="base URL (e.g. https://host:5244/dav)", id="url")
            yield Input(value=self._user, placeholder="username", id="user")
            yield Input(
                placeholder="password (blank = keep existing)" if self._editing else "password",
                password=True, id="password",
            )
            yield Checkbox("verify TLS certificate", value=self._verify, id="verify")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        if not name or not url:
            self.query_one("#name" if not name else "#url", Input).focus()
            return
        self.dismiss(
            {
                "name": name,
                "base_url": url,
                "username": self.query_one("#user", Input).value.strip(),
                "password": self.query_one("#password", Input).value,
                "verify_ssl": self.query_one("#verify", Checkbox).value,
            }
        )

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def _enter(self) -> None:
        """Enter anywhere in the form submits it."""
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextPromptScreen(ModalScreen[str | None]):
    """Single-line text prompt. Enter submits, Escape cancels."""

    CSS = """
    TextPromptScreen { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = ""):
        super().__init__()
        self._title, self._value = title, value

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._title)
            yield Input(value=self._value, id="field")

    def on_mount(self) -> None:
        self.query_one("#field", Input).focus()

    @on(Input.Submitted)
    def _submit(self) -> None:
        value = self.query_one("#field", Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickScreen(ModalScreen[str | None]):
    """Pick one value from a list of (label, value) pairs."""

    CSS = """
    PickScreen { align: center middle; }
    #box { width: 72; height: auto; max-height: 80%; padding: 1 2; border: thick $primary; background: $surface; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, choices: list[tuple[str, str]]):
        super().__init__()
        self._title, self._choices = title, choices

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._title)
            lv = ListView()
            yield lv

    def on_mount(self) -> None:
        lv = self.query_one(ListView)
        for label, value in self._choices:
            item = ListItem(Label(label))
            item.value = value  # type: ignore[attr-defined]
            lv.append(item)
        if self._choices:
            lv.index = 0  # highlight first row so Enter selects it
        lv.focus()

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "value", None))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(_ArrowNav, ModalScreen[bool]):
    """Yes/no confirmation. Escape and the Cancel button both mean no."""

    # Two buttons side by side, and no text input to want those keys back.
    ARROWS = ("up", "down", "left", "right")

    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; border: thick $error; background: $surface; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str, confirm_label: str = "Delete"):
        super().__init__()
        self._question, self._confirm_label = question, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._question)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="no")
                yield Button(self._confirm_label, variant="error", id="yes")

    def on_mount(self) -> None:
        # Focus Cancel, not the destructive button: a stray Enter must not delete.
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RenderSettingsScreen(_ArrowNav, ModalScreen[list[str] | None]):
    """Independent render toggles, with the resulting mpv command line shown
    live underneath and both config file paths at the bottom.

    Arrows walk the switches, Space toggles one, Ctrl+S saves.
    """

    CSS = """
    RenderSettingsScreen { align: center middle; }
    #box { width: 84; height: auto; max-height: 92%; padding: 1 2;
           border: thick $primary; background: $surface; }
    #toggles { height: auto; max-height: 20; }
    #box Checkbox { border: none; padding: 0; margin: 0; background: $surface; }
    .detail { color: $text-muted; padding: 0 0 1 4; }
    .hint { color: $success; padding: 0 0 1 0; }
    .warn { color: $warning; padding: 1 0 0 0; }
    .preview { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    .paths { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    .keys { color: $text-muted; padding: 0 0 1 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+r", "recommend", "Recommended"),
    ]

    def __init__(self, features, extra_args: list[str], config_path: str,
                 mpv_config_path: str, display_hint: str = "",
                 hdr_display: bool | None = None):
        super().__init__()
        self._features = render.normalize(features)
        self._extra = list(extra_args)
        self._config_path = config_path
        self._mpv_config_path = mpv_config_path
        self._display_hint = display_hint
        self._hdr_display = hdr_display

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("mpv render settings")
            yield Static("↑↓ move · Space toggle · Ctrl+S save · "
                         "Ctrl+R recommended · Esc cancel", classes="keys")
            if self._display_hint:
                yield Static(self._display_hint, classes="hint")
            with VerticalScroll(id="toggles"):
                for feature in render.FEATURES:
                    yield Checkbox(
                        feature.title,
                        value=feature.key in self._features,
                        id=f"ft-{feature.key}",
                    )
                    yield Static(feature.detail, classes="detail")
            yield Static("", id="warn", classes="warn")
            yield Static("", id="preview", classes="preview")
            yield Static(
                f"settings file:  {self._config_path}\n"
                f"mpv's own config: {self._mpv_config_path}  (always applied first)",
                classes="paths",
            )
            with Horizontal(id="buttons"):
                yield Button("Recommended", id="recommend")
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self._refresh()
        # Otherwise the scroll box is a focus stop of its own that eats up/down.
        self.query_one("#toggles", VerticalScroll).can_focus = False
        first = self.query(Checkbox).first()
        if first is not None:
            first.focus()

    # -- live preview -----------------------------------------------------
    def _selected(self) -> list[str]:
        return [
            f.key for f in render.FEATURES
            if self.query_one(f"#ft-{f.key}", Checkbox).value
        ]

    def _refresh(self) -> None:
        features = self._selected()
        args = render.build_args(features, self._extra)
        if args:
            body = "mpv " + " ".join(args)
        else:
            body = "mpv  (no extra flags — entirely your own mpv config)"
        self.query_one("#preview", Static).update(f"launches as ({len(args)} flags):\n{body}")
        notes = render.warnings(features, self._hdr_display)
        self.query_one("#warn", Static).update("\n".join("! " + n for n in notes))

    @on(Checkbox.Changed)
    def _toggled(self) -> None:
        self._refresh()

    @on(Button.Pressed, "#recommend")
    def _recommend(self) -> None:
        self.action_recommend()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    # -- keyboard ---------------------------------------------------------
    def action_recommend(self) -> None:
        for feature in render.FEATURES:
            self.query_one(f"#ft-{feature.key}", Checkbox).value = (
                feature.key in render.RECOMMENDED
            )
        self._refresh()

    def action_save(self) -> None:
        self.dismiss(self._selected())

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[str | None]):
    """Everything the footer does not carry: the full shortcut list (and the
    screen that rebinds it), the render switches, and the about box."""

    CSS = """
    SettingsScreen { align: center middle; }
    #box { width: 76; max-width: 100%; height: auto; padding: 1 2;
           border: thick $primary; background: $surface; }
    #box ListView { height: auto; max-height: 12; background: $surface; }
    .keys { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    # (result id, title, one-liner, the action whose shortcut opens it directly)
    ENTRIES: tuple[tuple[str, str, str, str], ...] = (
        ("keys", "Keyboard shortcuts", "the full list — rebind anything", ""),
        ("render", "mpv render settings", "gpu-next · HDR · scaling", "render_settings"),
        ("about", "About dav_video", "version · paths · backends", "about"),
    )

    def __init__(self, keys: dict[str, str]):
        super().__init__()
        self._keys = keys

    def compose(self) -> ComposeResult:
        items: list[ListItem] = []
        for entry_id, title, detail, action_id in self.ENTRIES:
            shortcut = keymap.format_keys(self._keys.get(action_id, "")) if action_id else ""
            item = ListItem(Label(Content.assemble(
                title.ljust(22),
                (detail.ljust(36), "dim"),
                (shortcut if shortcut and shortcut != "—" else "", "dim"),
            )))
            item.value = entry_id  # type: ignore[attr-defined]
            items.append(item)
        with Vertical(id="box"):
            yield Label("Settings")
            yield ListView(*items)
            yield Static("↑↓ move · Enter open · Esc close", classes="keys")

    def on_mount(self) -> None:
        lv = self.query_one(ListView)
        lv.index = 0          # a freshly composed ListView highlights nothing
        lv.focus()

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "value", None))

    def action_cancel(self) -> None:
        self.dismiss(None)


class KeyCaptureScreen(ModalScreen[str | None]):
    """Read one keystroke and hand back its name.

    Nothing in it is focusable on purpose: with no focused widget Textual routes
    the key event straight to the screen, so stopping it here keeps any binding
    from acting on a keystroke we only want to read. Escape cancels, so Escape
    is the one key this cannot capture.
    """

    CSS = """
    KeyCaptureScreen { align: center middle; }
    #box { width: 64; max-width: 100%; height: auto; padding: 1 2;
           border: thick $accent; background: $surface; }
    .dim { color: $text-muted; }
    .warn { color: $warning; padding: 1 0 0 0; }
    """

    def __init__(self, title: str, current: str = ""):
        super().__init__()
        self._title, self._current = title, current

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            # Content, not markup: an action title is data.
            yield Label(Content(f"Press the new key for “{self._title}”"))
            yield Static(
                Content(f"currently: {keymap.format_keys(self._current)}"),
                classes="dim",
            )
            yield Static("", id="problem", classes="warn")
            yield Static(
                "Escape cancels — which is also why Escape itself cannot be "
                "assigned from here.",
                classes="dim",
            )

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
            return
        problem = keymap.validate_key(event.key)
        if problem:
            self.query_one("#problem", Static).update(Content(problem))
            return
        self.dismiss(event.key)


class KeymapScreen(ModalScreen[dict[str, str] | None]):
    """Every action, its key, and the place to change it.

    Edits go to a working copy and are handed back only on save, so a
    half-finished rebind cannot strand the app. Rows are repainted in place
    rather than by rebuilding the ListView, which would drop the cursor.
    """

    CSS = """
    KeymapScreen { align: center middle; }
    #box { width: 88; max-width: 100%; height: auto; max-height: 92%; padding: 1 2;
           border: thick $primary; background: $surface; }
    #rows { height: 1fr; min-height: 10; background: $surface; }
    #rows ListItem.header { color: $secondary; text-style: bold; background: $surface; }
    .keys { color: $text-muted; padding: 0 0 1 0; }
    .detail { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    .note { color: $warning; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("d,delete", "unbind", "Unbind"),
        Binding("r", "restore", "Restore default"),
        Binding("ctrl+r", "restore_all", "Reset all"),
    ]

    def __init__(self, keys: dict[str, str]):
        super().__init__()
        self._keys = dict(keys)
        self._rows: list[str | None] = []      # row -> action id (None = header)
        self._row_of: dict[str, int] = {}

    # -- rendering --------------------------------------------------------
    def _row(self, action: keymap.Action) -> Content:
        """One row. Bold key = changed from the default."""
        spec = self._keys.get(action.id, "")
        title = action.title if spec else action.title + "   (unbound)"
        return Content.assemble(
            "  ",
            (keymap.format_keys(spec).ljust(14), "bold" if spec != action.default else ""),
            title,
        )

    def compose(self) -> ComposeResult:
        items: list[ListItem] = []
        for group in keymap.GROUPS:
            actions = [a for a in keymap.ACTIONS if a.group == group]
            if not actions:
                continue
            self._rows.append(None)
            # disabled: ListView's cursor skips disabled rows.
            items.append(ListItem(Label(Content(group)), classes="header", disabled=True))
            for action in actions:
                self._row_of[action.id] = len(self._rows)
                self._rows.append(action.id)
                items.append(ListItem(Label(self._row(action))))
        with Vertical(id="box"):
            yield Label("Keyboard shortcuts")
            yield Static("Enter rebind · d unbind · r default · Ctrl+R reset all · "
                         "Ctrl+S save · Esc discard", classes="keys")
            yield ListView(*items, id="rows")
            yield Static("", id="detail", classes="detail")
            yield Static("", id="note", classes="note")
            with Horizontal(id="buttons"):
                yield Button("Reset all", id="resetall")
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        lv = self.query_one("#rows", ListView)
        # Assigning .index does not skip disabled rows (only cursor movement
        # does), so aim past the first header explicitly.
        first = next((i for i, row in enumerate(self._rows) if row is not None), 0)
        lv.index = first
        lv.focus()
        self._show_detail(first)

    def _action_at(self, index: int | None) -> keymap.Action | None:
        if index is None or not (0 <= index < len(self._rows)):
            return None
        action_id = self._rows[index]
        return keymap.BY_ID.get(action_id) if action_id else None

    def _current(self) -> keymap.Action | None:
        return self._action_at(self.query_one("#rows", ListView).index)

    def _show_detail(self, index: int | None) -> None:
        action = self._action_at(index)
        self.query_one("#detail", Static).update(Content(action.detail if action else ""))

    def _note(self, text: str) -> None:
        self.query_one("#note", Static).update(Content(text))

    def _refresh_row(self, action_id: str) -> None:
        index = self._row_of.get(action_id)
        lv = self.query_one("#rows", ListView)
        if index is None or not (0 <= index < len(lv.children)):
            return
        try:
            label = lv.children[index].query_one(Label)
        except Exception:  # noqa: BLE001 - row went away underneath us
            return
        label.update(self._row(keymap.BY_ID[action_id]))

    @on(ListView.Highlighted, "#rows")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        self._show_detail(event.list_view.index)

    # -- editing ----------------------------------------------------------
    @on(ListView.Selected, "#rows")
    def _selected(self) -> None:
        self.action_rebind()

    def action_rebind(self) -> None:
        action = self._current()
        if action is None:
            return
        self.app.push_screen(
            KeyCaptureScreen(action.title, self._keys.get(action.id, "")),
            lambda key, action_id=action.id: self._assign(action_id, key),
        )

    def _take(self, spec: str, winner: str) -> str:
        """Give every key in ``spec`` to ``winner``, unbinding whoever held one.

        Taking a key rather than refusing it is what makes swapping two keys
        possible; the returned note names whoever lost one.
        """
        note = ""
        for key in keymap.parse(spec):
            loser = keymap.owner(self._keys, key, exclude=winner)
            if loser:
                self._keys[loser] = ""
                self._refresh_row(loser)
                note += f"   (taken from “{keymap.BY_ID[loser].title}”, now unbound)"
        self._keys[winner] = spec
        self._refresh_row(winner)
        return note

    def _assign(self, action_id: str, key: str | None) -> None:
        if not key:               # capture cancelled
            return
        note = self._take(key, action_id)
        self._note(f"{keymap.format_key(key)} → {keymap.BY_ID[action_id].title}{note}")

    def action_unbind(self) -> None:
        action = self._current()
        if action is None:
            return
        self._keys[action.id] = ""
        self._refresh_row(action.id)
        self._note(f"“{action.title}” unbound — still here, and still in Ctrl+P")

    def action_restore(self) -> None:
        action = self._current()
        if action is None:
            return
        note = self._take(action.default, action.id)
        self._note(f"“{action.title}” back to "
                   f"{keymap.format_keys(action.default)}{note}")

    def action_restore_all(self) -> None:
        self._keys = keymap.defaults()
        for action_id in self._row_of:
            self._refresh_row(action_id)
        self._note("every shortcut back to its default")

    def action_save(self) -> None:
        self.dismiss(dict(self._keys))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#resetall")
    def _resetall(self) -> None:
        self.action_restore_all()


class AboutScreen(ModalScreen[None]):
    """Version, project URL and the paths/backends this run is using.

    Read-only, so every key that plausibly means "done" closes it -- ``q``
    included, which would otherwise fall through to the app's quit binding.
    """

    CSS = """
    AboutScreen { align: center middle; }
    #box { width: 78; height: auto; max-height: 90%; padding: 1 2;
           border: thick $primary; background: $surface; }
    .name { text-style: bold; }
    .blurb { color: $text-muted; padding: 0 0 1 0; }
    #rows { height: auto; max-height: 16; }
    .close { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("enter", "close", "Close"),
        ("q", "close", "Close"),
        ("i", "close", "Close"),
    ]

    def __init__(self, version: str, blurb: str, rows: list[tuple[str, str]]):
        super().__init__()
        self._version = version
        self._blurb = blurb
        self._rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            # Content, not markup: these are paths and versions from the
            # environment, and a stray bracket would parse as a style tag.
            yield Label(Content(f"dav_video {self._version}"), classes="name")
            yield Label(Content(self._blurb), classes="blurb")
            width = max((len(key) for key, _ in self._rows), default=0)
            with VerticalScroll(id="rows"):
                for key, value in self._rows:
                    yield Label(
                        Content.assemble((key.ljust(width) + "   ", "dim"), value)
                    )
            yield Static("↑↓ scroll · Escape / Enter / q to close", classes="close")

    def on_mount(self) -> None:
        # Focus the scroll box so the arrow keys page a long list.
        self.query_one("#rows", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class MediaInfoScreen(ModalScreen[None]):
    """What is actually inside a file: how long it runs, and every audio and
    subtitle track with its language -- the answer to "are the Chinese subs
    even in there" without starting playback to find out.

    Opened BEFORE the probe finishes. A details probe is a network round-trip
    (~3s over WebDAV), and a dialog that appears three seconds after the
    keypress reads as a dropped key, so the panel shows what the app already
    knows and fills the rest in through ``set_info``/``set_error``.
    """

    CSS = """
    MediaInfoScreen { align: center middle; }
    #box { width: 84; height: auto; max-height: 90%; padding: 1 2;
           border: thick $primary; background: $surface; }
    .name { text-style: bold; }
    .blurb { color: $text-muted; padding: 0 0 1 0; }
    #body { height: auto; max-height: 24; }
    .close { color: $text-muted; padding: 1 0 0 0; border-top: solid $panel; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("enter", "close", "Close"),
        ("q", "close", "Close"),
        ("f", "close", "Close"),
    ]

    # Width of the label column in the header rows.
    _LABEL = 10

    def __init__(self, title: str, subtitle: str, size=None,
                 tags: list[str] | None = None):
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._file_size = size
        self._tags = tags or []
        # The body text is held here rather than only in the widget: the probe
        # worker can answer before compose() has mounted anything (and after
        # the screen is dismissed), so _apply has to work with no widget there.
        self._content = self._waiting()

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            # Content, not markup, everywhere in here: these are filenames,
            # track titles and codec names straight off a server, and '[' in
            # any of them would be eaten as a style tag.
            yield Label(Content(self._title), classes="name")
            yield Label(Content(self._subtitle), classes="blurb")
            with VerticalScroll(id="body"):
                yield Static(self._content, id="info")
            yield Static("↑↓ scroll · Escape / Enter / f to close", classes="close")

    def on_mount(self) -> None:
        self._apply(self._content)     # in case the probe beat the mount
        self.query_one("#body", VerticalScroll).focus()

    def _apply(self, content: Content) -> None:
        """Show ``content``, now or whenever this screen gets mounted. A no-op
        once the screen is gone, which is what a slow probe deserves."""
        self._content = content
        for widget in self.query("#info").results(Static):
            widget.update(content)

    # -- content ----------------------------------------------------------
    def _rows(self, rows: list[tuple[str, str]]) -> list:
        """Label/value pairs as dim-label Content spans."""
        out = []
        for key, value in rows:
            out.append((key.ljust(self._LABEL), "dim"))
            out.append(value + "\n")
        return out

    def _known(self) -> list[tuple[str, str]]:
        """What is on hand before the probe answers."""
        rows = []
        readable = probe.format_size(self._file_size)
        if readable:
            rows.append(("size", readable))
        if self._tags:
            rows.append(("your tags", " ".join(self._tags)))
        return rows

    def _waiting(self) -> Content:
        return Content.assemble(
            *self._rows(self._known()),
            ("\nreading streams…", "dim"),
        )

    def set_error(self, message: str) -> None:
        self._apply(
            Content.assemble(*self._rows(self._known()), ("\n" + message, "red"))
        )

    def set_info(self, info, live: bool = False, sidecars=None) -> None:
        """Fill in the probed detail.

        ``live`` marks the selection as coming from the running player rather
        than from mpv's default pick. ``sidecars`` is [(name, attached)] for the
        subtitle files sitting NEXT TO this one on the server -- None when they
        could not be looked up, [] when there are none. They belong here because
        a probe only ever sees inside the container, and on a WebDAV share the
        subtitles are usually not in there.
        """
        rows = info.summary()
        # ffprobe reports the size itself; the listing's byte count is only a
        # stand-in for the backends and rows that have none.
        readable = probe.format_size(self._file_size)
        if readable and not any(key == "size" for key, _ in rows):
            rows.insert(1 if rows else 0, ("size", readable))
        if self._tags:
            rows.append(("your tags", " ".join(self._tags)))
        rows.append(("read by", info.backend))

        spans = self._rows(rows)
        selected = False
        for kind in ("video", "audio", "sub"):
            streams = info.of_kind(kind)
            if not streams:
                continue
            spans.append(("\n" + streams[0].kind_name + "\n", "bold"))
            for stream in streams:
                selected = selected or stream.selected
                spans.append(("▶ " if stream.selected else "  ",
                              "$success" if stream.selected else ""))
                spans.append(stream.describe() + "\n")
        if selected:
            spans.append(("\n▶ = playing now" if live
                          else "\n▶ = what mpv would select", "dim"))
        if sidecars is not None:
            spans.append(("\n\nSidecar subs (next to it on the server)\n", "bold"))
            if sidecars:
                for name, attached in sidecars:
                    spans.append(("▶ " if attached else "  ",
                                  "$success" if attached else ""))
                    spans.append(name + "\n")
                spans.append(("▶ = attached when you play this", "dim"))
            else:
                spans.append(("  none\n", "dim"))
        self._apply(Content.assemble(*spans))

    def action_close(self) -> None:
        self.dismiss(None)
