#!/usr/bin/env python3
"""Independent screenshot oracle: JSON cells -> direct C inference -> RGB.

Does not import the display renderer, display cache, or video Python decoder.
"""
import argparse
import base64
import ctypes as C
import hashlib
import json
import unicodedata
from pathlib import Path
import numpy as np
if __package__:
    from .reference_graphemes import reference_composer
else:
    from reference_graphemes import reference_composer


def reconstruct(document, weights, library, video_weights=None, emoji_weights=None, emoji_manifest=None):
    lib=C.CDLL(str(library))
    lib.neuronte_open_file.argtypes=[C.c_char_p,C.POINTER(C.c_void_p)]
    lib.neuronte_close.argtypes=[C.c_void_p]
    lib.neuronte_coverage.argtypes=[C.c_void_p,C.c_uint32,C.c_int,C.c_int,C.c_int,C.POINTER(C.c_float)]
    if hasattr(lib,'neuronte_coverage_styled'):
        lib.neuronte_coverage_styled.argtypes=lib.neuronte_coverage.argtypes[:-1]+[C.c_uint,C.POINTER(C.c_float)]
    handle=C.c_void_p()
    video=C.c_void_p()
    emoji=C.c_void_p()
    if lib.neuronte_open_file(str(weights).encode(),C.byref(handle)):
        raise ValueError('cannot load reference weights')
    cw,ch=document['cell_width'],document['cell_height']
    width,height=document['screen']
    frame=np.zeros((height,width,3),np.uint8)
    ink=np.zeros((height,width),np.uint8)
    masks={}
    def mask(cp,style=0):
        key=cp,style
        if key not in masks:
            values=np.empty((ch,cw),np.float32)
            output=values.ctypes.data_as(C.POINTER(C.c_float))
            rc=(lib.neuronte_coverage_styled(handle,cp,cw,ch,2,style,output) if style
                else lib.neuronte_coverage(handle,cp,cw,ch,2,output))
            if rc: raise ValueError('reference inference failed')
            masks[key]=np.floor(np.clip(values,0,1)*255+0.5).astype(np.uint16)
        return masks[key]
    def rgb(value): return np.array([value>>16&255,value>>8&255,value&255],dtype=np.uint16)
    try:
        composed_mask=None
        if document.get('graphemes'):
            policy_path=Path(__file__).resolve().parents[1]/'graphemes.json'
            raw=policy_path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=document['graphemes']['sha256']:
                raise ValueError('reference grapheme policy identity differs')
            composed_mask=reference_composer(json.loads(raw),hashlib.sha256(Path(weights).read_bytes()).hexdigest(),cw,ch,mask)
        for index,(scalar,fg,bg,flags) in enumerate(document['records']):
            if composed_mask:
                coverage=composed_mask(scalar,flags&3).copy()
            elif isinstance(scalar,(list,tuple)):
                text=unicodedata.normalize('NFC',''.join(chr(cp) for cp in scalar if cp not in (65038,65039)))
                scalar=ord(text) if len(text)==1 else 65533
                coverage=mask(scalar,flags&3).copy()
            else:coverage=mask(scalar,flags&3).copy()
            for flag,cp in ((4,9601),(8,9472),(128,9620)):
                if flags&flag: coverage=np.maximum(coverage,mask(cp))
            if flags&64: coverage[:]=0
            foreground,background=rgb(fg),rgb(bg)
            if flags&16: foreground,background=background,foreground
            if flags&32: foreground=(foreground+background)//2
            y,x=divmod(index,document['cols'])
            ys,xs=slice(y*ch,(y+1)*ch),slice(x*cw,(x+1)*cw)
            alpha=coverage[...,None]
            frame[ys,xs]=((foreground*alpha+background*(255-alpha)+127)//255).astype(np.uint8)
            ink[ys,xs]=coverage
        if document.get('emoji'):
            if not emoji_weights or not emoji_manifest:
                raise ValueError('emoji reference weights and manifest required')
            for key,path in (('weights',emoji_weights),('manifest',emoji_manifest)):
                if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=document['emoji'][key]:
                    raise ValueError('emoji reference identity differs')
            if lib.neuronte_open_file(str(emoji_weights).encode(),C.byref(emoji)):
                raise ValueError('cannot load emoji reference')
            allowed={int(row['scalar'][2:],16) for row in json.loads(Path(emoji_manifest).read_text())['glyphs']}
            lib.neuronte_set_samples.argtypes=lib.neuronte_set_theme.argtypes=[C.c_void_p,C.c_int]
            lib.neuronte_draw_half.argtypes=[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_int,C.c_uint32]
            lib.neuronte_coverage_half.argtypes=[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float)]
            lib.neuronte_set_samples(emoji,2)
            for index,(value,fg,bg,flags) in enumerate(document['records']):
                if not flags&512: continue
                if isinstance(value,list):
                    if 0xfe0e in value: continue
                    value=[cp for cp in value if cp!=0xfe0f]
                    if len(value)!=1: continue
                    value=value[0]
                if value not in allowed: continue
                y,x=divmod(index,document['cols'])
                if flags&16: fg,bg=bg,fg
                if flags&32: fg=sum((((fg>>s&255)+(bg>>s&255))//2)<<s for s in (16,8,0))
                theme=document['emoji']['theme']
                mode=(int(299*(bg>>16&255)+587*(bg>>8&255)+114*(bg&255)>127500) if theme=='auto' else int(theme=='light'))
                lib.neuronte_set_theme(emoji,mode)
                for half in (0,1):
                    if x+half>=document['cols']: continue
                    packed=np.full((ch,cw),0xff000000|bg,np.uint32)
                    if not flags&64 and lib.neuronte_draw_half(emoji,packed.ctypes.data,cw,cw,ch,0,0,cw,ch,value,half,0xff000000|fg):
                        raise ValueError('reference half drawing failed')
                    frame[y*ch:(y+1)*ch,(x+half)*cw:(x+half+1)*cw]=np.stack([packed>>s&255 for s in (16,8,0)],axis=2).astype(np.uint8)
                    coverage=np.zeros((ch,cw),np.float32)
                    if not flags&64 and lib.neuronte_coverage_half(emoji,value,half,cw,ch,2,
                            coverage.ctypes.data_as(C.POINTER(C.c_float))):
                        raise ValueError('reference emoji alpha failed')
                    ink[y*ch:(y+1)*ch,(x+half)*cw:(x+half+1)*cw]=np.floor(np.clip(coverage,0,1)*255+.5).astype(np.uint8)
        if document.get('planes'):
            if not video_weights or lib.neuronte_open_file(str(video_weights).encode(),C.byref(video)):
                raise ValueError('video weights required')
            planes=[]
            for cp in (0x2590,0x1cd33,0x2584):
                values=np.empty((4,2),np.float32)
                if lib.neuronte_coverage(video,cp,2,4,1,values.ctypes.data_as(C.POINTER(C.c_float))):
                    raise ValueError('reference plane inference failed')
                planes.append(values)
            def decode(raw,shape):
                colours=np.frombuffer(base64.b64decode(raw,validate=True),np.uint8).reshape(shape)
                result=np.zeros((shape[0],4,shape[1],2,3),np.float32)
                for code in range(8):
                    alpha=np.ones((4,2),np.float32)
                    for bit in range(3): alpha*=planes[bit] if code&(1<<bit) else 1-planes[bit]
                    result+=colours[:,None,:,None,code,:]*alpha[None,:,None,:,None]
                return np.rint(result.reshape(shape[0]*4,shape[1]*2,3)).clip(0,255).astype(np.uint8)
            for plane in document['planes']:
                source=decode(plane['colours'],plane['shape'])
                x0,y0,x1,y1=plane['clip']; x,y=plane['x'],plane['y']
                src=source[y0-y:y1-y,x0-x:x1-x].astype(np.uint16)
                dst=frame[y0:y1,x0:x1].astype(np.uint16)
                if plane['alpha']:
                    a=decode(plane['alpha'],plane['shape'])[y0-y:y1-y,x0-x:x1-x,:1].astype(np.uint16)
                    src=(src*a+dst*(255-a)+127)//255
                if plane['z']<0:
                    a=ink[y0:y1,x0:x1,None].astype(np.uint16)
                    src=(dst*a+src*(255-a)+127)//255
                frame[y0:y1,x0:x1]=src.astype(np.uint8)
        if document.get('pointer'):
            x,y=document['pointer']
            if 0<=x<width and 0<=y<height:
                a=mask(0x2196)[:height-y,:width-x,None]
                patch=frame[y:y+a.shape[0],x:x+a.shape[1]]
                patch[:]=((255*a+patch.astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
        return frame
    finally:
        lib.neuronte_close(handle)
        if video: lib.neuronte_close(video)
        if emoji: lib.neuronte_close(emoji)


def read_ppm(path):
    with open(path,'rb') as fh:
        if fh.readline().strip()!=b'P6': raise ValueError('expected RGB PPM')
        line=fh.readline()
        while line.startswith(b'#'): line=fh.readline()
        width,height=map(int,line.split())
        if fh.readline().strip()!=b'255': raise ValueError('expected u8 PPM')
        return np.frombuffer(fh.read(),np.uint8).reshape(height,width,3)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('grid',type=Path); p.add_argument('screenshot',type=Path)
    p.add_argument('--weights',type=Path,required=True); p.add_argument('--library',type=Path,required=True)
    p.add_argument('--video-weights',type=Path); p.add_argument('--output',type=Path)
    p.add_argument('--emoji-weights',type=Path); p.add_argument('--emoji-manifest',type=Path)
    args=p.parse_args()
    doc=json.loads(args.grid.read_text())
    for field,path in (('weights',args.weights),('library',args.library)):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=doc['cache_identity'][field]:
            raise ValueError('reference asset hash differs: '+field)
    expected=reconstruct(doc,args.weights,args.library,args.video_weights,args.emoji_weights,args.emoji_manifest)
    actual=read_ppm(args.screenshot)
    if expected.shape!=actual.shape: raise ValueError('screenshot geometry differs')
    delta=np.abs(expected.astype(int)-actual.astype(int))
    result=dict(equal=bool(np.array_equal(expected,actual)),max_error=int(delta.max()),
                differing_pixels=int(np.count_nonzero(np.any(delta,axis=2))),pixels=actual.shape[0]*actual.shape[1])
    if args.output: args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    return 0 if result['equal'] else 1


if __name__=='__main__': raise SystemExit(main())
