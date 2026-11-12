"""Independent scalar-loop oracle for styled C colour layers.

No runtime colour, renderer, cache, or math helper is imported. Uses direct C
layers, explicit source-coordinate lookup and scalar float32 compositing.
"""
import ctypes as C
import numpy as np


def reference_colour(lib, handle, scalar, columns, cw, ch, fg, bg, flags, mode, line):
    lib.neuronte_layers.argtypes=[C.c_void_p]
    lib.neuronte_palette.argtypes=[C.c_void_p,C.c_int,C.POINTER(C.c_uint8)]
    lib.neuronte_coverage_layers.argtypes=[C.c_void_p,C.c_uint32]+[C.c_int]*3+[C.POINTER(C.c_float),C.POINTER(C.c_uint8)]
    lib.neuronte_coverage_layers_half.argtypes=[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float),C.POINTER(C.c_uint8)]
    k=lib.neuronte_layers(handle);width=columns*cw
    masks=np.empty((k,ch,width),np.float32);entries=np.empty((k,width),np.uint8)
    for half in range(columns):
        coverage=np.empty((k,ch,cw),np.float32);labels=np.empty(k,np.uint8)
        args=[handle,scalar]+([half] if columns==2 else [])+[cw,ch,2,coverage.ctypes.data_as(C.POINTER(C.c_float)),labels.ctypes.data_as(C.POINTER(C.c_uint8))]
        fn=lib.neuronte_coverage_layers_half if columns==2 else lib.neuronte_coverage_layers
        if fn(*args):raise ValueError('independent colour layers failed')
        masks[:,:,half*cw:(half+1)*cw]=coverage;entries[:,half*cw:(half+1)*cw]=labels[:,None]
    palette=np.empty((32,3),np.uint8)
    if lib.neuronte_palette(handle,mode,palette.ctypes.data_as(C.POINTER(C.c_uint8))):raise ValueError('reference palette failed')
    background=np.array([bg>>s&255 for s in (16,8,0)],np.float32)
    foreground=np.array([fg>>s&255 for s in (16,8,0)],np.float32)
    output=np.empty((ch,width,3),np.uint8);alpha=np.empty((ch,width),np.uint8)
    f=np.float32
    stroke=max(1,(cw+16)//32) if flags&1 else 0
    for y in range(ch):
        numerator=(27*ch-40*y-20)*cw
        offset=((abs(numerator)+40*ch)//(80*ch))*(1 if numerator>=0 else -1) if flags&2 else 0
        for x in range(width):
            rgb=np.zeros(3,np.float32);a=f(0)
            for layer in range(k):
                samples=[sx for sx in range(x-offset-stroke,x-offset+1) if 0<=sx<width]
                if not samples:continue
                source=max(samples,key=lambda sx:(float(masks[layer,y,sx]),sx))
                c=masks[layer,y,source];entry=int(entries[layer,source]);keep=f(1)-c
                if entry==1:
                    rgb=rgb*keep;a=f(a*keep)
                else:
                    ink=palette[entry].astype(np.float32) if 2<=entry<=31 else foreground
                    if flags&32:ink=np.floor((ink+background)/f(2)).astype(np.float32)
                    rgb=ink*c+rgb*keep;a=f(c+a*keep)
            if line is not None:
                l=f(line[y,x%cw])/f(255)
                retained=max(f(0),f(a-l))
                ink=foreground
                if flags&32:ink=np.floor((ink+background)/f(2)).astype(np.float32)
                rgb=(rgb/a if a>0 else rgb)*retained+ink*l
                a=max(a,l)
            if flags&64:rgb[:]=0;a=f(0)
            source=np.floor(np.clip(rgb/a,0,255)+f(.5)) if a>0 else np.zeros(3,np.float32)
            output[y,x]=np.floor(source*a+background*(f(1)-a)+f(.5)).clip(0,255).astype(np.uint8)
            alpha[y,x]=int(f(np.clip(a,0,1)*f(255))+f(.5))
    return output,alpha
