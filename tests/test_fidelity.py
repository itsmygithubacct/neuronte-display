"""Original display cursor, colour and icon contracts against independent C."""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
import neuronte_display as d
from display_emoji import Emoji
from display_icons import Icons
from display_graphics import Graphics,cursor_overlay,pointer
from reference import reconstruct
from reference_colour import reference_colour
from emoji_fixture import fixture

HOME=Path(os.environ.get('NEURONTE_HOME',Path.home()/'projects/neuronte'))
TRAIN=Path(os.environ.get('NEURONTE_TRAINING_HOME',Path.home()/'projects/neuronte-roadmap-training'))
VIDEO=Path(os.environ.get('NEURONTE_VIDEO_HOME',HOME))
LIB=HOME/'build/libneuronte.so'
TEXT=HOME/'weights/neuronte-split-v4.nrt'


class FidelityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cache=d.MaskCache(TEXT,LIB,self.tmp.name);self.addCleanup(self.cache.close)

    def emoji(self,theme='auto',cw=8,ch=16,analytic=False):
        if analytic:
            weights=Path(self.tmp.name)/'analytic.nrt';weights.write_bytes(fixture())
            manifest=Path(self.tmp.name)/'analytic.json';manifest.write_text(json.dumps(dict(cell_columns=2,glyphs=[dict(scalar='U+1F600')])))
        else:weights=TRAIN/'weights/neuronte-emoji-v0.nrt';manifest=TRAIN/'font/emoji-manifest.json'
        art=Emoji(weights,manifest,LIB,cw,ch,theme);self.addCleanup(art.close)
        return art,weights,manifest

    def icons(self,theme='auto'):
        art=Icons(TRAIN/'weights/neuronte-icon-v1.nrt',ROOT/'icons.json',LIB,8,16,theme)
        self.addCleanup(art.close);return art

    def workspace(self):
        t=d.Terminal(20,4);self.addCleanup(t.close)
        w=d.Workspace.__new__(d.Workspace)
        w.cols,w.rows,w.tab,w.focus,w.cw,w.ch=20,7,0,0,8,16
        w.tabs=[dict(panes=[types.SimpleNamespace(name='fixture',terminal=t,exit_status=None)],vertical=True)]
        w.layout=lambda:[(0,1,20,5)]
        return t,w

    def document(self,records,cols,rows,**extra):
        return dict(cols=cols,rows=rows,screen=[cols*8,rows*16],cell_width=8,cell_height=16,
                    records=json.loads(json.dumps(records)),**extra)

    def test_cursor_shape_movement_visibility_and_clipping(self):
        terminal,w=self.workspace();terminal.feed(b'AB\r\x1b[?25l')
        renderer=d.Renderer(self.cache);base=renderer.render(w.grid(),20,7).copy()
        frames=[]
        for code,name,cp in ((2,'block',None),(4,'underline',0x2581),(6,'bar',0x258f)):
            terminal.feed(f'\x1b[1;1H\x1b[{code} q\x1b[?25h'.encode())
            records=w.grid();actual=renderer.render(records,20,7).copy()
            cursor_overlay(actual,w.cursor,records,20,self.cache)
            self.assertEqual(w.cursor,dict(column=0,row=2,shape=name));frames.append(actual.copy())
            if cp:
                expected=base.copy();a=self.cache.infer(cp).astype(np.uint16)[...,None]
                expected[32:48,:8]=((d.colour(d.FG)*a+base[32:48,:8].astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
                np.testing.assert_array_equal(actual,expected)
            ref=reconstruct(self.document(records,20,7,cursor=w.cursor),TEXT,LIB)
            np.testing.assert_array_equal(actual,ref)
            terminal.feed(b'\x1b[1;2H');records=w.grid();moved=renderer.render(records,20,7).copy()
            cursor_overlay(moved,w.cursor,records,20,self.cache)
            np.testing.assert_array_equal(moved[32:48,:8],base[32:48,:8])
            terminal.feed(b'\x1b[?25l');records=w.grid();restored=renderer.render(records,20,7).copy()
            cursor_overlay(restored,w.cursor,records,20,self.cache)
            np.testing.assert_array_equal(restored,base)
        self.assertEqual(len({hashlib.sha256(f.tobytes()).digest() for f in frames}),3)
        clipped=np.zeros((7,3,3),np.uint8);records=[(32,0xffffff,0,0)]
        cursor_overlay(clipped,dict(column=0,row=0,shape='bar'),records,1,self.cache)
        np.testing.assert_array_equal(clipped,np.repeat(self.cache.infer(0x258f)[:7,:3,None],3,axis=2))

    def test_block_cursor_on_each_wide_half_restores_and_preserves_alpha(self):
        art,weights,manifest=self.emoji();terminal,w=self.workspace()
        terminal.feed('\x1b[?2027h\x1b[2 q😀\ufe0f\x1b[?25l'.encode())
        renderer=d.Renderer(self.cache,art);records=w.grid()
        base=renderer.render(records,20,7).copy();alpha=renderer.ink(records,20,7)
        for column in (1,2):
            terminal.feed(f'\x1b[?25h\x1b[1;{column}H'.encode());records=w.grid()
            actual=renderer.render(records,20,7).copy()
            renderer.cursor(actual,w.cursor,records,20)
            other=slice(8,16) if column==1 else slice(0,8)
            np.testing.assert_array_equal(actual[32:48,other],base[32:48,other])
            self.assertTrue(np.any(actual!=base));np.testing.assert_array_equal(renderer.ink(records,20,7),alpha)
            # Both halves are rendered with the original whole-glyph palette.
            np.testing.assert_array_equal(renderer.frame,base)
            modes=[]
            for i in (40,41):
                asset,_,_,fg,bg,flags=renderer.artwork(records,i,20)
                if flags&16:fg,bg=bg,fg
                modes.append(asset.mode(bg))
            self.assertEqual(modes,[0,0])
            ref=reconstruct(self.document(records,20,7,emoji=art.identity,cursor=w.cursor),TEXT,LIB,emoji_weights=weights,emoji_manifest=manifest)
            np.testing.assert_array_equal(actual,ref)
        terminal.feed(b'\x1b[?25l');np.testing.assert_array_equal(renderer.render(w.grid(),20,7),base)

    def test_colour_styles_halves_themes_lines_dim_and_hidden(self):
        for theme in ('dark','light'):
            art,weights,manifest=self.emoji(theme,analytic=True)
            for flags in (0,1,2,3,4,8,128,12,32,16,16|32,3|4|8|32,3|4|8|32|64):
                with self.subTest(theme=theme,flags=flags):
                    records=[(0x1f600,0x123456,0x607080,512|flags),(32,0x123456,0x607080,1024|flags)]
                    renderer=d.Renderer(self.cache,art);actual=renderer.render(records,2,1).copy()
                    doc=self.document(records,2,1,emoji=art.identity)
                    ref=reconstruct(doc,TEXT,LIB,emoji_weights=weights,emoji_manifest=manifest)
                    np.testing.assert_array_equal(actual,ref)
                    fg,bg=(0x607080,0x123456) if flags&16 else (0x123456,0x607080)
                    line=None
                    if flags&(4|8|128):
                        line=np.maximum.reduce([self.cache.infer(cp) for bit,cp in ((4,0x2581),(8,0x2500),(128,0x2594)) if flags&bit])
                    _,expected_alpha=reference_colour(art.lib,art.ptr,0x1f600,2,8,16,fg,bg,flags,int(theme=='light'),line)
                    np.testing.assert_array_equal(renderer.ink(records,2,1),expected_alpha)
                    if flags&64:self.assertFalse(np.any(renderer.ink(records,2,1)))
            # Full glyph shear carries the red/green seam, rather than clipping
            # left-layer ink separately at x=8. The oracle transports entries.
            for style in (1,2,3):
                frame=np.concatenate([art.cell(0x1f600,h,0xffffff,0,style) for h in (0,1)],axis=1)
                expected,_=reference_colour(art.lib,art.ptr,0x1f600,2,8,16,0xffffff,0,style,int(theme=='light'),None)
                np.testing.assert_array_equal(frame,expected)

    def test_opaque_colour_cursor_uses_contrast_without_reselecting_palette(self):
        art,_,_=self.emoji(analytic=True)
        for bg in (0x101820,0xf0f0f0):
            records=[(0x1f600,0xeeeeee,bg,512),(32,0xeeeeee,bg,1024)]
            renderer=d.Renderer(self.cache,art);base=renderer.render(records,2,1).copy()
            alpha=renderer.ink(records,2,1)
            records[0]=(0x1f600,0xeeeeee,bg,512|16|2048)
            frame=renderer.render(records,2,1).copy()
            np.testing.assert_array_equal(frame,base)
            cursor=dict(column=0,row=0,shape='block')
            renderer.cursor(frame,cursor,records,2)
            self.assertTrue(cursor['colour'])
            # Left half is fully opaque fixed red in the analytic C bundle.
            self.assertTrue(np.all(alpha[:,:8]==255))
            self.assertTrue(np.all(frame[:,:8]==255))
            np.testing.assert_array_equal(frame[:,8:],base[:,8:])
            np.testing.assert_array_equal(renderer.ink(records,2,1),alpha)

    def test_accepted_emoji_style0_all300_both_palettes_unchanged(self):
        art,_,_=self.emoji()
        for theme in (0,1):
            art.theme='light' if theme else 'dark'
            for cp in sorted(art.scalars):
                for half in (0,1):
                    expected=np.full((16,8),0xff102030,np.uint32)
                    art.lib.neuronte_set_theme(art.ptr,theme)
                    self.assertEqual(art.lib.neuronte_draw_half(art.ptr,expected.ctypes.data,8,8,16,0,0,8,16,cp,half,0xffabcdef),0)
                    rgb=np.stack([expected>>s&255 for s in (16,8,0)],axis=2).astype(np.uint8)
                    np.testing.assert_array_equal(art.cell(cp,half,0xabcdef,0x102030,0),rgb)

    def test_real_colour_geometry_multiple_sizes_and_auto_theme_after_reverse(self):
        for cw,ch in ((4,8),(8,16),(16,32)):
            art,_,_=self.emoji(cw=cw,ch=ch)
            for cp in (0x1f600,0x1f680,0x26a0):
                for fg,bg in ((0xeeeeee,0x101820),(0x102030,0xf0f0f0)):
                    for flags in (1,2,3,16|3,32|3,16|32|3):
                        effective_fg,effective_bg=(bg,fg) if flags&16 else (fg,bg)
                        mode=int(sum((effective_bg>>s&255)*k for s,k in ((16,299),(8,587),(0,114)))>127500)
                        expected,alpha=reference_colour(art.lib,art.ptr,cp,2,cw,ch,effective_fg,effective_bg,flags,mode,None)
                        actual=np.concatenate([art.cell(cp,half,fg,bg,flags) for half in (0,1)],axis=1)
                        np.testing.assert_array_equal(actual,expected)
                        np.testing.assert_array_equal(np.concatenate([art.mask(cp,half,flags) for half in (0,1)],axis=1),alpha)

    def test_icon_inventory_clear_foreground_and_negative_z_styles(self):
        icons=self.icons();self.assertEqual(len(icons.scalars),167)
        self.assertIsNone(icons.selected((0xf8ff,0,0,0)))
        self.assertIsNone(icons.selected(((0xf112,0x301),0,0,0)))
        self.assertIsNone(icons.selected((0xf112,0,0,512)))
        # Accepted keyboard has fg+clear; warning has fixed+fg. Muted mic has
        # fg+clear+fixed. The image exposes alpha errors independently of RGB.
        graphics=Graphics(VIDEO,LIB)
        for theme in ('dark','light'):
            icons.theme=theme;icons.identity['theme']=theme
            for flags in (0,1,2,3,4,8,32,16|32,3|4|32,3|4|64):
                records=[(32,d.FG,d.BG,0)]*80
                for i,cp in enumerate((0xf030c,0xf071,0xf036d)):
                    records[40+i]=(cp,0x9a50d0,0x203040,flags)
                renderer=d.Renderer(self.cache,icons=icons)
                frame=renderer.render(records,20,4).copy()
                pixels=np.full((16,24,3),(0,0,255),np.uint8)
                item=types.SimpleNamespace(generation=flags+1000,sx=0,sy=0,sw=24,sh=16,dw=24,dh=16,x=0,y=0,z=-1)
                pane=types.SimpleNamespace(terminal=types.SimpleNamespace(images=lambda:[(item,pixels)]))
                workspace=types.SimpleNamespace(cw=8,ch=16,current={'panes':[pane]},layout=lambda:[(0,1,20,3)],text_ink=renderer.ink(records,20,4))
                graphics.composite(frame,workspace)
                doc=self.document(records,20,4,icons=icons.identity,planes=graphics.records)
                ref=reconstruct(doc,TEXT,LIB,VIDEO/'weights/neuronte-video-masks-v0.nrt',icon_weights=TRAIN/'weights/neuronte-icon-v1.nrt',icon_manifest=ROOT/'icons.json')
                np.testing.assert_array_equal(frame,ref)
                if flags&64:np.testing.assert_array_equal(frame[32:48,:24],pixels)

    def test_icon_pointer_contrast_hotspot_edge_clipping_and_click_coordinates(self):
        icons=self.icons();self.assertEqual(icons.pointer_scalar,0xe0b2);self.assertEqual(icons.hotspot,[0,8])
        mask=icons.mask(0xe0b2,0,0).astype(np.uint16)
        for background in (0,255):
            for pos in ((0,0),(39,0),(0,47),(39,47),(12,23)):
                frame=np.full((48,40,3),background,np.uint8);expected=frame.copy()
                pointer(frame,pos,self.cache,icons=icons)
                x,y=pos[0],pos[1]-8;x0,y0,x1,y1=max(0,x),max(0,y),min(40,x+8),min(48,y+16)
                a=mask[y0-y:y1-y,x0-x:x1-x,None]
                expected[y0:y1,x0:x1]=((a*(255-background)+(255-a)*background+127)//255).astype(np.uint8)
                np.testing.assert_array_equal(frame,expected);self.assertTrue(np.any(frame!=background))
        terminal,w=self.workspace();terminal.resize(20,4,8,16)
        terminal.feed(b'\x1b[?1000h\x1b[?1006h')
        reports=[];w.tabs[0]['panes'][0].send=reports.append
        # Display y includes the2 chrome/title rows. Hotspot offset is visual
        # only; reports must use unchanged physical pointer coordinates.
        w.mouse(19,53,0,1);self.assertEqual(reports[-1],b'\x1b[<0;3;2M')


if __name__=='__main__':unittest.main()
