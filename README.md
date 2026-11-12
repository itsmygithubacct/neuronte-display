# neuronte-display — RC9

A terminal and character-cell desktop drawn by Neuronte nets. The display owns
PTYs, a pinned MIT libghostty-vt parser, tabs and splits, persistent coverage
caches, a Kitty framebuffer backend, and direct CPU DRM/KMS scanout. The RC9 package selects
the approved Greek/Cyrillic `neuronte-split-v5.nrt`; split-v4 remains bundled as
a preserved baseline and the direct CLI default. No font files are read. This work belongs to RC9, for
future Plebian-OS 0.2.3. It does not change the frozen RC8 release.

## Build and run

Build requirements: C11 compiler, pkg-config, libdrm and libpng development
headers, Python 3 with NumPy, zlib, pthreads, built Neuronte with styled/half
coverage APIs, and the sibling MIT `kitty-framebuffer` at `deps.lock.json`'s
revision. The parser requires the exact upstream revision **and** four vendored
MIT patches; both hashes are checked by the build. Rebuild it independently:

```sh
python3 tools/build_parser.py --source "$GHOSTTY_SOURCE" --zig "$ZIG_016" \
  --work "$BUILD_ROOT/parser-work" --prefix "$BUILD_ROOT/ghostty"
make GHOSTTY_PREFIX="$BUILD_ROOT/ghostty"
NEURONTE_HOME="$NEURONTE_SOURCE" NEURONTE_VIDEO_HOME="$VIDEO_SOURCE" make test
python3 neuronte_display.py --snapshot build/session.ppm
python3 neuronte_display.py --headless-seconds 2 --snapshot build/smoke.ppm -- \
  /bin/sh -c 'printf "Hello from Neuronte\n"'
```

`--home`/`NEURONTE_HOME`, `--weights` and `--library` select Neuronte inputs.
`--video-home` and `--video-weights` select the multi-ink module and calibrated
video bundle. `--cache` defaults to `$XDG_CACHE_HOME/neuronte-display` or
`~/.cache/neuronte-display`. Keep active builds, caches, VM disks and logs on
fast local storage.

From an already active text VT with access to its DRM and input devices:

```sh
python3 neuronte_display.py --backend kms --drm-card /dev/dri/card0 \
  --mode 640x480 --pointer-device /dev/input/by-id/DEVICE-event-mouse
```

KMS refuses to take a background or graphical VT. It saves and restores the
original CRTC/VT state, releases DRM ownership on a VT switch, and reacquires
it on return. Keyboard translation uses the kernel VT keymap; evdev supplies
relative or absolute pointer events. This is not a complete desktop keyboard
layout, key-release or remote-control implementation.

## Controls and rendering

The prefix is **Ctrl-A**, followed by one key:

| Key | Action |
|---|---|
| `n` | New tab |
| `s`, `h` | Vertical or horizontal split, up to four panes |
| `o` | Focus the next pane |
| `[`, `]` | Previous or next tab |
| `+`, `-` | Move the first split boundary one cell |
| `x` | Close the focused pane |
| `q` | Quit and restore terminal modes |
| Ctrl-A again | Send Ctrl-A to the child |

Each pane has its own PTY; hidden tabs continue running. Pane and outer-window
resizes update parser geometry and deliver SIGWINCH to children. The Kitty
backend reserves the last outer row according to kitty-framebuffer's contract.
Mouse input uses the parser's negotiated terminal protocol. The default pointer uses the existing U+2196 symbol net. Explicit icon mode
selects the accepted U+E0B2 triangle with an aligned hotspot and contrasting
foreground over light/dark pixels; legacy application defaults are unchanged.

Masks are sampled twice per axis, rounded to eight bits and cached by scalar,
style, weights/library hashes and geometry. Persistent entries have checksums
and atomic replacement. The current `neuronte_coverage_styled` API implements
synthetic raster styles: bold takes the maximum coverage over rightward offsets
through `max(1, round(cell_width / 32))` pixels; italic shifts each raster row
about the design baseline. Both are clipped to the original cell. This differs
from the original proposed SDF offset/query-coordinate approach and does not
select a separately trained italic face. Reverse, dim, underline, strike and
overline remain supported. Block cursors reverse text; colour cursors use a separate learned contrast
overlay in either half, preserving one artwork palette. Bar and underline use
learned U+258F/U+2581 masks. Cursor blinking is not implemented.

