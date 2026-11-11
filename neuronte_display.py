#!/usr/bin/env python3
"""Stage-one terminal: Ghostty state, Neuronte masks, framebuffer presentation."""
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

import numpy as np

HERE = Path(__file__).resolve().parent
FG, BG = 0xDCE2EB, 0x13171E


class NativeCell(C.Structure):
    _fields_ = [("points", C.c_uint32 * 8)] + [
        (n, C.c_uint32) for n in ("length", "fg", "bg", "flags", "wide")]


def native():
    lib = C.CDLL(str(HERE / "build/libneuronte-display.so"))
    signatures = {
        "nd_new": ([C.c_int, C.c_int], C.c_void_p),
        "nd_free": ([C.c_void_p], None),
        "nd_resize": ([C.c_void_p] + [C.c_int] * 4, C.c_int),
        "nd_feed": ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        "nd_replies": ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        "nd_snapshot": ([C.c_void_p, C.POINTER(NativeCell), C.c_size_t, C.POINTER(C.c_int)], C.c_int),
        "nd_presenter_open": ([C.c_int, C.c_int], C.c_void_p),
        "nd_present": ([C.c_void_p, C.c_void_p, C.c_int, C.c_int], C.c_int),
        "nd_presenter_close": ([C.c_void_p], None),
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
        out = []
        for c in cells:
            points = "".join(chr(c.points[i]) for i in range(c.length))
            text = unicodedata.normalize("NFC", points)
            scalar = ord(text[0]) if text else 32
            flags = c.flags | (256 if len(text) > 1 else 0)
            if c.wide >= 2:  # spacer, never a second unknown-character mark
                scalar = 32
            out.append((scalar, c.fg, c.bg, flags))
        return out, tuple(cursor)

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

    def infer(self, scalar):
        buf = (C.c_float * (self.cw * self.ch))()
        if self.lib.neuronte_coverage(self.ptr, scalar, self.cw, self.ch, 2, buf):
            raise RuntimeError(f"Cannot render U+{scalar:04X}")
        self.stats["inferences"] += 1
        values = np.frombuffer(buf, dtype=np.float32).reshape(self.ch, self.cw)
        return np.floor(np.clip(values, 0, 1) * 255 + 0.5).astype(np.uint8)

    def get(self, scalar):
        if scalar in self.memory:
            self.stats["memory_hits"] += 1
            self.memory.move_to_end(scalar)
            return self.memory[scalar]
        path = self.directory / f"{scalar:06x}.mask"
        identity = self.identity_bytes + struct.pack("<I", scalar)
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
            mask = self.infer(scalar)
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
        self.memory[scalar] = mask
        while len(self.memory) > self.limit:
            self.memory.popitem(last=False)
        return mask

    def close(self):
        if self.ptr:
            self.lib.neuronte_close(self.ptr)
            self.ptr = None


def colour(n):
    return np.array([(n >> 16) & 255, (n >> 8) & 255, n & 255], dtype=np.uint16)


class Renderer:
    def __init__(self, cache):
        self.cache = cache
        self.previous = []
        self.frame = None
        self.last = {}

    def cell(self, record, cached=True):
        scalar, fg, bg, flags = record
        get = self.cache.get if cached else self.cache.infer
        mask = get(scalar)
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
        if flags & 1:  # Stage 1: bold uses brightness until a style API is adopted.
            fg = np.minimum(255, fg + 36)
        if flags & 32:
            fg = (fg + bg) // 2
        alpha = mask.astype(np.uint16)[..., None]
        return ((fg * alpha + bg * (255 - alpha) + 127) // 255).astype(np.uint8)

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
            if force or index >= len(self.previous) or record != self.previous[index]:
                y, x = divmod(index, cols)
                y, x = y * self.cache.ch, x * self.cache.cw
                self.frame[y:y + self.cache.ch, x:x + self.cache.cw] = self.cell(record)
                changed += 1
        self.previous = list(records)
        self.last = {"seconds": time.perf_counter() - start, "changed_cells": changed,
                     "italic_cells_shown_upright": sum(bool(c[3] & 2) for c in records),
                     "unsupported_grapheme_cells": sum(bool(c[3] & 256) for c in records)}
        return self.frame


class Pane:
    def __init__(self, command, cols, rows, cw, ch, lib):
        self.terminal = Terminal(cols, rows, lib)
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
        pane = Pane(self.command, self.cols, max(1, self.rows - 3), self.cw, self.ch, self.lib)
        self.tabs.append({"panes": [pane], "vertical": True, "bias": 0})
        self.tab, self.focus = len(self.tabs) - 1, 0
        self.layout()

    def split(self, vertical=True):
        if len(self.current["panes"]) >= 4:
            return
        self.current["vertical"] = vertical
        self.current["panes"].append(Pane(self.command, 8, 3, self.cw, self.ch, self.lib))
        self.focus = len(self.current["panes"]) - 1
        self.layout()

    def layout(self, tab=None):
        tab = self.current if tab is None else tab
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
        return rectangles

    def resize(self, cols, rows):
        if not 20 <= cols <= 240 or not 18 <= rows <= 120:
            raise ValueError("Display geometry must be 20..240 columns and 18..120 rows")
        self.cols, self.rows = cols, rows
        for tab in self.tabs:
            self.layout(tab)

    def input(self, data):
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

    def poll(self, timeout=0.05, keyboard=False):
        owners = {pane.fd: pane for pane in self.panes() if pane.fd is not None}
        readable = list(owners) + ([0] if keyboard else [])
        writable = [fd for fd, pane in owners.items() if pane.pending]
        ready, writes, _ = select.select(readable, writable, [], timeout)
        changed = False
        for fd in ready:
            if keyboard and fd == 0:
                data = os.read(0, 65536)
                if not data:
                    self.quit = True
                else:
                    self.input(data)
                changed = True
            elif owners[fd].fd is not None:
                changed = owners[fd].read() or changed
        for fd in writes:
            if owners[fd].fd is not None:
                owners[fd].flush()
        for pane in self.panes():
            pane.reap()
        return changed

    def grid(self):
        records = [(32, FG, BG, 0)] * (self.cols * self.rows)

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
                records[index] = (cp, fg, bg, flags ^ 16)
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


def save_snapshot(path, frame, records, workspace, cache, timing):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width, _ = frame.shape
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + frame.tobytes())
    path.with_suffix(".json").write_text(json.dumps({
        "version": 1, "cols": workspace.cols, "rows": workspace.rows,
        "cell_width": cache.cw, "cell_height": cache.ch, "cache_identity": cache.identity,
        "records": records, "timing": timing, "cache": dict(cache.stats)}, indent=2) + "\n")


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
    parser.add_argument("--headless-seconds", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 20 <= args.cols <= 240 or not 18 <= args.rows <= 120:
        parser.error("geometry must be 20..240 columns and 18..120 rows")
    if args.headless_seconds is not None and args.headless_seconds <= 0:
        parser.error("--headless-seconds must be positive")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    cache = MaskCache(args.weights or args.home/"weights/neuronte-split-v4.nrt",
                      args.library or args.home/"build/libneuronte.so", args.cache,
                      args.cell_width, args.cell_height)
    workspace = None
    presenter = None
    saved_termios = None
    old_signals = {}
    renderer = Renderer(cache)
    frame, records = None, None
    try:
        workspace = Workspace(args.cols, args.rows, cache.cw, cache.ch, command or None)
        interactive = args.headless_seconds is None
        if interactive:
            if not os.isatty(0) or not os.isatty(1):
                raise RuntimeError("Interactive presentation requires a terminal")
            saved_termios = termios.tcgetattr(0)
            tty.setraw(0)
            presenter = workspace.lib.nd_presenter_open(args.cols*cache.cw, args.rows*cache.ch)
            if not presenter:
                raise RuntimeError("Cannot start kitty-framebuffer presentation")
        def stop(_signum, _frame):
            workspace.quit = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_signals[sig] = signal.signal(sig, stop)
        deadline = time.monotonic() + (args.headless_seconds or float("inf"))
        dirty = True
        while not workspace.quit and time.monotonic() < deadline:
            if dirty:
                records = workspace.grid()
                frame = renderer.render(records, workspace.cols, workspace.rows)
                if presenter:
                    rgba = np.concatenate((frame, np.full((*frame.shape[:2],1),255,np.uint8)), axis=2)
                    if workspace.lib.nd_present(presenter, rgba.ctypes.data, frame.shape[1], frame.shape[0]):
                        raise RuntimeError("Framebuffer presentation failed")
            dirty = workspace.poll(0.04, keyboard=interactive)
        if workspace.tabs:
            records = workspace.grid()
            frame = renderer.render(records, workspace.cols, workspace.rows)
        if args.snapshot and frame is not None:
            save_snapshot(args.snapshot, frame, records, workspace, cache, renderer.last)
    finally:
        if presenter:
            workspace.lib.nd_presenter_close(presenter)
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
