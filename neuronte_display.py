#!/usr/bin/env python3
"""Ghostty terminal state and Neuronte cell rendering to KMS or Kitty."""
import argparse
import collections
import ctypes as C
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import time
import tty
import unicodedata
from display_graphemes import Graphemes, IDENTITY as GRAPHEME_IDENTITY

import numpy as np
from display_io import KMS, OuterInput, EvdevPointer, PointerTelemetry
from display_graphics import Graphics, pointer, cursor_overlay, PointerFrame
from display_emoji import Emoji
from display_icons import Icons
from display_pointer import PointerModel

HERE = Path(__file__).resolve().parent
FG, BG = 0xDCE2EB, 0x13171E


class NativeCell(C.Structure):
    _fields_ = [("points", C.c_uint32 * 64)] + [
        (n, C.c_uint32) for n in ("length", "fg", "bg", "flags", "wide")]


class NativeImage(C.Structure):
    _fields_ = [("data", C.c_void_p), ("length", C.c_uint64), ("generation", C.c_uint64)] + [
        (n, C.c_uint32) for n in ("width", "height", "format")] + [
        (n, C.c_int32) for n in ("x", "y", "z")] + [
        (n, C.c_uint32) for n in ("sx", "sy", "sw", "sh", "dw", "dh")]


def native():
    lib = C.CDLL(str(HERE / "build/libneuronte-display.so"))
    signatures = {
        "nd_new": ([C.c_int, C.c_int], C.c_void_p),
        "nd_free": ([C.c_void_p], None),
        "nd_resize": ([C.c_void_p] + [C.c_int] * 4, C.c_int),
        "nd_feed": ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        "nd_replies": ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        "nd_snapshot": ([C.c_void_p, C.POINTER(NativeCell), C.c_size_t, C.POINTER(C.c_int)], C.c_int),
        "nd_cursor_style": ([C.c_void_p], C.c_int),
        "nd_mouse": ([C.c_void_p] + [C.c_int]*5 + [C.c_void_p, C.c_size_t], C.c_int),
        "nd_images": ([C.c_void_p, C.POINTER(NativeImage), C.c_size_t], C.c_int),
        "nd_presenter_open": ([C.c_int, C.c_int], C.c_void_p),
        "nd_present": ([C.c_void_p, C.c_void_p, C.c_int, C.c_int], C.c_int),
        "nd_presenter_close": ([C.c_void_p], None),
        "nd_presenter_size": ([C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)], C.c_int),
        "nd_presenter_origin": ([C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)], None),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    return lib


class Terminal:
    def __init__(self, cols, rows, lib=None):
        self.lib = lib or native()
        self.ptr = self.lib.nd_new(cols, rows)
        if not self.ptr:
            raise RuntimeError("Cannot create terminal parser")
        self.cols, self.rows = cols, rows
        self.cursor_style = 'block'

    def resize(self, cols, rows, cw=8, ch=16):
        if self.lib.nd_resize(self.ptr, cols, rows, cw, ch):
            raise ValueError("Invalid terminal geometry")
        self.cols, self.rows = cols, rows

    def feed(self, data):
        if self.lib.nd_feed(self.ptr, data, len(data)):
            raise RuntimeError("Parser reply queue overflow")
        buf = C.create_string_buffer(65536)
        size = self.lib.nd_replies(self.ptr, buf, len(buf))
        if size < 0:
            raise RuntimeError("Cannot read parser replies")
        return buf.raw[:size]

    def snapshot(self):
        cells = (NativeCell * (self.cols * self.rows))()
        cursor = (C.c_int * 2)()
        if self.lib.nd_snapshot(self.ptr, cells, len(cells), cursor) != len(cells):
            raise RuntimeError("Cannot capture complete terminal grid")
        style = self.lib.nd_cursor_style(self.ptr)
        if style not in (0, 1, 2, 3):
            raise RuntimeError("Cannot capture terminal cursor style")
        self.cursor_style = {0:'bar', 1:'block', 2:'underline', 3:'block'}[style]
        out = []
        for c in cells:
            points = "".join(chr(c.points[i]) for i in range(c.length))
            points = tuple(map(ord, points))
            scalar = (points[0] if len(points) == 1 else points) if points else 32
            flags = c.flags | (512 if c.wide == 1 else 0) | (1024 if c.wide >= 2 else 0)
            if c.wide >= 2:  # spacer, never a second unknown-character mark
                scalar = 32
            out.append((scalar, c.fg, c.bg, flags))
        return out, tuple(cursor)

    def mouse(self, action, button, x, y, mods=0):
        buf = C.create_string_buffer(128)
        size = self.lib.nd_mouse(self.ptr, action, button, mods, x, y, buf, len(buf))
        if size < 0:
            raise RuntimeError("Cannot encode terminal mouse event")
        return buf.raw[:size]

    def images(self):
        images = (NativeImage * 128)()
        count = self.lib.nd_images(self.ptr, images, len(images))
        if count < 0:
            raise RuntimeError("Cannot capture image placements (limit 128)")
        out = []
        for item in images[:count]:
            channels = {0: 3, 1: 4, 3: 2, 4: 1}.get(item.format)
            if not channels or item.length != item.width * item.height * channels or item.length > 64*1024*1024:
                raise RuntimeError("Invalid decoded image")
            pixels = np.frombuffer(C.string_at(item.data, item.length), np.uint8).reshape(item.height, item.width, channels)
            if channels <= 2:
                pixels = np.concatenate((np.repeat(pixels[..., :1], 3, axis=2), pixels[..., 1:]), axis=2)
            out.append((item, pixels))
        return out

    def close(self):
        if self.ptr:
            self.lib.nd_free(self.ptr)
            self.ptr = None


