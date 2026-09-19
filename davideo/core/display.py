"""Best-effort display capability probe, read straight from EDID.

Exists to answer one question the render settings screen would otherwise leave
to the user: "does my monitor actually do HDR?" Guessing wrong is invisible --
you enable HDR passthrough, nothing changes, and there is no error to read.

Pure stdlib, and every failure path returns "unknown" rather than raising: this
is a hint in a settings dialog, never something playback depends on.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass

# CTA-861 extension block tag, and the extended data block tag for
# "HDR Static Metadata" inside it.
_CTA_EXTENSION_TAG = 0x02
_HDR_STATIC_METADATA_TAG = 0x06
# Byte 0 of that block is a bitmap of supported EOTFs. Bit 2 is SMPTE ST 2084
# (PQ) -- the one that matters; bit 0 is plain SDR gamma, which everything sets.
_EOTF_ST2084_BIT = 0x04


@dataclass(frozen=True)
class Output:
    name: str            # connector name, e.g. "DP-1"
    hdr: bool            # advertises the PQ EOTF


def _cta_data_blocks(block: bytes):
    """Yield (extended_tag, payload) for each data block in a CTA-861 block."""
    if len(block) < 4 or block[0] != _CTA_EXTENSION_TAG:
        return
    dtd_offset = block[2]
    # A DTD offset of 0 means "no data blocks at all"; 4 is an empty collection.
    if dtd_offset < 4 or dtd_offset > len(block):
        return
    i = 4
    while i < dtd_offset:
        header = block[i]
        tag, length = header >> 5, header & 0x1F
        if length == 0:
            break
        body = block[i + 1 : i + 1 + length]
        if tag == 7 and body:          # 7 == "use extended tag"
            yield body[0], body[1:]
        i += 1 + length


def _edid_has_pq(edid: bytes) -> bool:
    if len(edid) < 128:
        return False
    extensions = edid[126]
    for n in range(1, extensions + 1):
        block = edid[128 * n : 128 * (n + 1)]
        if len(block) < 128:
            break
        for ext_tag, payload in _cta_data_blocks(block):
            if ext_tag == _HDR_STATIC_METADATA_TAG and payload:
                return bool(payload[0] & _EOTF_ST2084_BIT)
    return False


def outputs() -> list[Output]:
    """Connected outputs and whether each advertises HDR. Empty list if the
    sysfs layout isn't there (non-Linux, container without /sys, …)."""
    found: list[Output] = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*/edid")):
        connector = path.rsplit("/", 2)[-2]
        # "card1-DP-1" -> "DP-1"
        name = connector.split("-", 1)[1] if "-" in connector else connector
        try:
            with open(path, "rb") as handle:
                edid = handle.read()
        except OSError:
            continue
        if not edid:          # disconnected outputs expose an empty edid file
            continue
        found.append(Output(name=name, hdr=_edid_has_pq(edid)))
    return found


def hdr_summary() -> str:
    """One line for the settings screen. Deliberately vague when we can't tell --
    an unknown must not read as a "no"."""
    try:
        found = outputs()
    except Exception:  # noqa: BLE001 - a hint must never break the dialog
        return ""
    if not found:
        return ""
    capable = [o.name for o in found if o.hdr]
    if capable:
        return "your display: " + ", ".join(capable) + " supports HDR ✓"
    return "your display: " + ", ".join(o.name for o in found) + " reports SDR only"


def any_hdr() -> bool | None:
    """True if any connected output does HDR, False if none does, None if we
    can't tell. Tri-state on purpose -- "unknown" must never be shown as "no"."""
    try:
        found = outputs()
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    return any(o.hdr for o in found)
