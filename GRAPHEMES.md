# Net-derived combining marks

This is a bounded, synthetic raster compositor for the sixteen existing
`font/marks.txt` designs. It uses learned coverage only; it does not load those
stroke drawings or a font at runtime. [graphemes.json](graphemes.json) is the
capability and placement policy, version1. Its SHA is recorded in every new
snapshot. Snapshots without that field retain the old NFC/replacement oracle.

Composition is enabled only for the exact approved split-v4 and split-v5 hashes
in the policy. Their explicitly trained label inventories differ: Greek and
Russian bases are available only with v5. `route()` is never an availability
query. Unknown/custom bundles retain their original NFC/scalar/replacement
behavior and do not gain an assumed component inventory.

An available trained NFC singleton wins unchanged: decomposed e-acute still
uses the existing U+00E9 mask, and Russian e-diaeresis uses U+0451 on v5.
Otherwise canonical NFD supplies a trained letter plus supported marks. This
also applies to untrained precomposed input: U+1E3F and `m + U+0301` compose
identically, as do U+03AC and `alpha + U+0301`. Compatibility folding is not used.
The original parser records, cell widths, cursor advances and mode2027 default
are preserved.

| Combining mark | Learned component | Placement/derivation |
|---|---|---|
| 0300 grave | 00B4 acute | Horizontal reflection; the source grave strokes are that reflection |
| 0301 acute | 00B4 | Above |
| 0302 circumflex | 02C6 | Above |
| 0303 tilde | 02DC | Above |
| 0304 macron | 00AF | Above |
| 0306 breve | 02D8 | Above |
| 0307 dot | 02D9 | Above |
| 0308 diaeresis | 00A8 | Above |
| 030A ring | 02DA | Above |
| 030B double acute | 02DD | Above |
| 030C caron | 02C7 | Above; contextual beside form for d/l/t/L |
| 0312 turned comma | 002C comma | Rotate180°, then fit source-derived above-mark ink bounds |
| 0315 comma above right | 002C comma | Fit source-derived bounds, anchor beside the upper stem |
| 0326 comma below | 002C comma | Fit source-derived below-mark ink bounds |
| 0327 cedilla | 00B8 | Below; contextual comma/turned-comma forms as in the source |
| 0328 ogonek | 02DB | Below, anchored at the rightmost bottom ink |

The comma substitutions are disclosed synthetic derivations, not separately
trained combining glyphs or a claim of identical target outlines. Their
bounding boxes come from the existing source strokes plus its0.75-grid-unit
pen radius. For example0312 has stroke bounds x5..5.8,y3.3..5.5, hence fitted
ink bounds x4.25..6.55,y2.55..6.25 on the10×20 grid. The similarly recorded0315
and0326 bounds use the same rule. No subtraction of unrelated glyph masks is
used. Fitting takes the greatest coverage of overlapping source pixels;
it never invents a coverage value or reconstructs a vector outline.

## Geometry

The source grid has cap top4, x-height8, baseline15, shortened top5.8 and
capital-mark raise2.3. Coordinates scale by cell width/10 and height/20;
integer ties round away from zero. Learned-ink bounds use nonzero quantized
coverage, preserving faint edge samples.

Above marks align with the base's learned horizontal ink bounds, with the
source's explicit dotless-i, j, J and l centres. A capital's upper body is
fitted down to the shortened top while retaining its baseline; a lowercase
ascender is fitted only above the source x-height. Marks above capitals move
up by2.3 source units. Above marks on i use the trained dotless i0131. On j,
the cutoff is an integer pixel boundary inside the verified source gap
y6.05..7.25 (including pen). If no boundary fits, dot and body may share a
raster row, so this composition is unavailable: this affects heights8,10,11.
At heights9,12,14,16,32 the cutoffs are respectively3,4,5,5,10 rows. No
untrained dotless-j0237 is inferred, and the cutoff never retains a pixel
overlapping the source dot merely to claim small-cell support.

Below marks use the centre of the lowest learned body ink; ogonek uses its
rightmost point. The body is fitted above the source baseline when needed,
so a new below mark does not collide with a full descender. This is a synthetic
fixed-cell fit based on learned ink bounds: it also fits baseline antialiasing
that extends beyond that boundary on non-descenders such as x. The original
trained precomposed glyphs are unchanged. Beside
marks use source d/l/t/L anchors or x8.5 for other bases. Cedilla becomes a
comma for G/K/L/N/R/k/n/l/r and a turned comma above for g, as in the original
composition source.

Up to four marks total and two per anchor group are supported. Canonical order
is retained; the first mark is closest to the base and the second stacks
outward. Two above marks occupy a band ending at round(0.40×height), separated
by a pixel when space permits; the learned base fits beneath it. Two below
marks divide the band from round(0.72×height) to the bottom. Beside marks use
two slots of round(0.20×height). Components retain their anchor and horizontal
shape; height is reduced by maximum pooling when needed. These fixed-cell
stacks can be coarse at small sizes and are not a typography-quality claim.

The minimum composition cell is4×8. Unsupported marks/bases, excessive stacks,
empty learned components or a body that cannot fit render one whole-cluster
replacement and are counted as unsupported. No mark is silently discarded to
claim success. All results are clipped to the original parser-owned cell.
Arabic/Indic shaping and ZWJ/modifier/flag sequences remain unsupported.

The union mask receives the same right-dilation and integer row-shear operators
as scalar styling, then decorations and visibility. Negative-z graphics use
that complete mask as text ink. The in-memory composition cache is bounded512
and belongs to one asset/library/geometry/policy instance, keyed by canonical
components and style; persistent scalar masks are unchanged.

`tools/reference_graphemes.py` independently implements the policy using its
own C mask queries and pixel loops. It imports no production planner, cache,
placement or style helpers. Tests additionally require manually specified
pixels for translation, capital room, reflected acute, rotated comma and
whole-glyph styles; renderer/reference agreement alone is insufficient.
