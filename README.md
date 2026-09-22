# dav_video

A keyboard-driven terminal app that browses WebDAV servers and hands media to
**mpv**. Built to replace PotPlayer-on-Windows-style workflows on Linux, without
fighting Wayland: the TUI has no video surface of its own, so mpv (a separate
window) owns all rendering, seeking, shaders and codecs — your `~/.config/mpv`
config just works.

Core features:
- **Multiple WebDAV servers**, credentials stored **securely** (OS keyring by
  default; encrypted-file fallback for headless boxes).
- **Multiple named playlists**, saved to disk; the queue you were on comes back
  next launch.
- **mpv integration**: play and queue, with sidecar subtitles from the server
  attached automatically — per episode, no keypress.
- **Tags on queued tracks**: running time / resolution / HDR / codec detected
  from the file itself, plus your own labels.
- **Media info panel**: every audio track and subtitle in a file, with its
  language — before you start playing it.
- **Render switches**: turn on gpu-next / HDR / scaling on top of your
  `~/.config/mpv` from inside the TUI, without overwriting any of it.
- **Everything on the keyboard**: a short footer, one settings key for the rest,
  and every shortcut rebindable.

Works with generic WebDAV (Nextcloud / nginx dav / …) and Alist.

## Security model

Nothing secret is written to `config.json` — it holds only server name, URL,
username, playlists and app settings. Passwords go to:

- **keyring** (default): KWallet / GNOME Keyring via Secret Service. The app
  probes it on startup and refuses to persist plaintext.
- **encrypted-file** (fallback): set `DAV_VIDEO_MASTER=<master-pw>`; a Fernet
  key is derived with PBKDF2-HMAC-SHA256 (390k iters) and per-server tokens live
  in a `~/.config/dav_video/secrets.enc` that is created `0600` and replaced atomically.

## Install & run

```bash
pip install git+https://github.com/nambers/dav_video
dav_video
```

