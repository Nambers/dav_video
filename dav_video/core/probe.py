"""Read a video's technical properties so the queue can label them.

A filename says nothing about HDR -- 10-bit is an encoding depth, not a
transfer function -- and getting it wrong silently changes what the renderer
and the compositor do.

Two backends, ffprobe preferred:

  ffprobe -- structured JSON, and with ``-read_intervals %+#1`` it decodes the
            first frame, so colour metadata comes from the BITSTREAM rather
            than the container (a remux whose Matroska header omits the
            transfer function still reports smpte2084). Also the only backend
            that sees mastering-display, MaxCLL/MaxFALL, Dolby Vision and
            HDR10+ side data.
  mpv     -- fallback: mpv is a hard requirement of this app, while the ffprobe
            binary only ships alongside it on distros where mpv depends on the
            ffmpeg package (Arch), not on the libav* shared libraries alone
            (Debian, Fedora).

Two entry points, same two backends:

  probe(url)   -- short display tags for a queue row (duration, 4k, hdr, ...)
  details(url) -- the full stream list behind the info panel (which audio
                  tracks, which subtitles, what language each one is)

Either way it is one HTTP range request (~2-3s over WebDAV), so callers must
treat this as a network operation.

stdlib only, zero UI.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field

# Delimited so the values can be picked out of mpv's ordinary terminal chatter.
# NOTE: --really-quiet and --msg-level=all=no both suppress term-playing-msg,
# so this must run with mpv's default verbosity and be grepped out.
_SENTINEL = "@@WDT@@"
_FORMAT = (
    _SENTINEL
    + "|${=width}|${=height}|${video-params/gamma}|${video-params/primaries}"
    + "|${video-params/pixelformat}|${video-format}|${=duration}|"
    + _SENTINEL
)
_FIELDS = 7      # values between the sentinels; keep in step with _FORMAT

# Bucketed on WIDTH, not height: films are letterboxed to odd heights (a 1080p
# scope release is 1920x1036) and height buckets would mislabel every one.
_RESOLUTIONS = (
    (7000, "8k"),
    (3000, "4k"),
    (2300, "1440p"),
    (1600, "1080p"),
    (1100, "720p"),
)

# Transfer functions that mean actual HDR, as mpv names them.
_HDR_GAMMA = {"pq": "hdr", "hlg": "hlg"}


# Frame side-data types worth a tag. Matched case-insensitively as substrings
# because ffmpeg has renamed these strings across releases.
_SIDE_DATA_TAGS = (
    ("dovi", "dv"),
    ("dolby vision", "dv"),
    ("2094", "hdr10+"),
)


class ProbeError(Exception):
    pass


def _fallback_error(primary: "ProbeError | None", message: str) -> ProbeError:
    """The error to raise once both backends have failed.

    mpv's complaint is always the generic "no tracks here", which reads as "this
    file is broken" when the truth was a 401 or a 404 that ffprobe already
    diagnosed precisely. This text ends up in front of the user in the info
    panel, so lead with whichever backend actually knew something.
    """
    if primary is None:
        return ProbeError(message)
    return ProbeError(f"{primary} (mpv fallback: {message})")


# -- display formatting (pure; shared by the queue tags and the info panel) ---
def _positive(value) -> float | None:
    """``value`` as a usable positive number, or None.

    Every caller feeds these formatters a string off a server or a probe, so
    "", None, "N/A", NaN and inf all have to come back as "unknown" rather than
    raise or render as "nan TiB". ffprobe really does emit "N/A".
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return number if number > 0 else None


# -- probed media, keyed ------------------------------------------------------
# ``Track.media`` is a {field: value} map, not a bag of strings, so "this track
# was probed before <field> existed" is just a missing key -- no format version
# anywhere. A successful probe writes EVERY field, using "" for "probed, does
# not apply" (an SDR file has range=""); that is what makes an absent key mean
# "never attempted". Declaration order is also the order labels are shown in.
MEDIA_FIELDS: tuple[str, ...] = (
    "duration",     # 1h30m        -- what you scan a queue for, so it leads
    "resolution",   # 4k / 1080p / sd
    "range",        # hdr / hlg    -- "" is the SDR signal
    "extras",       # dv, hdr10+   -- space separated, a file can have both
    "codec",        # hevc
    "depth",        # 10bit / 12bit
)

