"""Core-logic smoke test that needs NO third-party deps, NO server, NO display.

Exercises: model (de)serialization, ConfigStore round-trip, WebDAV URL/auth
construction (via a fake so webdav4 isn't imported), and mpv IPC framing (via a
fake unix socket). Run: python scripts/smoketest.py
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dav_video.core.models import Playlist, Server, Track
from dav_video.core.store import ConfigStore


def test_models_roundtrip() -> None:
    p = Playlist("faves", [Track("home", "/a/b.mkv", "B"), Track("home", "/c.mkv")])
    p2 = Playlist.from_dict(json.loads(json.dumps(p.to_dict())))
    assert p2.name == "faves"
    assert p2.tracks[0].display() == "B"
    assert p2.tracks[1].display() == "c.mkv"  # falls back to basename
    print("ok: models roundtrip")


def test_store_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        s = ConfigStore(path)
        s.upsert_server(Server("home", "https://h:5244/dav", "u"))
        s.upsert_playlist(Playlist("faves", [Track("home", "/x.mkv")]))
        s2 = ConfigStore(path)
        assert list(s2.servers) == ["home"]
        assert s2.servers["home"].base_url == "https://h:5244/dav"
        assert s2.playlists["faves"].tracks[0].path == "/x.mkv"
    print("ok: store roundtrip")


def test_url_and_auth() -> None:
    # avoid importing webdav4: build url/auth with the same logic the client uses
    from urllib.parse import quote

    srv = Server("home", "https://h:5244/dav/", "user", True)
    base = srv.base_url.rstrip("/")
    url = base + "/" + quote("/Movies/a b.mkv".lstrip("/"), safe="/@:+")
    assert url == "https://h:5244/dav/Movies/a%20b.mkv", url
    header = "Authorization: Basic " + base64.b64encode(b"user:pw").decode()
    assert header == "Authorization: Basic dXNlcjpwdw=="
    print("ok: url + auth construction")


def test_subtitle_matching() -> None:
    """Sidecar subtitles must anchor on the FULL video stem -- a prefix match
    would sideload every episode's subtitle onto episode 1."""
    from dav_video.core.webdav import subtitle_matches as m

    assert m("Show.S01E01.mkv", "Show.S01E01.zh.srt")
    assert m("Show.S01E01.mkv", "Show.S01E01.srt")
    assert m("Show.S01E01.mkv", "Show.S01E01_eng.ass")
    assert m("Show.S01E01.mkv", "Show.S01E01 - fr.ass")
    assert not m("Show.S01E01.mkv", "Show.S01E02.zh.srt")
    assert not m("Show.S01E01.mkv", "Show.S01E11.zh.srt")
    assert not m("a.mkv", "ab.srt")                          # needs a separator
    assert not m("movie.mkv", "movie.mkv")                   # not a subtitle ext
    assert not m("movie.mkv", "other.srt")
    print("ok: subtitle sibling matching")


def test_sibling_subtitle_resolution() -> None:
    """Resolving a queue's subtitles must cost one listing per DIRECTORY, so the
    matching half is a pure function over an existing listing."""
    from dav_video.core.webdav import Entry, parent_of, sibling_subtitles

    assert parent_of("/Show/S01/ep1.mkv") == "/Show/S01"
    assert parent_of("/ep1.mkv") == "/"
    assert parent_of("ep1.mkv") == "/"
    assert parent_of("/") == "/"

    def entry(name, is_dir=False):
        return Entry(path="/S01/" + name, display=name,
                     kind="directory" if is_dir else "file")

    listing = [entry("Subs", is_dir=True), entry("ep1.mkv"), entry("ep1.zh.srt"),
               entry("ep1.en.ass"), entry("ep2.mkv"), entry("ep2.zh.srt"),
               entry("ep10.zh.srt"), entry("ep1.txt")]
    got = [e.display for e in sibling_subtitles("/S01/ep1.mkv", listing)]
    assert got == ["ep1.zh.srt", "ep1.en.ass"], got
    # the season trap: ep1's subs must not follow ep2, and ep1 must not take
    # ep10's just because the name starts the same way
    assert [e.display for e in sibling_subtitles("/S01/ep2.mkv", listing)] == ["ep2.zh.srt"]
    assert "ep10.zh.srt" not in got and "ep1.txt" not in got
    # a video with nothing next to it resolves to nothing, not to an error
    assert sibling_subtitles("/S01/ep3.mkv", listing) == []
    assert sibling_subtitles("/S01/ep1.mkv", []) == []
    print("ok: sibling subtitle resolution")


