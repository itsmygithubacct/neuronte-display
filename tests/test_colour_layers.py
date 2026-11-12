"""Analytic colour math tests, independent of C, models and renderer wrappers."""
from fractions import Fraction
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display_colour import compose_layers, paint_opaque, transform_layers


def scatter_reference(masks, entries, cell_width, style):
    """Independent source-pixel scatter with exact rational row positions."""
    layers, height, width = masks.shape
    output = np.zeros_like(masks)
    colours = np.zeros(masks.shape, np.uint8)
    winner = np.full(masks.shape, -1, np.int64)
    stroke = max(1, (cell_width + 16) // 32) if style & 1 else 0
    for layer in range(layers):
        for y in range(height):
            position = cell_width * (Fraction(27, 80) - Fraction(2 * y + 1, 4 * height))
            shift = (math.floor(position + Fraction(1, 2)) if position >= 0
                     else math.ceil(position - Fraction(1, 2))) if style & 2 else 0
            for x in range(width):
                value = masks[layer, y, x]
                entry = entries[layer] if entries.ndim == 1 else entries[layer, y, x]
                for extra in range(stroke + 1):
                    dest = x + shift + extra
                    if not 0 <= dest < width:
                        continue
                    if (value > output[layer, y, dest]
                            or (value == output[layer, y, dest] and x > winner[layer, y, dest])):
                        output[layer, y, dest] = value
                        colours[layer, y, dest] = entry
                        winner[layer, y, dest] = x
    return output, colours


class LayerGeometryTests(unittest.TestCase):
    def test_style_zero_exact_copies_for_readonly_strided_inputs(self):
        masks = np.array([[[0, 1, -0., .5, .125, .75, .25, .875]]], np.float32)[:, :, ::-1]
        entries = np.array([1], np.uint8)
        masks.setflags(write=False)
        entries.setflags(write=False)
        actual, colours = transform_layers(masks, entries, 4, 0)
        np.testing.assert_array_equal(actual.view(np.uint32), masks.view(np.uint32))
        np.testing.assert_array_equal(colours, np.ones(masks.shape, np.uint8))
        self.assertEqual(actual.dtype, np.float32)
        self.assertEqual(colours.dtype, np.uint8)
        self.assertFalse(np.shares_memory(actual, masks))
        self.assertFalse(np.shares_memory(colours, entries))
        mapped = np.arange(8, dtype=np.uint8).reshape(1, 1, 8)
        _, copied = transform_layers(masks, mapped, 4, 0)
        copied[:] = 0
        np.testing.assert_array_equal(mapped, np.arange(8, dtype=np.uint8).reshape(1, 1, 8))

    def test_all_styles_match_independent_scatter_on_one_and_two_cells(self):
        rng = np.random.default_rng(531)
        for cell_width, height in ((1, 1), (4, 8), (8, 16), (48, 5), (80, 8)):
            for cells in (1, 2):
                masks = rng.integers(0, 5, (3, height, cells * cell_width)).astype(np.float32) / np.float32(4)
                mapped = rng.integers(0, 256, masks.shape, dtype=np.uint8)
                for entries in (np.array([0, 1, 31], np.uint8), mapped):
                    before_masks, before_entries = masks.copy(), entries.copy()
                    masks.setflags(write=False)
                    entries.setflags(write=False)
                    for style in range(4):
                        with self.subTest(cell=cell_width, height=height, cells=cells, style=style, mapped=entries.ndim == 3):
                            expected = scatter_reference(masks, entries, cell_width, style)
                            actual = transform_layers(masks, entries, cell_width, style)
                            for value, wanted in zip(actual, expected):
                                np.testing.assert_array_equal(value, wanted)
                    np.testing.assert_array_equal(masks, before_masks)
                    np.testing.assert_array_equal(entries, before_entries)

    def test_bold_crosses_wide_seam_with_source_colour_and_no_wrap(self):
        masks = np.zeros((1, 1, 16), np.float32)
        masks[0, 0, [7, 8, 15]] = [.75, .25, 1]
        entries = np.full(masks.shape, 2, np.uint8)
        entries[:, :, 8:] = 3
        actual, colours = transform_layers(masks, entries, 8, 1)
        expected = np.zeros_like(masks)
        expected[0, 0, [7, 8, 9, 15]] = [.75, .75, .25, 1]
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(colours[0, 0, [7, 8, 9, 15]], [2, 2, 3, 3])

    def test_italic_crosses_seam_in_both_directions(self):
        masks = np.zeros((1, 16, 16), np.float32)
        masks[0, 0, 7] = .5
        masks[0, 15, 8] = .75
        entries = np.full(masks.shape, 2, np.uint8)
        entries[:, :, 8:] = 3
        actual, colours = transform_layers(masks, entries, 8, 2)
        expected = np.zeros_like(masks)
        expected[0, 0, 10] = .5
        expected[0, 15, 7] = .75
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(colours[0, 0, 10], 2)
        self.assertEqual(colours[0, 15, 7], 3)

    def test_combined_style_clips_only_after_dilation_and_shear(self):
        masks = np.zeros((2, 16, 16), np.float32)
        masks[:, 15, 15] = .75
        masks[:, 15, 0] = .25
        masks[:, 0, 12] = .5
        masks[:, 0, 15] = 1
        actual, colours = transform_layers(masks, np.array([2, 1], np.uint8), 8, 3)
        expected = np.zeros_like(masks)
        expected[:, 15, 14:16] = .75
        expected[:, 15, 0] = .25
        expected[:, 0, 15] = .5
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual[0], actual[1])
        np.testing.assert_array_equal(colours[:, 15, 15], [2, 1])

    def test_per_terminal_cell_bold_radius_includes_every_offset(self):
        for cell_width, radius in ((4, 1), (47, 1), (48, 2), (80, 3)):
            with self.subTest(cell_width=cell_width):
                masks = np.zeros((1, 1, 2 * cell_width), np.float32)
                masks[0, 0, cell_width - 1] = .625
                actual, _ = transform_layers(masks, np.array([2], np.uint8), cell_width, 1)
                expected = np.zeros_like(masks)
                expected[0, 0, cell_width - 1:cell_width + radius] = .625
                np.testing.assert_array_equal(actual, expected)

    def test_italic_half_pixel_ties_round_away_from_zero(self):
        masks = np.zeros((1, 8, 80), np.float32)
        masks[0, :, 40] = 1
        actual, _ = transform_layers(masks, np.array([2], np.uint8), 80, 2)
        expected = np.zeros_like(masks)
        for y, x in enumerate([65, 60, 55, 50, 45, 39, 34, 29]):
            expected[0, y, x] = 1
        np.testing.assert_array_equal(actual, expected)

    def test_equal_maxima_choose_rightmost_source_even_for_clear_and_zero(self):
        masks = np.array([[[0, .5, .5, 0, .75, .75, 0, 0]]], np.float32)
        entries = np.array([[[2, 3, 1, 7, 30, 32, 0, 255]]], np.uint8)
        actual, colours = transform_layers(masks, entries, 4, 1)
        np.testing.assert_array_equal(actual, [[[0, .5, .5, .5, .75, .75, .75, 0]]])
        np.testing.assert_array_equal(colours, [[[2, 3, 1, 1, 30, 32, 32, 255]]])


