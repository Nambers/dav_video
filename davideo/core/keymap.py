"""Every action this app has, declared once, with its default key.

The footer, the settings screen, the command palette and the bindings
themselves are all built from ``ACTIONS``. User overrides are a sparse
``{action_id: "key"}`` patch in ``settings.keybindings`` holding only what
differs from the defaults; ``""`` means deliberately unbound.

stdlib only, zero UI: key names are Textual's (``events.Key.key`` gives them
that way) but the display helpers are re-derived from ``unicodedata`` rather
than imported from ``textual.keys``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    """One thing the user can do, and the key that does it by default."""

    id: str          # stable: the Textual action name AND the config.json key
    default: str     # default key(s), comma separated, Textual key names
    title: str       # short label (footer + settings list)
    detail: str      # one line: what it does, in plain words
    group: str       # section in the settings list
    # Which panes this key is worth a footer slot in; () = never. Not a bool:
    # the footer is one line and it is the only hint a new user gets, so it has
    # to show what is useful HERE. "Unqueue" is noise on the server list.
    footer: tuple[str, ...] = ()


# Section order in the shortcut list.
# No "Playback" section any more: everything that acted on a RUNNING player
# turned out to be either mpv's own job (track cycling -> "#", "j"/"J") or a
# thing you could not reach without leaving the video. Subtitles are attached
# when playback starts instead. See the split rule below.
GROUPS: tuple[str, ...] = ("Browse", "Queue", "Servers", "App")

# The three things the browser pane can be showing / where focus can be. The
# footer is rebuilt when this changes.
CONTEXTS: tuple[str, ...] = ("servers", "browser", "queue")

# Only actions listing a context get a slot in that context's footer. Keep each
# bar to the keys pressed constantly there; everything else is one '?' away.
# Each bar has to fit an 80-column terminal, which is about 6-7 entries.
#
# ``title`` is what the footer prints next to the key, so write every one as a
# VERB. A noun ("Audio") reads as a label for something on screen rather than
# as what the key does, and the footer is the one place with no room to explain.
ACTIONS: tuple[Action, ...] = (
    # -- Browse -----------------------------------------------------------
    Action("add", "space", "Queue",
           "Queue the highlighted file, or every marked file",
           "Browse", footer=("browser",)),
    Action("media_info", "f", "Info",
           "Streams, languages and length of the highlighted file", "Browse",
           footer=("browser", "queue")),
    # "/" and not plain typing: 17 of the 26 letters already run an action, so
    # type-to-jump would have to take them away from the keyboard-driven design
    # this whole app is. "/" is what every other TUI file manager uses anyway.
    Action("filter", "slash", "Filter",
           "Narrow the browser to names containing what you type", "Browse",
           footer=("browser",)),
    Action("mark_toggle", "v", "Mark",
           "Mark / unmark the highlighted file", "Browse"),
    Action("select_down", "shift+down", "Mark down",
           "Extend the marked block downwards", "Browse"),
    Action("select_up", "shift+up", "Mark up",
           "Extend the marked block upwards", "Browse"),
    Action("clear_sel", "escape", "Clear marks",
           "Drop every mark in the browser", "Browse"),
    Action("sort", "s", "Sort",
           "Cycle sort order (name / size, ascending / descending)", "Browse",
           footer=("browser",)),
    Action("up", "backspace", "Up",
           "Go up one directory", "Browse", footer=("browser",)),
    Action("focus_browser", "left", "Focus browser",
           "Move focus to the file browser", "Browse"),
    Action("focus_queue", "right", "Focus queue",
           "Move focus to the queue", "Browse"),
    # -- Queue ------------------------------------------------------------
    Action("play_queue", "p", "Play",
           "Hand the whole queue to mpv", "Queue", footer=("browser", "queue")),
    Action("remove", "x", "Unqueue",
           "Drop the highlighted track from the queue", "Queue",
           footer=("queue",)),
    Action("queue_up", "left_square_bracket", "Move up",
           "Move the highlighted track one place up", "Queue"),
    Action("queue_down", "right_square_bracket", "Move down",
           "Move the highlighted track one place down", "Queue"),
    Action("probe_tags", "t", "Auto-tag",
           "Detect length / resolution / HDR / codec for queued tracks", "Queue",
           footer=("queue",)),
    Action("edit_tags", "T", "Edit tags",
           "Edit your own tags on the highlighted track", "Queue"),
    Action("open_playlist", "o,ctrl+o", "Open queue",
           "Replace the queue with a saved one", "Queue"),
    Action("merge_playlist", "m", "Merge queue",
           "Append a saved queue to this one", "Queue"),
    Action("rename_queue", "r,ctrl+s", "Rename queue",
           "Rename the queue (it is saved under this name)", "Queue"),
    # -- Servers ----------------------------------------------------------
    Action("servers", "S", "Servers",
           "Close this server and go back to the server list", "Servers",
           footer=("browser", "queue")),
    Action("new_server", "n", "New",
           "Add a WebDAV server", "Servers", footer=("servers",)),
    Action("edit_server", "e", "Edit",
           "Edit the highlighted server", "Servers", footer=("servers",)),
    Action("delete_server", "d", "Delete",
           "Delete the highlighted server and its saved password", "Servers",
           footer=("servers",)),
    # -- App --------------------------------------------------------------
    Action("settings", "question_mark", "Settings",
           "Shortcuts, mpv rendering, about", "App", footer=CONTEXTS),
    Action("render_settings", "g", "Render",
           "mpv render switches (gpu-next / HDR / scaling / …)", "App"),
    Action("about", "i", "About",
           "Version, paths and backends this run is using", "App",
           footer=CONTEXTS),
    Action("quit", "q", "Quit",
           "Leave davideo (mpv is shut down with it)", "App", footer=CONTEXTS),
)

BY_ID: dict[str, Action] = {a.id: a for a in ACTIONS}


def footer_actions(context: str) -> tuple[Action, ...]:
    """The actions whose keys belong in ``context``'s footer, in order."""
    return tuple(a for a in ACTIONS if context in a.footer)

