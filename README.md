# neuronte-display

A build-machine terminal whose text and tab/split chrome are drawn with Neuronte
coverage masks. Stage 1 includes a PTY per pane, the pinned libghostty-vt parser,
an in-memory mask cache with persistence across restarts, changed-cell rendering,
kitty-framebuffer presentation, and PPM snapshots with the complete cell grid.
It uses no font files. The terminal font remains `neuronte-split-v4.nrt`.

This is the stage-1 display program. QEMU/KMS, boot and installer integration,
video, colour-icon adoption, native Kilix remote control and mouse interaction
belong to later stages. Its tab/split surface implements the controls below;
it does not claim the full Kilix window-management feature set.

## Build and run

Dependencies: a C11 compiler, Python 3 with NumPy, zlib, pthreads, built Neuronte,
the sibling MIT `kitty-framebuffer` module, and libghostty-vt at the revision in
`deps.lock.json`. The default parser prefix is Batty's existing pinned build.
`GHOSTTY_PREFIX` can select another installation with the same `revision` file.
No dependency is downloaded by the build, and no training or GPU is required.

```sh
make
make test
python3 neuronte_display.py --snapshot build/session.ppm
python3 neuronte_display.py --headless-seconds 2 --snapshot build/smoke.ppm -- \
    /bin/sh -c 'printf "Hello from Neuronte\\n"'
```

`NEURONTE_HOME` or `--home` selects the source/weight tree. `--weights` and
`--library` override individual assets. `--cache` selects mask storage; the
default is `$XDG_CACHE_HOME/neuronte-display` or `~/.cache/neuronte-display`.
Keep all active source, builds, cache, logs and snapshots on local fast storage.

## Controls

The prefix is **Ctrl-A**. Press it, then one key:

| Key | Action |
|---|---|
| `n` | New tab with a shell |
| `s`, `h` | Add a vertical or horizontal split, up to four panes |
| `o` | Focus the next pane |
| `[`, `]` | Previous or next tab |
| `+`, `-` | Move the first split boundary by one cell |
| `x` | Close the focused pane |
| `q` | Quit and restore terminal modes |
| Ctrl-A again | Send a literal Ctrl-A to the child |

Tabs have independent PTYs and output continues while hidden. Pane changes
resize both parser state and the PTY, delivering SIGWINCH through the kernel.
The outer display uses the chosen `--cols`/`--rows` geometry; stage 1 does not
automatically change its grid when the hosting terminal window changes size.

## Rendering and persistence

Every scalar mask comes from `neuronte_coverage`, with two samples per axis.
The cache stores coverage rounded to eight bits, keyed by weight and library
content hashes, cache version, cell geometry and sampling/quantization settings.
Colours are applied later and never baked into masks. Entries carry a payload
checksum; corrupt, truncated or stale entries are regenerated. Writes use a
temporary file, fsync and atomic replacement. The memory cache is bounded.

Reverse and the block cursor swap colours. Dim and bold adjust brightness;
italic is retained in grid metadata and currently displayed upright. Underline,
strike and overline union masks from net-drawn line/block characters. NFC
composition handles common combining sequences. Unsupported longer graphemes
are counted in snapshot metadata; their base scalar (or replacement mark for
more than eight code points) is shown. A wide scalar reserves two cells and
shows its mark in the first, with no fabricated CJK/emoji glyph. Cursor blinking,
mouse reporting and picture protocols are outside this stage's implementation.

Each `.ppm` has a `.json` peer containing geometry, every cell's scalar/colours/
attributes, cache identity and timing counters. Cold inference, warm drawing and
restart-from-disk can therefore be compared without terminal screenshots. The
tests also compare composites directly with the C library (at most one channel
level from eight-bit mask quantization), exercise parser replies and alternate
screens, and drive real PTYs through focus, tabs, splits and resize.

The parser and framebuffer retain their own MIT licences. `deps.lock.json`
records the approved upstream parser revision; the local Batty build may carry
its documented graphics patches, which this text-only bridge does not call.