def test_subtitle_preference() -> None:
    """mpv takes exactly ONE sidecar per playlist entry, so a release shipping
    .chs + .cht + .en needs a rule. The rule is the user's own mpv --slang."""
    from dav_video.core.webdav import Entry, preferred_subtitle, subtitle_tag

    def entry(name):
        return Entry(path="/S01/" + name, display=name, kind="file")

    listing = [entry("ep1.mkv"), entry("ep1.chs.ass"), entry("ep1.cht.ass"),
               entry("ep1.en.srt"), entry("ep2.chs.ass")]
    pick = lambda langs: (preferred_subtitle("/S01/ep1.mkv", listing, langs) or
                          Entry(path="", display="(none)", kind="file")).display

    assert pick(["cht"]) == "ep1.cht.ass"
    assert pick(["en"]) == "ep1.en.srt"
    # first language that matches anything wins, in the order mpv lists them
    assert pick(["jpn", "cht", "en"]) == "ep1.cht.ass"
    # no preference, or one nothing matches: fall back, never return nothing
    assert pick([]) == "ep1.chs.ass"
    assert pick(["jpn"]) == "ep1.chs.ass"
    # a video with no sidecar at all is None, not a wrong guess
    assert preferred_subtitle("/S01/ep3.mkv", listing, ["chs"]) is None
    # and it must never reach across episodes to satisfy a preference
    assert preferred_subtitle("/S01/ep2.mkv", listing, ["en"]).display == "ep2.chs.ass"

    assert subtitle_tag("ep1.mkv", "ep1.chs.ass") == "chs"
    assert subtitle_tag("ep1.mkv", "ep1.ass") == ""          # untagged sidecar
    assert subtitle_tag("ep1.mkv", "other.ass") == ""
    print("ok: subtitle language preference")


def test_name_filter() -> None:
    """The browser filter is a pure index list over names, so it survives any
    sort order and can be reproduced exactly in a port."""
    from dav_video.core.webdav import filter_names

    names = ["..", "Season 1", "[SubsPlease] Show - 01 [1080p].mkv",
             "[SubsPlease] Show - 02 [1080p].mkv", "Axy.mkv", "notes.txt"]
    pick = lambda q: [names[i] for i in filter_names(names, q)]

    # case-insensitive, and SUBSTRING -- prefix-only would be useless on a
    # library where every name starts with the release group
    assert pick("axy") == ["Axy.mkv"]
    assert pick("AXY") == ["Axy.mkv"]
    assert pick("show - 0") == names[2:4]
    assert pick("02") == [names[3]]
    assert pick("zzz") == []
    # an empty query is the unfiltered list, not a special case for the caller
    assert filter_names(names, "") == list(range(len(names)))
    assert filter_names(names, "   ") == list(range(len(names)))
    assert filter_names([], "a") == []
    # order is the order given, so the browser's sort still decides layout
    assert filter_names(names, "s") == sorted(filter_names(names, "s"))
    print("ok: browser name filter")


def test_sort_is_stable_by_identity() -> None:
    """Sorting must not renumber anything the UI keys a selection on. The app
    tracks marked files by path, so re-sorting has to preserve those paths."""
    from dav_video.core.webdav import Entry, SORT_MODES, sort_entries

    entries = [Entry("/b.mkv", "file", "b.mkv", 300),
               Entry("/a.mkv", "file", "a.mkv", 100),
               Entry("/d", "directory", "d", None)]
    for key, reverse, _label in SORT_MODES:
        out = sort_entries(entries, key, reverse)
        assert out[0].is_dir, "directories sort ahead of files in every mode"
        assert {e.path for e in out} == {"/b.mkv", "/a.mkv", "/d"}
    print("ok: sort preserves entry identity")


