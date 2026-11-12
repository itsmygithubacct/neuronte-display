"""Exact restoration, bounded input, and native damage/VT regression tests."""
import collections
import ctypes as C
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import neuronte_display as d
from display_graphics import PointerFrame, pointer
from display_io import EvdevPointer, KMS, PointerTelemetry
from display_icons import Icons
from display_emoji import Emoji
from display_graphics import Graphics
from tools.reference import reconstruct


class Mask:
    cw, ch = 5, 7

    def get(self, scalar):
        return (np.arange(self.cw*self.ch).reshape(self.ch, self.cw)*7).astype(np.uint8)


class CompositionTests(unittest.TestCase):
    def test_learned_colour_graphics_carets_and_pointer_match_independent_c(self):
        home = Path(os.environ.get('NEURONTE_HOME', Path.home()/'projects/neuronte'))
        train = Path(os.environ.get('NEURONTE_TRAINING_HOME', Path.home()/'projects/neuronte-roadmap-training'))
        video = Path(os.environ.get('NEURONTE_VIDEO_HOME', home))
        library, weights = home/'build/libneuronte.so', home/'weights/neuronte-split-v4.nrt'
        with tempfile.TemporaryDirectory() as directory:
            cache = d.MaskCache(weights, library, directory, 16, 32)
            self.addCleanup(cache.close)
            icons = Icons(train/'weights/neuronte-icon-v1.nrt', ROOT/'icons.json', library, 16, 32)
            self.addCleanup(icons.close)
            emoji = Emoji(train/'weights/neuronte-emoji-v0.nrt', train/'font/emoji-manifest.json', library, 16, 32)
            self.addCleanup(emoji.close)
            graphics = Graphics(video, library)
            records = [(ord('A'), 0xdcbaba, 0x102030, 0)]*60
            records[20] = (0x1f600, 0xeeeeee, 0x102030, 512|3|4)
            records[21] = (32, 0xeeeeee, 0x102030, 1024|3|4)
            records[22] = (0xf071, 0x9a50d0, 0xe0e0e0, 3|4)
            placements = []
            for i, z in enumerate((-1, 1)):
                pixels = np.random.default_rng(i).integers(0, 256, (40, 80, 4), dtype=np.uint8)
                item = types.SimpleNamespace(generation=800+i, sx=0, sy=0, sw=80, sh=40,
                                             dw=80, dh=40, x=i*110, y=0, z=z)
                placements.append((item, pixels))
            pane = types.SimpleNamespace(terminal=types.SimpleNamespace(images=lambda: placements))
            workspace = types.SimpleNamespace(cols=20, rows=3, cw=16, ch=32, content_dirty=True,
                pointer=(0, 0), cursor=dict(column=2, row=1, shape='bar'), grid=lambda: records,
                current={'panes': [pane]}, layout=lambda: [(0, 0, 20, 3)])
            composer = d.FrameComposer(d.Renderer(cache, emoji, icons), graphics)
            for shape in ('bar', 'underline', 'block'):
                workspace.cursor = dict(column=2, row=1, shape=shape)
                workspace.content_dirty = True
                for pos in ((0, 0), (35, 36), (36, 37), (119, 40), (319, 95), None):
                    workspace.pointer = pos
                    frame, _, damage = composer.compose(workspace)
                    doc = dict(cols=20, rows=3, cell_width=16, cell_height=32, screen=[320, 96],
                               records=records, planes=graphics.records, cursor=workspace.cursor,
                               pointer=pos, icons=icons.identity, emoji=emoji.identity)
                    expected = reconstruct(doc, weights, library, video/'weights/neuronte-video-masks-v0.nrt',
                        train/'weights/neuronte-emoji-v0.nrt', train/'font/emoji-manifest.json',
                        train/'weights/neuronte-icon-v1.nrt', ROOT/'icons.json')
                    np.testing.assert_array_equal(frame, expected)
                    if pos != (0, 0): self.assertIsNotNone(damage)

    def test_exact_restore_overlap_clipping_hide_and_background_change(self):
        cache = Mask()
        rng = np.random.default_rng(871)
        base = rng.integers(0, 256, (30, 40, 3), dtype=np.uint8)
        composed = PointerFrame(cache)
        composed.replace(base.copy())
        for pos in ((0, 0), (2, 3), (3, 4), (39, 29), None, (-1, 0), (10, 10)):
            expected = pointer(base.copy(), pos, cache)
            before = composed.frame.copy()
            damage = composed.move(pos)
            np.testing.assert_array_equal(composed.frame, expected)
            touched = np.zeros(base.shape[:2], bool)
            for x0, y0, x1, y1 in damage: touched[y0:y1, x0:x1] = True
            np.testing.assert_array_equal(composed.frame[~touched], before[~touched])
            self.assertLessEqual(touched.sum(), 2*cache.cw*cache.ch)
        base ^= 255
        composed.replace(base.copy())
        composed.move((8, 9))
        np.testing.assert_array_equal(composed.frame, pointer(base.copy(), (8, 9), cache))

    def test_pointer_path_skips_grid_text_ink_graphics_and_text_cursor(self):
        cache = Mask()
        base = np.arange(30*40*3, dtype=np.uint16).astype(np.uint8).reshape(30, 40, 3)
        calls = collections.Counter()
        def count(name, result):
            def invoke(*_):
                calls[name] += 1
                return result
            return invoke
        def graphics(frame, workspace):
            calls['graphics'] += 1
            frame[3:17, 2:29] = (212, 18, 117)
        def cursor(frame, *_):
            calls['cursor'] += 1
            frame[4:11, 6:8] ^= 255
        renderer = types.SimpleNamespace(cache=cache, icons=None, last={},
            render=count('render', base), ink=count('ink', base[..., 0]), cursor=cursor)
        workspace = types.SimpleNamespace(cols=8, rows=4, content_dirty=True,
            pointer=(6, 4), cursor={}, grid=count('grid', []))
        composer = d.FrameComposer(renderer, types.SimpleNamespace(composite=graphics))
        frame, _, damage = composer.compose(workspace)
        self.assertIsNone(damage)
        background = composer.pointer.background.copy()
        original_calls = calls.copy()
        for pos in ((8, 6), (25, 15), (39, 29), None):
            workspace.pointer = pos
            frame, _, damage = composer.compose(workspace)
            np.testing.assert_array_equal(frame, pointer(background.copy(), pos, cache))
            self.assertEqual(calls, original_calls)
            self.assertIsNotNone(damage)
        # Content or VT invalidation must rebuild every overlay.
        for invalidate in ('content', 'vt'):
            if invalidate == 'content': workspace.content_dirty = True
            else: composer.valid = False
            _, _, damage = composer.compose(workspace)
            self.assertIsNone(damage)
        self.assertTrue(all(value == 3 for value in calls.values()))


