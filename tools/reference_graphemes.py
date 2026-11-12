"""Independent implementation of the version1 composition geometry.

Only the declarative capability/placement data are shared with production.
All masks arrive from the reference's own direct C inference calls.
"""
from collections import Counter
import math
import unicodedata
import numpy as np


class ReferenceUnavailable(ValueError):
    pass


def reference_composer(policy,digest,w,h,infer):
    profile=policy['bundles'].get(digest)
    intervals=policy['trained_ranges']['latin']+(policy['trained_ranges']['scripts'] if profile=='scripts' else [])
    available=lambda cp:bool(profile) and any(a<=cp<=b for a,b in intervals)
    recipes={int(k,16):v for k,v in policy['marks'].items()};g=policy['geometry']
    memo={}
    def nearest(x):return math.floor(x+.5) if x>=0 else -math.floor(-x+.5)
    def rectangle(a):
        coordinates=np.argwhere(a!=0)
        if not len(coordinates):return (0,0,0,0)
        lo=coordinates.min(axis=0);hi=coordinates.max(axis=0)+1
        return int(lo[1]),int(lo[0]),int(hi[1]),int(hi[0])
    def scale(a,ny,nx):
        # Explicit overlapping source intervals; every output comes from a C
        # coverage sample, with maximum area pooling on reduction.
        if not a.size or ny<1 or nx<1:raise ReferenceUnavailable('empty component or destination')
        return np.array([[max(int(a[yy,xx])
                    for yy in range(y*a.shape[0]//ny,max(y*a.shape[0]//ny+1,math.ceil((y+1)*a.shape[0]/ny)))
                    for xx in range(x*a.shape[1]//nx,max(x*a.shape[1]//nx+1,math.ceil((x+1)*a.shape[1]/nx))))
                for x in range(nx)] for y in range(ny)],np.uint16)
    def overlay(dst,src,dx,dy):
        visible=False
        for sy in range(src.shape[0]):
            if not 0<=sy+dy<dst.shape[0]:continue
            for sx in range(src.shape[1]):
                if 0<=sx+dx<dst.shape[1]:
                    visible=visible or bool(src[sy,sx])
                    dst[sy+dy,sx+dx]=max(dst[sy+dy,sx+dx],src[sy,sx])
        return visible
    def generate(value,style):
        sequence=tuple(value) if isinstance(value,(tuple,list)) else (value,)
        text=''.join(chr(cp) for cp in sequence if cp not in (65038,65039))
        composed=unicodedata.normalize('NFC',text)
        if not profile:return infer(ord(composed) if len(composed)==1 else 65533,style)
        if len(composed)==1 and available(ord(composed)):return infer(ord(composed),style)
        decomposed=list(map(ord,unicodedata.normalize('NFD',text)))
        valid=(2<=len(decomposed)<=5 and available(decomposed[0]) and chr(decomposed[0]).isalpha()
               and all(cp in recipes for cp in decomposed[1:]))
        if not valid:
            return infer(ord(composed) if len(composed)==1 and (len(sequence)==1 or len(decomposed)==1) else 65533,style)
        base=decomposed[0];attached=[]
        for cp in decomposed[1:]:
            if cp==780 and f'{base:04X}' in g['caron_beside']:cp=789
            if cp==807:
                if chr(base) in g['cedilla_comma']:cp=806
                elif chr(base) in g['cedilla_turned']:cp=786
            attached.append(cp)
        counts=Counter(recipes[cp]['anchor'] for cp in attached)
        if max(counts.values())>g['maximum_marks_per_anchor'] or w<4 or h<8:return infer(65533,style)
        has_above=counts['above']>0
        body_code=305 if base==105 and has_above else base
        body=infer(body_code,0).copy()
        if base==106 and has_above:body[:h*7//20,:]=0
        if not np.any(body):raise ReferenceUnavailable('empty base')
        left,top,right,bottom=rectangle(body)
        capital=chr(base).isupper()
        if counts['below']:
            stop=min(bottom,nearest(g['baseline']*h/20))
            if stop<=top:raise ReferenceUnavailable('no space for base')
            if stop<bottom:
                old=body.copy();body[top:bottom]=0;body[top:stop]=scale(old[top:bottom],stop-top,w)
        if has_above:
            start=max(top,nearest((g['short_top']-g['pen_radius'])*h/20))
            stop=min(h,nearest(g['baseline' if capital else 'x_height']*h/20))
            if top<start and start>=stop:raise ReferenceUnavailable('no fitting base')
            if top<start<stop:
                old=body.copy();body[top:stop,:]=0;body[start:stop,:]=scale(old[top:stop,:],stop-start,w)
        ink=np.zeros((h,w),np.uint16)
        for anchor in ('above','below','beside'):
            marks=[cp for cp in attached if recipes[cp]['anchor']==anchor]
            samples=[]
            for cp in marks:
                recipe=recipes[cp];a=infer(recipe['scalar'],0).copy()
                if not np.any(a):raise ReferenceUnavailable('empty mark')
                if recipe.get('transform')=='mirror-x':a=np.fliplr(a)
                if recipe.get('transform')=='rotate-180':a=np.rot90(a,2)
                if 'fit_grid' in recipe:
                    x0,y0,x1,y1=rectangle(a)
                    l,t,r,b=recipe['fit_grid'];l,r=nearest(l*w/10),nearest(r*w/10);t,b=nearest(t*h/20),nearest(b*h/20)
                    source=scale(a[y0:y1,x0:x1],max(1,b-t),max(1,r-l));a=np.zeros((h,w),np.uint16);overlay(a,source,l,t)
                samples.append(a)
            if len(marks)>1:
                if anchor=='above':
                    lower=max(2,nearest(h*.40));slot=max(1,(lower-1)//2);ys=[lower-slot,0]
                    _,first,_,last=rectangle(body);new_first=lower+1
                    if new_first>=last:raise ReferenceUnavailable('no room beneath stack')
                    old=body.copy();body[first:last]=0;body[new_first:last]=scale(old[first:last],last-new_first,w)
                elif anchor=='below':
                    first=nearest(h*.72);slot=max(1,(h-first)//2);ys=[first,h-slot]
                else:slot=max(1,nearest(h*.20));ys=[slot,0]
                for cp,a,y in zip(marks,samples,ys):
                    x0,y0,x1,y1=rectangle(a);piece=scale(a[y0:y1,x0:x1],min(slot,y1-y0),x1-x0)
                    if anchor=='above':cx=g['above_centres'].get(f'{body_code:04X}',(left+right)*5/w)*w/10
                    elif anchor=='beside':cx=g['caron_beside'].get(f'{base:04X}',8.5)*w/10
                    else:
                        yy,xx=np.nonzero(body);last=xx[yy==yy.max()]
                        cx=int(last.max()) if cp==808 else (int(last.min())+int(last.max())+1)/2
                    x=nearest(cx-piece.shape[1]/2)
                    if not overlay(ink,piece,x,y):raise ReferenceUnavailable('stack clipped away')
            elif marks:
                cp=marks[0]
                if anchor=='above':
                    cx=g['above_centres'].get(f'{body_code:04X}',(left+right)*5/w)*w/10
                    dx=nearest(cx-w/2);dy=-nearest(g['capital_raise']*h/20) if capital else 0
                elif anchor=='beside':
                    dx=nearest(g['caron_beside'].get(f'{base:04X}',8.5)*w/10-w/2);dy=0
                else:
                    rows,cols=np.nonzero(body)
                    lowest=cols[rows==rows.max()] if len(cols) else np.array([w//2])
                    cx=int(lowest.max()) if cp==808 else (int(lowest.min())+int(lowest.max())+1)/2
                    dx=nearest(cx-w/2);dy=0
                if not overlay(ink,samples[0],dx,dy):raise ReferenceUnavailable('mark clipped away')
        mask=np.maximum(body,ink);output=np.zeros_like(mask)
        radius=max(1,(w+16)//32) if style&1 else 0
        for row in range(h):
            numerator=(27*h-40*row-20)*w;denominator=80*h
            shift=(int(math.copysign((abs(numerator)+denominator//2)//denominator,numerator)) if style&2 else 0)
            for col in range(w):
                source_col=col-shift
                output[row,col]=max([0]+[int(mask[row,c]) for c in range(max(0,source_col-radius),min(w,source_col+1))])
        return output
    def result(value,style=0):
        key=(tuple(value) if isinstance(value,(tuple,list)) else (value,),style)
        if key not in memo:
            try:memo[key]=generate(value,style)
            except ReferenceUnavailable:memo[key]=infer(65533,style)
        return memo[key]
    return result
