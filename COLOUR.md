# RC9 display colour and cursor contract

Colour assets are explicit selections. `--emoji-weights` and its manifest
select only manifest scalars with two parser-reserved cells; mode2027 plus
VS16 covers all300 accepted emoji. `--icon-weights` and its hash-bound
`--icon-manifest` select the167 labelled single-cell icon-v1 scalars. Unknown
scalars and compounds keep the text/grapheme fallback. No `route()` result
is treated as proof of training. These options do not activate icons in
existing applications or replace window-manager images.

The package locks emoji to training c1a182d53ff4ecfc61e1b9c3f8b3c5d57a0505a3
and icons to faa4cb3ffab792886486bc07f924e037440fdf87. It verifies their Git
object hashes. `icons.json` is derived from all167 labels in that commit's
model metadata; the metadata hash and derivation are checked when packaging.
Accepted model bytes, snap policy and quality gates remain unchanged.

## Layer geometry and paint

Unstyled colour still uses the original C scalar/half drawing and composite
alpha APIs, at2×2 samples. The C styled API intentionally ignores colour
styles. This display supplies the original colour-plan behavior using C
layer coverage and the following raster operations; it does not modify the
C library or claim the earlier proposed SDF-offset approach was implemented.

Bold takes each layer's rightward maximum, radius `max(1,(cw+16)//32)`.
Italic shifts each row by the nearest integer, ties away from zero, of
`(27*ch-40*y-20)*cw/(80*ch)`. For their combination, the output reads the
original row at `x-shift-offset` for every bold offset; there is no clipped
intermediate. Both halves of a wide glyph are joined before this operation.
The terminal cell width controls stroke/slant for consistent neighbouring
text; clipping happens only at the full glyph's outer boundaries. Each
layer, including clear layers, receives exactly this same transform.
If a valid custom half bundle uses different palette entries across the
seam, the maximum carries its source entry; equal maxima select the
rightmost source. Accepted emoji have matching layer entries in both halves.

Composition proceeds back to front in float32 premultiplied colour and
alpha. Entry1 multiplies both existing colour and alpha by`1-coverage`.
Entry0 (or an out-of-palette entry) paints the caller's foreground; entries
2–31 paint the chosen palette colour. Paint uses source-over for colour
and alpha. Fixed colours are never arbitrarily inverted. Reverse swaps
foreground/background, then automatic palette selection uses that background.
Dim averages **every paint colour**, including fixed palette colours, with
the effective background, flooring each channel; alpha is unchanged.

Underline U+2581, strike U+2500 and overline U+2594 use actual text-net
coverage, repeated across both reserved cells. Their maximum is the line
mask. Lines replace covered artwork with effective foreground and form a
literal maximum union with its alpha: for line coverage`l` and artwork
alpha`a`, retained artwork is`max(a-l,0)`, and final alpha is`max(a,l)`.
This preserves the original monochrome union contract while giving lines
a visible foreground colour. Dim also affects the line foreground.
Hidden suppresses all artwork and decorations and produces zero ink alpha.
Negative-z image occlusion uses this same resulting alpha, not a union of
uncomposited layers. Opaque painting retains C's two rounding steps: round
unpremultiplied source RGB, then blend over the background and round again.

## Cursor and pointer

Ghostty's steady block, underline and bar shapes are exported by a new
cursor-style getter without changing the existing two-int snapshot ABI.
Block swaps foreground/background for text cells. A2048 record flag marks
that cursor operation; colour rendering removes it before palette selection,
so both halves retain the one palette selected from the application's SGR
foreground/background, including SGR reverse. The colour cursor is a separate
post-image contrast overlay: learned U+2588 for block, U+258F for bar and
U+2581 for underline. Under its learned mask, light pixels become black and
dark pixels white (luma299/587/114 threshold127.5). This is visible even on
opaque fixed-colour artwork; it does not change weights, the underlying
artwork palette or its alpha. Text bar/underline use the effective foreground.
Hiding/moving restores the retained underlying frame. Cursor blink timing is
not added. The snapshot records cursor shape, position and colour-overlay mode
separately from the application grid.

Opt-in icon mode uses the **labelled U+E0B2 left triangle** as its pointer.
Its hotspot is`(0,floor(ch/2))`; input reports retain the original physical
coordinates while the artwork origin is offset by that hotspot. All four
screen edges clip the learned artwork. Entry0 chooses black over each light
underlying pixel and white over each dark pixel (luma299/587/114 threshold
127.5), preserving C-derived coverage and clear alpha. Automatic palette
selection follows the underlying hotspot pixel. This also contrasts over
light/dark images. No untrained generic mouse-pointer scalar is assumed.

Runtime selection via the installed session uses `NEURONTE_EMOJI_WEIGHTS`,
`NEURONTE_EMOJI_MANIFEST`, `NEURONTE_ICON_WEIGHTS`, `NEURONTE_ICON_MANIFEST`
and `NEURONTE_THEME=auto|dark|light` in`/etc/default/neuronte-display`.
The payload carries both accepted assets; selection is optional. The
independent reference uses separate scalar loops and direct C inference,
never importing the runtime layer transform or renderer.