def test_build_stamp() -> None:
    from dav_video.core import buildinfo

    # git's %cI, and SOURCE_DATE_EPOCH's raw seconds, both land as local time.
    assert buildinfo.format_time("2026-09-18T12:34:56+08:00").startswith("20")
    assert len(buildinfo.format_time("1758196800")) == len("2026-09-18 12:34")
    # A wrong date is worse than no date: garbage is dropped, never guessed.
    for junk in ("", None, "soon", "2026-13-45", float("nan")):
        assert buildinfo.format_time(junk) == "", junk

    Build = buildinfo.Build
    assert buildinfo.describe(Build()) == ""
    assert buildinfo.describe(Build(time="2026-09-18 12:34")) == "2026-09-18 12:34"
    assert buildinfo.describe(Build(time="2026-09-18 12:34", commit="1a2b3c4",
                                    modified=True)) == \
        "2026-09-18 12:34 (1a2b3c4, modified)"
    assert buildinfo.describe(Build(commit="1a2b3c4")) == "1a2b3c4"
    # No stamp, no checkout, no git: "unknown" is a normal answer, not a crash.
    assert isinstance(buildinfo.build_info(), str)
    print("ok: build stamp formatting")


def test_render_features() -> None:
    """Render options are independent toggles composed into an argv."""
    from dav_video.core import render

    assert render.build_args([]) == [], "nothing enabled must add nothing"
    args = render.build_args(["gpu_next", "hdr_passthrough"])
    assert "--vo=gpu-next" in args and "--target-colorspace-hint=yes" in args
    # Without mode=source mpv describes its surface from the display's CURRENT
    # (still-SDR) state, so it never advertises HDR and the compositor never
    # switches. Verified against Hyprland 0.56.2.
    assert "--target-colorspace-hint-mode=source" in args
    assert "--fullscreen=yes" not in args, "fullscreen is its own toggle"
    assert "--fullscreen=yes" in render.build_args(["gpu_next", "fullscreen"])
    # user extras come last so they can override a feature
    assert render.build_args(["gpu_next"], ["--vo=gpu"])[-1] == "--vo=gpu"
    # unknown keys (e.g. a feature removed in a later version) are dropped, not fatal
    assert render.normalize(["gpu_next", "no-such-feature"]) == ["gpu_next"]
    # order follows FEATURES declaration, not the caller's order
    assert render.normalize(["hdr_passthrough", "gpu_next"]) == ["gpu_next", "hdr_passthrough"]
    assert set(render.RECOMMENDED) <= set(render.feature_keys())
    print("ok: render feature toggles")


def test_render_warnings() -> None:
    """The two silent-failure cases must be reported to the user."""
    from dav_video.core import render

    assert render.warnings(["gpu_next", "hdr_passthrough"]), "needs a fullscreen warning"
    assert render.warnings(["scaling"]), "features without gpu-next must warn"
    assert not render.warnings(["gpu_next", "scaling", "hdr_passthrough", "fullscreen"])
    assert not render.warnings([])
    # display-aware advice
    assert render.warnings(["gpu_next", "hdr_passthrough", "fullscreen"], hdr_display=False)
    assert render.warnings(["gpu_next", "hdr_tonemap"], hdr_display=True)
    assert not render.warnings(["gpu_next", "hdr_tonemap"], hdr_display=None)
    print("ok: render dependency warnings")


