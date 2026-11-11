import ctypes as C
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import neuronte_display as d

ROOT = Path(os.environ.get("NEURONTE_HOME", Path.home()/"projects/neuronte"))
WEIGHTS = ROOT/"weights/neuronte-split-v4.nrt"
LIBRARY = ROOT/"build/libneuronte.so"


def text(terminal):
    return "".join(chr(c[0]) for c in terminal.snapshot()[0])


def until(workspace, predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        workspace.poll(0.02)
        if predicate():
            return
    raise AssertionError("PTY did not reach expected state")


class ParserTests(unittest.TestCase):
    def test_state_colours_graphemes_and_replies(self):
        terminal = d.Terminal(24, 5)
        self.addCleanup(terminal.close)
        response = terminal.feed("A\x1b[38;2;10;20;30mB\x1b[0m\r\ne\u0301界Z\x1b[6n".encode())
        cells, cursor = terminal.snapshot()
        self.assertEqual(cells[0][0], ord("A"))
        self.assertEqual(cells[1][1], 0x0A141E)
        self.assertEqual(cells[24][0], (ord("e"),0x301))
        self.assertEqual(d.render_scalar(cells[24][0]),ord("é"))
        self.assertEqual(cells[25][0], ord("界"))
        self.assertEqual(cells[26][0], 32)
        self.assertEqual(cells[27][0], ord("Z"))
        self.assertEqual(cursor, (4, 1))
        self.assertEqual(response, b"\x1b[2;5R")

    def test_resize_alternate_screen_and_bounds(self):
        terminal = d.Terminal(24, 5)
        self.addCleanup(terminal.close)
        terminal.feed(b"primary\x1b[?1049h\x1b[2J\x1b[Halternate")
        self.assertTrue(text(terminal).startswith("alternate"))
        terminal.feed(b"\x1b[?1049l")
        self.assertTrue(text(terminal).startswith("primary"))
        terminal.resize(30, 7)
        self.assertEqual(len(terminal.snapshot()[0]), 210)
        with self.assertRaises(ValueError):
            terminal.resize(0, 7)
        self.assertEqual(len(terminal.snapshot()[0]), 210)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = d.MaskCache(WEIGHTS, LIBRARY, self.temp.name)
        self.addCleanup(self.cache.close)

    def test_restart_and_corrupt_record_recovery(self):
        mask = self.cache.get(ord("A")).copy()
        self.assertEqual(self.cache.stats["inferences"], 1)
        reopened = d.MaskCache(WEIGHTS, LIBRARY, self.temp.name)
        self.addCleanup(reopened.close)
        np.testing.assert_array_equal(reopened.get(ord("A")), mask)
        self.assertEqual(reopened.stats["inferences"], 0)
        self.assertEqual(reopened.stats["disk_hits"], 1)
        path = reopened.directory / "000041.mask"
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        reopened.memory.clear()
        np.testing.assert_array_equal(reopened.get(ord("A")), mask)
        self.assertEqual(reopened.stats["corrupt_entries"], 1)
        self.assertEqual(reopened.stats["inferences"], 1)
        self.assertEqual(path.read_bytes(), raw)

    def test_geometry_separation_and_memory_limit(self):
        self.cache.limit = 2
        for cp in map(ord, "ABC"):
            self.cache.get(cp)
        self.assertEqual(len(self.cache.memory), 2)
        self.cache.get(ord("A"))
        self.assertEqual(self.cache.stats["disk_hits"], 1)
        other = d.MaskCache(WEIGHTS, LIBRARY, self.temp.name, 9, 18)
        self.addCleanup(other.close)
        self.assertNotEqual(self.cache.directory, other.directory)
        self.assertEqual(other.get(ord("A")).shape, (18, 9))
        self.assertEqual(other.stats["disk_hits"], 0)

    def test_misnamed_valid_record_is_rejected(self):
        a = self.cache.get(ord("A")).copy()
        b = self.cache.get(ord("B")).copy()
        self.assertFalse(np.array_equal(a, b))
        (self.cache.directory/"000042.mask").write_bytes((self.cache.directory/"000041.mask").read_bytes())
        self.cache.memory.clear()
        np.testing.assert_array_equal(self.cache.get(ord("B")), b)
        self.assertEqual(self.cache.stats["corrupt_entries"], 1)

    def test_composites_against_independent_c_draw(self):
        lib = self.cache.lib
        lib.neuronte_set_samples.argtypes = [C.c_void_p, C.c_int]
        lib.neuronte_draw_scalar.argtypes = [C.c_void_p, C.POINTER(C.c_uint32),
                                            C.c_int, C.c_int, C.c_int,
                                            C.c_int, C.c_int, C.c_int, C.c_int,
                                            C.c_uint32, C.c_uint32]
        lib.neuronte_draw_scalar.restype = C.c_int
        self.assertEqual(lib.neuronte_set_samples(self.cache.ptr, 2), 0)
        renderer = d.Renderer(self.cache)
        for scalar in (ord("A"), 0x2500, 0x2588, 0x00E9, 0x4E2D, 0x10FFFF):
            foreground, background = 0x42DF8B, 0x24364A
            buf = (C.c_uint32 * 128)(*([0xFF000000 | background] * 128))
            self.assertEqual(lib.neuronte_draw_scalar(self.cache.ptr, buf, 8, 8, 16, 0, 0, 8, 16,
                                                      scalar, 0xFF000000 | foreground), 0)
            packed = np.frombuffer(buf, np.uint32).reshape(16, 8)
            want = np.stack([(packed >> shift) & 255 for shift in (16, 8, 0)], axis=2)
            got = renderer.cell((scalar, foreground, background, 0))
            self.assertLessEqual(int(np.max(np.abs(got.astype(int)-want.astype(int)))), 1)

    def test_dirty_and_restart_frames_match(self):
        records = [(ord(c), d.FG, d.BG, 0) for c in "Abé "]
        renderer = d.Renderer(self.cache)
        frame = renderer.render(records, 4, 1).copy()
        renderer.render(records, 4, 1)
        self.assertEqual(renderer.last["changed_cells"], 0)
        reopened = d.MaskCache(WEIGHTS, LIBRARY, self.temp.name)
        self.addCleanup(reopened.close)
        np.testing.assert_array_equal(d.Renderer(reopened).render(records, 4, 1), frame)
        self.assertEqual(reopened.stats["inferences"], 0)
        records[1] = (ord("Z"), d.FG, d.BG, 0)
        renderer.render(records, 4, 1)
        self.assertEqual(renderer.last["changed_cells"], 1)


class PtyTests(unittest.TestCase):
    def test_close_last_pane_with_queued_keys(self):
        workspace = d.Workspace(40, 20, command=["/bin/sh"])
        self.addCleanup(workspace.close)
        pane = workspace.selected
        workspace.input(b"\x01xMORE\x01n")
        self.assertTrue(workspace.quit)
        self.assertEqual(workspace.tabs, [])
        self.assertIsNone(pane.pid)
        self.assertIsNone(pane.fd)

    def test_focus_tabs_splits_resize_and_reap(self):
        command = ["/bin/sh", "-c", 'printf READY; while IFS= read -r line; do printf "\\r\\nGOT:%s\\r\\n" "$line"; stty size; done']
        workspace = d.Workspace(60, 24, command=command)
        self.addCleanup(workspace.close)
        first = workspace.selected
        until(workspace, lambda: "READY" in text(first.terminal))
        workspace.input(b"\x01s")
        second = workspace.selected
        self.assertIsNot(first, second)
        until(workspace, lambda: "READY" in text(second.terminal))
        workspace.input(b"SECOND\n")
        until(workspace, lambda: "GOT:SECOND" in text(second.terminal))
        self.assertNotIn("GOT:SECOND", text(first.terminal))
        workspace.input(b"\x01oFIRST\n")
        until(workspace, lambda: "GOT:FIRST" in text(first.terminal))
        before = first.cols
        workspace.input(b"\x01+")
        self.assertEqual(first.cols, before + 1)
        workspace.resize(80, 30)
        workspace.input(b"RESIZED\n")
        until(workspace, lambda: "GOT:RESIZED" in text(first.terminal))
        until(workspace, lambda: f"{first.rows} {first.cols}" in text(first.terminal))
        workspace.input(b"\x01n")
        third = workspace.selected
        self.assertEqual(len(workspace.tabs), 2)
        workspace.input(b"\x01[")
        self.assertIs(workspace.selected, first)
        self.assertEqual(len(workspace.grid()), 80*30)
        workspace.close()
        for pane in (first, second, third):
            self.assertIsNone(pane.pid)
            self.assertIsNone(pane.fd)


if __name__ == "__main__":
    unittest.main()