class LayerCompositionTests(unittest.TestCase):
    def palette(self):
        palette = np.full((32, 3), 253, np.uint8)
        palette[2] = [0, 80, 200]
        palette[31] = [9, 19, 29]
        return palette

    def test_foreground_fixed_and_clear_known_fractions_in_two_palettes(self):
        masks = np.array([[[.5, .5, .5]], [[.25, .25, .25]], [[.5, 1, 0]]], np.float32)
        entries = np.array([0, 2, 1], np.uint8)
        for fixed, expected in (([0, 80, 200], [37.5, 28.75, 32.5]),
                                ([160, 40, 0], [57.5, 23.75, 7.5])):
            palette = self.palette()
            palette[2] = fixed
            with self.subTest(palette=fixed):
                rgb, alpha = compose_layers(masks, entries, palette, 0xc86428, 0x123456)
                np.testing.assert_array_equal(rgb, [[expected, [0, 0, 0], [2 * v for v in expected]]])
                np.testing.assert_array_equal(alpha, [[.3125, 0, .625]])
                self.assertEqual(rgb.dtype, np.float32)
                self.assertEqual(alpha.dtype, np.float32)
                # Clear erases artwork; it does not paint a background colour.
                other = compose_layers(masks, entries, palette, 0xc86428, 0xfedcba)
                for a, b in zip((rgb, alpha), other):
                    np.testing.assert_array_equal(a, b)

    def test_paint_after_clear_obeys_layer_order(self):
        palette = self.palette()
        palette[2] = [200, 100, 50]
        rgb, alpha = compose_layers(np.array([[[1]], [[.5]], [[.5]]], np.float32),
                                    np.array([2, 1, 0], np.uint8), palette, 0x001428, 0xffffff)
        np.testing.assert_array_equal(rgb, [[[50, 35, 32.5]]])
        np.testing.assert_array_equal(alpha, [[.75]])

    def test_entry_map_foreground_fallback_and_palette_boundaries(self):
        masks = np.ones((1, 1, 6), np.float32)
        entries = np.array([[[0, 1, 2, 31, 32, 255]]], np.uint8)
        rgb, alpha = compose_layers(masks, entries, self.palette(), 0x1163c9, 0x102030)
        np.testing.assert_array_equal(rgb, [[[17, 99, 201], [0, 0, 0], [0, 80, 200],
                                             [9, 19, 29], [17, 99, 201], [17, 99, 201]]])
        np.testing.assert_array_equal(alpha, [[1, 0, 1, 1, 1, 1]])

    def test_dim_floors_every_paint_colour_before_compositing_without_alpha_change(self):
        palette = self.palette()
        palette[2] = [101, 151, 201]
        masks = np.array([[[.5]], [[.25]], [[.5]]], np.float32)
        entries = np.array([2, 0, 1], np.uint8)
        rgb, alpha = compose_layers(masks, entries, palette, 0x0564fb, 0x0a141e, dim=True)
        # Dimmed fixed ink is (55,85,115), FG is (7,60,140), before coverage.
        np.testing.assert_array_equal(rgb, [[[11.1875, 23.4375, 39.0625]]])
        np.testing.assert_array_equal(alpha, [[.3125]])
        _, original_alpha = compose_layers(masks, entries, palette, 0x0564fb, 0x0a141e)
        np.testing.assert_array_equal(alpha, original_alpha)

    def test_decoration_replaces_visible_ink_with_literal_union_coverage(self):
        palette = self.palette()
        palette[2] = [255, 0, 0]
        masks = np.array([[[.75, .25, 0, 0]]], np.float32)
        decoration = np.array([[128, 128, 128, 0]], np.uint8)
        rgb, alpha = compose_layers(masks, np.array([2], np.uint8), palette,
                                    0x0000ff, 0xffffff, decoration=decoration)
        np.testing.assert_allclose(rgb, [[[63.25, 0, 128], [0, 0, 128],
                                         [0, 0, 128], [0, 0, 0]]], rtol=0, atol=8e-6)
        np.testing.assert_array_equal(alpha, np.array([[.75, 128 / 255, 128 / 255, 0]], np.float32))
        self.assertTrue(np.all(np.isfinite(rgb)))

    def test_decoration_can_fill_a_clear_hole_and_is_dimmed_as_foreground(self):
        masks = np.ones((2, 1, 2), np.float32)
        entries = np.array([2, 1], np.uint8)
        decoration = np.array([[255, 0]], np.uint8)
        rgb, alpha = compose_layers(masks, entries, self.palette(), 0x0564fb, 0x0a141e,
                                    dim=True, decoration=decoration)
        np.testing.assert_array_equal(rgb, [[[7, 60, 140], [0, 0, 0]]])
        np.testing.assert_array_equal(alpha, [[1, 0]])

    def test_wide_decorations_keep_supplied_shape_after_artwork_styles(self):
        masks = np.zeros((1, 16, 16), np.float32)
        masks[0, 0, 7] = 1
        decoration = np.zeros((16, 16), np.uint8)
        decoration[12, :] = 255
        palette = self.palette()
        palette[2] = [255, 0, 0]
        for style, positions in ((0, [7]), (1, [7, 8]), (2, [10]), (3, [10, 11])):
            with self.subTest(style=style):
                styled, entries = transform_layers(masks, np.array([2], np.uint8), 8, style)
                rgb, alpha = compose_layers(styled, entries, palette, 0x00ff00, 0, decoration=decoration)
                expected_rgb = np.zeros((16, 16, 3), np.float32)
                expected_rgb[0, positions, 0] = 255
                expected_rgb[12, :, 1] = 255
                expected_alpha = np.zeros((16, 16), np.float32)
                expected_alpha[0, positions] = 1
                expected_alpha[12, :] = 1
                np.testing.assert_array_equal(rgb, expected_rgb)
                np.testing.assert_array_equal(alpha, expected_alpha)

    def test_readonly_inputs_not_mutated_and_entry_forms_equivalent(self):
        masks = np.array([[[.5, .75]], [[.25, .5]], [[.125, .25]]], np.float32)[:, :, ::-1]
        entries = np.array([0, 2, 1], np.uint8)
        palette = self.palette()
        decoration = np.array([[32, 255]], np.uint8)
        arrays = masks, entries, palette, decoration
        copies = [a.copy() for a in arrays]
        for a in arrays:
            a.setflags(write=False)
        first = compose_layers(masks, entries, palette, 0x123456, 0x987654, True, decoration)
        mapped = np.broadcast_to(entries[:, None, None], masks.shape)
        second = compose_layers(masks, mapped, palette, 0x123456, 0x987654, True, decoration)
        for a, b in zip(first, second):
            np.testing.assert_array_equal(a, b)
            self.assertEqual(a.dtype, np.float32)
        for a, b in zip(arrays, copies):
            np.testing.assert_array_equal(a, b)

    def test_empty_artwork_supports_transparency_and_lines(self):
        masks = np.empty((0, 1, 2), np.float32)
        entries = np.empty(0, np.uint8)
        rgb, alpha = compose_layers(masks, entries, self.palette(), 0xffffff, 0x123456)
        np.testing.assert_array_equal(rgb, np.zeros((1, 2, 3), np.float32))
        np.testing.assert_array_equal(alpha, [[0, 0]])
        rgb, alpha = compose_layers(masks, entries, self.palette(), 0xffffff, 0,
                                    decoration=np.array([[0, 127]], np.uint8))
        np.testing.assert_array_equal(rgb, [[[0, 0, 0], [127, 127, 127]]])
        np.testing.assert_array_equal(alpha, np.array([[0, 127 / 255]], np.float32))