# What format_duration emits: "1h30m" / "47m" / "45s".
_DURATION_TAG = re.compile(r"^\d+(?:h\d{2}m|m|s)$")


def is_duration_tag(tag: str) -> bool:
    return bool(_DURATION_TAG.match(tag or ""))


# Closed vocabularies, so a pre-dict ``media`` list can be keyed exactly.
_LEGACY_FIELD = {
    **{v: "resolution" for v in ("8k", "4k", "1440p", "1080p", "720p", "sd")},
    **{v: "range" for v in ("hdr", "hlg")},
    **{v: "extras" for v in ("dv", "hdr10+")},
    **{v: "depth" for v in ("10bit", "12bit")},
}


def media_from_legacy(labels) -> dict[str, str]:
    """Key a ``media`` list written before this was a map.

    Nothing is dropped: every vocabulary word goes to its field and anything
    else is the codec, which is what an unrecognised label was. Missing fields
    stay missing on purpose -- that is what gets the track re-probed.
    """
    out: dict[str, str] = {}
    for raw in labels or []:
        label = str(raw)
        if is_duration_tag(label):
            field = "duration"
        else:
            field = _LEGACY_FIELD.get(label.lower(), "codec")
        out[field] = f"{out[field]} {label}".strip() if out.get(field) else label
    return out


def as_media(media) -> dict[str, str]:
    """``media`` as a keyed map, whatever shape it arrived in.

    The one place that knows about the pre-keyed list format. Both readers go
    through it, so neither can be handed a shape it chokes on: these feed a
    queue row, and an AttributeError there takes the whole app down over a
    label.
    """
    if isinstance(media, dict):
        return media
    if isinstance(media, (list, tuple)):
        return media_from_legacy(media)
    return {}


def media_labels(media) -> list[str]:
    """The probed labels of one track, in reading order."""
    keyed = as_media(media)
    out: list[str] = []
    for field in MEDIA_FIELDS:
        out.extend(str(keyed.get(field) or "").split())
    return out


def needs_probe(media) -> bool:
    """True when this track has never been probed, or was probed by a build
    that did not yet produce every field this one does.

    Adding a field to MEDIA_FIELDS is therefore the whole migration: every
    track missing that key becomes eligible again on the next probe.
    """
    keyed = as_media(media)
    return any(field not in keyed for field in MEDIA_FIELDS)


def format_duration(seconds) -> str:
    """Seconds -> "1h32m" / "47m" / "8s". Empty for junk or zero.

    Rounded to the minute above an hour, which is the precision anyone
    actually reads off a queue row.
    """
    total = _positive(seconds)
    if total is None:
        return ""
    if total < 60:
        return f"{int(total) or 1}s"
    minutes = int(round(total / 60))
    if minutes < 60:
        return f"{minutes}m"
    # round() can carry 59.6 minutes into a full hour -- divmod after rounding
    # keeps that from printing "1h60m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def format_size(size) -> str:
    """Byte count -> "12.4 GiB". Empty for unknown."""
    value = _positive(size)
    if value is None:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def format_bitrate(bits_per_second) -> str:
    """Bits/s -> "18.2 Mb/s". Empty for unknown."""
    value = _positive(bits_per_second)
    if value is None:
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} Mb/s"
    return f"{value / 1000:.0f} kb/s"


def _resolution_tag(width: int) -> str:
    for threshold, name in _RESOLUTIONS:
        if width >= threshold:
            return name
    return "sd"