mpv must be on `PATH`. **ffprobe** (from the `ffmpeg` package) is optional but
worth having: it is faster and reads more than the mpv fallback — see
[Tags](#tags) and [File info](#file-info-f). On Arch it already comes in with
mpv; on Debian/Fedora it does not.

### Build date (packagers)

The about box (`?` → About, or `i`) prints a `built` line, so a bug report says
how old the copy is — a release and a `-git` build both call themselves 0.1.0.
Running from a git checkout it reads HEAD's commit time and short sha by
itself. An **installed** copy has no `.git` left to read, so stamp it at build
time, inside the source tree, before installing:

```bash
python scripts/stamp_build.py     # writes dav_video/_build.py; git HEAD, else now
```

`SOURCE_DATE_EPOCH` overrides both, so reproducible builds stay reproducible.
Skipping this is fine — the line then just reads `unknown`.

## Keybindings

The footer only carries the keys you press constantly — the ones marked ★
below. **Everything else lives behind `?`**, which opens Settings: the full
shortcut list, the mpv render switches, and the about box.

### Rebinding

`?` → **Keyboard shortcuts** is both the cheat sheet and the editor:

| key | |
|---|---|
| ↑ / ↓ | walk the list (section headers are skipped) |
| Enter | rebind — the next key you press becomes this action's key |
| `d` ★ | unbind |
| `r` | put the default back |
| `Ctrl+R` | reset everything |
| `Ctrl+S` | save (takes effect immediately, footer included) |
| Esc | discard |

Only what you actually changed is written, to `settings.keybindings` in
`config.json`. If you take a key that another action already had, that action
is unbound rather than shadowed, and the screen says so.

`↑ ↓`, `Enter`, `Tab`, `Esc`, `Ctrl+P` and `Ctrl+Q` are structural — they are
how you move around and how you get out — so they are not rebindable.

**Nothing can become unreachable:** `Ctrl+P` (Textual's command palette) lists
every action by name, including any you have unbound.

### Defaults

The footer shows a different set depending on which pane you are in — it is the
only hint you get, so it shows what is useful *here*:

```
server list   n New  e Edit  d Delete  ? Settings  i About  q Quit
browser       Space Queue  f Info  / Filter  s Sort  ⌫ Up  p Play  S Servers
              ? Settings  i About  q Quit
queue         f Info  p Play  x Unqueue  t Auto-tag  S Servers  ? Settings
              i About  q Quit
```

`S` closes the server you are in and takes you back to the list.

Only the *hint* is per-pane; every key below works everywhere regardless, and
`?` / `Ctrl+P` always find all of them. ★ marks a key that appears in some
footer.

**Browse**

| key | what it does |
|---|---|
| `Space` ★ | Queue the highlighted file, or every marked file |
| `/` ★ | Narrow the browser to names containing what you type |
| `f` ★ | Streams, languages and length of the highlighted file |
| `v` | Mark / unmark the highlighted file |
| `⇧↓` | Extend the marked block downwards |
| `⇧↑` | Extend the marked block upwards |
| `Esc` | Drop every mark in the browser |
| `s` ★ | Cycle sort order (name / size, ascending / descending) |
| `⌫` ★ | Go up one directory |
| `←` | Move focus to the file browser |
| `→` | Move focus to the queue |

**Queue**

| key | what it does |
|---|---|
| `p` ★ | Hand the whole queue to mpv |
| `x` ★ | Drop the highlighted track from the queue |
| `[` | Move the highlighted track one place up |
| `]` | Move the highlighted track one place down |
| `t` ★ | Detect length / resolution / HDR / codec for queued tracks |
| `T` | Edit your own tags on the highlighted track |
| `o / ^o` | Replace the queue with a saved one |
| `m` | Append a saved queue to this one |
| `r / ^s` | Rename the queue (it is saved under this name) |

**Playback** — none, on purpose. Everything about a running player belongs to
mpv, in the window you are looking at: `space` pause, arrows seek, `#` audio
track, `j` / `J` subtitle track, `v` hide subtitles, `9` / `0` volume. A copy of
any of that here would only be reachable by leaving the video first.

Subtitles that live *next to the file on the server* are the one thing mpv
cannot find for itself — and they are attached **when playback starts**, so
there is no key for them either. See [Subtitles](#subtitles).

**Servers**

| key | what it does |
|---|---|
| `S` ★ | Close this server and go back to the server list |
| `n` ★ | Add a WebDAV server |
| `e` ★ | Edit the highlighted server |
| `d` ★ | Delete the highlighted server and its saved password |

**App**

| key | what it does |
|---|---|
| `?` ★ | Shortcuts, mpv rendering, about |
| `g` | mpv render switches (gpu-next / HDR / scaling / …) |
| `i` ★ | Version, paths and backends this run is using |
| `q` ★ | Leave dav_video (mpv is shut down with it) |

Every dialog is keyboard-only too: ↑ / ↓ move between fields, switches and
buttons, Space toggles a switch, Enter accepts, Escape cancels.

## Filtering (`/`)

`/` narrows the browser as you type; every key is a letter while you are typing,
including the ones that are normally shortcuts:

```
/show - 0                    ← 's' types an s here, it does not re-sort
  [SubsPlease] Show - 01 [1080p].mkv
  [SubsPlease] Show - 02 [1080p].mkv
```

Matching is a **case-insensitive substring** anywhere in the name.

- **Enter** keeps the narrowed list and gives the keys back, which is the point:
  filter down to one season, `⇧↓` to mark the block, `Space` to queue it.
- **Escape** clears the filter. Pressing it again clears the marks — one layer
  per press, most recent first.
- Narrowing the view never drops marks you made before narrowing.
- Changing directory clears the filter; re-sorting keeps it.

## Tags

Queued tracks carry two kinds of label, shown dimmed after the title:

```
 1. Liz.and.the.Blue.Bird.2018.1080p.BluRay.x265.10bit.mkv   1h30m 1080p hevc 10bit
 2. Some.UHD.Release.mkv                                     2h11m 4k hdr hevc 10bit rewatch
```

- **`t`** detects the running time, `4k`/`1080p`/…, `hdr`/`hlg`/`dv`/`hdr10+`,
  the codec and the bit depth. Uses **ffprobe** when present (~2s over WebDAV)
  and falls back to **mpv** (~3s) otherwise, so nothing new becomes a hard
  dependency. Newly queued tracks are probed automatically in the background;
  `t` fills in anything still missing. Results are saved, so it happens once
  per track. With nothing missing, `t` re-probes the whole queue instead of
  refusing, so a file that changed on the server picks up its new labels.
- **`T`** edits your own comma-separated tags on the highlighted track. Probing
  replaces the detected labels and never touches yours.

## File info (`f`)

`t` gives you a label; `f` opens the whole picture for whatever is highlighted
— in the browser or in the queue, playing or not:

```
Liz.and.the.Blue.Bird.2018.mkv
home:/Movies/Liz.and.the.Blue.Bird.2018.mkv

duration   1h30m
size       12.4 GiB
container  matroska,webm
bitrate    18.2 Mb/s
tracks     1 video · 2 audio · 3 subtitle
read by    ffprobe

Video
  #0  hevc  1920x1080 23.976 fps
Audio
▶ #1  flac  jpn  'Japanese 2.0'  stereo 48000 Hz
  #2  aac   eng  stereo 48000 Hz
Subs
▶ #3  ass   chi  'CHS'  [default]
  #4  ass   cht  'CHT'
  #5  subrip eng

▶ = playing now

Sidecar subs (next to it on the server)
▶ ep1.cht.ass
  ep1.chs.ass
  ep1.en.srt
▶ = attached when you play this
```

The point is the subtitle and audio list: you can see whether the Chinese subs
are even there, and in what language each track is, **before** starting
playback. Both halves matter — a probe only ever sees *inside* the container,
and on a WebDAV share the subtitles are usually a separate file next to it, so
the panel lists those too and marks the one that will be attached. When mpv is
playing that exact file, `▶` marks the tracks it currently has selected.

Same two backends as tagging, and the panel opens immediately with what is
already known and fills the streams in when the probe answers, so a slow server
never looks like a dropped keypress.

**Why this exists:** `1080p BluRay x265 10bit` in a filename says nothing about
HDR — 10-bit is an encoding depth, not a transfer function. Only files tagged
`hdr` will make the HDR passthrough switch below do anything.

## Subtitles

A `.srt` / `.ass` sitting next to the video **on the server** is the one thing
mpv cannot find for itself: it would need a directory listing, a filename rule,
and the `Authorization` header. So dav_video resolves it and hands it to mpv
*with the file* — every entry in a queue carries its own, so episode 2 gets
episode 2's subtitles when mpv walks on to it. **No keypress, nothing to
remember.**

Matching is a filename rule: the subtitle's stem must equal the video's,
optionally followed by `. _ -` or a space and a language tag. `Show.S01E01.mkv`
takes `Show.S01E01.zh.srt` but not `Show.S01E02.srt`.

**mpv accepts exactly one sidecar per playlist entry**, so when a release ships
`.chs` *and* `.cht` *and* `.en`, something has to choose. The choice is your own
`slang` from `~/.config/mpv`:

```
slang=zh,chs,chi      in ~/.config/mpv/mpv.conf
→ ep1.chs.ass wins over ep1.cht.ass and ep1.en.srt
```

With no `slang` set, the first match wins. Subtitles already *inside* the file
are unaffected — `f` lists them, `j` switches between them.

## Render settings

Press `g` (or `?` → mpv render settings). One screen, one place — independent
switches, the resulting mpv command line shown live underneath, and the paths of
both config files at the bottom. Nothing is on by default, so installing this
changes nothing until you switch something on. ↑ / ↓ walk the switches, Space
toggles one, `Ctrl+R` ticks the recommended set, `Ctrl+S` saves.

| switch | what it gets you | flags |
|---|---|---|
| gpu-next renderer | libplacebo on Vulkan — the base the rest builds on | `--vo=gpu-next --gpu-api=vulkan` |
| High-quality scaling | sharper upscale, correct downscale | `--scale/--cscale=ewa_lanczossharp`, `--dscale=catmull_rom`, … |
| Dithering + debanding | kills banding in gradients and dark scenes | `--error-diffusion`, `--deband*` |
| HDR tone mapping | maps HDR down to an SDR screen properly | `--tone-mapping=spline`, `--hdr-compute-peak`, `--gamut-mapping-mode` |
| HDR passthrough | sends HDR untouched so the compositor flips the screen into HDR | `--target-colorspace-hint=yes --target-colorspace-hint-mode=source` |
| Smooth motion | smooths 24p judder; costs a lot of GPU | `--video-sync=display-resample --interpolation=yes` |
| Start mpv fullscreen | also what lets a compositor switch into HDR | `--fullscreen=yes` |

**Recommended** ticks the first three. The screen reads your monitors' EDID and
tells you whether they actually support HDR, and it warns about the two
combinations that fail silently — HDR without fullscreen, and the picture
options without gpu-next.

Everything is **additive**: these flags are appended after mpv reads
`~/.config/mpv`, so your own config and shaders still win for anything not
listed here. `vo`/`gpu-api` are launch-time only, so saving a change stops the
running mpv; the next play starts it again with the new flags (the position is
written to mpv's watch-later file first, so it resumes).

### Where the config lives

Both paths are printed at the bottom of the `g` screen:

- `~/.config/dav_video/config.json` — servers, playlists, and `settings`.
  In `settings`, `render_features` is the switch list, `mpv_extra_args` is a
  free-form list applied **last** so it overrides anything above, and
  `keybindings` holds your key changes.
- `~/.config/mpv/mpv.conf` — mpv's own config, always applied first.

## Known limitations

- A queue mixing multiple servers uses the first track's auth for all (mpv's
  `http-header-fields` is global). Fine for single-server queues.
- Subtitle sibling-matching is a filename rule: the subtitle's stem must equal
  the video's, optionally plus a `. _ -` or space separated language tag. Only
  the same directory is searched — a `Subs/` subfolder is not found.
- One sidecar subtitle per queued file (mpv's limit), chosen by your `slang`.
  Playing a single file from the browser attaches all of its sidecars.
- No in-TUI transport bar (by design — mpv owns it).
