"""Cached content must equal fresh rendering after every dependency change."""
import ctypes as C
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import neuronte_display as d
from display_emoji import Emoji
from display_icons import Icons
from display_graphics import Graphics, PointerFrame
from tools.reference import reconstruct

HOME = Path(os.environ.get('NEURONTE_HOME', Path.home()/'projects/neuronte'))
TRAIN = Path(os.environ.get('NEURONTE_TRAINING_HOME', Path.home()/'projects/neuronte-roadmap-training'))
VIDEO = Path(os.environ.get('NEURONTE_VIDEO_HOME', HOME))
LIBRARY = HOME/'build/libneuronte.so'
TEXT = HOME/'weights/neuronte-split-v4.nrt'


class SnapshotCacheTests(unittest.TestCase):
    def test_failed_initial_snapshot_does_not_publish_a_cache(self):
        terminal = d.Terminal(20, 6)
        self.addCleanup(terminal.close)
        terminal.feed(b'recovery')
        with patch.object(terminal.lib, 'nd_snapshot', return_value=-1):
            with self.assertRaisesRegex(RuntimeError, 'complete terminal grid'): terminal.snapshot()
        self.assertEqual(''.join(chr(r[0]) for r in terminal.snapshot()[0][:8]), 'recovery')

    def test_sparse_native_decode_matches_uncached_records_and_cursor(self):
        terminal = d.Terminal(20, 6)
        self.addCleanup(terminal.close)
        for sequence in (b'plain', 'e\u0301界Z'.encode(), b'\x1b[38;2;10;20;30mB',
                         b'\x1b[1;1H\x1b[1;4mX\x1b[0m', b'\x1b[2J\x1b[H',
                         b'\x1b[?1049hother', b'\x1b[?1049l', b'\r\n'*10,
                         'q\u0301\u0323\x1b[6 q'.encode()):
            terminal.feed(sequence)
            cached, cursor = terminal.snapshot()
            cells = (d.NativeCell*(terminal.cols*terminal.rows))()
            caret = (C.c_int*2)()
            self.assertEqual(terminal.lib.nd_snapshot(terminal.ptr, cells, len(cells), caret), len(cells))
            expected = []
            for cell in cells:
                points = tuple(cell.points[i] for i in range(cell.length))
                scalar = points[0] if len(points) == 1 else points or 32
                flags = cell.flags | (512 if cell.wide == 1 else 0) | (1024 if cell.wide >= 2 else 0)
                expected.append((32 if cell.wide >= 2 else scalar, cell.fg, cell.bg, flags))
            self.assertEqual((cached, cursor), (expected, tuple(caret)))
            cached[0] = (0, 0, 0, 0)
            self.assertEqual(terminal.snapshot()[0], expected)
        terminal.resize(30, 4)
        self.assertEqual(len(terminal.snapshot()[0]), 120)
        terminal.feed(b'\x1b[4;30HZ')
        self.assertEqual(terminal.snapshot()[0][-1][0], ord('Z'))


class ContentCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = d.MaskCache(TEXT, LIBRARY, temporary.name, 16, 32)
        self.addCleanup(self.cache.close)
        self.icons = Icons(TRAIN/'weights/neuronte-icon-v1.nrt', ROOT/'icons.json', LIBRARY, 16, 32)
        self.addCleanup(self.icons.close)
        self.emoji = Emoji(TRAIN/'weights/neuronte-emoji-v0.nrt', TRAIN/'font/emoji-manifest.json', LIBRARY, 16, 32)
        self.addCleanup(self.emoji.close)

    def renderer(self): return d.Renderer(self.cache, self.emoji, self.icons)

    def assert_fresh(self, renderer, records, cols, rows, force=False):
        actual = renderer.render(records, cols, rows, force).copy()
        ink = renderer.ink(records, cols, rows).copy()
        fresh = self.renderer()
        expected = fresh.render(records, cols, rows).copy()
        expected_ink = fresh.ink(records, cols, rows)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(ink, expected_ink)
        for key in ('styled_cells', 'composed_grapheme_cells', 'unsupported_grapheme_cells'):
            self.assertEqual(renderer.last[key], fresh.last[key], key)

    def test_ink_and_statistics_touch_only_changed_cells_including_wide_dependencies(self):
        records = [(ord('A'), 0xabcdef, 0x123456, 0)]*80
        renderer = self.renderer()
        self.assert_fresh(renderer, records, 20, 4)
        before = renderer.ink(records, 20, 4).copy()
        records[42] = (ord('B'), 0xabcdef, 0x123456, 1|4)
        with patch.object(renderer, 'artwork', wraps=renderer.artwork) as artwork:
            renderer.render(records, 20, 4)
            ink = renderer.ink(records, 20, 4)
            self.assertEqual(artwork.call_count, 2)  # one RGB cell and its ink
        self.assertEqual((renderer.last['changed_cells'], renderer.last['ink_changed_cells']), (1, 1))
        self.assertFalse(ink.flags.writeable)
        unchanged = np.ones(ink.shape, bool)
        unchanged[64:96, 32:48] = False
        np.testing.assert_array_equal(ink[unchanged], before[unchanged])
        self.assert_fresh(renderer, records, 20, 4)
        self.assertEqual((renderer.last['changed_cells'], renderer.last['ink_changed_cells']), (0, 0))

    def test_mutable_records_wide_pair_creation_removal_themes_and_same_count_resize(self):
        records = [[ord('A'), 0xabcdef, 0x123456, 0] for _ in range(80)]
        renderer = self.renderer()
        for mutation in (
            lambda: records.__setitem__(9, [0x1f600, 0xeeeeee, 0x102030, 512|3|4]),
            lambda: records.__setitem__(10, [32, 0xeeeeee, 0x102030, 1024|3|4]),
            lambda: records[9].__setitem__(0, 0x1f680),
            lambda: records[10].__setitem__(3, 0),
            lambda: records[10].__setitem__(3, 1024|64),
            lambda: records.__setitem__(2, [0xf071, 0xeeeeee, 0x102030, 3|32]),
            lambda: records.__setitem__(3, [[ord('q'), 0x301], 0xeeeeee, 0x102030, 2]),
            lambda: records[3][0].append(0x3099),
            lambda: records[2].__setitem__(3, 64),
            lambda: setattr(self.icons, 'theme', 'light'),
            lambda: setattr(self.emoji, 'theme', 'light'),
        ):
            mutation()
            self.assert_fresh(renderer, records, 20, 4)
        self.assert_fresh(renderer, records, 10, 8)
        self.assertEqual(renderer.last['ink_changed_cells'], 80)
        self.assert_fresh(renderer, records, 20, 4, force=True)
        self.assertEqual(renderer.last['ink_changed_cells'], 80)

    def test_changed_text_ink_under_graphics_and_caret_matches_independent_c(self):
        records = [(ord('A'), 0xc0e0ff, 0x102030, 0)]*80
        image = np.random.default_rng(314).integers(0, 256, (96, 320, 4), np.uint8)
        item = types.SimpleNamespace(generation=8765, sx=0, sy=0, sw=320, sh=96,
                                     dw=320, dh=96, x=0, y=0, z=-1)
        pane = types.SimpleNamespace(terminal=types.SimpleNamespace(images=lambda: [(item, image)]))
        workspace = types.SimpleNamespace(cols=20, rows=4, cw=16, ch=32,
            current={'panes': [pane]}, layout=lambda: [(0, 0, 20, 4)], grid=lambda: records,
            content_dirty=True, pointer=(19, 39), cursor=dict(column=1, row=1, shape='bar'))
        graphics = Graphics(VIDEO, LIBRARY)
        composer = d.FrameComposer(self.renderer(), graphics)
        for scalar, flags, shape in ((ord('B'), 0, 'bar'), (32, 0, 'underline'),
                                    ((ord('q'), 0x301), 3|4, 'bar'), (ord('C'), 64, 'bar')):
            records[21] = (scalar, 0xabcdef, 0x123456, flags)
            workspace.cursor = dict(column=1, row=1, shape=shape)
            workspace.content_dirty = True
            frame, _, _ = composer.compose(workspace)
            doc = dict(cols=20, rows=4, cell_width=16, cell_height=32, screen=[320, 128],
                       records=records, planes=graphics.records, cursor=workspace.cursor,
                       pointer=workspace.pointer, icons=self.icons.identity, emoji=self.emoji.identity,
                       graphemes=d.GRAPHEME_IDENTITY)
            expected = reconstruct(doc, TEXT, LIBRARY, VIDEO/'weights/neuronte-video-masks-v0.nrt',
                TRAIN/'weights/neuronte-emoji-v0.nrt', TRAIN/'font/emoji-manifest.json',
                TRAIN/'weights/neuronte-icon-v1.nrt', ROOT/'icons.json')
            np.testing.assert_array_equal(frame, expected)

    def test_transferred_scene_uses_only_pointer_patch_and_keeps_inspection_exact(self):
        base = np.random.default_rng(15).integers(0, 256, (80, 96, 3), np.uint8)
        original = base.copy()
        overlay = PointerFrame(self.cache, self.icons)
        overlay.replace(base, owned=True)
        self.assertIs(overlay.frame, base)
        for position in ((0, 0), (5, 7), (6, 8), (95, 79), None):
            overlay.move(position)
            np.testing.assert_array_equal(overlay.background, original)
            if overlay.saved is not None:
                self.assertLessEqual(overlay.saved.nbytes, 16*32*3)
        np.testing.assert_array_equal(base, original)


if __name__ == '__main__': unittest.main()
