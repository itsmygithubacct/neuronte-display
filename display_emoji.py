"""Explicit two-cell colour artwork; availability comes from its manifest."""
import json
from pathlib import Path
from display_art import ColourArt


class Emoji(ColourArt):
    def __init__(self,weights,manifest,library,cw,ch,theme='auto'):
        doc=json.loads(Path(manifest).read_text())
        if doc.get('cell_columns') != 2:
            raise ValueError('emoji manifest must declare cell_columns=2')
        self.scalars={int(row['scalar'][2:],16) for row in doc['glyphs']}
        if not self.scalars or any(cp>0xeffff or cp<0 or 0xd800<=cp<=0xdfff for cp in self.scalars):
            raise ValueError('invalid two-cell emoji manifest')
        super().__init__(weights,manifest,library,cw,ch,2,theme)

    def selected(self,record):
        value,_,_,flags=record
        if not flags&512: return None
        if isinstance(value,(tuple,list)):
            if 0xfe0e in value: return None
            visible=[cp for cp in value if cp!=0xfe0f]
            if len(visible)!=1: return None
            value=visible[0]
        return value if value in self.scalars else None
