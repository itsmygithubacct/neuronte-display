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
Mouse input uses the parser's negotiated terminal protocol. The visible pointer
is the existing U+2196 symbol net, with no new generic icon bundle activated.

Masks are sampled twice per axis, rounded to eight bits and cached by scalar,
style, weights/library hashes and geometry. Persistent entries have checksums
and atomic replacement. Bold uses an SDF offset; italic uses a shear. Reverse,
dim, underline, strike and overline remain supported. Block cursors invert
colours; cursor blinking and distinct bar/underline cursors are not implemented.

Full graphemes up to 64 scalars are preserved in grid records. NFC-composable
clusters render their composed scalar. Unsupported multiscalar shaping and
oversized clusters render U+FFFD and are counted explicitly. There is no claim
of Arabic/Indic shaping, combining-mark placement, ZWJ emoji, modifiers or flags.
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
wide cells select the colour bundle. VS16 can request emoji width; VS15 keeps
text presentation. ZWJ, skin-tone and flag sequences use the documented complex
cluster fallback. Each half calls `neuronte_draw_half` independently; layered
colours and both theme palettes are retained. `--theme` accepts `auto`, `dark`
and `light`; auto uses cell-background luminance. The bounded cache includes
both halves, palette, colours and geometry. The analytic fixture checks unequal
halves and both palettes; trained-bundle acceptance is separate evidence.

`python3 cell_desktop.py` opens a keyboard-driven Terminal/Files/System launcher.
It is a small character-cell provider, not a complete Kilix desktop replacement.
Generic icon adoption remains held for coordinator approval.

## RC9 package and installer

```sh
python3 tools/package.py --neuronte-home "$NEURONTE_SOURCE" \
  --video-home "$VIDEO_SOURCE" --ghostty "$BUILD_ROOT/ghostty" \
  --weights "$APPROVED_SPLIT_V5" \
  --output "$BUILD_ROOT/display.tar.gz"
python3 tools/build_installer.py --source-iso "$DEBIAN_ISO" \
  --os-source "$PLEBIAN_SOURCE" --payload "$BUILD_ROOT/display.tar.gz" \
  --output "$BUILD_ROOT/neuronte-rc9.iso"
```

The package includes the parser shared library, patches, rebuild recipe, pinned
video source read from Git objects, nets, licenses and SHA256 manifest. Optional
`--emoji-weights` and `--emoji-manifest` package both assets without enabling them.
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