class OpaquePaintingTests(unittest.TestCase):
    def test_round_source_channels_before_blend_and_round_again(self):
        rgb = np.array([[[5.25, 10.25, .25]]], np.float32)
        alpha = np.array([[.5]], np.float32)
        np.testing.assert_array_equal(paint_opaque(rgb, alpha, 0), [[[6, 11, 1]]])
        np.testing.assert_array_equal(paint_opaque(rgb, alpha, 0x0a141e), [[[11, 21, 16]]])

    def test_blend_rounding_is_float32_not_float64(self):
        # float32(.7) is below .7. Float32 multiplication rounds these products
        # back to half-integers; float64 intermediates would return 3/10/17.
        rgb = np.array([[[3.5, 10.5, 17.5]]], np.float32)
        alpha = np.array([[.7]], np.float32)
        np.testing.assert_array_equal(paint_opaque(rgb, alpha, 0), [[[4, 11, 18]]])

    def test_zero_safe_clamping_ties_and_nonmutating_readonly_inputs(self):
        rgb = np.array([[[200, 100, 50], [-1, 255.9, 127.5]]], np.float32)
        alpha = np.array([[0, 1]], np.float32)
        before_rgb, before_alpha = rgb.copy(), alpha.copy()
        rgb.setflags(write=False)
        alpha.setflags(write=False)
        with np.errstate(divide='raise', invalid='raise'):
            result = paint_opaque(rgb, alpha, 0x123456)
        np.testing.assert_array_equal(result, [[[18, 52, 86], [0, 255, 128]]])
        self.assertEqual(result.dtype, np.uint8)
        np.testing.assert_array_equal(rgb, before_rgb)
        np.testing.assert_array_equal(alpha, before_alpha)


