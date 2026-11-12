"""Bounded composition of learned base/spacing-mark masks in one parser cell."""
import collections
import hashlib
import json
from pathlib import Path
import unicodedata
import numpy as np

POLICY_PATH=Path(__file__).with_name('graphemes.json')
POLICY_BYTES=POLICY_PATH.read_bytes()
POLICY=json.loads(POLICY_BYTES)
IDENTITY={'version':POLICY['version'],'sha256':hashlib.sha256(POLICY_BYTES).hexdigest()}
MARKS={int(k,16):v for k,v in POLICY['marks'].items()}
G=POLICY['geometry']


class Unavailable(ValueError):
    pass


def rounded(value):
    return int(np.floor(value+.5)) if value>=0 else -int(np.floor(-value+.5))


def bounds(mask):
    ys,xs=np.nonzero(mask)
    return (int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)) if len(xs) else (0,0,0,0)


def resize(mask,height,width):
    """Maximum over overlapping source pixels; never invent a coverage value."""
    if not mask.size or height<1 or width<1:raise Unavailable("empty component or target")
    out=np.empty((height,width),np.uint8)
    for y in range(height):
        y0=y*mask.shape[0]//height;y1=max(y0+1,((y+1)*mask.shape[0]+height-1)//height)
        for x in range(width):
            x0=x*mask.shape[1]//width;x1=max(x0+1,((x+1)*mask.shape[1]+width-1)//width)
            out[y,x]=mask[y0:y1,x0:x1].max()
    return out


def paste(out,mask,x,y):
    x0,y0=max(x,0),max(y,0);x1,y1=min(x+mask.shape[1],out.shape[1]),min(y+mask.shape[0],out.shape[0])
    if x0<x1 and y0<y1:
        visible=mask[y0-y:y1-y,x0-x:x1-x]
        out[y0:y1,x0:x1]=np.maximum(out[y0:y1,x0:x1],visible)
        return bool(np.any(visible))
    return False


