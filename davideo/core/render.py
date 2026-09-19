"""mpv render options -- the only rendering knobs this app owns.

Strictly ADDITIVE: these flags are appended after mpv has read
``~/.config/mpv``, so the user's own config and shaders still decide everything
not mentioned here, and with nothing enabled we add nothing at all.

Independent toggles, not a ladder of presets -- each feature stands on its own
and says what it does for you.

Launch-time flags (``vo``/``gpu-api`` cannot change on a live mpv), so changing
them restarts the player (``MpvController.set_render_args``).

stdlib only, zero UI.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderFeature:
    key: str                       # stable id, this is what lands in config.json
    title: str                     # switch label
    detail: str                    # one line: what it gets you, in plain words
    args: tuple[str, ...]


FEATURES: tuple[RenderFeature, ...] = (
    RenderFeature(
        "gpu_next",
        "gpu-next renderer",
        "libplacebo on Vulkan — the modern renderer the options below build on",
        (
            "--vo=gpu-next",
            "--gpu-api=vulkan",
        ),
    ),
    RenderFeature(
        "scaling",
        "High-quality scaling",
        "Sharper upscaling (Jinc) and correct, non-aliased downscaling",
        (
            "--scale=ewa_lanczossharp",
            "--cscale=ewa_lanczossharp",
            "--dscale=catmull_rom",
            "--correct-downscaling=yes",
            "--linear-downscaling=yes",
            "--sigmoid-upscaling=yes",
        ),
    ),
    RenderFeature(
        "dither_deband",
        "Dithering + debanding",
        "Removes colour banding in gradients and dark scenes",
        (
            "--dither-depth=auto",
            "--error-diffusion=sierra-lite",
            "--deband=yes",
            "--deband-iterations=4",
            "--deband-threshold=35",
            "--deband-range=16",
            "--deband-grain=5",
        ),
    ),
    RenderFeature(
        "hdr_tonemap",
        "HDR tone mapping",
        "Maps HDR content down to an SDR screen properly, instead of washed out",
        (
            "--tone-mapping=spline",
            "--hdr-compute-peak=yes",
            "--gamut-mapping-mode=perceptual",
        ),
    ),
    RenderFeature(
        "hdr_passthrough",
        "HDR passthrough (switch the display into HDR)",
        "Sends HDR untouched, so the compositor flips the screen into HDR mode",
        (
            "--target-colorspace-hint=yes",
            # Without mode=source mpv describes its surface using the display's
            # CURRENT capabilities. While the screen is still in SDR that reads
            # back as "SDR", so mpv never advertises HDR, so the compositor
            # never switches -- a deadlock that looks exactly like the feature
            # being broken. mode=source describes the surface from the SOURCE
            # instead, which is what breaks the tie. Verified on Hyprland
            # 0.56.2: default (target) never fires cm_auto_hdr, source does.
            "--target-colorspace-hint-mode=source",
        ),
    ),
    RenderFeature(
        "motion",
        "Smooth motion (interpolation)",
        "Smooths 24p judder on a high-refresh screen. Costs a lot of GPU",
        (
            "--video-sync=display-resample",
            "--interpolation=yes",
            "--tscale=oversample",
        ),
    ),
    RenderFeature(
        "fullscreen",
        "Start mpv fullscreen",
        "Also required for a compositor to auto-switch the display into HDR",
        ("--fullscreen=yes",),
    ),
)

_BY_KEY = {f.key: f for f in FEATURES}

# One-click sensible default: better picture, no HDR surprise, no GPU blowout.
RECOMMENDED: tuple[str, ...] = ("gpu_next", "scaling", "dither_deband")

# Only for migrating configs written before the toggles replaced presets.
_LEGACY_PRESETS: dict[str, tuple[str, ...]] = {
    "inherit": (),
    "gpu-next": ("gpu_next",),
    "quality": ("gpu_next", "scaling", "dither_deband"),
    "hdr": ("gpu_next", "scaling", "dither_deband", "hdr_tonemap", "hdr_passthrough"),
    "hdr-motion": ("gpu_next", "scaling", "dither_deband", "hdr_tonemap",
                   "hdr_passthrough", "motion"),
}


def feature_keys() -> list[str]:
    return [f.key for f in FEATURES]


def normalize(keys) -> list[str]:
    """Keep only keys we still know, in declaration order. A feature removed in
    a later version must not break someone's startup."""
    wanted = set(keys or ())
    return [f.key for f in FEATURES if f.key in wanted]


def migrate_preset(preset_name: str) -> list[str]:
    """Translate a pre-toggle config's ``render_preset`` into feature keys."""
    return normalize(_LEGACY_PRESETS.get(preset_name, ()))


def build_args(features, extra: list[str] | None = None) -> list[str]:
    """Full extra-argv for launching mpv.

    Order matters: later mpv flags win, so user ``extra`` args come last and can
    override anything a feature set.
    """
    args: list[str] = []
    for key in normalize(features):
        args.extend(_BY_KEY[key].args)
    args.extend(extra or [])
    return args


def describe(features, extra: list[str] | None = None) -> str:
    """Short status-bar description of the active render config."""
    keys = normalize(features)
    if not keys and not extra:
        return "off (your ~/.config/mpv only)"
    bits = list(keys)
    if extra:
        bits.append(f"+{len(extra)} custom")
    return ", ".join(bits)


def warnings(features, hdr_display: bool | None = None) -> list[str]:
    """Combinations that fail silently -- the flags apply, mpv starts, and you
    simply don't get the effect you switched on."""
    keys = set(normalize(features))
    out: list[str] = []
    if keys & {"scaling", "dither_deband", "hdr_passthrough", "motion"} and "gpu_next" not in keys:
        out.append("These work best with the gpu-next renderer switched on.")
    if "hdr_passthrough" in keys and "fullscreen" not in keys:
        out.append(
            "A compositor only switches the display into HDR for a fullscreen "
            "window — turn on 'Start mpv fullscreen' too."
        )
    if "hdr_passthrough" in keys and hdr_display is False:
        out.append(
            "No HDR-capable display detected. Passthrough hands tone mapping to "
            "the compositor; 'HDR tone mapping' does it in mpv and looks better."
        )
    if "hdr_passthrough" not in keys and "hdr_tonemap" in keys and hdr_display:
        out.append(
            "Your display supports HDR — add 'HDR passthrough' to actually use "
            "it, otherwise HDR is mapped down to SDR."
        )
    return out


def mpv_config_path() -> str:
    """Where mpv reads its own config from; shown in the settings screen."""
    import os

    home = os.environ.get("MPV_HOME")
    if not home:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        home = os.path.join(base, "mpv")
    return os.path.join(home, "mpv.conf")