class ColourValidationTests(unittest.TestCase):
    def test_reject_invalid_dimensions_entries_and_style(self):
        masks = np.zeros((1, 2, 4), np.float32)
        entries = np.array([2], np.uint8)
        for width, style in ((0, 0), (3, 0), (4.0, 0), (4, -1), (4, 4), (4, 64), (4, 1.5)):
            with self.subTest(width=width, style=style), self.assertRaises(ValueError):
                transform_layers(masks, entries, width, style)
        for bad_masks in (masks[0], masks.astype(np.float64), np.zeros((1, 0, 4), np.float32),
                          np.full_like(masks, np.nan), np.full_like(masks, np.inf),
                          np.full_like(masks, -.1), np.full_like(masks, 1.1)):
            with self.subTest(shape=bad_masks.shape, dtype=bad_masks.dtype), self.assertRaises(ValueError):
                transform_layers(bad_masks, entries, 4, 0)
        for bad_entries in (np.array([2, 3], np.uint8), np.zeros((1, 2, 2), np.uint8),
                            entries.astype(np.int64)):
            with self.subTest(shape=bad_entries.shape), self.assertRaises(ValueError):
                transform_layers(masks, bad_entries, 4, 0)

    def test_reject_bad_palette_decoration_and_packed_colours(self):
        masks = np.zeros((1, 2, 4), np.float32)
        entries = np.array([2], np.uint8)
        palette = np.zeros((32, 3), np.uint8)
        for bad in (palette[:31], palette.astype(np.float32)):
            with self.assertRaises(ValueError):
                compose_layers(masks, entries, bad, 0, 0)
        for bad in (np.zeros((2, 2), np.uint8), np.zeros((2, 4), np.float32)):
            with self.assertRaises(ValueError):
                compose_layers(masks, entries, palette, 0, 0, decoration=bad)
        for bad in (-1, 0x1000000, 1.5):
            for fg, bg in ((bad, 0), (0, bad)):
                with self.assertRaises(ValueError):
                    compose_layers(masks, entries, palette, fg, bg)

    def test_reject_invalid_paint_shapes_dtypes_and_alpha(self):
        rgb = np.zeros((2, 4, 3), np.float32)
        alpha = np.ones((2, 4), np.float32)
        for bad_rgb, bad_alpha in ((rgb[:, :, :2], alpha), (rgb, alpha[0]),
                                   (rgb.astype(np.float64), alpha), (rgb, alpha.astype(np.float64)),
                                   (rgb, np.full_like(alpha, 1.1)), (rgb, np.full_like(alpha, -.1)),
                                   (rgb, np.full_like(alpha, np.nan)), (np.full_like(rgb, np.inf), alpha)):
            with self.assertRaises(ValueError):
                paint_opaque(bad_rgb, bad_alpha, 0)


if __name__ == '__main__':
    unittest.main()
