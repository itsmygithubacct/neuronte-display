import json
import os
from pathlib import Path
import tempfile
import unittest
import numpy as np

import neuronte_display as d
from display_graphemes import Graphemes,IDENTITY,MARKS,POLICY
from tools.reference import reconstruct
from tools.reference_graphemes import reference_composer

HOME=Path(os.environ.get('NEURONTE_HOME',Path.home()/'projects/neuronte'))
SCRIPT=Path(os.environ.get('NEURONTE_SCRIPT_WEIGHTS',HOME/'weights/neuronte-split-v5.nrt'))


class FixtureCache:
    cw,ch=8,16
    identity={'weights':'1e2d89c4d8cc96e7c23eeec2ef688d31222aeb5e4303ecb2dce0c7c783aa8cf5'}
    def __init__(self):
        self.values={cp:np.zeros((16,8),np.uint8) for cp in (120,88,180,44,0xfffd)}
        self.values[120][6:13,1]=255;self.values[120][6:13,6]=255
        self.values[88][2:13,3]=255
        self.values[180][2,4]=255;self.values[180][3,3]=255
        self.values[44][12,4]=255;self.values[44][13,4]=255;self.values[44][14,3]=255
        self.values[0xfffd][8,0]=255
    def get(self,cp,style=0):return self.values[cp]
    infer=get


class GraphemeGeometryTests(unittest.TestCase):
    def test_known_pixels_capital_raise_mirror_and_comma_rotation(self):
        c=FixtureCache();g=Graphemes(c)
        np.testing.assert_array_equal(g.mask((120,769)),np.maximum(c.get(120),c.get(180)))
        np.testing.assert_array_equal(g.mask((120,768)),np.maximum(c.get(120),c.get(180)[:,::-1]))
        expected=np.zeros((16,8),np.uint8);expected[4:13,3]=255;expected[0,3]=255;expected[1,2]=255
        np.testing.assert_array_equal(g.mask((88,769)),expected)
        expected=c.get(120).copy();expected[2,4]=255;expected[3,3]=255;expected[4,3]=255
        np.testing.assert_array_equal(g.mask((120,786)),expected)
        expected=c.get(120).copy()
        for y,x in ((2,7),(3,7),(4,6),(4,7),(5,6)):expected[y,x]=255
        np.testing.assert_array_equal(g.mask((120,789)),expected)
        # Below comma is detached in the bottom band. The body is fitted to
        # the source baseline before union, so descenders cannot hide it.
        expected=np.zeros((16,8),np.uint8);expected[6:12,1]=255;expected[6:12,6]=255
        expected[13,4]=255;expected[14,4]=255;expected[15,3]=255
        np.testing.assert_array_equal(g.mask((120,806)),expected)

    def test_known_pixels_above_below_and_two_above(self):
        c=FixtureCache();c.values[168]=np.zeros((16,8),np.uint8)
        c.values[168][2,2]=255;c.values[168][2,5]=255;g=Graphemes(c)
        expected=np.zeros((16,8),np.uint8);expected[6:12,1]=255;expected[6:12,6]=255
        for y,x in ((2,4),(3,3),(13,4),(14,4),(15,3)):expected[y,x]=255
        np.testing.assert_array_equal(g.mask((120,769,806)),expected)
        expected=np.zeros((16,8),np.uint8);expected[7:13,1]=255;expected[7:13,6]=255
        for y,x in ((4,2),(4,5),(0,4),(1,3)):expected[y,x]=255
        np.testing.assert_array_equal(g.mask((120,776,769)),expected)

    def test_styles_apply_to_complete_mask_and_never_wrap(self):
        c=FixtureCache();g=Graphemes(c);plain=np.maximum(c.get(120),c.get(180))
        shifts=[3,2,2,2,2,1,1,1,1,0,0,0,0,-1,-1,-1]
        for style in (1,2,3):
            expected=np.zeros_like(plain)
            for y,x in np.argwhere(plain):
                for extra in range(2 if style&1 else 1):
                    dest=x+extra+(shifts[y] if style&2 else 0)
                    if 0<=dest<8:expected[y,dest]=255
            np.testing.assert_array_equal(g.mask((120,769),style),expected)

    def test_empty_stacked_component_is_whole_cluster_fallback(self):
        c=FixtureCache();c.values[168]=np.zeros((16,8),np.uint8)
        g=Graphemes(c);value=(120,776,769)
        np.testing.assert_array_equal(g.mask(value),c.get(0xfffd))
        self.assertFalse(g.supported(value))
        reference=reference_composer(POLICY,c.identity['weights'],8,16,c.get)
        np.testing.assert_array_equal(reference(value),c.get(0xfffd))
        self.assertTrue(np.any(g.mask((120,769))!=c.get(0xfffd)))

    def test_unfitting_body_and_unknown_asset_are_not_claimed_supported(self):
        c=FixtureCache();c.values[120][:]=0;c.values[120][0,1]=255
        g=Graphemes(c)
        np.testing.assert_array_equal(g.mask((120,769,769)),c.get(0xfffd))
        self.assertFalse(g.supported((120,769,769)))
        c=FixtureCache();c.identity={'weights':'unrecognized'};g=Graphemes(c)
        self.assertIsNone(g.plan((120,769)))
        self.assertEqual(g.plan((101,769)),(233,)) # original custom-weight NFC policy


class GraphemeAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.library=HOME/'build/libneuronte.so';self.weights=HOME/'weights/neuronte-split-v4.nrt'
    def cache(self,w=8,h=16,weights=None):
        c=d.MaskCache(weights or self.weights,self.library,self.tmp.name,w,h);self.addCleanup(c.close);return c

    def test_all_sixteen_marks_minimum_sizes_and_actual_comparison(self):
        for w,h in ((4,8),(4,9),(8,8),(8,16),(16,32)):
            c=self.cache(w,h);g=Graphemes(c)
            for cp in MARKS:
                with self.subTest(cell=(w,h),mark=hex(cp)):
                    mask=g.mask((120,cp))
                    self.assertEqual(mask.shape,(h,w));self.assertTrue(g.supported((120,cp)))
                    self.assertFalse(np.array_equal(mask,c.get(0xfffd)))
            values=[(120,cp) for cp in MARKS]+[(113,776,769),(120,769,807),(81,769)]
            records=[(v,0xffffff,0,style) for style in range(4) for v in values]
            renderer=d.Renderer(c);frame=renderer.render(records,len(values),4)
            doc=dict(records=records,cols=len(values),rows=4,screen=[w*len(values),h*4],cell_width=w,cell_height=h,graphemes=IDENTITY)
            np.testing.assert_array_equal(frame,reconstruct(doc,self.weights,self.library))

    @unittest.skipUnless(SCRIPT.exists(),'approved script bundle required')
    def test_canonical_script_cases_precomposed_controls_and_cache(self):
        c=self.cache(weights=SCRIPT);g=Graphemes(c)
        for value,cp in (((101,769),233),((1077,776),1105)):
            np.testing.assert_array_equal(g.mask(value),c.get(cp))
        for a,b in (((109,769),0x1e3f),((945,769),0x3ac),((233,807),(101,807,769)),((120,769,807),(120,807,769))):
            np.testing.assert_array_equal(g.mask(a),g.mask(b))
            self.assertTrue(g.supported(a));self.assertGreater(len(g.plan(a)),1)
        for value in ((113,769),(81,769),(1072,769),(113,776,769),(120,769,807),(105,769,807),(106,769)):
            self.assertFalse(np.array_equal(g.mask(value),c.get(0xfffd)))
        for value in ((0x627,0x64e),(0x915,0x93f),(0x1f468,0x200d,0x1f469),(0x1f1fa,0x1f1f8),(120,0x309),(120,769,769,769)):
            self.assertIsNone(g.plan(value))
        self.assertEqual(g.plan(0x10fffd),(0x10fffd,))
        before=g.mask((113,776,769),3).copy();reopened=self.cache(weights=SCRIPT)
        np.testing.assert_array_equal(Graphemes(reopened).mask((113,776,769),3),before)
        self.assertEqual(Graphemes(self.cache()).plan((1072,769)),None)

    def test_width_neighbours_visibility_and_composed_ink(self):
        c=self.cache();renderer=d.Renderer(c)
        terminal=d.Terminal(20,4);self.addCleanup(terminal.close)
        terminal.feed('x\u0301Z'.encode());records,cursor=terminal.snapshot()
        self.assertEqual(cursor,(2,0));self.assertEqual(records[0][0],(120,769));self.assertEqual(records[1][0],ord('Z'))
        for flags in (0,1,2,3,4,8,16,32,64,128):
            cells=[((120,769),0xe0c040,0x123456,flags),(ord('Z'),d.FG,d.BG,0)]
            frame=renderer.render(cells,2,1,force=True).copy();ink=renderer.ink(cells,2,1)
            np.testing.assert_array_equal(frame[:,8:],renderer.cell(cells[1]))
            if flags==64:self.assertFalse(np.any(ink[:,:8]))
            elif not flags&(4|8|128):np.testing.assert_array_equal(ink[:,:8],renderer.graphemes.mask(cells[0][0],flags&3))
            doc=dict(records=cells,cols=2,rows=1,screen=[16,16],cell_width=8,cell_height=16,graphemes=IDENTITY)
            np.testing.assert_array_equal(frame,reconstruct(doc,self.weights,self.library))

    def test_j_dot_isolation_requires_a_whole_pixel_source_gap(self):
        # Independent boundaries from the source dot end6.05/body start7.25;
        # no boundary exists at heights8/10/11, so the whole cluster falls back.
        for h in (8,10,11):
            c=self.cache(8,h);g=Graphemes(c)
            for value in ((106,769),(106,769,776)):
                np.testing.assert_array_equal(g.mask(value),c.get(0xfffd))
                self.assertFalse(g.supported(value))
        for w,h,cut,shift in ((8,9,3,1),(8,12,4,1),(8,14,5,1),(8,16,5,1),(16,32,10,2)):
            c=self.cache(w,h);g=Graphemes(c)
            # At2x2 sampling, row samples are at y+.25/y+.75. A whole
            # boundary within the gap is conservative: the last removed
            # sample precedes the body, and the first retained follows the dot.
            self.assertLess((cut-.25)*20/h,7.25)
            self.assertGreater((cut+.25)*20/h,6.05)
            expected=c.get(106).copy();expected[:cut]=0
            expected[:,shift:]=np.maximum(expected[:,shift:],c.get(180)[:,:-shift])
            np.testing.assert_array_equal(g.mask((106,769)),expected)
            self.assertTrue(g.supported((106,769)))
            # Every retained body pixel survives the union, including rows
            #10/11 at16x32; this is not ceil(7*h/20), which would cut at12.
            self.assertTrue(np.all(g.mask((106,769))[cut:]>=c.get(106)[cut:]))
            reference=reference_composer(POLICY,c.identity['weights'],w,h,c.get)
            np.testing.assert_array_equal(g.mask((106,769)),reference((106,769)))

    def test_actual_below_anchors_on_descenders_and_baseline_edges(self):
        c=self.cache();g=Graphemes(c)
        # Source baseline15 maps to pixel boundary12. All these learned
        # bodies extend past it, including the antialiased non-descender x.
        for base,columns in (('q',(6,7)),('g',(3,4)),('j',(2,3)),('x',(3,4))):
            with self.subTest(base=base):
                self.assertTrue(np.any(c.get(ord(base))[12:]))
                a=g.mask((ord(base),0x326))
                self.assertFalse(np.any(a[12])) # one complete row separates body and comma
                expected=np.zeros((3,8),bool);expected[:,columns]=True
                np.testing.assert_array_equal(a[13:]!=0,expected)
                self.assertTrue(np.any(a[:12]))
