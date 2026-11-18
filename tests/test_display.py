import ctypes as C
import os
import select
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
        self.assertEqual(cells[24][0], ord("é"))
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


class EmbedderTests(unittest.TestCase):
    """Scrollback, pointer reports, modes and paste, for an embedder that
    draws the grid itself (Zell's pane rooms)."""

    def test_a_pane_runs_in_the_environment_it_is_given(self):
        lib = d.native()
        env = {"PATH": "/usr/bin:/bin", "ZELL_GIVEN": "given", "LINES": "3"}
        pane = d.Pane(["/bin/sh", "-c", 'printf "%s|%s|%s|%s" "$ZELL_GIVEN" "$TERM" "${LINES-unset}" "${HOME-unset}"'],
                            40, 8, 8, 16, lib, env=env)
        try:
            deadline = time.monotonic() + 10
            while pane.fd is not None and time.monotonic() < deadline:
                select.select([pane.fd], [], [], 0.1)
                pane.read()
            text = "".join("".join(row) for row in pane.terminal.rows_text())
            self.assertIn("given|xterm-256color|unset|unset", text)
            # The caller's dict is not changed.
            self.assertEqual(env, {"PATH": "/usr/bin:/bin", "ZELL_GIVEN": "given", "LINES": "3"})
        finally:
            pane.close()

    def pane_report(self, cols, rows, resize=None):
        # What the program sees: its size, and whether the pane is its
        # controlling terminal with it in the foreground.
        report = ("import os, time; time.sleep(0.3); size = os.get_terminal_size(0); "
                  "print('size', size.columns, size.lines, os.tcgetpgrp(0) == os.getpgrp(), sep='|')")
        pane = d.Pane([sys.executable, "-c", report], cols, rows, 8, 16, d.native(), env={"PATH": "/usr/bin:/bin"})
        try:
            if resize:
                pane.resize(*resize)
            deadline = time.monotonic() + 10
            while pane.fd is not None and time.monotonic() < deadline:
                select.select([pane.fd], [], [], 0.1)
                pane.read()
            return "".join("".join(row) for row in pane.terminal.rows_text())
        finally:
            pane.close()

    def test_a_pane_resized_at_once_keeps_the_new_size(self):
        # A split makes its pane at 8x3 and its layout resizes it straight
        # away; the program must see the layout's size, not the first one.
        for _ in range(3):
            self.assertIn("size|70|9|True", self.pane_report(8, 3, resize=(70, 9)))
        # Not resized, a pane keeps the size it was made with.
        self.assertIn("size|33|7|True", self.pane_report(33, 7))

    def terminal(self, cols=20, rows=5):
        terminal = d.Terminal(cols, rows)
        self.addCleanup(terminal.close)
        return terminal

    def first_row(self, terminal):
        return "".join(terminal.rows_text()[0]).rstrip()

    def test_scrollback_moves_what_is_shown(self):
        terminal = self.terminal()
        for i in range(30):
            terminal.feed(f"line {i}\r\n".encode())
        self.assertEqual(terminal.scrollbar(), (31, 26, 5))
        self.assertEqual(self.first_row(terminal), "line 26")
        terminal.scroll(-10)
        self.assertEqual(terminal.scrollbar(), (31, 16, 5))
        self.assertEqual(self.first_row(terminal), "line 16")
        self.assertEqual(chr(terminal.snapshot()[0][0][0]), "l")
        # Scrolled back, the cursor is not shown.
        self.assertEqual(terminal.snapshot()[1], (-1, -1))
        terminal.scroll(3)
        self.assertEqual(self.first_row(terminal), "line 19")
        terminal.scroll_to(4)
        self.assertEqual((terminal.scrollbar()[1], self.first_row(terminal)), (4, "line 4"))
        terminal.scroll_top()
        self.assertEqual(self.first_row(terminal), "line 0")
        # Output while scrolled back leaves the rows shown where they are.
        terminal.feed(b"more\r\n")
        self.assertEqual(self.first_row(terminal), "line 0")
        terminal.scroll_bottom()
        self.assertEqual(terminal.scrollbar(), (32, 27, 5))
        self.assertEqual(terminal.snapshot()[1], (0, 4))
        terminal.scroll(-1000)
        self.assertEqual(terminal.scrollbar()[1], 0)
        terminal.scroll(1000)
        self.assertEqual(terminal.scrollbar()[1], 27)

    def test_the_title_a_program_sets(self):
        terminal = self.terminal()
        self.assertEqual(terminal.title(), "")
        terminal.feed(b"\x1b]2;Kilix pane 7: build\x07")
        self.assertEqual(terminal.title(), "Kilix pane 7: build")
        terminal.feed("\x1b]0;caf\u00e9 \u65e5\x1b\\".encode())
        self.assertEqual(terminal.title(), "caf\u00e9 \u65e5")
        terminal.feed(b"\x1b]2;\x07")
        self.assertEqual(terminal.title(), "")

    def test_alternate_screen_keeps_no_scrollback(self):
        terminal = self.terminal()
        for i in range(10):
            terminal.feed(f"{i}\r\n".encode())
        self.assertFalse(terminal.alternate_screen())
        terminal.feed(b"\x1b[?1049h")
        self.assertTrue(terminal.alternate_screen())
        self.assertEqual(terminal.scrollbar(), (5, 0, 5))
        terminal.scroll(-3)
        self.assertEqual(terminal.scrollbar(), (5, 0, 5))
        terminal.feed(b"\x1b[?1049l")
        self.assertFalse(terminal.alternate_screen())
        self.assertEqual(terminal.scrollbar()[0], 11)

    def test_modes(self):
        terminal = self.terminal()
        self.assertFalse(terminal.mode(2004))
        terminal.feed(b"\x1b[?2004h\x1b[4h")
        self.assertTrue(terminal.mode(2004))
        self.assertTrue(terminal.mode(4, ansi=True))
        self.assertFalse(terminal.mode(4))
        with self.assertRaises(ValueError):
            terminal.mode(31999)

    def test_pointer_reports_follow_the_programs_modes(self):
        terminal = self.terminal(20, 5)
        T = d.Terminal
        self.assertFalse(terminal.mouse_tracking())
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 10, 20), b"")
        terminal.feed(b"\x1b[?1000h\x1b[?1006h")
        self.assertTrue(terminal.mouse_tracking())
        # Pixel (10, 20) is cell (2, 2) counted from one: 8x16 cells.
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 10, 20), b"\x1b[<0;2;2M")
        self.assertEqual(terminal.mouse(T.RELEASE, T.LEFT, 0, 10, 20), b"\x1b[<0;2;2m")
        self.assertEqual(terminal.mouse(T.PRESS, T.RIGHT, 0, 79, 79), b"\x1b[<2;10;5M")
        self.assertEqual(terminal.mouse(T.PRESS, T.WHEEL_UP, 0, 0, 0), b"\x1b[<64;1;1M")
        self.assertEqual(terminal.mouse(T.PRESS, T.WHEEL_DOWN, T.CTRL, 0, 0), b"\x1b[<81;1;1M")
        # Normal tracking reports no motion; button tracking reports drags.
        self.assertEqual(terminal.mouse(T.MOTION, T.LEFT, 0, 30, 20), b"")
        terminal.feed(b"\x1b[?1002h")
        self.assertEqual(terminal.mouse(T.MOTION, T.LEFT, 0, 30, 20), b"\x1b[<32;4;2M")
        # A resize changes the cell size the pixels are counted in.
        terminal.resize(20, 5, 10, 20)
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 10, 20), b"\x1b[<0;2;2M")
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 9, 19), b"\x1b[<0;1;1M")
        terminal.feed(b"\x1b[?1016h")
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 9, 19), b"\x1b[<0;10;20M")
        terminal.feed(b"\x1b[?1000l\x1b[?1002l")
        self.assertEqual(terminal.mouse(T.PRESS, T.LEFT, 0, 10, 20), b"")
        with self.assertRaises(ValueError):
            terminal.mouse(7, T.LEFT, 0, 0, 0)

    def test_paste_is_framed_as_asked(self):
        terminal = self.terminal()
        self.assertEqual(terminal.paste(b""), b"")
        self.assertEqual(terminal.paste(b"echo hi\nls\n"), b"echo hi\rls\r")
        # Control bytes cannot end the paste or type keys.
        self.assertEqual(terminal.paste(b"a\x1b[201~b\x03"), b"a [201~b ")
        terminal.feed(b"\x1b[?2004h")
        self.assertEqual(terminal.paste(b"echo hi\nls"), b"\x1b[200~echo hi\nls\x1b[201~")
        self.assertEqual(terminal.paste(b"x\x1b[201~y"), b"\x1b[200~x [201~y\x1b[201~")
        big = b"0123456789" * 100000
        self.assertEqual(terminal.paste(big), b"\x1b[200~" + big + b"\x1b[201~")
        self.assertEqual(terminal.paste(b""), b"\x1b[200~\x1b[201~")

    def test_rows_text_keeps_whole_graphemes(self):
        terminal = self.terminal(8, 2)
        terminal.feed("e\u0301界Z".encode())
        row = terminal.rows_text()[0]
        self.assertEqual(row[:4], ["e\u0301", "界", "", "Z"])
        self.assertEqual(len(row), 8)
        self.assertEqual("".join(row).rstrip(), "e\u0301界Z")
        self.assertEqual(terminal.rows_text()[1], [" "] * 8)


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