def media_from_fields(width: str, height: str, gamma: str, primaries: str,
                      pixelformat: str, video_format: str,
                      duration: str = "") -> dict[str, str]:
    """Turn raw mpv property values into a keyed media map. Pure -- unit
    testable without mpv or a server.

    Parameter order follows mpv's property list, so the sentinel line maps
    onto it positionally. EVERY field is written, "" included: a field that is
    present-but-empty means "probed, does not apply", which is what keeps
    ``needs_probe`` from confusing an SDR file with an unprobed one.
    """
    try:
        w = int(width)
    except (TypeError, ValueError):
        w = 0
    fmt = (pixelformat or "").lower()
    depth = "12bit" if "12" in fmt else ("10bit" if "10" in fmt else "")
    return {
        "duration": format_duration(duration),
        "resolution": _resolution_tag(w) if w else "",
        # Only the notable case gets a word. Labelling every ordinary file
        # "sdr" would be noise in a narrow queue pane; empty *is* the SDR
        # signal, and it is still a probed, present field.
        "range": _HDR_GAMMA.get((gamma or "").lower(), ""),
        "extras": "",
        "codec": (video_format or "").lower()
                 if video_format and video_format != "(unavailable)" else "",
        "depth": depth,
    }


# ffmpeg spells the transfer function differently from mpv; both map to "hdr".
_FFMPEG_HDR_TRC = {"smpte2084": "hdr", "arib-std-b67": "hlg"}


def _media_from_ffprobe(payload: dict) -> dict[str, str]:
    """Turn one ffprobe JSON document into a keyed media map. Pure."""
    frames = payload.get("frames") or [{}]
    frame = frames[0] if frames else {}
    streams = payload.get("streams") or [{}]
    stream = streams[0] if streams else {}
    gamma = _FFMPEG_HDR_TRC.get((frame.get("color_transfer") or "").lower(), "")
    duration = (payload.get("format") or {}).get("duration")
    if duration is None:
        duration = stream.get("duration")       # some containers only tag the stream
    media = media_from_fields(
        str(frame.get("width") or ""),
        str(frame.get("height") or ""),
        # media_from_fields speaks mpv's names, so translate on the way in
        {"hdr": "pq", "hlg": "hlg"}.get(gamma, ""),
        frame.get("color_primaries") or "",
        frame.get("pix_fmt") or "",
        stream.get("codec_name") or "",
        duration or "",
    )
    # Side data lands in its own field, so there is no insert position to get
    # right -- which is the whole reason this is a map and not a list.
    extras: list[str] = []
    for entry in frame.get("side_data_list") or []:
        kind = (entry.get("side_data_type") or "").lower()
        for needle, tag in _SIDE_DATA_TAGS:
            if needle in kind and tag not in extras:
                extras.append(tag)
    media["extras"] = " ".join(extras)
    return media


def _probe_ffprobe(url: str, auth_header: str | None, timeout: float) -> dict[str, str]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-read_intervals", "%+#1",          # decode just the first frame
        "-show_entries",
        # format=duration rides along for free: -select_streams only narrows
        # the stream/frame sections, the format section is whole-file.
        "format=duration:stream=codec_name,duration"
        ":frame=width,height,pix_fmt,color_transfer,color_primaries"
        ":frame_side_data=side_data_type",
        "-of", "json",
    ]
    if auth_header:
        cmd += ["-headers", auth_header + "\r\n"]
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout:g}s") from exc
    if done.returncode != 0:
        raise ProbeError(f"ffprobe failed: {done.stderr.strip()[:200]}")
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        raise ProbeError("ffprobe returned no usable JSON") from exc
    media = _media_from_ffprobe(payload)
    if not media_labels(media):
        raise ProbeError("no video properties reported (not a video?)")
    return media