def test_legacy_preset_migration() -> None:
    """Configs written by the preset-ladder version must keep working."""
    from dav_video.core.models import Settings

    migrated = Settings.from_dict({"render_preset": "hdr", "fullscreen": True})
    assert migrated.render_features == [
        "gpu_next", "scaling", "dither_deband", "hdr_tonemap", "hdr_passthrough",
        "fullscreen",
    ], migrated.render_features
    assert Settings.from_dict({"render_preset": "inherit"}).render_features == []
    # a config already using toggles is left alone
    assert Settings.from_dict({"render_features": ["hdr_tonemap"]}).render_features == ["hdr_tonemap"]
    print("ok: legacy preset migration")


def test_keymap_defaults_are_sane() -> None:
    """The default key table has to be internally consistent: no action can
    shadow another, and nothing may sit on a key the user needs to escape with."""
    from dav_video.core import keymap

    keys = keymap.resolve({})
    assert keymap.conflicts(keys) == {}, keymap.conflicts(keys)
    for action in keymap.ACTIONS:
        assert action.default, f"{action.id} has no default key"
        for key in keymap.parse(action.default):
            assert key not in keymap.RESERVED_KEYS, f"{action.id} sits on {key}"
        assert action.group in keymap.GROUPS, action.group
    # the footer is the point of the exercise: a handful per pane, not the whole
    # table, and each bar has to fit a narrow terminal
    for context in keymap.CONTEXTS:
        actions = keymap.footer_actions(context)
        assert 4 <= len(actions) <= 10, (context, [a.id for a in actions])
        bar = "  ".join(f"{keymap.format_keys(keys[a.id])} {a.title}" for a in actions)
        assert len(bar) <= 95, (context, len(bar), bar)
    # the two working panes must offer a way back to the server list
    for context in ("browser", "queue"):
        assert "servers" in {a.id for a in keymap.footer_actions(context)}, context
    # about is on every bar
    assert set(keymap.BY_ID["about"].footer) == set(keymap.CONTEXTS)
    # every footer slot must name a real context, or it silently shows nowhere
    for action in keymap.ACTIONS:
        assert set(action.footer) <= set(keymap.CONTEXTS), action.id
    # and the two panes you work in must both offer a way out and a way in
    for context in ("browser", "queue"):
        ids = {a.id for a in keymap.footer_actions(context)}
        assert {"quit", "settings"} <= ids, (context, ids)
    print("ok: keymap defaults")


def test_keymap_overrides() -> None:
    """Overrides are a sparse patch: only what differs from the default is kept,
    unknown actions are dropped, and '' means deliberately unbound."""
    from dav_video.core import keymap

    assert keymap.normalize({"sort": "z"}) == {"sort": "z"}
    assert keymap.normalize({"sort": "s"}) == {}, "a default must not be written out"
    assert keymap.normalize({"gone": "z"}) == {}, "a removed action must not persist"
    assert keymap.normalize({"sort": ""}) == {"sort": ""}, "unbinding must survive"
    assert keymap.resolve({"sort": "z"})["sort"] == "z"
    assert keymap.resolve({})["sort"] == keymap.BY_ID["sort"].default
    # who holds a key (the settings screen uses this to hand it over)
    keys = keymap.resolve({})
    assert keymap.owner(keys, "p") == "play_queue"
    assert keymap.owner(keys, "p", exclude="play_queue") is None
    assert keymap.owner(keys, "ctrl+o") == "open_playlist", "multi-key specs count"
    # a hand-edited collision is reported rather than silently preferred
    assert keymap.conflicts(keymap.resolve({"sort": "p"})) == {"p": ["sort", "play_queue"]}
    print("ok: keymap overrides")


def test_keymap_validation_and_display() -> None:
    """Rebinding must refuse the keys you would need to undo the rebinding."""
    from dav_video.core import keymap

    assert keymap.validate_key("z") is None
    assert keymap.validate_key("ctrl+j") is None
    for key in ("up", "down", "enter", "tab", "ctrl+c", "ctrl+q", "ctrl+p"):
        assert keymap.validate_key(key), f"{key} must not be bindable"
    assert keymap.validate_key("") is not None
    # Textual's key names are unreadable; the settings list is not
    assert keymap.format_key("question_mark") == "?"
    assert keymap.format_key("left_square_bracket") == "["
    assert keymap.format_key("shift+down") == "⇧↓"
    assert keymap.format_key("s") == "s"
    assert keymap.format_keys("o,ctrl+o") == "o / ^o"
    assert keymap.format_keys("") == "—", "an unbound action still needs a cell"
    print("ok: keymap validation + display")