Full graphemes up to64 scalars are preserved in grid records. Known trained NFC
characters retain their exact masks. With the hash-bound approved text bundles,
supported Latin/Greek/Russian bases and all16 existing marks compose from
trained spacing/punctuation masks, including non-NFC and bounded stacked cases.
The [composition policy](GRAPHEMES.md) defines synthetic anchors, dot/descender
handling, clipping and limits. Unsupported shaping/marks, excessive stacks or
empty components use a whole-cluster U+FFFD and are counted explicitly.
Arabic/Indic shaping, ZWJ emoji, modifiers and flags remain unsupported.
Unknown single scalars still use the bundle's learned routing. Untrained wide
characters reserve two cells with the resulting mark in the first cell.

## Pictures, colour emoji and desktop

Visible explicit Kitty graphics placements support raw RGB/RGBA, zlib payloads,
PNG, cropping, scaling, deletion and alternate-screen lifetime. Every picture
uses three pixel-net masks and eight colours per 2×4 cell. Startup requires all
256 patterns to be exactly binary. Alpha is represented by separate multi-ink
cells. Odd dimensions are padded then clipped. The pinned video module uses its
exact binary gather optimization, retaining its fractional blending path.
Unicode-placeholder placements, animation ticks and Sixel are not implemented;
negative-z composition is an approximation, not full Kitty background semantics.

Standalone colour emoji are **optional**:

```sh
python3 neuronte_display.py --emoji-weights weights/emoji.nrt \
  --emoji-manifest weights/emoji-manifest.json --theme auto
```

The manifest must specify version 1, `cell_columns: 2`, and `glyphs` with
`scalar` strings such as `U+1F600`. Only listed single scalars in parser-reserved
wide cells select the colour bundle. Applications enable grapheme width mode
with `CSI ? 2027 h` before using VS16 to request emoji width; the pinned parser
keeps its compatibility default with mode 2027 disabled. Bare text-presentation
scalars stay narrow, and VS15 keeps text presentation. ZWJ, skin-tone and flag
sequences use the documented complex
cluster fallback. Style0 calls `neuronte_draw_half` independently for each half and retains its
accepted pixels. Bold/italic transform every coverage layer in the joined
two-cell glyph before compositing, including clear layers. Learned line
decorations span both cells, dim affects all coloured ink, and hidden suppresses
artwork/decorations and their image-occlusion alpha. Both palettes are retained. `--theme` accepts `auto`, `dark`
and `light`; auto uses cell-background luminance. The bounded cache includes
both halves, palette, colours and geometry. The analytic fixture checks unequal
halves and both palettes; trained-bundle acceptance is separate evidence.

`python3 cell_desktop.py` opens a keyboard-driven Terminal/Files/System launcher.
It is a small character-cell provider, not a complete Kilix desktop replacement.
Stage5 icon support in this display is explicitly opt-in:

```sh
python3 neuronte_display.py --icon-weights weights/neuronte-icon-v1.nrt \
  --icon-manifest weights/icons.json --theme auto
```

Only the167 accepted, hash-bound labels select this one-cell colour route. It
shares the colour-layer style contract and supplies the learned icon pointer.
See [COLOUR.md](COLOUR.md) for layer order, clear/foreground rules, wide seams,
decoration alpha and pointer geometry. Generic legacy-app/widget adoption and
window-manager PNG replacement remain deferred.

## RC9 package and installer

```sh
python3 tools/package.py --neuronte-home "$NEURONTE_SOURCE" \
  --video-home "$VIDEO_SOURCE" --ghostty "$BUILD_ROOT/ghostty" \
  --emoji-home "$TRAINING_SOURCE" --icon-home "$TRAINING_SOURCE" \
  --weights "$APPROVED_SPLIT_V5" \
  --output "$BUILD_ROOT/display.tar.gz"
python3 tools/build_installer.py --source-iso "$DEBIAN_ISO" \
  --os-source "$PLEBIAN_SOURCE" --payload "$BUILD_ROOT/display.tar.gz" \
  --output "$BUILD_ROOT/neuronte-rc9.iso"
```