def probe(url: str, auth_header: str | None = None,
          timeout: float = 30.0) -> dict[str, str]:
    """Keyed media facts for ``url`` (see MEDIA_FIELDS). Raises ProbeError;
    never blocks forever.

    Prefers ffprobe and falls back to mpv, so the richer backend is used where
    it exists without making it a new hard dependency.
    """
    first: ProbeError | None = None
    if shutil.which("ffprobe") is not None:
        try:
            return _probe_ffprobe(url, auth_header, timeout)
        except ProbeError as exc:
            if shutil.which("mpv") is None:
                raise
            first = exc      # fall through to mpv rather than failing outright
    if shutil.which("mpv") is None:
        raise ProbeError("neither ffprobe nor mpv found on PATH")
    cmd = [
        "mpv", "--no-config", "--vo=null", "--ao=null", "--frames=1",
        "--no-resume-playback",
        f"--term-playing-msg={_FORMAT}",
    ]
    if auth_header:
        cmd.append(f"--http-header-fields={auth_header}")
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"timed out after {timeout:g}s") from exc
    for line in (done.stdout + done.stderr).splitlines():
        line = line.strip()
        if line.startswith(_SENTINEL) and line.endswith(_SENTINEL):
            fields = line.split("|")[1:-1]
            if len(fields) == _FIELDS:
                return media_from_fields(*fields)
    raise _fallback_error(
        first, "mpv did not report video properties (not a video?)")


# =============================================================================
# Full stream details -- what the info panel shows
# =============================================================================
# The tag probe above answers "what is this file"; this answers "what is IN
# it": every audio track and subtitle, with language and title, so you can see
# whether the Chinese subs are even in there before starting playback.
#
# No -read_intervals here: track identity is container metadata, so this is a
# header read rather than a decode. Colour accuracy is the tag probe's job.

_KIND_NAMES = {"video": "Video", "audio": "Audio", "sub": "Subs"}


@dataclass
class Stream:
    """One track inside a file, as shown in the info panel.

    ``index`` is whatever the backend numbers tracks by: the ffmpeg stream
    index for ffprobe (which is also mpv's ``ff-index``, so a live player's
    selection can be matched against it) and mpv's per-type ``--aid``/``--sid``
    for the mpv backend. It is display-only either way.
    """

    kind: str                  # "video" | "audio" | "sub"
    index: int | None = None
    codec: str = ""
    lang: str = ""
    title: str = ""
    detail: str = ""           # "1920x1080 24 fps" / "2ch 48000 Hz"
    default: bool = False
    forced: bool = False
    selected: bool = False

    @property
    def kind_name(self) -> str:
        return _KIND_NAMES.get(self.kind, self.kind or "?")

    def describe(self) -> str:
        """Everything but the kind, on one line."""
        parts: list[str] = []
        if self.index is not None:
            parts.append(f"#{self.index}")
        if self.codec:
            parts.append(self.codec)
        if self.lang:
            parts.append(self.lang)
        if self.title:
            parts.append(f"'{self.title}'")
        if self.detail:
            parts.append(self.detail)
        if self.default:
            parts.append("[default]")
        if self.forced:
            parts.append("[forced]")
        return "  ".join(parts)


@dataclass
class MediaInfo:
    """Everything the info panel knows about one file."""

    container: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    streams: list[Stream] = field(default_factory=list)
    backend: str = ""          # which probe answered: "ffprobe" | "mpv"

    def of_kind(self, kind: str) -> list[Stream]:
        return [s for s in self.streams if s.kind == kind]

    def summary(self) -> list[tuple[str, str]]:
        """Label/value rows for the header of the panel. Empty values dropped,
        so an unhelpful backend just shows fewer rows."""
        rows = [
            ("duration", format_duration(self.duration)),
            ("size", format_size(self.size) if self.size else ""),
            ("container", self.container),
            ("bitrate", format_bitrate(self.bitrate)),
        ]
        counts = [
            f"{len(self.of_kind(k))} {name}"
            for k, name in (("video", "video"), ("audio", "audio"), ("sub", "subtitle"))
            if self.of_kind(k)
        ]
        rows.append(("tracks", " · ".join(counts)))
        return [(k, v) for k, v in rows if v]

    def with_selection(self, indexes) -> "MediaInfo":
        """A copy with ``selected`` set from a live player's stream indexes.

        Only meaningful for the ffprobe backend, whose ``index`` is the ffmpeg
        stream index mpv reports as ``ff-index``.
        """
        chosen = set(indexes or ())
        if not chosen:
            return self
        return MediaInfo(
            container=self.container, duration=self.duration, size=self.size,
            bitrate=self.bitrate, backend=self.backend,
            streams=[
                Stream(**{**s.__dict__, "selected": s.index in chosen})
                for s in self.streams
            ],
        )