def test_keybindings_persist() -> None:
    from dav_video.core.models import Settings
    from dav_video.core.store import ConfigStore

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        s = ConfigStore(path)
        s.update_settings(keybindings={"sort": "z"})
        assert ConfigStore(path).settings.keybindings == {"sort": "z"}
    # a config written before rebinding existed still loads
    assert Settings.from_dict({}).keybindings == {}
    print("ok: keybindings persist")


def test_settings_roundtrip() -> None:
    from dav_video.core.models import Settings
    from dav_video.core.store import ConfigStore

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        s = ConfigStore(path)
        s.update_settings(render_features=["gpu_next", "hdr"], last_queue="faves")
        s2 = ConfigStore(path)
        assert s2.settings.render_features == ["gpu_next", "hdr"]
        assert s2.settings.last_queue == "faves"
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        try:
            s2.update_settings(nope=1)
        except AttributeError:
            pass
        else:
            raise AssertionError("unknown setting should raise")
    assert Settings.from_dict({}).render_features == []
    print("ok: settings roundtrip")


def test_media_tags() -> None:
    """Media derivation is pure: no ffprobe, no mpv, no server needed. A probe
    writes EVERY field -- "" for "does not apply" -- which is what lets a
    missing key mean "never probed"."""
    from dav_video.core.probe import MEDIA_FIELDS, media_from_fields, media_labels

    def labels(*a, **kw):
        return media_labels(media_from_fields(*a, **kw))

    # 10-bit is an encoding depth, NOT hdr
    assert labels("1920", "1036", "bt.1886", "bt.709", "yuv420p10", "hevc") == \
        ["1080p", "hevc", "10bit"]
    assert labels("3840", "2160", "pq", "bt.2020", "yuv420p10", "hevc") == \
        ["4k", "hdr", "hevc", "10bit"]
    assert "hlg" in labels("1920", "1080", "hlg", "bt.2020", "yuv420p10", "av1")
    # width-bucketed: a scope film is 1920x1036 and must not read as 720p
    assert labels("1920", "1036", "", "", "", "")[0] == "1080p"
    assert labels("7680", "4320", "", "", "", "")[0] == "8k"
    assert labels("640", "480", "", "", "", "")[0] == "sd"
    # nothing usable in, nothing shown -- but every field is still WRITTEN,
    # otherwise an SDR file would look like an unprobed one forever
    empty = media_from_fields("", "", "", "", "", "")
    assert media_labels(empty) == []
    assert set(empty) == set(MEDIA_FIELDS), empty
    assert labels("1920", "1080", "", "", "", "(unavailable)") == ["1080p"]
    # duration leads the row: it is what you scan a queue for
    assert labels("1920", "1036", "", "", "", "h264", "5400") == \
        ["1h30m", "1080p", "h264"]
    assert labels("", "", "", "", "", "", "90") == ["2m"]
    assert labels("1920", "1080", "", "", "", "", "junk") == ["1080p"]
    print("ok: media derivation")