The package includes the parser shared library, patches, rebuild recipe, pinned
video source read from Git objects, nets, licenses and SHA256 manifest. When an
accepted emoji pair is pinned in the lock, `--emoji-home "$TRAINING_SOURCE"` is
required and exports both assets from that immutable revision. Their hashes and
300-scalar inventory are checked; runtime selection stays optional. The final
`tools/accept_emoji.py --installed-assets` gate verifies and uses those installed
files without uploading weights or a manifest into the guest. The icon lock
requires `--icon-home`; the package verifies the immutable model and labelled
metadata and carries its derived167-scalar inventory. All asset hashes are
recorded in `deps.lock.json` and the payload manifest.
The session selects the locked text bundle; `NEURONTE_WEIGHTS` can explicitly
select a preserved baseline. Runtime dependencies are in `provision/dependencies.json`. No mutable host parser
installation is needed by an installed payload.

`provision/install.sh TARGET_ROOT USER` stages an add-on without restarting any
service. Existing selection is preserved; a new add-on defaults to X.
`PLEB_DISPLAY=neuronte` in `/etc/pleb/display.env` selects the service at boot;
`pleb.display=x` or `pleb.display=neuronte` overrides it for one boot. The generator
masks the display manager only in Neuronte mode. `PLEB_DESKTOP=1` in
`/etc/default/neuronte-display` selects the cell launcher. Optional colour uses
`NEURONTE_EMOJI_WEIGHTS`, `NEURONTE_EMOJI_MANIFEST` and `NEURONTE_THEME` there.
The accepted RC9 pair is installed as
`/opt/neuronte-display/weights/neuronte-emoji-v0.nrt` and
`/opt/neuronte-display/weights/emoji-manifest.json`; set those two variables to
these paths to select it. The application must still request mode2027/VS16 for
two-cell presentation. The bundled pair is pinned to training commit
`c1a182d53ff4ecfc61e1b9c3f8b3c5d57a0505a3`; its hashes are in the dependency lock.
Optional icons use `NEURONTE_ICON_WEIGHTS=/opt/neuronte-display/weights/neuronte-icon-v1.nrt`
and `NEURONTE_ICON_MANIFEST=/opt/neuronte-display/weights/icons.json`. The
accepted icon source commit is `faa4cb3ffab792886486bc07f924e037440fdf87`.
A second VT retains kernel-console recovery.

The installer builder derives from the existing Plebian-OS preseed without
editing it. The ordinary installer leaves disk selection interactive and adds
no SSH key/server. An explicitly selected `--acceptance-vm --ssh-public-key KEY`
variant restricts automatic partitioning to QEMU `/dev/vda` and uses key-only SSH
for disposable test guests. Both variants install into Neuronte mode and remove
external console/GRUB fonts only from the new target. The base ISO and donor disks
remain unchanged. Firmware and the Debian installer keep their own screens;
the strict neural-screen claim concerns the installed system. X can be selected
on an existing X-capable installation; a minimal fresh image does not contain X.

## Evidence and limits

`tools/accept_guest.py` drives an owned QEMU guest with external keyboard/tablet
events and compares its screenshots with `tools/reference.py`, which independently
reconstructs text, image planes and pointer from the reported grid using C
inference. `tools/boot_capture.py` samples power-on at a nominal 100 ms interval,
records actual timing, and classifies every distinct frame against independently
reconstructed grid history or black. `--snapshot`, `--snapshot-live` and
`--snapshot-history` provide atomic PPM/grid pairs and presented-frame history.

Acceptance must separately report empty font inventory, no other renderer,
unknown-scalar routing, full application/resize checks, boot frames, recovery and
latency. A first KMS screenshot alone does not satisfy the roadmap. Physical
hardware acceptance requires a dedicated target with its X default preserved;
QEMU results are not hardware results. Trained emoji and generic icons also have
separate adoption gates. Release reports record the actual completed gates and
remaining limitations rather than assuming these commands have passed.