# ffprobe names the subtitle stream type "subtitle"; mpv and this module say "sub".
_FF_KINDS = {"video": "video", "audio": "audio", "subtitle": "sub"}


def _fps(rate: str) -> str:
    """ffprobe's "24000/1001" as "23.976 fps". Empty when it means nothing."""
    try:
        num, _, den = str(rate).partition("/")
        value = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    if value <= 0:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".") + " fps"


def details_from_ffprobe(payload: dict) -> MediaInfo:
    """Turn one ffprobe JSON document into a MediaInfo. Pure -- unit testable."""
    fmt = payload.get("format") or {}
    streams: list[Stream] = []
    for raw in payload.get("streams") or []:
        kind = _FF_KINDS.get((raw.get("codec_type") or "").lower())
        if kind is None:
            continue                      # attachments, data streams, fonts
        tags = {str(k).lower(): v for k, v in (raw.get("tags") or {}).items()}
        disposition = raw.get("disposition") or {}
        bits: list[str] = []
        if kind == "video":
            if raw.get("width") and raw.get("height"):
                bits.append(f"{raw['width']}x{raw['height']}")
            bits.append(_fps(raw.get("r_frame_rate") or ""))
        elif kind == "audio":
            if raw.get("channels"):
                bits.append(raw.get("channel_layout") or f"{raw['channels']}ch")
            if raw.get("sample_rate"):
                bits.append(f"{raw['sample_rate']} Hz")
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            index = None
        streams.append(Stream(
            kind=kind,
            index=index,
            codec=raw.get("codec_name") or "",
            lang=str(tags.get("language") or ""),
            title=str(tags.get("title") or ""),
            detail=" ".join(b for b in bits if b),
            default=bool(disposition.get("default")),
            forced=bool(disposition.get("forced")),
        ))
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        bitrate = int(float(fmt.get("bit_rate") or 0))
    except (TypeError, ValueError):
        bitrate = 0
    try:
        size = int(float(fmt.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return MediaInfo(container=fmt.get("format_name") or "", duration=duration,
                     size=size, bitrate=bitrate, streams=streams, backend="ffprobe")


# mpv's own track list, as it prints it on load:
#   ● Video  --vid=1               (h264 1920x1080 24 fps)
#   ● Audio  --aid=1  --alang=jpn  'Japanese 5.1' (aac 1ch 44100 Hz)
#   ○ Audio  --aid=2  --alang=eng  (aac 1ch 44100 Hz)
#   ● Subs   --sid=1  --slang=chi  'Chinese Simplified' (subrip) [default]
# The leading glyph is the selection marker (older mpv wrote "(+)"/"(*)").
_TRACK_LINE = re.compile(
    r"^\s*(?P<marker>[●○•]|\(\+\)|\(-\))?\s*"
    r"(?P<kind>Video|Audio|Subs)\s+--(?:vid|aid|sid)=(?P<id>\d+)"
    r"(?P<rest>.*)$"
)
_MPV_KINDS = {"video": "video", "audio": "audio", "subs": "sub"}
# Parenthesised bits that are markers, not a codec description.
_MPV_MARKERS = {"*", "+", "-", "default", "forced", "external", "dependent",
                "visual impaired", "hearing impaired", "image", "albumart"}
_SELECTED_MARKERS = {"●", "•", "(+)", "(*)"}
_DURATION_LINE = re.compile(r"^\s*@@DUR@@([0-9.]*)@@")


def details_from_mpv_output(text: str) -> MediaInfo:
    """Parse mpv's printed track list. Pure -- unit testable.

    Deliberately forgiving: every field past the track id is optional, because
    this output is human-facing and has been restyled across mpv releases.
    """
    streams: list[Stream] = []
    duration = 0.0
    for line in text.splitlines():
        found = _DURATION_LINE.match(line)
        if found:
            try:
                duration = float(found.group(1) or 0)
            except ValueError:
                duration = 0.0
            continue
        match = _TRACK_LINE.match(line)
        if match is None:
            continue
        rest = match.group("rest")
        lang = re.search(r"--[avs]lang=(\S+)", rest)
        title = re.search(r"'([^']*)'", rest)
        groups = re.findall(r"\(([^()]*)\)", rest)
        flags = {g.strip().lower() for g in re.findall(r"\[([^\]]*)\]", rest)}
        flags |= {g.strip().lower() for g in groups if g.strip().lower() in _MPV_MARKERS}
        codec_blob = next(
            (g.strip() for g in groups if g.strip().lower() not in _MPV_MARKERS), ""
        )
        codec, _, detail = codec_blob.partition(" ")
        try:
            index = int(match.group("id"))
        except (TypeError, ValueError):
            index = None
        streams.append(Stream(
            kind=_MPV_KINDS[match.group("kind").lower()],
            index=index,
            codec=codec,
            lang=lang.group(1) if lang else "",
            title=title.group(1) if title else "",
            detail=detail.strip(),
            default="default" in flags,
            forced="forced" in flags,
            selected=(match.group("marker") or "") in _SELECTED_MARKERS,
        ))
    return MediaInfo(duration=duration, streams=streams, backend="mpv")


def _details_ffprobe(url: str, auth_header: str | None, timeout: float) -> MediaInfo:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=format_name,duration,size,bit_rate"
        ":stream=index,codec_type,codec_name,width,height,r_frame_rate"
        ",channels,sample_rate,channel_layout"
        ":stream_tags=language,title"
        ":stream_disposition=default,forced",
        "-of", "json",
    ]
    if auth_header:
        cmd += ["-headers", auth_header + "\r\n"]
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout:g}s") from exc
    if done.returncode != 0:
        raise ProbeError(f"ffprobe failed: {done.stderr.strip()[:200]}")
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        raise ProbeError("ffprobe returned no usable JSON") from exc
    info = details_from_ffprobe(payload)
    if not info.streams:
        raise ProbeError("no streams reported (not a media file?)")
    return info


