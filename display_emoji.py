"""Explicit two-cell colour artwork; availability comes from its manifest."""
import collections
import ctypes as C
import hashlib
import json
from pathlib import Path
import numpy as np


class Emoji:
    def __init__(self,weights,manifest,library,cw,ch,theme='auto'):
        doc=json.loads(Path(manifest).read_text())
        if doc.get('cell_columns') != 2:
            raise ValueError('emoji manifest must declare cell_columns=2')
        self.scalars={int(row['scalar'][2:],16) for row in doc['glyphs']}
        if not self.scalars or any(cp>0xeffff or 0xd800<=cp<=0xdfff for cp in self.scalars):
            raise ValueError('invalid two-cell emoji manifest')
        self.cw,self.ch,self.theme=cw,ch,theme
        self.identity=dict(weights=hashlib.sha256(Path(weights).read_bytes()).hexdigest(),
                           manifest=hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),theme=theme)
        self.lib=C.CDLL(str(library)); self.ptr=C.c_void_p()
        self.lib.neuronte_open_file.argtypes=[C.c_char_p,C.POINTER(C.c_void_p)]
        self.lib.neuronte_close.argtypes=[C.c_void_p]
        self.lib.neuronte_set_theme.argtypes=self.lib.neuronte_set_samples.argtypes=[C.c_void_p,C.c_int]
        self.lib.neuronte_draw_half.argtypes=[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_int,C.c_uint32]
        self.lib.neuronte_coverage_half.argtypes=[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float)]
        if self.lib.neuronte_open_file(str(weights).encode(),C.byref(self.ptr)):
            raise ValueError('cannot load selected emoji weights')
        self.lib.neuronte_set_samples(self.ptr,2)
        self.cache=collections.OrderedDict()
        self.masks=collections.OrderedDict()

    def selected(self,record):
        value,_,_,flags=record
        if not flags&512: return None
        if isinstance(value,(tuple,list)):
            if 0xfe0e in value: return None
            visible=[cp for cp in value if cp!=0xfe0f]
            if len(visible)!=1: return None
            value=visible[0]
        return value if value in self.scalars else None

    def cell(self,scalar,half,fg,bg,flags):
        if flags&16: fg,bg=bg,fg
        if flags&32:
            fg=sum((((fg>>shift&255)+(bg>>shift&255))//2)<<shift for shift in (16,8,0))
        theme=(int(299*(bg>>16&255)+587*(bg>>8&255)+114*(bg&255)>127500)
               if self.theme=='auto' else int(self.theme=='light'))
        key=scalar,half,fg,bg,flags&64,theme
        if key in self.cache:
            self.cache.move_to_end(key); return self.cache[key]
        packed=np.full((self.ch,self.cw),0xff000000|bg,np.uint32)
        if not flags&64:
            self.lib.neuronte_set_theme(self.ptr,theme)
            if self.lib.neuronte_draw_half(self.ptr,packed.ctypes.data,self.cw,self.cw,self.ch,
                                           0,0,self.cw,self.ch,scalar,half,0xff000000|fg):
                raise RuntimeError('cannot draw emoji half')
        frame=np.stack([(packed>>shift&255).astype(np.uint8) for shift in (16,8,0)],axis=2)
        frame.setflags(write=False); self.cache[key]=frame
        while len(self.cache)>512: self.cache.popitem(last=False)
        return frame

    def mask(self,scalar,half,flags):
        if flags&64: return np.zeros((self.ch,self.cw),np.uint8)
        key=scalar,half
        if key not in self.masks:
            values=np.empty((self.ch,self.cw),np.float32)
            if self.lib.neuronte_coverage_half(self.ptr,scalar,half,self.cw,self.ch,2,
                                               values.ctypes.data_as(C.POINTER(C.c_float))):
                raise RuntimeError('cannot infer emoji composite alpha')
            result=np.floor(np.clip(values,0,1)*255+.5).astype(np.uint8)
            result.setflags(write=False);self.masks[key]=result
            while len(self.masks)>512:self.masks.popitem(last=False)
        self.masks.move_to_end(key)
        return self.masks[key]

    def close(self):
        if self.ptr: self.lib.neuronte_close(self.ptr); self.ptr=None
