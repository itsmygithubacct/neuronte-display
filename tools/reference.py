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
    from .reference_colour import reference_colour
else:
    from reference_graphemes import reference_composer
    from reference_colour import reference_colour


def reconstruct(document, weights, library, video_weights=None, emoji_weights=None, emoji_manifest=None, icon_weights=None, icon_manifest=None, pointer_weights=None, pointer_manifest=None):
    lib=C.CDLL(str(library))
    lib.neuronte_open_file.argtypes=[C.c_char_p,C.POINTER(C.c_void_p)]
    lib.neuronte_close.argtypes=[C.c_void_p]
    lib.neuronte_coverage.argtypes=[C.c_void_p,C.c_uint32,C.c_int,C.c_int,C.c_int,C.POINTER(C.c_float)]
    if hasattr(lib,'neuronte_coverage_styled'):
        lib.neuronte_coverage_styled.argtypes=lib.neuronte_coverage.argtypes[:-1]+[C.c_uint,C.POINTER(C.c_float)]
    handle=C.c_void_p()
    video=C.c_void_p()
    pointer_handle=C.c_void_p()
    artworks=[]
    icon_handle=None
    icon_inventory=None
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
        lib.neuronte_set_samples.argtypes=lib.neuronte_set_theme.argtypes=[C.c_void_p,C.c_int]
        lib.neuronte_draw_half.argtypes=[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_int,C.c_uint32]
        lib.neuronte_draw_scalar.argtypes=[C.c_void_p,C.c_void_p]+[C.c_int]*7+[C.c_uint32,C.c_uint32]
        lib.neuronte_coverage_half.argtypes=[C.c_void_p,C.c_uint32]+[C.c_int]*4+[C.POINTER(C.c_float)]
        for name,art_weights,manifest_path,columns in (('emoji',emoji_weights,emoji_manifest,2),('icons',icon_weights,icon_manifest,1)):
            identity=document.get(name)
            if not identity:continue
            if not art_weights or not manifest_path:raise ValueError(name+' reference weights and manifest required')
            for key,path in (('weights',art_weights),('manifest',manifest_path)):
                if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=identity[key]:raise ValueError(name+' reference identity differs')
            inventory=json.loads(Path(manifest_path).read_text())
            allowed={int(row['scalar'][2:],16) for row in inventory['glyphs']}
            art=C.c_void_p()
            if lib.neuronte_open_file(str(art_weights).encode(),C.byref(art)):raise ValueError('cannot load colour reference')
            artworks.append(art);lib.neuronte_set_samples(art,2)
            if name=='icons':icon_handle,icon_inventory=art,inventory
            computed={}
            for index,(value,fg0,bg0,original_flags) in enumerate(document['records']):
                if columns==2:
                    if not original_flags&512:continue
                    if isinstance(value,(list,tuple)):
                        if 0xfe0e in value:continue
                        value=[cp for cp in value if cp!=0xfe0f]
                        if len(value)!=1:continue
                        value=value[0]
                    if identity.get('colour_policy') and (index%document['cols']+1>=document['cols'] or not document['records'][index+1][3]&1024):continue
                elif not isinstance(value,int) or original_flags&(512|1024):continue
                if value not in allowed:continue
                y,x=divmod(index,document['cols'])
                for half in range(columns):
                    if x+half>=document['cols']:continue
                    flags=original_flags
                    if columns==2 and identity.get('colour_policy'):
                        flags ^= 16 if original_flags&2048 else 0
                    elif columns==1 and identity.get('colour_policy'):
                        flags ^= 16 if original_flags&2048 else 0
                    fg,bg=fg0,bg0
                    if flags&16:fg,bg=bg,fg
                    theme=identity['theme']
                    mode=(int(299*(bg>>16&255)+587*(bg>>8&255)+114*(bg&255)>127500) if theme=='auto' else int(theme=='light'))
                    if identity.get('colour_policy') and flags&(3|32|4|8|128):
                        line=None
                        if flags&(4|8|128):
                            line=np.zeros((ch,cw),np.uint8)
                            for bit,cp in ((4,0x2581),(8,0x2500),(128,0x2594)):
                                if flags&bit:line=np.maximum(line,mask(cp).astype(np.uint8))
                        key=value,fg,bg,flags&255,mode
                        if key not in computed:computed[key]=reference_colour(lib,art,value,columns,cw,ch,fg,bg,flags,mode,line)
                        colour,coverage=computed[key]
                        pixels=colour[:,half*cw:(half+1)*cw];coverage=coverage[:,half*cw:(half+1)*cw]
                    else:
                        # Old receipts keep their original unstyled-art policy.
                        if flags&32:fg=sum((((fg>>s&255)+(bg>>s&255))//2)<<s for s in (16,8,0))
                        lib.neuronte_set_theme(art,mode)
                        packed=np.full((ch,cw),0xff000000|bg,np.uint32)
                        coverage=np.zeros((ch,cw),np.float32)
                        if not flags&64:
                            args=[art,packed.ctypes.data,cw,cw,ch,0,0,cw,ch,value]+([half] if columns==2 else [])+[0xff000000|fg]
                            fn=lib.neuronte_draw_half if columns==2 else lib.neuronte_draw_scalar
                            if fn(*args):raise ValueError('reference colour drawing failed')
                            args=[art,value]+([half] if columns==2 else [])+[cw,ch,2,coverage.ctypes.data_as(C.POINTER(C.c_float))]
                            fn=lib.neuronte_coverage_half if columns==2 else lib.neuronte_coverage
                            if fn(*args):raise ValueError('reference colour alpha failed')
                        pixels=np.stack([packed>>s&255 for s in (16,8,0)],axis=2).astype(np.uint8)
                        coverage=np.floor(np.clip(coverage,0,1)*255+.5).astype(np.uint8)
                    frame[y*ch:(y+1)*ch,(x+half)*cw:(x+half+1)*cw]=pixels
                    ink[y*ch:(y+1)*ch,(x+half)*cw:(x+half+1)*cw]=coverage
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
        cursor=document.get('cursor')
        if cursor and (cursor['shape'] in ('bar','underline') or cursor.get('colour')):
            col,row=cursor['column'],cursor['row']
            if 0<=col<document['cols'] and 0<=row<document['rows']:
                _,fg,bg,flags=document['records'][row*document['cols']+col]
                if flags&16:fg,bg=bg,fg
                fg,bg=rgb(fg),rgb(bg)
                if flags&32:fg=(fg+bg)//2
                x,y=col*cw,row*ch
                cp={'block':0x2588,'bar':0x258f,'underline':0x2581}[cursor['shape']]
                a=mask(cp)[:height-y,:width-x,None]
                patch=frame[y:y+a.shape[0],x:x+a.shape[1]]
                if cursor.get('colour'):
                    brightness=patch.astype(np.int32)@[299,587,114]
                    fg=np.where(brightness[...,None]>127500,0,255).astype(np.uint16)
                patch[:]=((fg*a+patch.astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
        if document.get('pointer'):
            px,py=document['pointer']
            if 0<=px<width and 0<=py<height:
                if document.get('pointer_model'):
                    identity=document['pointer_model']
                    if pointer_weights is None or pointer_manifest is None:
                        raise ValueError('reference requires the selected dedicated pointer model and manifest')
                    for key,path in (('weights',pointer_weights),('manifest',pointer_manifest)):
                        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=identity[key]:
                            raise ValueError('reference pointer asset hash differs')
                    spec=json.loads(Path(pointer_manifest).read_text())
                    sizes=[s for s in spec['sizes'] if (s['width'],s['height'])==(cw,ch)]
                    if (spec['format']!='neuronte-pointer-manifest' or spec['version']!=1 or
                        spec['scalar']!=0x100001 or identity['scalar']!=0x100001 or len(sizes)!=1 or
                        spec['model_sha256']!=identity['weights'] or spec['crop'] is not None or
                        spec['other_scalars']!='unsupported' or sizes[0]['hotspot']!=identity['hotspot'] or
                        sizes[0]['supersampling']!=identity['supersampling'] or
                        (identity['width'],identity['height'])!=(cw,ch)):
                        raise ValueError('reference dedicated pointer identity differs')
                    if lib.neuronte_open_file(str(pointer_weights).encode(),C.byref(pointer_handle)):
                        raise ValueError('cannot open reference pointer model')
                    lib.neuronte_set_samples(pointer_handle,sizes[0]['supersampling'])
                    lib.neuronte_set_theme(pointer_handle,0)
                    hx,hy=sizes[0]['hotspot'];x,y=px-hx,py-hy
                    x0,y0,x1,y1=max(0,x),max(0,y),min(width,x+cw),min(height,y+ch)
                    packed=np.full((ch,cw),0xff000000,np.uint32)
                    for channel,shift in enumerate((16,8,0)):
                        packed[y0-y:y1-y,x0-x:x1-x]|=frame[y0:y1,x0:x1,channel].astype(np.uint32)<<shift
                    if lib.neuronte_draw_scalar(pointer_handle,packed.ctypes.data,cw,cw,ch,0,0,cw,ch,0x100001,0xffffffff):
                        raise ValueError('reference dedicated pointer draw failed')
                    painted=np.stack([packed>>s&255 for s in (16,8,0)],axis=2).astype(np.uint8)
                    frame[y0:y1,x0:x1]=painted[y0-y:y1-y,x0-x:x1-x]
                elif icon_handle:
                    spec=icon_inventory['pointer'];cp=int(spec['scalar'][2:],16)
                    hx,hy=[size*n//d for size,n,d in zip((cw,ch),spec['hotspot_numerator'],spec['hotspot_denominator'])]
                    expected=dict(scalar=cp,hotspot=[hx,hy],contrast='per-pixel-luma-127.5')
                    if document['icons']['pointer']!=expected or cp not in {int(r['scalar'][2:],16) for r in icon_inventory['glyphs']}:raise ValueError('reference pointer identity differs')
                    x,y=px-hx,py-hy;x0,y0,x1,y1=max(0,x),max(0,y),min(width,x+cw),min(height,y+ch)
                    background=np.zeros((ch,cw,3),np.uint8)
                    background[y0-y:y1-y,x0-x:x1-x]=frame[y0:y1,x0:x1]
                    theme=document['icons']['theme'];pixel=frame[py,px].astype(int)
                    mode=int(sum(a*b for a,b in zip(pixel,(299,587,114)))>127500) if theme=='auto' else int(theme=='light')
                    lib.neuronte_set_theme(icon_handle,mode);painted=[]
                    for foreground in (0xffffff,0):
                        packed=np.full((ch,cw),0xff000000,np.uint32)
                        for channel,shift in enumerate((16,8,0)):packed|=background[...,channel].astype(np.uint32)<<shift
                        if lib.neuronte_draw_scalar(icon_handle,packed.ctypes.data,cw,cw,ch,0,0,cw,ch,cp,0xff000000|foreground):raise ValueError('reference icon pointer failed')
                        painted.append(np.stack([packed>>s&255 for s in (16,8,0)],axis=2).astype(np.uint8))
                    luma=background.astype(np.int32)@[299,587,114]
                    result=np.where((luma>127500)[...,None],painted[1],painted[0])
                    frame[y0:y1,x0:x1]=result[y0-y:y1-y,x0-x:x1-x]
                else:
                    a=mask(0x2196)[:height-py,:width-px,None]
                    patch=frame[py:py+a.shape[0],px:px+a.shape[1]]
                    patch[:]=((255*a+patch.astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
        return frame
    finally:
        lib.neuronte_close(handle)
        if video: lib.neuronte_close(video)
        if pointer_handle: lib.neuronte_close(pointer_handle)
        for art in artworks:lib.neuronte_close(art)


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
    p.add_argument('--icon-weights',type=Path); p.add_argument('--icon-manifest',type=Path)
    p.add_argument('--pointer-weights',type=Path); p.add_argument('--pointer-manifest',type=Path)
    args=p.parse_args()
    doc=json.loads(args.grid.read_text())
    for field,path in (('weights',args.weights),('library',args.library)):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=doc['cache_identity'][field]:
            raise ValueError('reference asset hash differs: '+field)
    expected=reconstruct(doc,args.weights,args.library,args.video_weights,args.emoji_weights,args.emoji_manifest,args.icon_weights,args.icon_manifest,args.pointer_weights,args.pointer_manifest)
    actual=read_ppm(args.screenshot)
    if expected.shape!=actual.shape: raise ValueError('screenshot geometry differs')
    delta=np.abs(expected.astype(int)-actual.astype(int))
    result=dict(equal=bool(np.array_equal(expected,actual)),max_error=int(delta.max()),
                differing_pixels=int(np.count_nonzero(np.any(delta,axis=2))),pixels=actual.shape[0]*actual.shape[1])
    if args.output: args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    return 0 if result['equal'] else 1


if __name__=='__main__': raise SystemExit(main())