class InputTests(unittest.TestCase):
    def setUp(self):
        class Sink:
            cols, rows, cw, ch = 200, 100, 8, 16
            pointer = (0, 0)
            def __init__(self): self.events = []
            def mouse(self, *args):
                self.events.append(args)
                self.pointer = args[:2]
        self.sink = Sink()
        self.device = EvdevPointer([], self.sink)
        self.read, self.write = os.pipe()
        os.set_blocking(self.read, False)
        self.device.devices[self.read] = {}
        self.device.pending_buttons[self.read] = []
        self.addCleanup(self.device.close)
        self.addCleanup(os.close, self.write)

    def enqueue(self, events):
        os.write(self.write, b''.join(self.device.event.pack(0, 0, *event) for event in events))

    def test_every_motion_report_is_dispatched_in_order_around_every_transition(self):
        events = [(2, 0, 1), (0, 0, 0)]*3
        events += [(1, 272, 1), (2, 0, 2), (0, 0, 0)]
        events += [(2, 0, 1), (0, 0, 0)]*2
        events += [(2, 8, 2), (2, 0, 3), (1, 273, 1), (2, 8, -1), (0, 0, 0)]
        events += [(1, 272, 0), (1, 273, 0), (2, 0, 1), (0, 0, 0)]
        self.enqueue(events)
        self.assertTrue(self.device.read(self.read))
        self.assertEqual(self.sink.events, [
            (1, 0, 2, 0), (2, 0, 2, 0), (3, 0, 2, 0), (5, 0, 0, 1),
            (6, 0, 2, 1), (7, 0, 2, 1),
            (10, 0, 0, 4), (10, 0, 0, 4), (10, 0, 0, 2), (10, 0, 0, 5),
            (11, 0, 1, 1), (11, 0, 1, 2)])
        self.assertEqual(self.device.held_buttons, [])

    def test_a_burst_of_motion_reaches_the_application_one_report_each(self):
        # 120 one-pixel reports queued behind one slow frame, as a 1000 Hz
        # mouse produces. Each must arrive, with its own coordinate.
        self.enqueue([(2, 0, 1), (0, 0, 0)]*120)
        self.device.read(self.read)
        for _ in range(120):
            if not self.device.ready_fds: break
            self.device.read(self.read)
        self.assertEqual(self.sink.events, [(x, 0, 2, 0) for x in range(1, 121)])

    def test_large_wheel_count_is_bounded_and_lossless_before_release(self):
        self.enqueue([(1, 272, 1), (2, 8, 700), (1, 272, 0), (0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(len(self.sink.events), self.device.MAX_DISPATCH_EVENTS)
        self.assertIn(self.read, self.device.ready_fds)
        while self.device.ready_fds: self.device.read(self.read)
        self.assertEqual(self.sink.events, [(0, 0, 0, 1)]+[(0, 0, 0, 4)]*700+[(0, 0, 1, 1)])

    def test_fragmented_syn_vt_multibutton_cancel_and_bounded_resume(self):
        self.enqueue([(1, 272, 1), (1, 273, 1), (0, 0, 0)])
        self.device.read(self.read)
        self.enqueue([(2, 0, 9), (2, 8, 1)])
        self.assertFalse(self.device.read(self.read))
        self.assertEqual(len(self.sink.events), 2)
        self.device.leave()
        self.assertEqual([e[2:] for e in self.sink.events], [(0, 1), (0, 2), (1, 1), (1, 2)])
        self.assertEqual([e[:2] for e in self.sink.events[-2:]], [(0, 0), (0, 0)])
        before = list(self.sink.events)
        self.enqueue([(1, 272, 1), (0, 0, 0)])
        self.device.read(self.read, active=False)
        self.assertEqual(self.sink.events, before)
        raw = self.device.event.pack(0, 0, 0, 0, 0)*self.device.MAX_READ_EVENTS
        with patch('display_io.os.read', return_value=raw) as reader:
            self.device.resume()
            self.assertEqual(reader.call_count, 4)
        self.assertIn(self.read, self.device.ready_fds)
        self.device.read(self.read)
        self.assertNotIn(self.read, self.device.ready_fds)
        self.enqueue([(2, 0, 1), (0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(self.sink.events[-1][2:], (2, 0))

    def test_horizontal_wheel_extra_buttons_and_kernel_overflow_barrier(self):
        self.enqueue([(1, 275, 1), (2, 6, -2), (2, 0, 5), (0, 0, 0),
                      (0, 3, 0), (1, 273, 1), (0, 0, 0),
                      (2, 6, 1), (0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(self.sink.events, [(5, 0, 0, 8), (5, 0, 0, 6), (5, 0, 0, 6),
                                          (5, 0, 1, 8), (5, 0, 0, 7)])

    def test_partial_device_report_cannot_move_another_devices_click(self):
        other_read, other_write = os.pipe()
        os.set_blocking(other_read, False)
        self.addCleanup(os.close, other_write)
        self.device.devices[other_read] = {}
        self.device.pending_buttons[other_read] = []
        self.enqueue([(2, 0, 7)])
        self.device.read(self.read)
        os.write(other_write, b''.join(self.device.event.pack(0, 0, *event) for event in
                                      [(2, 0, 4), (1, 272, 1), (0, 0, 0)]))
        self.device.read(other_read)
        self.enqueue([(0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(self.sink.events, [(4, 0, 0, 1), (11, 0, 2, 1)])
        self.enqueue([(2, 0, 10000), (2, 0, -50), (0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(self.sink.events[-1], (1549, 0, 2, 1))

    def test_other_device_cannot_overtake_deferred_wheels_or_move_pointer_back(self):
        other_read, other_write = os.pipe()
        os.set_blocking(other_read, False)
        self.addCleanup(os.close, other_write)
        self.device.devices[other_read] = {}
        self.device.pending_buttons[other_read] = []
        self.enqueue([(2, 8, 700), (0, 0, 0), (2, 0, 5), (0, 0, 0)])
        self.device.read(self.read)
        self.assertEqual(len(self.sink.events), 256)
        os.write(other_write, b''.join(self.device.event.pack(0, 0, *event) for event in
                                      [(1, 272, 1), (0, 0, 0)]))
        with patch('display_io.os.read', side_effect=AssertionError('later device overtook queued reports')):
            self.assertFalse(self.device.read(other_read))
        while self.device.ready_fds:
            before = len(self.sink.events)
            self.device.read(self.read)
            self.assertLessEqual(len(self.sink.events)-before, self.device.MAX_DISPATCH_EVENTS)
        self.device.read(other_read)
        self.assertEqual(self.sink.events, [(0, 0, 0, 4)]*700+[(5, 0, 2, 0), (5, 0, 0, 1)])

    def test_telemetry_coordinates_withheld_motion_and_cancelled_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'timing.jsonl'
            telemetry = PointerTelemetry(path)
            self.device.telemetry = telemetry
            # The workspace withholds the first report (a backed-up pane)
            # and delivers the second; telemetry records which was which.
            answers = iter([False, None])
            deliver = self.sink.mouse
            self.sink.mouse = lambda *args: (deliver(*args), next(answers))[1]
            with patch('display_io.time.monotonic_ns', return_value=123):
                self.enqueue([(2, 0, 1), (0, 0, 0), (2, 0, 2), (0, 0, 0)])
                self.device.read(self.read)
            self.assertEqual(self.sink.events, [(1, 0, 2, 0), (3, 0, 2, 0)])
            telemetry.present((3, 0), 'pointer', 130, dict(submit_ns=140, pageflip_ns=150))
            telemetry.present((4, 1), 'pointer', 160, None)
            telemetry.close()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['dispatched'] for r in rows[1:3]], [False, True])
            self.assertEqual([r['input_ns'] for r in rows[1:3]], [123, 123])
            self.assertEqual([r['x'] for r in rows[1:3]], [1, 3])
            self.assertEqual(rows[3]['pointer_seq'], 2)
            self.assertEqual((rows[3]['input_events'], rows[3]['coalesced_motion']), (2, 1))
            self.assertEqual(rows[3]['presented_x'], 3)
            self.assertIsNone(rows[4]['presented_x'])
            self.assertIsNone(rows[4]['pageflip_ns'])
            with self.assertRaises(FileExistsError): PointerTelemetry(path)


class NativeDamageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libdrm'], text=True))
        target = str(Path(cls.temp.name)/'kms-test.so')
        subprocess.run(['cc', '-std=c11', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fPIC', '-shared', '-D_POSIX_C_SOURCE=200809L', '-D_DEFAULT_SOURCE',
                        str(ROOT/'tests/kms_harness.c'), '-o', target, *flags], check=True)
        cls.lib = C.CDLL(target)
        for name, args, result in (
            ('test_new', [C.c_int, C.c_int], C.c_void_p),
            ('test_free', [C.c_void_p], None),
            ('test_buffer', [C.c_void_p, C.c_int], C.c_void_p),
            ('test_front', [C.c_void_p], C.c_int),
            ('test_pending', [C.c_void_p], C.c_int),
            ('test_fault', [C.c_int], None),
            ('test_acknowledgements', [], C.c_int),
            ('nd_kms_present_damage', [C.c_void_p, C.c_void_p, C.c_int, C.c_int,
                                       C.c_void_p, C.c_int, C.c_void_p], C.c_int),
            ('nd_kms_vt', [C.c_void_p, C.c_int], C.c_int)):
            fn = getattr(cls.lib, name)
            fn.argtypes, fn.restype = args, result

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.width, self.height = 47, 31
        self.kms = KMS.__new__(KMS)
        self.kms.lib = self.lib
        self.kms.ptr = self.lib.test_new(self.width, self.height)
        self.kms.active = True
        self.addCleanup(self.lib.test_free, self.kms.ptr)
        self.base = np.random.default_rng(88).integers(0, 256, (27, 43, 3), dtype=np.uint8)
        self.pointer = PointerFrame(Mask())
        self.pointer.replace(self.base)

    def buffer(self, index):
        address = self.lib.test_buffer(self.kms.ptr, index)
        return np.ctypeslib.as_array((C.c_uint32*((self.width+4)*self.height)).from_address(address)).reshape(self.height, self.width+4)

    def assert_front(self):
        expected = np.zeros((self.height, self.width+4), np.uint32)
        pixels = self.pointer.frame.astype(np.uint32)
        expected[:pixels.shape[0], :pixels.shape[1]] = pixels[..., 0]<<16 | pixels[..., 1]<<8 | pixels[..., 2]
        np.testing.assert_array_equal(self.buffer(self.lib.test_front(self.kms.ptr)), expected)

    def test_alternating_buffers_restore_every_pixel_and_preserve_padding(self):
        for i, pos in enumerate(((0, 0), (10, 8), (11, 9), (42, 26), None, (1, 1), (37, 21))):
            damage = self.pointer.move(pos)
            self.assertIsNotNone(self.kms.present(self.pointer.frame, None if i == 0 else damage))
            self.assert_front()
        self.pointer.replace(self.base ^ 255)
        self.pointer.move((20, 20))
        self.kms.present(self.pointer.frame)
        self.assert_front()
        self.kms.present(self.pointer.frame, self.pointer.move((0, 0)))
        self.assert_front()

    def test_small_damage_does_not_convert_unrelated_pixels(self):
        self.pointer.move((0, 0))
        self.kms.present(self.pointer.frame)
        self.kms.present(self.pointer.frame, [])  # Prime the other buffer.
        back = self.buffer(1-self.lib.test_front(self.kms.ptr))
        back[25, 40] = 0x12345678
        damage = self.pointer.move((3, 3))
        self.kms.present(self.pointer.frame, damage)
        self.assertEqual(self.buffer(self.lib.test_front(self.kms.ptr))[25, 40], 0x12345678)

    def test_inactive_interrupted_failed_flip_and_reacquire(self):
        self.pointer.move((3, 3))
        result = self.kms.present(self.pointer.frame, timed=True)
        self.assertGreater(result['submit_ns'], 0)
        self.assertGreaterEqual(result['pageflip_ns'], result['submit_ns'])
        self.lib.test_fault(1)
        damage = self.pointer.move((9, 9))
        self.assertIsNone(self.kms.present(self.pointer.frame, damage, timed=True))
        self.assertEqual(self.lib.test_pending(self.kms.ptr), 1)
        self.kms.vt(False)
        before = self.buffer(self.lib.test_front(self.kms.ptr)).copy()
        self.assertIsNone(self.kms.present(self.pointer.frame, timed=True))
        np.testing.assert_array_equal(self.buffer(self.lib.test_front(self.kms.ptr)), before)
        self.lib.test_fault(1)
        self.assertFalse(self.kms.vt(True))
        self.assertFalse(self.kms.active)
        self.assertEqual(self.lib.test_pending(self.kms.ptr), 1)
        # A new release can supersede an interrupted acquisition; another
        # acquisition still owns the pending callback and redraws both buffers.
        self.assertTrue(self.kms.vt(False))
        self.lib.test_fault(1)
        self.assertFalse(self.kms.vt(True))
        self.kms.vt(True)  # Drain interrupted callback from persistent state.
        self.assertEqual(self.lib.test_pending(self.kms.ptr), 0)
        self.kms.present(self.pointer.frame, [])
        self.assert_front()
        for fault in (2, 3):
            self.lib.test_fault(fault)
            with self.assertRaises(RuntimeError): self.kms.present(self.pointer.frame, [])
            self.kms.present(self.pointer.frame)
            self.assert_front()

    def test_reject_invalid_rectangles_and_noncontiguous_source(self):
        for damage in ([(-1, 0, 4, 4)], [(0, 0, 44, 28)], [(4, 4, 1, 1)], [(0, 0, 1, 1)]*5):
            with self.assertRaises(RuntimeError): self.kms.present(self.pointer.frame, damage)
        with self.assertRaises(ValueError): self.kms.present(self.pointer.frame[:, ::2])

    def test_loop_defers_interrupted_acquire_and_processes_newer_release(self):
        self.kms.present(self.pointer.frame)
        self.lib.test_fault(1)
        self.assertIsNone(self.kms.present(self.pointer.frame, []))
        self.kms.vt(False)
        transitions = [True]
        device, composer = Mock(), types.SimpleNamespace(valid=True)
        self.lib.test_fault(1)
        with patch('neuronte_display.print'):
            self.assertFalse(d.process_vt_transitions(transitions, self.kms, device, composer))
            self.assertEqual(transitions, [True])
            self.assertFalse(self.kms.active)
            self.assertFalse(composer.valid)
            device.resume.assert_not_called()
            self.assertTrue(d.process_vt_transitions(transitions, self.kms, device, composer))
            device.resume.assert_called_once_with()
            self.assertTrue(self.kms.active)
            # During another interrupted acquire, a signal handler appends
            # release. It must not be displaced by a stale acquire retry.
            transitions[:] = [True]
            def interrupt(acquire):
                transitions.append(False)
                return False
            with patch.object(self.kms, 'vt', side_effect=interrupt):
                self.assertFalse(d.process_vt_transitions(transitions, self.kms, device, composer))
            self.assertEqual(transitions, [False])
            self.assertTrue(d.process_vt_transitions(transitions, self.kms, device, composer))
            self.assertFalse(self.kms.active)
            device.leave.assert_called_once_with()

    def test_acquire_retries_only_the_interrupted_ack_master_or_modeset_phase(self):
        self.kms.present(self.pointer.frame)
        for fault in (4, 5, 6):
            self.kms.vt(False)
            before = self.lib.test_acknowledgements()
            self.lib.test_fault(fault)
            self.assertFalse(self.kms.vt(True))
            self.assertFalse(self.kms.active)
            self.assertIsNone(self.kms.present(self.pointer.frame, []))
            self.assertTrue(self.kms.vt(True))
            self.assertEqual(self.lib.test_acknowledgements()-before, 2 if fault == 4 else 1)
            self.kms.present(self.pointer.frame, [])
            self.assert_front()


if __name__ == '__main__': unittest.main()