def styled(mask,style):
    h,w=mask.shape;out=np.zeros_like(mask)
    stroke=max(1,(w+16)//32) if style&1 else 0
    for y in range(h):
        num=(27*h-40*y-20)*w;den=80*h
        shift=((num+den//2)//den if num>=0 else -((-num+den//2)//den)) if style&2 else 0
        for offset in range(stroke+1):paste(out[y:y+1],mask[y:y+1],shift+offset,0)
    return out


class Graphemes:
    def __init__(self,cache):
        self.cache=cache;self.profile=POLICY['bundles'].get(cache.identity['weights'])
        ranges=POLICY['trained_ranges']['latin']+(POLICY['trained_ranges']['scripts'] if self.profile=='scripts' else [])
        self.trained={cp for lo,hi in ranges for cp in range(lo,hi+1)} if self.profile else set()
        self.plans=collections.OrderedDict();self.masks=collections.OrderedDict();self.failures=collections.OrderedDict()

    def plan(self,value):
        key=tuple(value) if isinstance(value,(tuple,list)) else (value,)
        if key in self.plans:self.plans.move_to_end(key);return self.plans[key]
        text=''.join(chr(cp) for cp in key if cp not in (0xfe0e,0xfe0f))
        nfc=unicodedata.normalize('NFC',text)
        # Existing trained precomposed glyphs stay byte-identical.
        if not self.profile:result=(ord(nfc),) if len(nfc)==1 else None
        elif len(nfc)==1 and ord(nfc) in self.trained:result=(ord(nfc),)
        else:
            nfd=tuple(map(ord,unicodedata.normalize('NFD',text)))
            possible=(self.profile and 2<=len(nfd)<=5 and nfd[0] in self.trained
                      and unicodedata.category(chr(nfd[0])).startswith('L') and all(cp in MARKS for cp in nfd[1:]))
            if possible:
                base=nfd[0];marks=[]
                for cp in nfd[1:]:
                    if cp==0x30c and f'{base:04X}' in G['caron_beside']:cp=0x315
                    elif cp==0x327 and chr(base) in G['cedilla_comma']:cp=0x326
                    elif cp==0x327 and chr(base) in G['cedilla_turned']:cp=0x312
                    marks.append(cp)
                counts=collections.Counter(MARKS[cp]['anchor'] for cp in marks)
                possible=max(counts.values())<=G['maximum_marks_per_anchor'] and self.cache.cw>=4 and self.cache.ch>=8
                result=(base,*marks) if possible else None
            # Keep unrelated unknown single scalars routed, but never silently
            # substitute an untrained NFC singleton for a combining cluster.
            elif len(key)==1 and len(nfc)==1:result=(ord(nfc),)
            elif len(nfc)==1 and len(nfd)==1:result=(ord(nfc),)
            else:result=None
        self.plans[key]=result
        while len(self.plans)>512:self.plans.popitem(last=False)
        return result

    def mask(self,value,style=0,cached=True):
        plan=self.plan(value)
        get=self.cache.get if cached else self.cache.infer
        if plan is None:return get(0xfffd,style)
        if len(plan)==1:return get(plan[0],style)
        key=(plan,style)
        if cached and key in self.masks:self.masks.move_to_end(key);return self.masks[key]
        if key in self.failures:return get(0xfffd,style)
        try:result=self._compose(plan,style,get)
        except Unavailable:
            self.failures[key]=True
            while len(self.failures)>512:self.failures.popitem(last=False)
            return get(0xfffd,style)
        result.setflags(write=False)
        if cached:
            self.masks[key]=result
            while len(self.masks)>512:self.masks.popitem(last=False)
        return result

    def supported(self,value,style=0):
        plan=self.plan(value)
        return plan is not None and (plan,style) not in self.failures

    def _compose(self,plan,style,get):
        base,*marks=plan;w,h=self.cache.cw,self.cache.ch
        groups={a:[cp for cp in marks if MARKS[cp]['anchor']==a] for a in ('above','below','beside')}
        source_base=0x131 if base==ord('i') and groups['above'] else base
        body=get(source_base).copy()
        if base==ord('j') and groups['above']:
            # Keep only whole pixels separated by a boundary inside the source
            # gap. If dot/body can share a pixel, extraction is unavailable.
            dot_end,body_start=G['dotless_j_gap']
            cut=int(np.ceil(dot_end*h/20))
            if cut>int(np.floor(body_start*h/20)):raise Unavailable('j dot/body share a raster row')
            body[:cut]=0
        if not np.any(body):raise Unavailable("empty base")
        bx0,by0,bx1,by1=bounds(body);capital=chr(base).isupper()
        if groups['below']:
            # A new below mark must not overlap an existing descender. Fit the
            # learned body above the source baseline; the mark owns the lower
            # band. Existing trained precomposed glyphs never take this path.
            end=min(by1,rounded(G['baseline']*h/20))
            if end<=by0:raise Unavailable('no room above below mark')
            if end<by1:
                old=body.copy();body[by0:by1]=0;body[by0:end]=resize(old[by0:by1],end-by0,w)
        def shorten(top,end):
            nonlocal body
            if by0<top and top>=end:raise Unavailable("no room for base")
            if by0<top<end:
                old=body.copy();body[by0:end]=0
                body[top:end]=resize(old[by0:end],end-top,w)
        if groups['above']:
            top=max(by0,rounded((G['short_top']-G['pen_radius'])*h/20))
            end=min(h,rounded((G['baseline'] if capital else G['x_height'])*h/20))
            shorten(top,end)
        def component(cp):
            recipe=MARKS[cp];a=get(recipe['scalar']).copy()
            if not np.any(a):raise Unavailable('empty mark')
            if recipe.get('transform')=='mirror-x':a=a[:,::-1]
            elif recipe.get('transform')=='rotate-180':a=a[::-1,::-1]
            if 'fit_grid' in recipe:
                x0,y0,x1,y1=bounds(a);a=a[y0:y1,x0:x1]
                left,top,right,bottom=recipe['fit_grid']
                left,right=rounded(left*w/10),rounded(right*w/10)
                top,bottom=rounded(top*h/20),rounded(bottom*h/20)
                result=np.zeros((h,w),np.uint8)
                paste(result,resize(a,max(1,bottom-top),max(1,right-left)),left,top);a=result
            return a
        layer=np.zeros_like(body)
        for anchor,points in groups.items():
            if not points:continue
            pieces=[component(cp) for cp in points]
            if len(points)>1:
                # Canonical order: first is closest to the base, subsequent
                # marks stack outward. Two marks share a bounded cell band.
                if anchor=='above':
                    bottom=max(2,rounded(h*.40));slot=max(1,(bottom-1)//2)
                    old=body.copy();_,top,_,end=bounds(body)
                    target=bottom+1
                    if target>=end:raise Unavailable("stack leaves no room for base")
                    body[top:end]=0;body[target:end]=resize(old[top:end],end-target,w)
                    positions=[bottom-slot,0]
                elif anchor=='below':
                    top=rounded(h*.72);slot=max(1,(h-top)//2);positions=[top,h-slot]
                else:
                    top=0;slot=max(1,rounded(h*.20));positions=[slot,0]
                for cp,a,y in zip(points,pieces,positions):
                    x0,y0,x1,y1=bounds(a);piece=resize(a[y0:y1,x0:x1],min(slot,y1-y0),x1-x0)
                    if anchor=='above':centre=G['above_centres'].get(f'{source_base:04X}',(bx0+bx1)*5/w)*w/10
                    elif anchor=='beside':centre=G['caron_beside'].get(f'{base:04X}',8.5)*w/10
                    else:
                        yy,xx=np.nonzero(body);low=xx[yy==yy.max()]
                        centre=int(low.max()) if cp==0x328 else (int(low.min())+int(low.max())+1)/2
                    x=rounded(centre-piece.shape[1]/2)
                    if not paste(layer,piece,x,y):raise Unavailable("stacked mark clipped away")
                continue
            cp=points[0];a=pieces[0]
            if anchor=='above':
                centre=G['above_centres'].get(f'{source_base:04X}',(bx0+bx1)*5/w)*w/10
                dx=rounded(centre-w/2);dy=-rounded(G['capital_raise']*h/20) if capital else 0
            elif anchor=='beside':
                centre=G['caron_beside'].get(f'{base:04X}',8.5)*w/10
                dx=rounded(centre-w/2);dy=0
            else:
                yy,xx=np.nonzero(body)
                low=xx[yy==yy.max()] if len(xx) else np.array([w//2])
                centre=int(low.max()) if cp==0x328 else (int(low.min())+int(low.max())+1)/2
                dx=rounded(centre-w/2);dy=0
            if not paste(layer,a,dx,dy):raise Unavailable("mark clipped away")
        return styled(np.maximum(body,layer),style)
