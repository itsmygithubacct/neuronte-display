"""Opt-in single-cell icons, restricted to the hash-bound trained inventory."""
import hashlib
import json
from pathlib import Path
from display_art import ColourArt


class Icons(ColourArt):
    def __init__(self,weights,manifest,library,cw,ch,theme='auto'):
        doc = json.loads(Path(manifest).read_text())
        scalars = [int(row['scalar'][2:],16) for row in doc['glyphs']]
        if (doc.get('version') != 1 or doc.get('cell_columns') != 1 or not scalars or
            len(set(scalars)) != len(scalars) or
            any(cp<0 or cp>0x10ffff or 0xd800<=cp<=0xdfff for cp in scalars)):
            raise ValueError('invalid single-cell icon inventory')
        if doc.get('weights_sha256') != hashlib.sha256(Path(weights).read_bytes()).hexdigest():
            raise ValueError('icon inventory does not match selected weights')
        self.scalars = set(scalars)
        pointer = doc['pointer']
        self.pointer_scalar = int(pointer['scalar'][2:],16)
        if self.pointer_scalar not in self.scalars:
            raise ValueError('icon pointer must be in the trained inventory')
        nums, dens = pointer['hotspot_numerator'], pointer['hotspot_denominator']
        if (len(nums)!=2 or len(dens)!=2 or any(type(n)!=int for n in nums+dens) or
            any(d<=0 or n<0 or n>=d for n,d in zip(nums,dens))):
            raise ValueError('invalid icon pointer hotspot')
        self.hotspot = [cw*nums[0]//dens[0],ch*nums[1]//dens[1]]
        super().__init__(weights,manifest,library,cw,ch,1,theme)
        self.identity['pointer'] = dict(scalar=self.pointer_scalar,hotspot=self.hotspot,
                                       contrast='per-pixel-luma-127.5')

    def selected(self,record):
        scalar,_,_,flags = record
        # No routing heuristic or untrained compound is a membership proof.
        return scalar if isinstance(scalar,int) and not flags&(512|1024) and scalar in self.scalars else None
