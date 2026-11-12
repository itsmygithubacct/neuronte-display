"""C-derived colour layers shared by explicit icon and emoji selections."""
import collections
import ctypes as C
import hashlib
from pathlib import Path
import numpy as np
from display_colour import transform_layers, compose_layers, paint_opaque

POLICY = 'layers-v1-wide-geometry-line-union'


def remember(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit: cache.popitem(last=False)
    return value


class ColourArt:
    def __init__(self, weights, manifest, library, cw, ch, columns, theme):
        self.cw, self.ch, self.columns, self.theme = cw, ch, columns, theme
        self.identity = dict(weights=hashlib.sha256(Path(weights).read_bytes()).hexdigest(),
                             manifest=hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
                             theme=theme, colour_policy=POLICY)
        self.lib = C.CDLL(str(library)); self.ptr = C.c_void_p()
        signatures = {
            'neuronte_open_file':[C.c_char_p,C.POINTER(C.c_void_p)],
            'neuronte_close':[C.c_void_p], 'neuronte_layers':[C.c_void_p],
            'neuronte_palette':[C.c_void_p,C.c_int,C.POINTER(C.c_uint8)],
            'neuronte_set_theme':[C.c_void_p,C.c_int],
            'neuronte_set_samples':[C.c_void_p,C.c_int],
            'neuronte_draw_scalar':[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_uint32],
            'neuronte_draw_half':[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_int,C.c_uint32],
            'neuronte_coverage':[C.c_void_p,C.c_uint32]+[C.c_int]*3+[C.POINTER(C.c_float)],
            'neuronte_coverage_half':[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float)],
            'neuronte_coverage_layers':[C.c_void_p,C.c_uint32]+[C.c_int]*3+[C.POINTER(C.c_float),C.POINTER(C.c_uint8)],
            'neuronte_coverage_layers_half':[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float),C.POINTER(C.c_uint8)],
        }
        for name, args in signatures.items(): getattr(self.lib,name).argtypes = args
        if self.lib.neuronte_open_file(str(weights).encode(),C.byref(self.ptr)):
            raise ValueError('cannot load selected colour weights')
        self.lib.neuronte_set_samples(self.ptr,2)
        self.layer_count = self.lib.neuronte_layers(self.ptr)
        if not self.layer_count:
            self.close(); raise ValueError('selected artwork must have colour layers')
        self.palettes = []
        for mode in (0,1):
            pal = np.empty((32,3),np.uint8)
            if self.lib.neuronte_palette(self.ptr,mode,pal.ctypes.data_as(C.POINTER(C.c_uint8))):
                self.close(); raise ValueError('cannot read colour palette')
            self.palettes.append(pal)
        self.cache, self.masks, self.layer_cache = (collections.OrderedDict() for _ in range(3))

    def mode(self, bg):
        return (int(299*(bg>>16&255)+587*(bg>>8&255)+114*(bg&255)>127500)
                if self.theme=='auto' else int(self.theme=='light'))

    def layers(self, scalar, style):
        key = scalar, style
        if key in self.layer_cache:
            self.layer_cache.move_to_end(key); return self.layer_cache[key]
        masks, entries = [], []
        for half in range(self.columns):
            values = np.empty((self.layer_count,self.ch,self.cw),np.float32)
            labels = np.empty(self.layer_count,np.uint8)
            args = [self.ptr,scalar]+([half] if self.columns==2 else [])+[self.cw,self.ch,2,
                values.ctypes.data_as(C.POINTER(C.c_float)),labels.ctypes.data_as(C.POINTER(C.c_uint8))]
            fn = self.lib.neuronte_coverage_layers_half if self.columns==2 else self.lib.neuronte_coverage_layers
            if fn(*args): raise RuntimeError('cannot infer colour layers')
            masks.append(values)
            entries.append(np.broadcast_to(labels[:,None,None],values.shape))
        values = np.concatenate(masks,axis=2)
        labels = np.concatenate(entries,axis=2)
        result = transform_layers(values,labels,self.cw,style)
        for value in result: value.setflags(write=False)
        return remember(self.layer_cache,key,result,64)

    def unstyled(self, scalar, fg, background, mode):
        """Keep the accepted C draw path and its exact rounding for style0."""
        packed = (np.uint32(0xff000000) | background[...,0].astype(np.uint32)<<16 |
                  background[...,1].astype(np.uint32)<<8 | background[...,2].astype(np.uint32))
        packed = np.ascontiguousarray(packed)
        self.lib.neuronte_set_theme(self.ptr,mode)
        width = self.cw*self.columns
        for half in range(self.columns):
            args = [self.ptr,packed.ctypes.data,width,width,self.ch,half*self.cw,0,self.cw,self.ch,
                    scalar]+([half] if self.columns==2 else [])+[0xff000000|fg]
            fn = self.lib.neuronte_draw_half if self.columns==2 else self.lib.neuronte_draw_scalar
            if fn(*args): raise RuntimeError('cannot draw colour artwork')
        return np.stack([(packed>>s&255).astype(np.uint8) for s in (16,8,0)],axis=2)

    def line(self, flags, lines):
        if not flags & (4|8|128): return None
        if lines is None or lines.shape != (self.ch,self.cw):
            raise ValueError('colour decorations require the learned text line masks')
        return np.tile(lines,(1,self.columns))

    def cell(self, scalar, half, fg, bg, flags, lines=None):
        if not 0 <= half < self.columns: raise ValueError('invalid colour cell half')
        if flags&16: fg,bg = bg,fg
        mode = self.mode(bg)
        line = self.line(flags,lines) if not flags&64 else None
        key = scalar,fg,bg,flags&255,mode,line.tobytes() if line is not None else None
        if key not in self.cache:
            background = np.full((self.ch,self.cw*self.columns,3),[bg>>s&255 for s in (16,8,0)],np.uint8)
            if flags&64: frame = background
            elif not flags&(3|32|4|8|128): frame = self.unstyled(scalar,fg,background,mode)
            else:
                layers,entries = self.layers(scalar,flags&3)
                premul,alpha = compose_layers(layers,entries,self.palettes[mode],fg,bg,bool(flags&32),line)
                frame = paint_opaque(premul,alpha,bg)
            frame.setflags(write=False)
            remember(self.cache,key,frame,512)
        self.cache.move_to_end(key)
        return self.cache[key][:,half*self.cw:(half+1)*self.cw]

    def mask(self, scalar, half, flags, lines=None):
        if not 0 <= half < self.columns: raise ValueError('invalid colour cell half')
        if flags&64: return np.zeros((self.ch,self.cw),np.uint8)
        line = self.line(flags,lines)
        key = scalar,flags&3,line.tobytes() if line is not None else None
        if key not in self.masks:
            if not flags&3 and line is None:
                parts = []
                for part in range(self.columns):
                    values = np.empty((self.ch,self.cw),np.float32)
                    args = [self.ptr,scalar]+([part] if self.columns==2 else [])+[self.cw,self.ch,2,
                        values.ctypes.data_as(C.POINTER(C.c_float))]
                    fn = self.lib.neuronte_coverage_half if self.columns==2 else self.lib.neuronte_coverage
                    if fn(*args): raise RuntimeError('cannot infer colour composite alpha')
                    parts.append(values)
                alpha = np.concatenate(parts,axis=1)
            else:
                layers,entries = self.layers(scalar,flags&3)
                _,alpha = compose_layers(layers,entries,self.palettes[0],0,0,False,line)
            result = np.floor(np.clip(alpha,0,1)*255+.5).astype(np.uint8)
            result.setflags(write=False)
            remember(self.masks,key,result,512)
        self.masks.move_to_end(key)
        return self.masks[key][:,half*self.cw:(half+1)*self.cw]

    def close(self):
        if self.ptr: self.lib.neuronte_close(self.ptr); self.ptr = None
