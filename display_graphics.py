"""Image placements represented by the video module's neural multi-ink cells."""
import base64
import collections
import importlib.util
import json
from pathlib import Path
import numpy as np


class Graphics:
    def __init__(self, home, library, weights=None):
        module = Path(home)/'tools/neuronte_video.py'
        spec = importlib.util.spec_from_file_location('neuronte_video',module)
        video = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(video)
        weights = weights or Path(home)/'weights/neuronte-video-masks-v0.nrt'
        self.exactness = video.exactness(weights,library)
        if not self.exactness['exact']:
            raise RuntimeError('2x4 graphics requires all 256 octants to be exact')
        self.codec = video.MultiInk(weights,library,2,4)
        self.cache = collections.OrderedDict()
        self.records = []

    def convert(self, item, pixels):
        # The pinned parser guarantees process-global image generations,
        # including replacements and images belonging to different terminals.
        key = (item.generation,item.sx,item.sy,item.sw,item.sh,item.dw,item.dh)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if not item.dw or not item.dh or item.dw*item.dh > 16*1024*1024:
            raise ValueError('image placement exceeds 16 million pixels')
        source = pixels[item.sy:item.sy+item.sh,item.sx:item.sx+item.sw]
        # Nearest pixel sampling is a layout operation; all displayed pixels
        # still pass through the three net masks. No bitmap glyph fallback.
        yy = np.arange(item.dh)*source.shape[0]//item.dh
        xx = np.arange(item.dw)*source.shape[1]//item.dw
        resized = source[yy[:,None],xx[None,:]]
        h,w = resized.shape[:2]
        padded = np.pad(resized,((0,(-h)%4),(0,(-w)%2),(0,0)),mode='edge')
        colours = self.codec.encode(padded[...,:3])
        image = self.codec.decode(colours)[:h,:w]
        alpha = None
        if pixels.shape[2] == 4:
            alpha_colours = self.codec.encode(np.repeat(padded[...,3:],3,axis=2))
            alpha = self.codec.decode(alpha_colours)[:h,:w,:1]
        else:
            alpha_colours = None
        record = dict(width=w,height=h,shape=list(colours.shape),
                      colours=base64.b64encode(colours.tobytes()).decode(),
                      alpha=base64.b64encode(alpha_colours.tobytes()).decode() if alpha is not None else None)
        self.cache[key] = (image,alpha,record)
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        return self.cache[key]

    def composite(self, frame, workspace):
        self.records = []
        for pane,(px,py,w,h) in zip(workspace.current['panes'],workspace.layout()):
            # Pane titles and chrome are never covered by a child image.
            left,top=px*workspace.cw,(py+1)*workspace.ch
            right,bottom=left+w*workspace.cw,top+(h-1)*workspace.ch
            for item,pixels in sorted(pane.terminal.images(),key=lambda pair:pair[0].z):
                image,alpha,record = self.convert(item,pixels)
                x,y = left+item.x,top+item.y
                x0,y0,x1,y1=max(left,x),max(top,y),min(right,x+item.dw),min(bottom,y+item.dh)
                if x0>=x1 or y0>=y1:
                    continue
                src=image[y0-y:y1-y,x0-x:x1-x]
                dst=frame[y0:y1,x0:x1]
                if alpha is not None:
                    a=alpha[y0-y:y1-y,x0-x:x1-x].astype(np.uint16)
                    src=((src.astype(np.uint16)*a+dst.astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
                # Negative-z placement is behind text ink, as in Kitty. The
                # text background remains the image's compositing substrate.
                if item.z < 0:
                    # Masking by text coverage is supplied by the caller below.
                    ink = workspace.text_ink[y0:y1,x0:x1,None].astype(np.uint16)
                    src=((dst.astype(np.uint16)*ink+src.astype(np.uint16)*(255-ink)+127)//255).astype(np.uint8)
                dst[:]=src
                self.records.append(dict(record,x=x,y=y,z=item.z,clip=[x0,y0,x1,y1]))
        return frame


def pointer(frame, position, cache, scalar=0x2196):
    if position is None:
        return frame
    x,y=position
    h,w=frame.shape[:2]
    if not 0<=x<w or not 0<=y<h:
        return frame
    mask=cache.get(scalar)[:h-y,:w-x,None].astype(np.uint16)
    patch=frame[y:y+mask.shape[0],x:x+mask.shape[1]]
    patch[:]=((255*mask+patch.astype(np.uint16)*(255-mask)+127)//255).astype(np.uint8)
    return frame
