"""Device and outer-terminal input, separate from the neural renderer."""
import ctypes as C
import collections
import json
import fcntl
import os
from pathlib import Path
import re
import struct
import time


class KMS:
    def __init__(self, card, width=0, height=0):
        self.lib = C.CDLL(str(Path(__file__).parent/'build/libneuronte-kms.so'), use_errno=True)
        for name, args, result in (
            ('nd_kms_open', [C.c_char_p,C.c_int,C.c_int,C.POINTER(C.c_int)], C.c_void_p),
            ('nd_kms_present', [C.c_void_p,C.c_void_p,C.c_int,C.c_int], C.c_int),
            ('nd_kms_present_damage', [C.c_void_p,C.c_void_p,C.c_int,C.c_int,
                                      C.c_void_p,C.c_int,C.c_void_p], C.c_int),
            ('nd_kms_vt', [C.c_void_p,C.c_int], C.c_int),
            ('nd_kms_close', [C.c_void_p], None)):
            fn = getattr(self.lib,name)
            fn.argtypes,fn.restype = args,result
        geometry = (C.c_int*2)()
        self.ptr = self.lib.nd_kms_open(os.fsencode(card),width,height,geometry)
        if not self.ptr:
            raise OSError(C.get_errno(), 'KMS requires an unused DRM card and active text VT')
        self.width,self.height = geometry
        self.active = True

    def present(self, frame, damage=None, timed=False):
        if frame.dtype.name != 'uint8' or frame.ndim != 3 or frame.shape[2] != 3 or not frame.flags.c_contiguous:
            raise ValueError('KMS requires contiguous RGB uint8 pixels')
        rects = None if damage is None else (C.c_int*(4*len(damage)))(*(v for r in damage for v in r))
        timing = (C.c_uint64*2)() if timed else None
        status = self.lib.nd_kms_present_damage(self.ptr,frame.ctypes.data,frame.shape[1],frame.shape[0],
                                                rects,-1 if damage is None else len(damage),timing)
        if status < 0:
            raise RuntimeError('KMS page flip failed')
        if status == 1:
            return None
        return dict(submit_ns=timing[0],pageflip_ns=timing[1]) if timed else {}

    def vt(self, acquire):
        status = self.lib.nd_kms_vt(self.ptr,acquire)
        if status < 0:
            raise RuntimeError('VT ownership transition failed')
        self.active = bool(acquire) and status == 0
        return status == 0

    def close(self):
        if self.ptr:
            self.lib.nd_kms_close(self.ptr)
            self.ptr = None


class OuterInput:
    """Only consume complete SGR pointer reports; preserve fragmented keys."""
    pattern = re.compile(rb'\x1b\[<(\d+);(\d+);(\d+)([Mm])')

    def __init__(self, workspace):
        self.workspace = workspace
        self.pending = bytearray()
        self.origin = (0,0)

    def feed(self, data):
        self.pending.extend(data)
        while self.pending:
            marker = b'\x1b[<'
            if marker.startswith(self.pending) or (self.pending.startswith(marker) and
                    len(self.pending) < 64 and not re.search(rb'[Mm]', self.pending[3:])):
                return
            match = self.pattern.match(self.pending)
            if match:
                code,x,y = map(int,match.group(1,2,3))
                action = 1 if match[4] == b'm' else (2 if code & 32 else 0)
                button = ({0:1,1:3,2:2}.get(code&3,0) if not code&64 else 4+(code&3))
                mods = ((1 if code&4 else 0) | (4 if code&8 else 0) | (2 if code&16 else 0))
                self.workspace.mouse(x-1-self.origin[0],y-1-self.origin[1],action,button,mods)
                del self.pending[:match.end()]
            else:
                self.workspace.input(bytes(self.pending[:1]))
                del self.pending[:1]

    def flush(self):
        if self.pending:
            self.workspace.input(bytes(self.pending))
            self.pending.clear()


class PointerTelemetry:
    """Opt-in buffered software timing; never requires a screen capture."""
    schema = 'neuronte.pointer.v1'

    def __init__(self, path, snapshot_live=False, snapshot_history=False):
        self.file = open(path, 'x', buffering=65536)
        self.seq = 0
        self.pointer_seq = None
        self.input_events = self.coalesced_motion = 0
        self.write('metadata', clock='CLOCK_MONOTONIC',
                   input_boundary='userspace evdev read completion',
                   present_boundary='userspace DRM pageflip callback',
                   diagnostic_snapshot_live=snapshot_live,
                   diagnostic_snapshot_history=snapshot_history,
                   max_read_events=EvdevPointer.MAX_READ_EVENTS,
                   max_dispatch_events=EvdevPointer.MAX_DISPATCH_EVENTS)

    def write(self, event, **fields):
        self.file.write(json.dumps(dict(schema=self.schema, event=event, **fields),
                                   separators=(',', ':'))+'\n')

    def input(self, input_ns, x, y, action, button, dispatched):
        self.seq += 1
        self.input_events += 1
        self.coalesced_motion += not dispatched
        if dispatched:
            self.pointer_seq = self.seq
        self.write('pointer_input', seq=self.seq, input_ns=input_ns, x=x, y=y,
                   action=action, button=button, dispatched=dispatched)

    def present(self, position, frame_kind, render_start_ns, completion):
        x, y = position if position is not None else (None, None)
        ok = completion is not None
        self.write('present', status='presented' if ok else 'cancelled',
                   pointer_seq=self.pointer_seq, requested_x=x, requested_y=y,
                   presented_x=x if ok else None, presented_y=y if ok else None,
                   render_start_ns=render_start_ns,
                   submit_ns=completion['submit_ns'] if ok else None,
                   pageflip_ns=completion['pageflip_ns'] if ok else None,
                   frame_kind=frame_kind, input_events=self.input_events,
                   coalesced_motion=self.coalesced_motion)
        self.input_events = self.coalesced_motion = 0

    def vt(self, acquire):
        self.write('vt', acquire=acquire, monotonic_ns=time.monotonic_ns())
        self.pointer_seq = None
        self.input_events = self.coalesced_motion = 0
        self.file.flush()

    def close(self):
        self.file.close()