def digest_file(path):
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


class MaskCache:
    """Weight/library/config-bound, checksummed masks; no external font files."""
    MAGIC = b"NRMASK2\0"

    def __init__(self, weights, library, cache_dir, cw=8, ch=16, limit=4096):
        if not 1 <= cw <= 256 or not 1 <= ch <= 512:
            raise ValueError("Invalid cell size")
        self.cw, self.ch, self.limit = cw, ch, limit
        self.memory = collections.OrderedDict()
        self.stats = collections.Counter()
        self.identity = {"version": 2, "weights": digest_file(weights),
                         "library": digest_file(library), "width": cw, "height": ch,
                         "samples": 2, "quantization": "nearest-u8"}
        namespace = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.identity_bytes = bytes.fromhex(namespace)
        self.directory = Path(cache_dir) / namespace
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lib = C.CDLL(str(library))
        self.lib.neuronte_open_file.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        self.lib.neuronte_open_file.restype = C.c_int
        self.lib.neuronte_coverage.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_int,
                                             C.c_int, C.POINTER(C.c_float)]
        self.lib.neuronte_coverage.restype = C.c_int
        self.lib.neuronte_close.argtypes = [C.c_void_p]
        self.ptr = C.c_void_p()
        if self.lib.neuronte_open_file(os.fsencode(weights), C.byref(self.ptr)):
            raise RuntimeError("Cannot open Neuronte weights")

    def infer(self, scalar, style=0):
        if style:
            if not hasattr(self.lib, "neuronte_coverage_styled"):
                raise RuntimeError("Bold/italic require the Neuronte styled API")
            fn = self.lib.neuronte_coverage_styled
            fn.argtypes = [C.c_void_p,C.c_uint32,C.c_int,C.c_int,C.c_int,C.c_uint,C.POINTER(C.c_float)]
            fn.restype = C.c_int
            values = np.empty((self.ch,self.cw),np.float32)
            if fn(self.ptr,scalar,self.cw,self.ch,2,style,values.ctypes.data_as(C.POINTER(C.c_float))):
                raise RuntimeError("Cannot render styled cell")
            self.stats["inferences"] += 1
            return np.floor(np.clip(values,0,1)*255+0.5).astype(np.uint8)
        buf = (C.c_float * (self.cw * self.ch))()
        if self.lib.neuronte_coverage(self.ptr, scalar, self.cw, self.ch, 2, buf):
            raise RuntimeError(f"Cannot render U+{scalar:04X}")
        self.stats["inferences"] += 1
        values = np.frombuffer(buf, dtype=np.float32).reshape(self.ch, self.cw)
        return np.floor(np.clip(values, 0, 1) * 255 + 0.5).astype(np.uint8)

    def get(self, scalar, style=0):
        key = (scalar, style) if style else scalar
        if key in self.memory:
            self.stats["memory_hits"] += 1
            self.memory.move_to_end(key)
            return self.memory[key]
        suffix = f"-style{style}" if style else ""
        path = self.directory / f"{scalar:06x}{suffix}.mask"
        identity = self.identity_bytes + struct.pack("<I", scalar) + (struct.pack("<I",style) if style else b"")
        mask = None
        try:
            # Bound the read even if a stale cache entry is unexpectedly huge.
            with path.open("rb") as fh:
                raw = fh.read(40 + self.cw * self.ch + 1)
            payload = raw[40:]
            if (len(raw) == 40 + self.cw * self.ch and raw[:8] == self.MAGIC
                    and hashlib.sha256(identity + payload).digest() == raw[8:40]):
                mask = np.frombuffer(payload, np.uint8).reshape(self.ch, self.cw)
                self.stats["disk_hits"] += 1
            else:
                self.stats["corrupt_entries"] += 1
        except FileNotFoundError:
            pass
        except OSError:
            self.stats["read_errors"] += 1
        if mask is None:
            mask = self.infer(scalar, style)
            payload = mask.tobytes()
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".mask-", delete=False) as fh:
                    tmp = fh.name
                    fh.write(self.MAGIC + hashlib.sha256(identity + payload).digest() + payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except OSError:
                self.stats["write_errors"] += 1
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
        mask.setflags(write=False)
        self.memory[key] = mask
        while len(self.memory) > self.limit:
            self.memory.popitem(last=False)
        return mask

    def close(self):
        if self.ptr:
            self.lib.neuronte_close(self.ptr)
            self.ptr = None


def colour(n):
    return np.array([(n >> 16) & 255, (n >> 8) & 255, n & 255], dtype=np.uint16)


def render_scalar(value):
    if not isinstance(value,(tuple,list)):
        return value
    text=unicodedata.normalize('NFC',''.join(chr(cp) for cp in value if cp not in (0xfe0e,0xfe0f)))
    return ord(text) if len(text)==1 else 0xfffd


class Renderer:
    def __init__(self, cache, emoji=None, icons=None):
        self.cache = cache
        self.emoji = emoji
        self.icons = icons
        self.graphemes = Graphemes(cache)
        self.previous = []
        self.frame = None
        self.last = {}

    def lines(self, flags):
        if not flags & (4|8|128): return None
        mask = np.zeros((self.cache.ch,self.cache.cw),np.uint8)
        for bit,scalar in ((4,0x2581),(8,0x2500),(128,0x2594)):
            if flags&bit: mask = np.maximum(mask,self.cache.get(scalar))
        return mask

    def artwork(self, records, index, cols):
        record = records[index]
        half = int(bool(index%cols and record[3]&1024))
        left = records[index-half]
        scalar = self.emoji.selected(left) if self.emoji else None
        if scalar is not None and (half or (index%cols+1<cols and records[index+1][3]&1024)):
            # Remove the grid's text-cursor reversal before selecting a
            # colour palette. The cursor is a separate contrast overlay.
            flags = left[3] ^ (16 if left[3]&2048 else 0)
            return self.emoji, scalar, half, left[1], left[2], flags
        scalar = self.icons.selected(record) if self.icons else None
        if scalar is not None:
            return self.icons, scalar, 0, record[1], record[2], record[3] ^ (16 if record[3]&2048 else 0)
        return None

    def cursor(self, frame, cursor, records, cols):
        if cursor:
            cursor['colour'] = bool(self.artwork(records,cursor['row']*cols+cursor['column'],cols))
        return cursor_overlay(frame,cursor,records,cols,self.cache)

    def cell(self, record, cached=True):
        scalar, fg, bg, flags = record
        get = self.cache.get if cached else self.cache.infer
        mask = self.graphemes.mask(scalar, flags & 3, cached)
        if flags & 4:
            mask = np.maximum(mask, get(0x2581))
        if flags & 8:
            mask = np.maximum(mask, get(0x2500))
        if flags & 128:
            mask = np.maximum(mask, get(0x2594))
        if flags & 64:
            mask = np.zeros_like(mask)
        fg, bg = colour(fg), colour(bg)
        if flags & 16:
            fg, bg = bg, fg
        if flags & 32:
            fg = (fg + bg) // 2
        alpha = mask.astype(np.uint16)[..., None]
        return ((fg * alpha + bg * (255 - alpha) + 127) // 255).astype(np.uint8)

    def ink(self, records, cols, rows):
        out = np.empty((rows*self.cache.ch,cols*self.cache.cw),np.uint8)
        for i,(scalar,_fg,_bg,flags) in enumerate(records):
            art = self.artwork(records,i,cols)
            if art:
                asset,cp,half,_,_,style = art
                mask=asset.mask(cp,half,style,self.lines(style))
            else:
                mask=self.graphemes.mask(scalar,flags&3)
                for bit,cp in ((4,0x2581),(8,0x2500),(128,0x2594)):
                    if flags&bit: mask=np.maximum(mask,self.cache.get(cp))
                if flags&64: mask=np.zeros_like(mask)
            y,x=divmod(i,cols)
            out[y*self.cache.ch:(y+1)*self.cache.ch,x*self.cache.cw:(x+1)*self.cache.cw]=mask
        return out

    def render(self, records, cols, rows, force=False):
        if len(records) != cols * rows:
            raise ValueError("Grid record count does not match geometry")
        start = time.perf_counter()
        shape = (rows * self.cache.ch, cols * self.cache.cw, 3)
        if self.frame is None or self.frame.shape != shape:
            self.frame = np.empty(shape, np.uint8)
            force = True
        changed = 0
        for index, record in enumerate(records):
            left=records[index-1] if index%cols and record[3]&1024 else record
            dependent_change=(index%cols and record[3]&1024 and
                              (index-1>=len(self.previous) or left!=self.previous[index-1]))
            if force or index >= len(self.previous) or record != self.previous[index] or dependent_change:
                y, x = divmod(index, cols)
                y, x = y * self.cache.ch, x * self.cache.cw
                art = self.artwork(records,index,cols)
                if art:
                    asset,cp,half,fg,bg,style = art
                    cell = asset.cell(cp,half,fg,bg,style,self.lines(style))
                else: cell = self.cell(record)
                self.frame[y:y + self.cache.ch, x:x + self.cache.cw] = cell
                changed += 1
        self.previous = list(records)
        self.last = {"seconds": time.perf_counter() - start, "changed_cells": changed,
                     "styled_cells": sum(bool(c[3] & 3) for c in records),
                     "composed_grapheme_cells": sum(len(self.graphemes.plan(c[0]) or ())>1 and
                         self.graphemes.supported(c[0],c[3]&3) for c in records),
                     "unsupported_grapheme_cells": sum(not self.artwork(records,i,cols) and
                         (bool(c[3]&256) or not self.graphemes.supported(c[0],c[3]&3)) for i,c in enumerate(records))}
        return self.frame


class FrameComposer:
    """Scene invalidation is independent of pointer motion and scanout."""
    def __init__(self, renderer, graphics=None, pointer_model=None):
        self.renderer, self.graphics = renderer, graphics
        self.pointer = PointerFrame(renderer.cache, renderer.icons, pointer_model)
        self.records = None
        self.valid = False

    def compose(self, workspace):
        full = not self.valid or workspace.content_dirty
        if full:
            self.records = workspace.grid()
            background = self.renderer.render(self.records, workspace.cols, workspace.rows).copy()
            if self.graphics:
                workspace.text_ink = self.renderer.ink(self.records, workspace.cols, workspace.rows)
                self.graphics.composite(background, workspace)
            self.renderer.cursor(background, workspace.cursor, self.records, workspace.cols)
            self.pointer.replace(background)
        start = time.perf_counter()
        damage = self.pointer.move(workspace.pointer)
        self.renderer.last['frame_kind'] = 'full' if full else 'pointer'
        if not full:
            self.renderer.last.update(seconds=time.perf_counter()-start, changed_cells=0)
        workspace.content_dirty = False
        self.valid = True
        return self.pointer.frame, self.records, None if full else damage


class Pane:
    def __init__(self, command, cols, rows, cw, ch, lib):
        self.terminal = Terminal(cols, rows, lib)
        self.terminal.resize(cols, rows, cw, ch)
        self.cw, self.ch, self.cols, self.rows = cw, ch, cols, rows
        self.name = Path(command[0]).name
        self.pending = bytearray()
        self.exit_status = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * cw, rows * ch))
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
                for key in ("LINES", "COLUMNS"):
                    env.pop(key, None)
                os.execvpe(command[0], command, env)
            except Exception:
                os._exit(127)
        os.set_blocking(self.fd, False)

    def resize(self, cols, rows):
        if (cols, rows) == (self.cols, self.rows):
            return
        self.terminal.resize(cols, rows, self.cw, self.ch)
        self.cols, self.rows = cols, rows
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, cols * self.cw, rows * self.ch))

    def send(self, data):
        if self.fd is None:
            return
        if len(self.pending) + len(data) > 1024 * 1024:
            raise RuntimeError("PTY input queue exceeds one MiB")
        self.pending.extend(data)
        self.flush()

    def flush(self):
        if self.fd is not None and self.pending:
            try:
                size = os.write(self.fd, self.pending)
                del self.pending[:size]
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                self.eof()

    def eof(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.reap()

    def read(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            data = b""
        if data:
            self.send(self.terminal.feed(data))
        else:
            self.eof()
        return True

    def reap(self):
        if self.pid:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.pid = None
                self.exit_status = os.waitstatus_to_exitcode(status)

    def close(self):
        self.eof()
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            for _ in range(20):
                self.reap()
                if not self.pid:
                    break
                time.sleep(0.01)
            if self.pid:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(self.pid, 0)
                self.pid = None
        self.terminal.close()


class Workspace:
    def __init__(self, cols=80, rows=25, cw=8, ch=16, command=None):
        self.lib = native()
        self.cols, self.rows, self.cw, self.ch = cols, rows, cw, ch
        self.command = command or [os.environ.get("SHELL", "/bin/sh")]
        self.tabs, self.tab, self.focus, self.prefix = [], 0, 0, False
        self.quit = False
        self.pointer = None
        self.content_dirty = True
        self.new_tab()

    @property
    def current(self):
        return self.tabs[self.tab]

    @property
    def selected(self):
        return self.current["panes"][self.focus]

    def panes(self):
        return [pane for tab in self.tabs for pane in tab["panes"]]

    def new_tab(self):
        self.content_dirty = True
        pane = Pane(self.command, self.cols, max(1, self.rows - 3), self.cw, self.ch, self.lib)
        self.tabs.append({"panes": [pane], "vertical": True, "bias": 0})
        self.tab, self.focus = len(self.tabs) - 1, 0
        self.layout()

    def split(self, vertical=True):
        self.content_dirty = True
        if len(self.current["panes"]) >= 4:
            return
        self.current["vertical"] = vertical
        self.current["panes"].append(Pane(self.command, 8, 3, self.cw, self.ch, self.lib))
        self.focus = len(self.current["panes"]) - 1
        self.layout()

    def layout(self, tab=None):
        tab = self.current if tab is None else tab
        key = (id(tab), self.cols, self.rows, tab['vertical'], tab['bias'],
               tuple(id(pane) for pane in tab['panes']))
        if getattr(self, '_layout_key', None) == key:
            return self._rectangles
        count = len(tab["panes"])
        vertical = tab["vertical"]
        length = self.cols if vertical else self.rows - 2
        available = length - (count - 1)
        base, extra = divmod(available, count)
        sizes = [base + (i < extra) for i in range(count)]
        if count >= 2:
            bias = max(4 - sizes[0], min(tab["bias"], sizes[1] - 4))
            sizes[0] += bias; sizes[1] -= bias
        rectangles, offset = [], 0
        for pane, size in zip(tab["panes"], sizes):
            x, y, w, h = (offset, 1, size, self.rows - 2) if vertical else (0, 1 + offset, self.cols, size)
            if w < 1 or h < 2:
                raise ValueError("Screen too small for this split layout")
            pane.resize(w, h - 1)
            rectangles.append((x, y, w, h))
            offset += size + 1
        self._layout_key, self._rectangles = key, rectangles
        return rectangles

    def resize(self, cols, rows):
        if not 20 <= cols <= 240 or not 18 <= rows <= 120:
            raise ValueError("Display geometry must be 20..240 columns and 18..120 rows")
        self.cols, self.rows = cols, rows
        self.content_dirty = True
        for tab in self.tabs:
            self.layout(tab)

    def input(self, data):
        if data:
            self.content_dirty = True
        for byte in data:
            if self.quit:
                break
            if self.prefix:
                self.prefix = False
                if byte == ord("q"):
                    self.quit = True
                elif byte == ord("n"):
                    self.new_tab()
                elif byte in (ord("s"), ord("h")):
                    self.split(byte == ord("s"))
                elif byte == ord("o"):
                    self.focus = (self.focus + 1) % len(self.current["panes"])
                elif byte in (ord("]"), ord("[")):
                    self.tab = (self.tab + (1 if byte == ord("]") else -1)) % len(self.tabs)
                    self.focus = 0
                elif byte in (ord("+"), ord("-")):
                    self.current["bias"] += 1 if byte == ord("+") else -1
                    self.layout()
                elif byte == ord("x"):
                    self.current["panes"].pop(self.focus).close()
                    self.focus = 0
                    if not self.current["panes"]:
                        self.tabs.pop(self.tab)
                        self.tab = 0
                    if not self.tabs:
                        self.quit = True
                    else:
                        self.layout()
                else:
                    self.selected.send(bytes([1 if byte == 1 else byte]))
            elif byte == 1:
                self.prefix = True
            else:
                self.selected.send(bytes([byte]))

    def mouse(self, x, y, action=2, button=0, mods=0):
        self.pointer = (max(0,min(x,self.cols*self.cw-1)), max(0,min(y,self.rows*self.ch-1)))
        col, row = x//self.cw, y//self.ch
        if row == 0 and action == 0 and button == 1:
            start = 0
            for i, tab in enumerate(self.tabs):
                end = start + len(f" [1:{tab['panes'][0].name}] ")
                if start <= col < end:
                    if (self.tab, self.focus) != (i, 0): self.content_dirty = True
                    self.tab, self.focus = i, 0
                    return
                start = end
        for i, (pane, (px,py,w,h)) in enumerate(zip(self.current["panes"], self.layout())):
            if px <= col < px+w and py <= row < py+h:
                if action == 0 and button == 1:
                    if self.focus != i: self.content_dirty = True
                    self.focus = i
                if row > py:
                    pane.send(pane.terminal.mouse(action, button, x-px*self.cw, y-(py+1)*self.ch, mods))
                return

    def poll(self, timeout=0.05, keyboard=False, input_handler=None, pointer_input=None, active=True):
        owners = {pane.fd: pane for pane in self.panes() if pane.fd is not None}
        readable = list(owners) + ([0] if keyboard and active else []) + (list(pointer_input.devices) if pointer_input else [])
        writable = [fd for fd, pane in owners.items() if pane.pending]
        queued = pointer_input.ready_fds if pointer_input and active else set()
        ready, writes, _ = select.select(readable, writable, [], 0 if queued else timeout)
        ready += sorted(queued.difference(ready))
        changed = False
        for fd in ready:
            if pointer_input and fd in pointer_input.devices:
                changed = pointer_input.read(fd,active) or changed
            elif keyboard and fd == 0:
                data = os.read(0, 65536)
                if not data:
                    self.quit = True
                else:
                    (input_handler or self.input)(data)
                changed = True
            elif owners[fd].fd is not None:
                content = owners[fd].read()
                if content: self.content_dirty = True
                changed = content or changed
        for fd in writes:
            if owners[fd].fd is not None:
                owners[fd].flush()
        for pane in self.panes():
            previous_status = pane.exit_status
            pane.reap()
            if previous_status != pane.exit_status:
                self.content_dirty = changed = True
        return changed

    def grid(self):
        records = [(32, FG, BG, 0)] * (self.cols * self.rows)
        self.cursor = None

        def text(x, y, value, foreground=FG, background=0x263448, width=None):
            for index, char in enumerate(value[:self.cols - x if width is None else width]):
                records[y * self.cols + x + index] = (ord(char), foreground, background, 0)

        text(0, 0, "".join(f" {'[' if i == self.tab else ' '}{i+1}:{tab['panes'][0].name}{']' if i == self.tab else ' '} "
                          for i, tab in enumerate(self.tabs)).ljust(self.cols))
        rectangles = self.layout()
        for i, (pane, (x, y, w, h)) in enumerate(zip(self.current["panes"], rectangles)):
            title = f"{'*' if i == self.focus else ' '} {i+1}:{pane.name}"
            if pane.exit_status is not None:
                title += f" [exit {pane.exit_status}]"
            text(x, y, title.ljust(w), background=0x31557A if i == self.focus else 0x263448, width=w)
            cells, cursor = pane.terminal.snapshot()
            for row in range(h - 1):
                records[(y+1+row)*self.cols+x:(y+1+row)*self.cols+x+w] = cells[row*w:(row+1)*w]
            cx, cy = cursor
            if i == self.focus and 0 <= cx < w and 0 <= cy < h - 1:
                index = (y+1+cy)*self.cols+x+cx
                cp, fg, bg, flags = records[index]
                shape = getattr(pane.terminal, 'cursor_style', 'block')
                self.cursor = dict(column=x+cx, row=y+1+cy, shape=shape)
                if shape == 'block':
                    records[index] = (cp, fg, bg, (flags ^ 16) | 2048)
            if i < len(rectangles) - 1:
                if self.current["vertical"]:
                    for row in range(1, self.rows-1):
                        records[row*self.cols+x+w] = (0x2502, 0x709BC5, BG, 0)
                else:
                    for col in range(self.cols):
                        records[(y+h)*self.cols+col] = (0x2500, 0x709BC5, BG, 0)
        text(0, self.rows-1, "C-a n:tab s/h:split o:focus [/]:tab +/-:resize x:close q:quit".ljust(self.cols))
        return records

    def close(self):
        for pane in self.panes():
            pane.close()


def save_snapshot(path, frame, records, workspace, cache, timing, graphics=None, screen=None, emoji=None, history=None, icons=None, pointer_model=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width, _ = frame.shape
    raw=f"P6\n{width} {height}\n255\n".encode() + frame.tobytes()
    temp=path.with_suffix('.ppm.tmp')
    temp.write_bytes(raw)
    os.replace(temp,path)
    document={
        "version": 2, "cols": workspace.cols, "rows": workspace.rows,
        "cell_width": cache.cw, "cell_height": cache.ch, "cache_identity": cache.identity,
        "records": records, "timing": timing, "cache": dict(cache.stats),
        "pointer": getattr(workspace, 'presented_pointer', workspace.pointer), "planes": graphics.records if graphics else [],
        "cursor": getattr(workspace, 'cursor', None),
        "emoji": emoji.identity if emoji else None,
        "icons": icons.identity if icons else None,
        "pointer_model": pointer_model.identity if pointer_model else None,
        "graphemes": GRAPHEME_IDENTITY,
        "screen": screen or [width,height], "frame_sha256":hashlib.sha256(raw).hexdigest(),
        "rgb_sha256":hashlib.sha256(frame.tobytes()).hexdigest()}
    temp=path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(document)+"\n")
    os.replace(temp,path.with_suffix('.json'))
    if history:
        history=Path(history); history.mkdir(parents=True,exist_ok=True)
        target=history/(document['rgb_sha256']+'.json')
        if not target.exists(): target.write_text(json.dumps(document)+'\n')


def process_vt_transitions(transitions, kms, pointer_input, composer, telemetry=None):
    """Defer an interrupted acquire, or let a newer release supersede it."""
    while transitions:
        acquire = transitions.pop(0)
        composer.valid = False
        if pointer_input and not acquire: pointer_input.leave()
        if not kms.vt(acquire):
            if not transitions: transitions.append(acquire)
            return False
        if pointer_input and acquire: pointer_input.resume()
        if telemetry: telemetry.vt(acquire)
        print(json.dumps({'event':'vt-acquire' if acquire else 'vt-release',
                          'monotonic':time.monotonic()}), file=sys.stderr, flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("NEURONTE_HOME", Path.home()/"projects/neuronte")))
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", Path.home()/".cache"))/"neuronte-display")
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--rows", type=int, default=25)
    parser.add_argument("--cell-width", type=int, default=8)
    parser.add_argument("--cell-height", type=int, default=16)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-live", action="store_true", help="atomically update grid and PPM on every presented frame")
    parser.add_argument("--snapshot-history", type=Path, help="retain each distinct presented grid for boot acceptance")
    parser.add_argument("--backend", choices=("kitty","kms"), default="kitty")
    parser.add_argument("--drm-card", default="/dev/dri/card0")
    parser.add_argument("--mode", help="KMS WIDTHxHEIGHT; default connected preferred mode")
    parser.add_argument("--pointer-device", action="append", default=[], help="evdev pointer device, repeatable")
    parser.add_argument("--pointer-telemetry", type=Path, help="exclusive buffered KMS input/pageflip JSONL; no screen dumps")
    parser.add_argument("--pointer-weights", type=Path, help="opt-in dedicated learned pointer model")
    parser.add_argument("--pointer-manifest", type=Path, help="hash-bound pointer sizes, samples, scalar and hotspots")
    parser.add_argument("--video-home", type=Path, help="Neuronte tree containing tools/neuronte_video.py")
    parser.add_argument("--video-weights", type=Path)
    parser.add_argument("--emoji-weights", type=Path, help="explicit standalone two-cell colour bundle")
    parser.add_argument("--emoji-manifest", type=Path, help="matching availability manifest; required with emoji weights")
    parser.add_argument("--icon-weights", type=Path, help="opt-in accepted single-cell colour icons and pointer")
    parser.add_argument("--icon-manifest", type=Path, help="hash-bound trained icon inventory and pointer hotspot")
    parser.add_argument("--theme", choices=("auto","dark","light"), default="auto")
    parser.add_argument("--headless-seconds", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if bool(args.emoji_weights)!=bool(args.emoji_manifest):
        parser.error('--emoji-weights and --emoji-manifest must be supplied together')
    if bool(args.icon_weights)!=bool(args.icon_manifest):
        parser.error('--icon-weights and --icon-manifest must be supplied together')
    if bool(args.pointer_weights)!=bool(args.pointer_manifest):
        parser.error('--pointer-weights and --pointer-manifest must be supplied together')
    if not 20 <= args.cols <= 240 or not 18 <= args.rows <= 120:
        parser.error("geometry must be 20..240 columns and 18..120 rows")
    if args.headless_seconds is not None and args.headless_seconds <= 0:
        parser.error("--headless-seconds must be positive")
    if args.pointer_telemetry and args.backend != 'kms':
        parser.error('--pointer-telemetry requires --backend kms')
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    cache = MaskCache(args.weights or args.home/"weights/neuronte-split-v4.nrt",
                      args.library or args.home/"build/libneuronte.so", args.cache,
                      args.cell_width, args.cell_height)
    workspace = None
    presenter = None
    saved_termios = None
    kms = None
    pointer_input = None
    graphics = None
    emoji = None
    icons = None
    outer = None
    telemetry = None
    pointer_model = None
    transitions = []
    old_signals = {}
    renderer = Renderer(cache)
    frame, records = None, None
    frame_presented = False
    try:
        interactive = args.headless_seconds is None
        if args.backend == "kms" and not interactive:
            raise ValueError("KMS needs a controlling VT; use a timed child command instead")
        def stop(_signum, _frame):
            if workspace: workspace.quit = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_signals[sig] = signal.signal(sig, stop)
        if args.backend == "kms":
            for sig in (signal.SIGUSR1,signal.SIGUSR2):
                old_signals[sig]=signal.signal(sig,lambda number,_frame:transitions.append(number == signal.SIGUSR2))
            width,height=map(int,args.mode.lower().split('x')) if args.mode else (0,0)
            kms=KMS(args.drm_card,width,height)
            args.cols=min(240,kms.width//cache.cw)
            args.rows=min(120,kms.height//cache.ch)
        workspace = Workspace(args.cols, args.rows, cache.cw, cache.ch, command or None)
        if args.emoji_weights:
            emoji=Emoji(args.emoji_weights,args.emoji_manifest,args.library or args.home/'build/libneuronte.so',cache.cw,cache.ch,args.theme)
            renderer.emoji=emoji
        if args.icon_weights:
            icons=Icons(args.icon_weights,args.icon_manifest,args.library or args.home/'build/libneuronte.so',cache.cw,cache.ch,args.theme)
            renderer.icons=icons
        if args.pointer_weights:
            pointer_model=PointerModel(args.pointer_weights,args.pointer_manifest,
                                       args.library or args.home/'build/libneuronte.so',cache.cw,cache.ch)
        workspace.resize(args.cols,args.rows)
        if args.video_home:
            graphics=Graphics(args.video_home,args.library or args.home/'build/libneuronte.so',args.video_weights)
        if args.pointer_telemetry:
            telemetry=PointerTelemetry(args.pointer_telemetry, bool(args.snapshot and args.snapshot_live),
                                       bool(args.snapshot and args.snapshot_live and args.snapshot_history))
        if args.pointer_device:
            pointer_input=EvdevPointer(args.pointer_device,workspace,telemetry)
        composer = FrameComposer(renderer, graphics, pointer_model)
        if interactive:
            if not os.isatty(0) or (not kms and not os.isatty(1)):
                raise RuntimeError("Interactive presentation requires a terminal")
            saved_termios = termios.tcgetattr(0)
            tty.setraw(0)
            if not kms:
                presenter = workspace.lib.nd_presenter_open(20*cache.cw,18*cache.ch)
                if not presenter:
                    raise RuntimeError("Cannot start kitty-framebuffer presentation")
                outer=OuterInput(workspace)
                os.write(1,b'\x1b[?1003h\x1b[?1006h\x1b[?1016h')
        deadline = time.monotonic() + (args.headless_seconds or float("inf"))
        dirty = True
        while not workspace.quit and time.monotonic() < deadline:
            if transitions:
                dirty=True
                if not process_vt_transitions(transitions,kms,pointer_input,composer,telemetry):
                    continue
            if presenter:
                width,height=C.c_int(),C.c_int()
                workspace.lib.nd_presenter_size(presenter,C.byref(width),C.byref(height))
                ox,oy=C.c_int(),C.c_int()
                workspace.lib.nd_presenter_origin(presenter,C.byref(ox),C.byref(oy))
                outer.origin=ox.value,oy.value
                cols,rows=max(20,min(240,width.value//cache.cw)),max(18,min(120,height.value//cache.ch))
                if (cols,rows)!=(workspace.cols,workspace.rows):
                    workspace.resize(cols,rows)
                    dirty=True
            if dirty and (not kms or kms.active):
                render_start_ns = time.monotonic_ns() if telemetry else None
                frame, records, damage = composer.compose(workspace)
                frame_presented = False
                # Signals received during composition must be handled before
                # submission; an interrupted native flip also returns here.
                if transitions:
                    composer.valid=False
                    continue
                if presenter:
                    rgba = np.concatenate((frame, np.full((*frame.shape[:2],1),255,np.uint8)), axis=2)
                    if workspace.lib.nd_present(presenter, rgba.ctypes.data, frame.shape[1], frame.shape[0]):
                        raise RuntimeError("Framebuffer presentation failed")
                completion = kms.present(frame,damage,timed=bool(telemetry)) if kms else {}
                if telemetry:
                    telemetry.present(composer.pointer.position,renderer.last['frame_kind'],render_start_ns,completion)
                if completion is None:
                    composer.valid=False
                    dirty=True
                    continue
                frame_presented = True
                workspace.presented_pointer = composer.pointer.position
                renderer.last['presented_monotonic']=time.monotonic()
                if args.snapshot and args.snapshot_live:
                    save_snapshot(args.snapshot,frame,records,workspace,cache,renderer.last,graphics,
                                  [kms.width,kms.height] if kms else None,emoji,args.snapshot_history,icons,pointer_model)
                dirty = False
            changed = workspace.poll(0.04, keyboard=interactive,input_handler=outer.feed if outer else None,
                                     pointer_input=pointer_input,active=kms.active if kms else True)
            dirty = changed or dirty
            if outer and not changed:
                pending=bool(outer.pending)
                outer.flush()
                dirty=pending or dirty
        if args.snapshot and frame_presented:
            save_snapshot(args.snapshot,frame,records,workspace,cache,renderer.last,graphics,
                          [kms.width,kms.height] if kms else None,emoji,args.snapshot_history,icons,pointer_model)
    finally:
        if outer:
            os.write(1,b'\x1b[?1003l\x1b[?1006l\x1b[?1016l')
        if presenter:
            workspace.lib.nd_presenter_close(presenter)
        if pointer_input:
            pointer_input.close()
        if telemetry:
            telemetry.close()
        if kms:
            kms.close()
        if emoji:
            emoji.close()
        if icons:
            icons.close()
        if pointer_model:
            pointer_model.close()
        if saved_termios is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved_termios)
        if workspace:
            workspace.close()
        cache.close()
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
