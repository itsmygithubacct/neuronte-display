"""Pure float32 geometry and compositing for learned colour layers.

RGB values are in byte-channel units (0..255), premultiplied by coverage.
The caller joins wide halves, selects the palette after reverse swapping,
supplies full-glyph decoration masks, and handles hidden before calling here.
These functions never change or retain writable views of their inputs.
"""
import operator

import numpy as np


_ZERO = np.float32(0)
_ONE = np.float32(1)
_HALF = np.float32(.5)
_BYTE = np.float32(255)


def _layer_arrays(masks, entries):
    masks = np.asarray(masks)
    entries = np.asarray(entries)
    if masks.dtype != np.float32 or masks.ndim != 3:
        raise ValueError('masks must be float32[K,H,W]')
    if masks.shape[1] < 1 or masks.shape[2] < 1:
        raise ValueError('glyph height and width must be positive')
    if not np.all(np.isfinite(masks)) or np.any(masks < _ZERO) or np.any(masks > _ONE):
        raise ValueError('coverage must be finite and within 0..1')
    if entries.dtype != np.uint8:
        raise ValueError('entries must be uint8[K] or uint8[K,H,W]')
    if entries.shape == (masks.shape[0],):
        entries = np.broadcast_to(entries[:, None, None], masks.shape)
    elif entries.shape != masks.shape:
        raise ValueError('entries must match the layers or the full mask shape')
    return masks, entries


def _rgb(packed):
    try:
        packed = operator.index(packed)
    except TypeError as error:
        raise ValueError('colour must be a packed 24-bit integer') from error
    if not 0 <= packed <= 0xffffff:
        raise ValueError('colour must be a packed 24-bit integer')
    return np.array([(packed >> shift) & 255 for shift in (16, 8, 0)], np.float32)


def transform_layers(masks, entries, cell_width, style):
    """Apply bold/italic to every layer over the complete one/two-cell glyph.

    Entries travel with the maximum coverage sample. Equal maxima choose the
    rightmost source x, including zero-coverage ties. Unmapped pixels are zero
    coverage with entry 0. Style 0 returns exact, independent copies.
    """
    masks, entries = _layer_arrays(masks, entries)
    try:
        cell_width = operator.index(cell_width)
        style = operator.index(style)
    except TypeError as error:
        raise ValueError('cell_width and style must be integers') from error
    _, height, width = masks.shape
    if cell_width < 1 or width not in (cell_width, 2 * cell_width):
        raise ValueError('glyph width must be one or two positive-width cells')
    if style & ~3:
        raise ValueError('style permits only bold (1) and italic (2)')
    if not style:
        return masks.copy(), entries.copy()

    radius = max(1, (cell_width + 16) // 32) if style & 1 else 0
    result = np.zeros_like(masks)
    result_entries = np.zeros(masks.shape, np.uint8)
    denominator = 80 * height
    for y in range(height):
        numerator = (27 * height - 40 * y - 20) * cell_width
        shift = ((numerator + denominator // 2) // denominator if numerator >= 0
                 else -((-numerator + denominator // 2) // denominator)) if style & 2 else 0
        # Visit smaller source x first, then replace ties with larger x. Each
        # offset lands directly in the final glyph; no intermediate crop can
        # discard a dilation sample that a negative shear brings back inside.
        for offset in range(radius, -1, -1):
            displacement = shift + offset
            left, right = max(0, displacement), min(width, width + displacement)
            if left >= right:
                continue
            source = slice(left - displacement, right - displacement)
            target = result[:, y, left:right]
            candidate = masks[:, y, source]
            replace = candidate >= target
            np.copyto(target, candidate, where=replace)
            np.copyto(result_entries[:, y, left:right], entries[:, y, source], where=replace)
    return result, result_entries


def compose_layers(masks, entries, palette, foreground, background, dim=False, decoration=None):
    """Composite paint/clear layers, then optional learned-line max-union.

    Entry 1 erases RGB and alpha; entry 0 and entries >=32 use foreground.
    Dim floors every paint colour's average with background before painting.
    Decorations use foreground, keep their supplied geometry, and replace
    covered artwork while retaining literal max-union alpha.
    """
    masks, entries = _layer_arrays(masks, entries)
    palette = np.asarray(palette)
    if palette.dtype != np.uint8 or palette.shape != (32, 3):
        raise ValueError('palette must be uint8[32,3]')
    fg, bg = _rgb(foreground), _rgb(background)
    inks = palette.astype(np.float32)
    inks[0] = fg
    if dim:
        inks = np.floor((inks + bg) * _HALF)
    height, width = masks.shape[1:]
    premultiplied = np.zeros((height, width, 3), np.float32)
    alpha = np.zeros((height, width), np.float32)
    for coverage, entry in zip(masks, entries):
        keep = _ONE - coverage
        # Clear contributes no paint at all, irrespective of palette[1].
        paint = np.where(entry != 1, coverage, _ZERO)
        index = np.where((entry >= 2) & (entry < 32), entry, 0)
        premultiplied = inks[index] * paint[..., None] + premultiplied * keep[..., None]
        alpha = paint + alpha * keep

    if decoration is not None:
        decoration = np.asarray(decoration)
        if decoration.dtype != np.uint8 or decoration.shape != (height, width):
            raise ValueError('decoration must be uint8[H,W] over the complete glyph')
        line = decoration.astype(np.float32) / _BYTE
        straight = np.zeros_like(premultiplied)
        np.divide(premultiplied, alpha[..., None], out=straight, where=alpha[..., None] > _ZERO)
        keep_art = np.maximum(alpha - line, _ZERO)
        premultiplied = straight * keep_art[..., None] + inks[0] * line[..., None]
        alpha = np.maximum(alpha, line)
    return premultiplied, alpha


def paint_opaque(premultiplied_rgb, alpha, background):
    """Paint on an opaque background with the C draw path's two byte rounds.

    Unpremultiplied source channels are rounded/clamped first, then blended
    using float32 alpha and rounded again. Alpha itself is not quantized here.
    """
    premultiplied_rgb, alpha = np.asarray(premultiplied_rgb), np.asarray(alpha)
    if (premultiplied_rgb.dtype != np.float32 or premultiplied_rgb.ndim != 3
            or premultiplied_rgb.shape[2] != 3 or alpha.dtype != np.float32
            or alpha.ndim != 2 or premultiplied_rgb.shape[:2] != alpha.shape
            or min(alpha.shape) < 1):
        raise ValueError('paint requires float32[H,W,3] RGB and float32[H,W] alpha')
    if (not np.all(np.isfinite(premultiplied_rgb)) or not np.all(np.isfinite(alpha))
            or np.any(alpha < _ZERO) or np.any(alpha > _ONE)):
        raise ValueError('paint values must be finite, with alpha within 0..1')
    bg = _rgb(background)
    straight = np.zeros_like(premultiplied_rgb)
    np.divide(premultiplied_rgb, alpha[..., None], out=straight, where=alpha[..., None] > _ZERO)
    source = np.floor(np.clip(straight, _ZERO, _BYTE) + _HALF)
    blended = source * alpha[..., None] + bg * (_ONE - alpha[..., None])
    return np.floor(blended + _HALF).astype(np.uint8)