class EvdevPointer:
    """Bounded SYN-report processing, with lossless button/wheel barriers.

    No EVIOCGRAB. Only adjacent complete motion reports in one bounded read
    are coalesced; all transitions use their report's final coordinates.
    Large wheel counts are retained and dispatched over subsequent polls.
    """
    event = struct.Struct('@llHHi')
    MAX_READ_EVENTS = 256
    MAX_DISPATCH_EVENTS = 256
    BUTTONS = {272:1, 273:2, 274:3, 275:8, 276:9, 277:10, 278:11}

    def __init__(self, paths, workspace, telemetry=None):
        self.workspace, self.telemetry = workspace, telemetry
        self.devices = {}
        self.x = self.y = 0
        self.dispatched_position = (0, 0)
        self.button = 0
        self.held_buttons = []
        self.pending_buttons = {}
        self.pending_motion = set()
        self.pending_axes = {}
        self.reports = {}
        self.resuming = set()
        self.dropped = set()
        for path in paths:
            fd = os.open(path, os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
            self.devices[fd] = {}
            self.pending_buttons[fd] = []
            for axis in (0, 1):
                self._axis(fd, axis)
        self.dispatched_position = self.x, self.y

    @property
    def ready_fds(self):
        return {fd for fd, reports in self.reports.items() if reports} | self.resuming

    def _axis(self, fd, axis):
        try:
            current, lo, hi, _, _, _ = struct.unpack(
                '6i', fcntl.ioctl(fd, 0x80184540+axis, b'\0'*24))
        except OSError:
            return
        self.devices[fd][axis] = lo, hi
        self._absolute(axis, current, lo, hi)

    def _absolute(self, axis, value, lo, hi):
        position = self._scale(axis, value, lo, hi)
        if axis == 0: self.x = position
        else: self.y = position

    def _scale(self, axis, value, lo, hi):
        size = self.workspace.cols*self.workspace.cw if axis == 0 else self.workspace.rows*self.workspace.ch
        return max(0, min(size-1, (value-lo)*(size-1)//max(1, hi-lo)))

    def _queue_axis(self, fd, axis, value, absolute=False):
        # A partial report from one device must not move another device's
        # buttons. Retain each axis as clamp(position+offset, low, high), which
        # composes arbitrary relative deltas in constant space while keeping
        # edge saturation exact. Commit axes only at that device's SYN.
        axes = self.pending_axes.setdefault(fd, {})
        size = self.workspace.cols*self.workspace.cw if axis == 0 else self.workspace.rows*self.workspace.ch
        if absolute:
            axes[axis] = 0, value, value
        else:
            offset, low, high = axes.get(axis, (0, 0, size-1))
            axes[axis] = offset+value, max(0, min(size-1, low+value)), max(0, min(size-1, high+value))
        self.pending_motion.add(fd)

    def _finish_axes(self, fd):
        for axis, (offset, low, high) in self.pending_axes.pop(fd, {}).items():
            value = max(low, min(high, (self.x if axis == 0 else self.y)+offset))
            if axis == 0: self.x = value
            else: self.y = value

    def _cancel(self, fd=None):
        for owner, button in list(self.held_buttons):
            if fd is None or fd == owner:
                self.workspace.mouse(*self.dispatched_position, 1, button)
                self.held_buttons.remove((owner, button))
        self.button = self.held_buttons[-1][1] if self.held_buttons else 0

    def leave(self):
        self._cancel()
        self.x, self.y = self.dispatched_position
        self.pending_motion.clear()
        self.pending_axes.clear()
        self.reports.clear()
        self.dropped.clear()
        for pending in self.pending_buttons.values(): pending.clear()

    def _remove(self, fd):
        self._cancel(fd)
        os.close(fd)
        self.devices.pop(fd, None)
        self.pending_buttons.pop(fd, None)
        self.pending_motion.discard(fd)
        self.pending_axes.pop(fd, None)
        self.reports.pop(fd, None)
        self.resuming.discard(fd)
        self.dropped.discard(fd)

    def _discard(self, fd):
        # Bounded even if a producer continuously fills the input queue. Stay
        # in discard mode until EAGAIN, then refresh absolute tablet position.
        for _ in range(4):
            try: raw = os.read(fd, self.event.size*self.MAX_READ_EVENTS)
            except BlockingIOError:
                self.resuming.discard(fd)
                for axis in tuple(self.devices[fd]): self._axis(fd, axis)
                if getattr(self.workspace, 'pointer', None) is not None:
                    self.workspace.pointer = self.x, self.y
                self.dispatched_position = self.x, self.y
                return True
            if not raw:
                self._remove(fd)
                return False
        return False

    def resume(self):
        self.leave()
        self.resuming = set(self.devices)
        for fd in list(self.devices): self._discard(fd)

    def _dispatch(self, fd):
        reports = self.reports.setdefault(fd, collections.deque())
        changed = False
        for _ in range(self.MAX_DISPATCH_EVENTS):
            if not reports: break
            x, y, action, button, count, input_ns = reports.popleft()
            if action == 3:
                self._cancel(fd)
                if self.telemetry:
                    self.telemetry.write('input_discontinuity', input_ns=input_ns, reason='SYN_DROPPED')
                    self.telemetry.pointer_seq = None
                changed = True
                continue
            # A transition (including a wheel notch) is a coalescing barrier.
            dispatched = action != 2 or not reports or reports[0][2] != 2
            if action == 2: button = self.button
            if self.telemetry:
                self.telemetry.input(input_ns, x, y, action, button, dispatched)
            if dispatched:
                self.workspace.mouse(x, y, action, button)
                self.dispatched_position = x, y
                changed = True
                if button in self.BUTTONS.values() and action in (0, 1):
                    key = fd, button
                    if action == 0 and key not in self.held_buttons:
                        self.held_buttons.append(key)
                    elif action == 1 and key in self.held_buttons:
                        self.held_buttons.remove(key)
                    self.button = self.held_buttons[-1][1] if self.held_buttons else 0
            if count > 1:
                reports.appendleft((x, y, action, button, count-1, input_ns))
        return changed

    def read(self, fd, active=True):
        if active:
            older = next((owner for owner, reports in self.reports.items() if reports), None)
            if older is not None:
                # Preserve global parsed order across bounded wheel batches.
                # Later devices stay unread until the older queue is drained;
                # Workspace.poll explicitly schedules its fd even without new
                # kernel input. Never spend that fd's budget on another call.
                return self._dispatch(fd) if fd == older else False
        if fd in self.resuming:
            return self._discard(fd) if active else self._inactive(fd)
        try:
            raw = os.read(fd, self.event.size*self.MAX_READ_EVENTS)
        except BlockingIOError:
            return False
        if not raw:
            self._remove(fd)
            return False
        input_ns = time.monotonic_ns() if self.telemetry else None
        if not active:
            self.pending_buttons[fd].clear()
            self.pending_motion.discard(fd)
            self.pending_axes.pop(fd, None)
            return False
        reports = self.reports.setdefault(fd, collections.deque())
        for offset in range(0, len(raw), self.event.size):
            _, _, kind, code, value = self.event.unpack_from(raw, offset)
            if fd in self.dropped:
                if kind == 0 and code == 0:
                    self.dropped.discard(fd)
                    for axis in tuple(self.devices[fd]): self._axis(fd, axis)
                continue
            if kind == 0 and code == 3:  # SYN_DROPPED: cancel, never invent clicks.
                reports.append((self.x, self.y, 3, 0, 1, input_ns))
                self.pending_buttons[fd].clear()
                self.pending_motion.discard(fd)
                self.pending_axes.pop(fd, None)
                self.dropped.add(fd)
            elif kind == 2 and code in (0, 1):
                self._queue_axis(fd, code, value)
            elif kind == 3 and code in self.devices[fd]:
                self._queue_axis(fd, code, self._scale(code, value, *self.devices[fd][code]), True)
            elif kind == 1 and code in self.BUTTONS and value in (0, 1):
                self.pending_buttons[fd].append((0 if value else 1, self.BUTTONS[code], 1))
            elif kind == 2 and code in (6, 8) and value:
                button = (4 if value > 0 else 5) if code == 8 else (7 if value > 0 else 6)
                self.pending_buttons[fd].append((0, button, abs(value)))
            elif kind == 0 and code == 0:
                self._finish_axes(fd)
                actions = self.pending_buttons[fd]
                if actions or fd in self.pending_motion:
                    for action, button, count in actions or [(2, 0, 1)]:
                        reports.append((self.x, self.y, action, button, count, input_ns))
                actions.clear()
                self.pending_motion.discard(fd)
        return self._dispatch(fd)

    def _inactive(self, fd):
        try: raw = os.read(fd, self.event.size*self.MAX_READ_EVENTS)
        except BlockingIOError: return False
        if not raw: self._remove(fd)
        return False

    def close(self):
        for fd in list(self.devices): self._remove(fd)