def details(url: str, auth_header: str | None = None,
            timeout: float = 30.0) -> MediaInfo:
    """Full track listing for ``url``. Raises ProbeError; never blocks forever.

    Same backend order as ``probe``: ffprobe where it exists, otherwise mpv --
    which prints its track list on load, and is a hard dependency anyway.
    """
    first: ProbeError | None = None
    if shutil.which("ffprobe") is not None:
        try:
            return _details_ffprobe(url, auth_header, timeout)
        except ProbeError as exc:
            if shutil.which("mpv") is None:
                raise
            first = exc
    if shutil.which("mpv") is None:
        raise ProbeError("neither ffprobe nor mpv found on PATH")
    cmd = [
        "mpv", "--no-config", "--vo=null", "--ao=null", "--frames=1",
        "--no-resume-playback",
        # Same trap as the tag probe: --really-quiet would take the track list
        # with it. Duration comes through the playing-msg since mpv prints it
        # only as a progress line.
        "--term-playing-msg=@@DUR@@${=duration}@@",
    ]
    if auth_header:
        cmd.append(f"--http-header-fields={auth_header}")
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"timed out after {timeout:g}s") from exc
    info = details_from_mpv_output(done.stdout + done.stderr)
    if not info.streams:
        raise _fallback_error(first, "mpv listed no tracks (not a media file?)")
    return info