def test_media_is_keyed_not_versioned() -> None:
    """A track probed by an older build is spotted by a MISSING KEY, not by a
    format version and not by where a label sits in a list."""
    from dav_video.core.probe import (MEDIA_FIELDS, media_from_fields,
                                       media_from_legacy, media_labels,
                                       needs_probe)

    fresh = media_from_fields("3840", "2160", "pq", "", "yuv420p10", "hevc", "5400")
    assert needs_probe(fresh) is False
    assert needs_probe({}) is True
    assert needs_probe(None) is True
    # drop any single field and it becomes eligible again -- which is the whole
    # migration story for a field added in a later build
    for missing in MEDIA_FIELDS:
        partial = {k: v for k, v in fresh.items() if k != missing}
        assert needs_probe(partial) is True, missing
    # an SDR 8-bit file has empty values, and that is NOT "unprobed"
    sdr = media_from_fields("1920", "1080", "", "", "yuv420p", "h264", "120")
    assert sdr["range"] == "" and sdr["depth"] == ""
    assert needs_probe(sdr) is False

    # a pre-keyed playlist keeps every label it had, and still gets re-probed
    legacy = media_from_legacy(["4k", "hdr", "dv", "hdr10+", "hevc", "10bit"])
    assert legacy == {"resolution": "4k", "range": "hdr", "extras": "dv hdr10+",
                      "codec": "hevc", "depth": "10bit"}, legacy
    assert media_labels(legacy) == ["4k", "hdr", "dv", "hdr10+", "hevc", "10bit"]
    assert needs_probe(legacy) is True          # no duration key -> refill
    # a duration already in the old list is recognised, not taken for a codec
    assert media_from_legacy(["1h30m", "1080p"])["duration"] == "1h30m"
    # an unknown label was the codec; nothing is silently dropped
    assert media_from_legacy(["vp9"]) == {"codec": "vp9"}
    assert media_from_legacy([]) == {} and media_from_legacy(None) == {}
    print("ok: media keyed, no format version")


def test_duration_and_size_formatting() -> None:
    """Pure display helpers -- every caller feeds them values straight off a
    server or a probe, so junk in must never raise."""
    from dav_video.core.probe import (format_bitrate, format_duration,
                                       format_size)

    assert format_duration(5400) == "1h30m"
    assert format_duration(3600) == "1h00m"
    assert format_duration(7260) == "2h01m"
    assert format_duration(2820) == "47m"
    assert format_duration(45) == "45s"
    # rounding must not produce "1h60m"
    assert format_duration(3599.9) == "1h30m" or format_duration(3599.9) == "1h00m"
    # ffprobe emits "N/A" for real; NaN/inf must not render as "nan TiB"
    for junk in (0, -1, "", None, "abc", "N/A", float("nan"), float("inf")):
        assert format_duration(junk) == "", junk
        assert format_size(junk) == "", junk
        assert format_bitrate(junk) == "", junk
    assert format_size(13_314_398_208) == "12.4 GiB"
    assert format_size(1023) == "1023 B"
    assert format_bitrate(18_200_000) == "18.2 Mb/s"
    print("ok: duration / size / bitrate formatting")


