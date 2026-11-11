"""Device and outer-terminal input, separate from the neural renderer."""
import ctypes as C
import fcntl
import os
from pathlib import Path
import re
import struct


class KMS:
    def __init__(self, card, width=0, height=0):
        self.lib = C.CDLL(str(Path(__file__).parent/'build/libneuronte-kms.so'), use_errno=True)
        for name, args, result in (
            ('nd_kms_open', [C.c_char_p,C.c_int,C.c_int,C.POINTER(C.c_int)], C.c_void_p),
            ('nd_kms_present', [C.c_void_p,C.c_void_p,C.c_int,C.c_int], C.c_int),
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

    def present(self, frame):
        if self.lib.nd_kms_present(self.ptr,frame.ctypes.data,frame.shape[1],frame.shape[0]):
            raise RuntimeError('KMS page flip failed')

    def vt(self, acquire):
        if self.lib.nd_kms_vt(self.ptr,acquire):
            raise RuntimeError('VT ownership transition failed')
        self.active = bool(acquire)

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
                button = ({0:1,1:3,2:2}.get(code&3,0) if not code&64 else 4+(code&1))
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


class EvdevPointer:
    """Relative mice and absolute tablets; keyboard stays on the VT keymap.

    No EVIOCGRAB: other VTs retain their input devices. Read events only while
    active, discard queued events on reacquire, and close every owned fd.
    """
    event = struct.Struct('@llHHi')

    def __init__(self, paths, workspace):
        self.workspace = workspace
        self.devices = {}
        self.x = self.y = 0
        self.button = 0
        self.pending_buttons = {}
        self.pending_motion = set()
        for path in paths:
            fd = os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
            axes = {}
            for axis in (0,1):
                try:
                    value = fcntl.ioctl(fd,0x80184540+axis,b'\0'*24)
                    current,lo,hi,_,_,_ = struct.unpack('6i',value)
                    axes[axis] = (lo,hi)
                    size=workspace.cols*workspace.cw if axis==0 else workspace.rows*workspace.ch
                    position=max(0,min(size-1,(current-lo)*(size-1)//max(1,hi-lo)))
                    if axis==0: self.x=position
                    else: self.y=position
                except OSError:
                    pass
            self.devices[fd] = axes
            self.pending_buttons[fd] = []

    def leave(self):
        # Cancel a logical drag on the owning VT before releasing it. Events
        # subsequently read from another VT are discarded, never forwarded.
        if self.button:
            self.workspace.mouse(self.x,self.y,1,self.button)
        self.button=0
        self.pending_motion.clear()
        for pending in self.pending_buttons.values(): pending.clear()

    def resume(self):
        self.button=0
        self.pending_motion.clear()
        for fd,axes in list(self.devices.items()):
            self.pending_buttons[fd].clear()
            while True:
                try: data=os.read(fd,self.event.size*64)
                except BlockingIOError: break
                if not data:
                    os.close(fd);del self.devices[fd];del self.pending_buttons[fd]
                    break
            if fd not in self.devices: continue
            # A tablet may have moved on the other VT without sending another
            # ABS event after return. Query its current position after draining.
            for axis in axes:
                try: current,lo,hi,_,_,_=struct.unpack('6i',fcntl.ioctl(fd,0x80184540+axis,b'\0'*24))
                except OSError: continue
                axes[axis]=(lo,hi)
                size=self.workspace.cols*self.workspace.cw if axis==0 else self.workspace.rows*self.workspace.ch
                position=max(0,min(size-1,(current-lo)*(size-1)//max(1,hi-lo)))
                if axis==0:self.x=position
                else:self.y=position
        if getattr(self.workspace,'pointer',None) is not None:
            self.workspace.pointer=self.x,self.y

    def read(self, fd, active=True):
        try:
            raw = os.read(fd,self.event.size*64)
        except BlockingIOError:
            return False
        if not raw:
            os.close(fd)
            del self.devices[fd]
            self.pending_buttons.pop(fd,None)
            self.pending_motion.discard(fd)
            return False
        changed = False
        for offset in range(0,len(raw),self.event.size):
            _,_,kind,code,value = self.event.unpack_from(raw,offset)
            if not active:
                self.button=0
                self.pending_buttons[fd].clear()
                self.pending_motion.discard(fd)
                continue
            w,h = self.workspace.cols*self.workspace.cw,self.workspace.rows*self.workspace.ch
            if kind == 2 and code in (0,1):
                if code == 0: self.x=max(0,min(w-1,self.x+value))
                else: self.y=max(0,min(h-1,self.y+value))
                changed = True
                self.pending_motion.add(fd)
            elif kind == 3 and code in self.devices[fd]:
                lo,hi = self.devices[fd][code]
                size = w if code == 0 else h
                position = max(0,min(size-1,(value-lo)*(size-1)//max(1,hi-lo)))
                if code == 0: self.x=position
                else: self.y=position
                changed = True
                self.pending_motion.add(fd)
            elif kind == 1 and code in (272,273,274):
                button={272:1,273:2,274:3}[code]
                # evdev may deliver BTN before ABS within one SYN report.
                # Dispatch clicks only after every axis in that report.
                self.pending_buttons[fd].append((button,bool(value)))
                changed = True
            elif kind == 2 and code == 8 and value:
                for _ in range(min(abs(value),32)):
                    self.workspace.mouse(self.x,self.y,0,4 if value > 0 else 5)
                changed = True
            elif kind == 0 and code == 0 and (fd in self.pending_motion or self.pending_buttons[fd]):
                for button,down in self.pending_buttons[fd]:
                    self.workspace.mouse(self.x,self.y,0 if down else 1,button)
                    self.button=button if down else 0
                if not self.pending_buttons[fd]:
                    self.workspace.mouse(self.x,self.y,2,self.button)
                self.pending_buttons[fd].clear()
                self.pending_motion.discard(fd)
                changed = True
        return changed

    def close(self):
        for fd in self.devices:
            os.close(fd)
        self.devices.clear()