# Structural keys: owned by the widgets, listed for the user, never rebindable.
FIXED_KEYS: tuple[tuple[str, str], ...] = (
    ("↑ / ↓", "Move the highlight in the focused list"),
    ("Enter", "Open a directory · play a file · pick a server · "
              "in the queue, play from that track"),
    ("Tab", "Move between fields and buttons inside a dialog"),
    ("Esc", "Close a dialog"),
    ("Ctrl+P", "Textual's command palette"),
    ("Ctrl+Q", "Force quit"),
)

# Binding onto one of these would shadow list navigation or the emergency exits.
RESERVED_KEYS: frozenset[str] = frozenset({
    "up", "down", "enter", "tab", "shift+tab", "home", "end",
    "pageup", "pagedown", "ctrl+c", "ctrl+q", "ctrl+p",
})


def parse(spec: str) -> list[str]:
    """A comma separated key spec as a list of key names."""
    return [k.strip() for k in (spec or "").split(",") if k.strip()]


def defaults() -> dict[str, str]:
    return {a.id: a.default for a in ACTIONS}


def normalize(mapping) -> dict[str, str]:
    """Overrides worth writing to disk: known actions whose key differs from the
    default. Unknown ids are discarded rather than carried forever."""
    out: dict[str, str] = {}
    for action_id, spec in dict(mapping or {}).items():
        action = BY_ID.get(action_id)
        if action is None:
            continue
        clean = ",".join(parse(str(spec)))
        if clean != action.default:
            out[action_id] = clean
    return out


def resolve(overrides) -> dict[str, str]:
    """Effective key spec per action id. ``""`` means deliberately unbound."""
    keys = defaults()
    keys.update(normalize(overrides))
    return keys


def conflicts(keys) -> dict[str, list[str]]:
    """key -> the action ids sharing it. The settings screen prevents these; a
    hand-edited config.json can still produce one."""
    owners: dict[str, list[str]] = {}
    for action in ACTIONS:                      # declaration order, not dict order
        for key in parse(keys.get(action.id, "")):
            owners.setdefault(key, []).append(action.id)
    return {key: ids for key, ids in owners.items() if len(ids) > 1}


def owner(keys, key: str, exclude: str = "") -> str | None:
    """Which action currently holds ``key`` (ignoring ``exclude``)."""
    for action in ACTIONS:
        if action.id == exclude:
            continue
        if key in parse(keys.get(action.id, "")):
            return action.id
    return None


def validate_key(key: str) -> str | None:
    """Why ``key`` cannot be bound, or None if it can."""
    if not key:
        return "No key."
    if key in RESERVED_KEYS:
        return f"{format_key(key)} is reserved — it is how you move around and get out."
    return None


# -- display ------------------------------------------------------------------
# Textual names a punctuation key after its Unicode character ("?" ->
# "question_mark"); these turn it back into something printable.
_UNICODE_NAMES = {
    "slash": "SOLIDUS",
    "backslash": "REVERSE SOLIDUS",
    "at": "COMMERCIAL AT",
    "minus": "HYPHEN-MINUS",
    "plus": "PLUS SIGN",
    "underscore": "LOW LINE",
    "less_than_sign": "LESS-THAN SIGN",
    "greater_than_sign": "GREATER-THAN SIGN",
    "hyphen_minus": "HYPHEN-MINUS",
}
_DISPLAY = {
    "space": "Space", "escape": "Esc", "backspace": "⌫", "enter": "⏎",
    "delete": "Del", "insert": "Ins", "tab": "Tab",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "pageup": "PgUp", "pagedown": "PgDn", "home": "Home", "end": "End",
}
_MODIFIERS = {"shift": "⇧", "ctrl": "^", "alt": "⌥", "super": "◆", "meta": "◆"}


def key_character(key: str) -> str | None:
    """The printable character a bare key name stands for, if any."""
    if "+" in key:
        return None
    if len(key) == 1:
        return key
    name = _UNICODE_NAMES.get(key, key.replace("_", " ").upper())
    try:
        return unicodedata.lookup(name)
    except KeyError:
        return None


def format_key(key: str) -> str:
    """One key name, as it should be shown to a human."""
    *modifiers, base = key.split("+")
    prefix = "".join(_MODIFIERS.get(m, m + "+") for m in modifiers)
    if base in _DISPLAY:
        return prefix + _DISPLAY[base]
    char = key_character(base)
    if char and char.isprintable() and not char.isspace():
        return prefix + char
    return prefix + base


def format_keys(spec: str) -> str:
    """A whole key spec ('o,ctrl+o'), as shown in the shortcut list."""
    keys = parse(spec)
    return " / ".join(format_key(k) for k in keys) if keys else "—"