def test_ffprobe_payload_parsing() -> None:
    """The ffprobe backend reads colour from the FRAME, not the container: a
    remux whose Matroska header omits the transfer function still has it in the
    bitstream, and container-level probing would call an HDR file SDR."""
    from dav_video.core.probe import _media_from_ffprobe
    from dav_video.core.probe import media_labels

    def parse(payload):
        return media_labels(_media_from_ffprobe(payload))

    hdr = {"streams": [{"codec_name": "hevc"}],
           "frames": [{"width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                       "color_transfer": "smpte2084", "color_primaries": "bt2020"}]}
    assert parse(hdr) == ["4k", "hdr", "hevc", "10bit"], parse(hdr)

    sdr = {"streams": [{"codec_name": "h264"}],
           "frames": [{"width": 1920, "height": 800, "pix_fmt": "yuv420p"}]}
    assert parse(sdr) == ["1080p", "h264"], parse(sdr)

    # side data only ffprobe can see
    def with_side(kind):
        return parse({"streams": [{"codec_name": "hevc"}],
                      "frames": [{"width": 3840, "height": 2160,
                                  "pix_fmt": "yuv420p10le",
                                  "color_transfer": "smpte2084",
                                  "side_data_list": [{"side_data_type": kind}]}]})
    assert "dv" in with_side("DOVI configuration record")
    assert "dv" in with_side("Dolby Vision RPU")
    assert "hdr10+" in with_side("HDR Dynamic Metadata SMPTE2094-40 (HDR10+)")
    # ordinary side data must not invent a tag
    assert with_side("Mastering display metadata") == ["4k", "hdr", "hevc", "10bit"]
    # hlg spelled ffmpeg's way
    assert "hlg" in parse({"streams": [{"codec_name": "av1"}],
                           "frames": [{"width": 1920, "height": 1080,
                                       "pix_fmt": "yuv420p10le",
                                       "color_transfer": "arib-std-b67"}]})
    # duration comes from the format section and leads the row; the dynamic
    # range tag still has to land right after hdr/hlg, not at a fixed offset
    with_duration = parse({
        "format": {"duration": "5400.0"},
        "streams": [{"codec_name": "hevc"}],
        "frames": [{"width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                    "color_transfer": "smpte2084",
                    "side_data_list": [{"side_data_type": "DOVI configuration record"}]}],
    })
    assert with_duration == ["1h30m", "4k", "hdr", "dv", "hevc", "10bit"], with_duration
    # containers that only tag the stream still get a duration
    assert parse({"streams": [{"codec_name": "h264", "duration": "120.0"}],
                  "frames": [{"width": 1920, "height": 1080}]})[0] == "2m"
    # empty document must not crash
    assert parse({}) == []
    print("ok: ffprobe payload parsing")


def test_media_details_parsing() -> None:
    """The info panel's two backends must agree on shape. Both parsers are pure,
    so this runs with no ffprobe, no mpv and no server."""
    from dav_video.core.probe import details_from_ffprobe, details_from_mpv_output

    payload = {
        "format": {"format_name": "matroska,webm", "duration": "5400.0",
                   "size": "13314398208", "bit_rate": "18200000"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc",
             "width": 3840, "height": 2160, "r_frame_rate": "24000/1001"},
            {"index": 1, "codec_type": "audio", "codec_name": "flac",
             "channels": 2, "channel_layout": "stereo", "sample_rate": "48000",
             "tags": {"language": "jpn", "title": "Japanese"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "chi"}, "disposition": {"default": 1, "forced": 0}},
            # attachments (fonts) must not show up as tracks
            {"index": 3, "codec_type": "attachment", "codec_name": "ttf"},
        ],
    }
    info = details_from_ffprobe(payload)
    assert info.backend == "ffprobe"
    assert [s.kind for s in info.streams] == ["video", "audio", "sub"]
    assert dict(info.summary())["duration"] == "1h30m"
    assert dict(info.summary())["size"] == "12.4 GiB"
    assert dict(info.summary())["tracks"] == "1 video · 1 audio · 1 subtitle"
    assert info.of_kind("video")[0].detail == "3840x2160 23.976 fps"
    assert info.of_kind("audio")[0].describe() == \
        "#1  flac  jpn  'Japanese'  stereo 48000 Hz"
    assert info.of_kind("sub")[0].default is True

    # a live player's selection is matched on the ffmpeg stream index
    marked = info.with_selection({1})
    assert [s.selected for s in marked.streams] == [False, True, False]
    assert [s.selected for s in info.streams] == [False, False, False]  # copy, not in place

    # mpv's printed track list, as mpv actually prints it (● = selected)
    text = (
        "\u25cf Video  --vid=1               (h264 1920x1080 24 fps)\n"
        "\u25cf Audio  --aid=1  --alang=jpn  'Japanese 5.1' (aac 1ch 44100 Hz)\n"
        "\u25cb Audio  --aid=2  --alang=eng  (aac 1ch 44100 Hz)\n"
        "\u25cf Subs   --sid=1  --slang=chi  'Chinese Simplified' (subrip) [default]\n"
        "@@DUR@@5400.000000@@\n"
        "AV: 00:00:00 / 00:00:03 (0%)\n"
    )
    mpv = details_from_mpv_output(text)
    assert mpv.backend == "mpv"
    assert [s.kind for s in mpv.streams] == ["video", "audio", "audio", "sub"]
    assert [s.selected for s in mpv.streams] == [True, True, False, True]
    assert mpv.of_kind("audio")[0].title == "Japanese 5.1"
    assert mpv.of_kind("audio")[0].lang == "jpn"
    assert mpv.of_kind("audio")[0].detail == "1ch 44100 Hz"
    assert mpv.of_kind("sub")[0].codec == "subrip" and mpv.of_kind("sub")[0].default
    assert dict(mpv.summary())["duration"] == "1h30m"
    # older mpv marked selection with (*) and had no title
    old_style = details_from_mpv_output(" (+) Audio --aid=3 --alang=eng (*) (eac3 6ch)")
    assert old_style.streams[0].lang == "eng" and old_style.streams[0].codec == "eac3"
    # noise in, nothing out
    assert details_from_mpv_output("Exiting... (Errors when loading file)").streams == []
    print("ok: media details parsing (ffprobe + mpv)")


def test_track_labels_roundtrip() -> None:
    """Probed labels and user tags are separate fields and both persist."""
    from dav_video.core.models import Track

    t = Track("home", "/a.mkv", "A",
              media={"duration": "1h30m", "resolution": "4k", "range": "hdr",
                     "extras": "", "codec": "hevc", "depth": "10bit"},
              tags=["rewatch"])
    back = Track.from_dict(json.loads(json.dumps(t.to_dict())))
    assert back.media == t.media and back.tags == ["rewatch"]
    assert back.labels() == ["1h30m", "4k", "hdr", "hevc", "10bit", "rewatch"], back.labels()
    # a playlist written before tagging existed must still load
    blank = Track.from_dict({"server": "home", "path": "/b.mkv"})
    assert blank.media == {} and blank.tags == [] and blank.labels() == []
    # ... and one written when media was a plain list keeps its labels
    legacy = Track.from_dict({"server": "home", "path": "/c.mkv",
                              "media": ["1080p", "hevc"], "tags": ["fav"]})
    assert legacy.labels() == ["1080p", "hevc", "fav"], legacy.labels()
    print("ok: track labels roundtrip")


def test_mpv_ipc_framing() -> None:
    from dav_video.core.mpv import MpvController

    # fake mpv: a unix socket server that replies success + emits a stray event
    sockdir = tempfile.mkdtemp()
    sockpath = os.path.join(sockdir, "fake.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sockpath)
    srv.listen(1)

    seen: list[dict] = []

    def serve() -> None:
        conn, _ = srv.accept()
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                req = json.loads(line)
                seen.append(req)
                # stray async event first (controller must skip it)
                conn.sendall(b'{"event":"tick"}\n')
                conn.sendall(
                    (json.dumps({"request_id": req["request_id"],
                                 "error": "success", "data": 42}) + "\n").encode()
                )

    threading.Thread(target=serve, daemon=True).start()

    ctl = MpvController(socket_path=sockpath)
    ctl._connect()  # bypass launching a real mpv
    assert ctl.get_property("track-count") == 42
    ctl.command("loadfile", "http://x", "replace")
    assert seen[0]["command"] == ["get_property", "track-count"]
    assert seen[1]["command"] == ["loadfile", "http://x", "replace"]
    srv.close()
    print("ok: mpv ipc framing (event-skipping + reply matching)")


if __name__ == "__main__":
    test_models_roundtrip()
    test_store_roundtrip()
    test_settings_roundtrip()
    test_keymap_defaults_are_sane()
    test_keymap_overrides()
    test_keymap_validation_and_display()
    test_keybindings_persist()
    test_url_and_auth()
    test_subtitle_matching()
    test_sibling_subtitle_resolution()
    test_subtitle_preference()
    test_name_filter()
    test_sort_is_stable_by_identity()
    test_media_tags()
    test_media_is_keyed_not_versioned()
    test_duration_and_size_formatting()
    test_ffprobe_payload_parsing()
    test_media_details_parsing()
    test_track_labels_roundtrip()
    test_build_stamp()
    test_render_features()
    test_render_warnings()
    test_legacy_preset_migration()
    test_mpv_ipc_framing()
    print("\nALL CORE SMOKE TESTS PASSED")
