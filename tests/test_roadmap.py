import base64
import ctypes as C
import importlib.machinery
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import fcntl
import pty
import select
import signal
import shutil
import struct
import subprocess
import termios
import time
import unittest
from unittest.mock import patch
import types
import zlib
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import neuronte_display as d
from display_io import OuterInput,KMS,EvdevPointer
from display_graphics import Graphics,pointer
from display_emoji import Emoji
from emoji_fixture import fixture
sys.path.insert(0,str(ROOT/'tools'))
from reference import reconstruct
from build_installer import preseed

HOME=Path(os.environ.get('NEURONTE_HOME',Path.home()/'projects/neuronte'))
VIDEO=Path(os.environ.get('NEURONTE_VIDEO_HOME',HOME))


class InputTests(unittest.TestCase):
    def test_emoji_presentation_width_requires_application_mode(self):
        emoji=Emoji.__new__(Emoji);emoji.scalars={0x2764}
        for mode,sequence,wide in ((False,'\u2764\ufe0f',False),
                                   (True,'\u2764',False),
                                   (True,'\u2764\ufe0e',False),
                                   (True,'\u2764\ufe0f',True)):
            with self.subTest(mode=mode,sequence=sequence):
                terminal=d.Terminal(20,4)
                try:
                    if mode: terminal.feed(b'\x1b[?2027h')
                    terminal.feed(sequence.encode())
                    cells,_=terminal.snapshot()
                    self.assertEqual(bool(cells[0][3]&512),wide)
                    self.assertEqual(bool(cells[1][3]&1024),wide)
                    self.assertEqual(emoji.selected(cells[0]),0x2764 if wide else None)
                finally: terminal.close()

    def test_real_outer_pty_resize_updates_child_geometry(self):
        library=HOME/'build/libneuronte.so'
        with tempfile.TemporaryDirectory() as directory:
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,80,640,480))
            process=subprocess.Popen([sys.executable,str(ROOT/'neuronte_display.py'),'--home',str(HOME),
                '--library',str(library),'--cache',directory+'/cache','--snapshot',directory+'/frame.ppm',
                '--snapshot-live','--','/bin/sh','-c','stty size; while IFS= read -r line; do stty size; done'],
                stdin=slave,stdout=slave,stderr=subprocess.PIPE,start_new_session=True,
                env=dict(os.environ,KITTYFB_SKIP_PROBE='1'))
            os.close(slave)
            try:
                def wait(cols,rows):
                    deadline=time.monotonic()+8
                    while time.monotonic()<deadline:
                        if select.select([master],[],[],.03)[0]:
                            try: os.read(master,65536)
                            except OSError: self.fail(process.stderr.read().decode())
                        path=Path(directory)/'frame.json'
                        if path.exists():
                            doc=json.loads(path.read_text())
                            if (doc['cols'],doc['rows'])==(cols,rows): return doc
                        if process.poll() is not None: self.fail(process.stderr.read().decode())
                    self.fail('outer geometry did not propagate')
                # kitty-framebuffer reserves the outer terminal's last row.
                wait(80,29)
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,960,640))
                os.kill(process.pid,signal.SIGWINCH)
                doc=wait(120,39)
                os.write(master,b'\r')
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    if select.select([master],[],[],.04)[0]: os.read(master,65536)
                    doc=json.loads((Path(directory)/'frame.json').read_text())
                    text=''.join(chr(c[0]) for c in doc['records'] if isinstance(c[0],int))
                    if '36 120' in text: break
                else: self.fail('resized PTY did not report the child geometry')
            finally:
                process.terminate()
                try: process.wait(timeout=4)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                os.close(master); process.stderr.close()

    def test_mouse_modes_coords_and_release(self):
        t=d.Terminal(40,20); self.addCleanup(t.close); t.resize(40,20,8,16)
        self.assertEqual(t.mouse(0,1,9,17),b'')
        t.feed(b'\x1b[?1000h\x1b[?1006h')
        self.assertEqual(t.mouse(0,1,9,17),b'\x1b[<0;2;2M')
        self.assertEqual(t.mouse(1,1,9,17),b'\x1b[<0;2;2m')
        self.assertEqual(t.mouse(2,0,20,30),b'')
        t.feed(b'\x1b[?1003h\x1b[?1016h')
        self.assertEqual(t.mouse(2,0,20,30),b'\x1b[<35;21;31M')

    def test_evdev_button_before_axes_uses_syn_position(self):
        class Sink:
            cols=80;rows=30;cw=8;ch=16
            def __init__(self): self.events=[]
            def mouse(self,*args): self.events.append(args)
        sink=Sink(); device=EvdevPointer([],sink)
        read,write=os.pipe(); os.set_blocking(read,False)
        device.devices[read]={0:(0,32767),1:(0,32767)}; device.pending_buttons[read]=[]
        try:
            events=[(1,272,1),(3,0,16384),(3,1,16384),(0,0,0)]
            os.write(write,b''.join(device.event.pack(0,0,*event) for event in events))
            self.assertTrue(device.read(read)); self.assertEqual(sink.events,[(319,239,0,1)])
            # A read can end between an axis/button and its SYN_REPORT.
            sink.events.clear()
            os.write(write,device.event.pack(0,0,1,272,0)+device.event.pack(0,0,3,0,0))
            device.read(read); self.assertEqual(sink.events,[])
            os.write(write,device.event.pack(0,0,0,0,0))
            self.assertTrue(device.read(read)); self.assertEqual(sink.events,[(0,239,1,1)])
        finally: os.close(write); device.close()

    def test_vt_release_cancels_drag_and_reacquire_refreshes_tablet(self):
        class Sink:
            cols=80;rows=30;cw=8;ch=16;pointer=(0,0)
            def __init__(self):self.events=[]
            def mouse(self,*args):self.events.append(args)
        sink=Sink();device=EvdevPointer([],sink)
        read,write=os.pipe();os.set_blocking(read,False)
        device.devices[read]={0:(0,32767),1:(0,32767)};device.pending_buttons[read]=[]
        def enqueue(events):os.write(write,b''.join(device.event.pack(0,0,*e) for e in events))
        try:
            enqueue([(1,272,1),(0,0,0)]);device.read(read)
            device.leave()
            self.assertEqual(sink.events,[(0,0,0,1),(0,0,1,1)])
            before=list(sink.events)
            enqueue([(1,272,0),(3,0,15000),(0,0,0)])
            device.read(read,active=False)
            self.assertEqual(sink.events,before);self.assertEqual(device.button,0)
            enqueue([(1,272,1),(3,0,20000),(0,0,0)])
            with patch('display_io.fcntl.ioctl',return_value=struct.pack('6i',32767,0,32767,0,0,0)):
                device.resume()
            self.assertEqual(sink.events,before)
            self.assertEqual(sink.pointer,(639,479));self.assertEqual(device.button,0)
            enqueue([(2,0,-1),(0,0,0)]);device.read(read)
            self.assertEqual(sink.events[-1],(638,479,2,0))
        finally:os.close(write);device.close()

    def test_fragmented_outer_input_and_modifiers(self):
        class Sink:
            def __init__(self): self.keys=bytearray(); self.events=[]
            def input(self,data): self.keys.extend(data)
            def mouse(self,*args): self.events.append(args)
        sink=Sink(); parser=OuterInput(sink)
        for part in (b'abc\x1b',b'[<',b'16;20;',b'40M\x1b[D'):
            parser.feed(part)
        parser.flush()
        self.assertEqual(sink.keys,b'abc\x1b[D')
        self.assertEqual(sink.events,[(19,39,0,1,2)])

    def test_clusters_preserved_and_unsupported_reported(self):
        t=d.Terminal(40,20); self.addCleanup(t.close)
        t.feed('e\u0301 x\u0301\u0327 👨\u200d👩\u200d👧\u200d👦'.encode())
        records,_=t.snapshot()
        self.assertEqual(records[0][0],(101,769))
        self.assertEqual(d.render_scalar(records[0][0]),233)
        self.assertEqual(set(records[2][0]),{ord('x'),0x301,0x327})
        self.assertEqual(d.render_scalar(records[2][0]),0xfffd)
        self.assertTrue(any(isinstance(c[0],tuple) and 0x200d in c[0] for c in records))
        t.feed(b'\r\n'+('a'+'\u0301'*70).encode())
        cells,_=t.snapshot()
        self.assertTrue(cells[40][3]&256)
        self.assertEqual(cells[40][0],0xfffd)

    def test_kms_refuses_non_vt_before_opening_card(self):
        with self.assertRaises(OSError): KMS('/does/not/exist')


class GraphicsTests(unittest.TestCase):
    def setUp(self):
        self.t=d.Terminal(40,20); self.addCleanup(self.t.close); self.t.resize(40,20,8,16)

    def test_raw_zlib_png_chunk_delete_and_alternate(self):
        original=np.arange(7*5*4,dtype=np.uint8).reshape(5,7,4)
        for format in (32,100):
            if format==100:
                stream=io.BytesIO(); Image.fromarray(original).save(stream,format='PNG'); raw=stream.getvalue()
            else: raw=original.tobytes()
            data=base64.b64encode(zlib.compress(raw))
            self.t.feed(f'\x1b_Ga=T,f={format},s=7,v=5,i=8,o=z,m=1;'.encode()+data[:12]+b'\x1b\\')
            reply=self.t.feed(b'\x1b_Gm=0;'+data[12:]+b'\x1b\\')
            self.assertIn(b'OK',reply)
            item,pixels=self.t.images()[0]
            np.testing.assert_array_equal(pixels,original)
            self.assertEqual((item.dw,item.dh),(7,5))
            self.t.feed(b'\x1b[?1049h'); self.assertEqual(self.t.images(),[])
            self.t.feed(b'\x1b[?1049l'); self.assertEqual(len(self.t.images()),1)
            self.t.feed(b'\x1b_Ga=d,d=A;\x1b\\'); self.assertEqual(self.t.images(),[])

    @unittest.skipUnless((VIDEO/'weights/neuronte-video-masks-v0.nrt').exists(),'video weights required')
    def test_image_generations_separate_panes_and_replacements(self):
        other=d.Terminal(40,20); self.addCleanup(other.close); other.resize(40,20,8,16)
        graphics=Graphics(VIDEO,HOME/'build/libneuronte.so')
        outputs=[]; generations=[]
        for terminal,colour in ((self.t,(255,0,0)),(other,(0,255,0)),(self.t,(0,0,255))):
            pixels=np.full((4,2,3),colour,dtype=np.uint8)
            terminal.feed(b'\x1b_Ga=T,f=24,s=2,v=4,i=1;'+base64.b64encode(pixels)+b'\x1b\\')
            item,raw=terminal.images()[0]; generations.append(item.generation)
            outputs.append(graphics.convert(item,raw)[0])
            np.testing.assert_array_equal(outputs[-1],pixels)
        self.assertEqual(len(set(generations)),3)
        self.assertFalse(np.array_equal(outputs[0],outputs[1]))

    @unittest.skipUnless((VIDEO/'weights/neuronte-video-masks-v0.nrt').exists(),'video weights required')
    def test_multiink_odd_crop_alpha_and_independent_oracle(self):
        rng=np.random.default_rng(102)
        pixels=rng.integers(0,256,(5,7,4),dtype=np.uint8)
        self.t.feed(b'\x1b_Ga=T,f=32,s=7,v=5,i=8;'+base64.b64encode(pixels)+b'\x1b\\')
        graphics=Graphics(VIDEO,HOME/'build/libneuronte.so')
        item,original=self.t.images()[0]
        rgb,alpha,record=graphics.convert(item,original)
        np.testing.assert_array_equal(rgb,pixels[...,:3]); np.testing.assert_array_equal(alpha,pixels[...,3:])
        with tempfile.TemporaryDirectory() as directory:
            cache=d.MaskCache(HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',directory)
            self.addCleanup(cache.close)
            records=[(65,d.FG,d.BG,0)]*80
            frame=d.Renderer(cache).render(records,20,4).copy()
            source=pixels[...,:3].astype(np.uint16); a=pixels[...,3:].astype(np.uint16)
            frame[:5,:7]=((source*a+frame[:5,:7].astype(np.uint16)*(255-a)+127)//255).astype(np.uint8)
            pointer(frame,(30,20),cache)
            doc=dict(cols=20,rows=4,screen=[160,64],cell_width=8,cell_height=16,records=records,
                     planes=[dict(record,x=0,y=0,z=0,clip=[0,0,7,5])],pointer=[30,20])
            actual=reconstruct(doc,HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',VIDEO/'weights/neuronte-video-masks-v0.nrt')
            np.testing.assert_array_equal(actual,frame)

    @unittest.skipUnless((VIDEO/'weights/neuronte-video-masks-v0.nrt').exists(),'video weights required')
    def test_negative_image_preserves_known_colour_half_ink(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);weights=root/'emoji.nrt';weights.write_bytes(fixture())
            manifest=root/'emoji.json';manifest.write_text(json.dumps(dict(version=1,cell_columns=2,glyphs=[dict(scalar='U+1F600')])))
            cache=d.MaskCache(HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',root/'cache')
            self.addCleanup(cache.close)
            graphics=Graphics(VIDEO,HOME/'build/libneuronte.so')
            pixels=np.full((16,16,3),(0,0,255),np.uint8)
            item=types.SimpleNamespace(generation=999,sx=0,sy=0,sw=16,sh=16,dw=16,dh=16,x=0,y=0,z=-1)
            pane=types.SimpleNamespace(terminal=types.SimpleNamespace(images=lambda:[(item,pixels)]))
            for theme,level in (('dark',255),('light',128)):
                emoji=Emoji(weights,manifest,HOME/'build/libneuronte.so',8,16,theme);self.addCleanup(emoji.close)
                for hidden in (False,True):
                    records=[(32,d.FG,d.BG,0)]*(20*18)
                    records[40]=(0x1f600,d.FG,d.BG,512|3|(64 if hidden else 0))
                    records[41]=(32,d.FG,d.BG,1024)
                    renderer=d.Renderer(cache,emoji)
                    frame=renderer.render(records,20,18).copy()
                    workspace=types.SimpleNamespace(cw=8,ch=16,current={'panes':[pane]},
                        layout=lambda:[(0,1,20,16)],text_ink=renderer.ink(records,20,18))
                    graphics.composite(frame,workspace)
                    # Ground truth comes from the analytic artwork, not from
                    # the renderer or reference's compositing implementation.
                    expected=pixels.copy()
                    if not hidden:expected[:,:8]=[level,0,0];expected[:,8:10]=[0,level,0]
                    np.testing.assert_array_equal(frame[32:48,:16],expected)
                    doc=dict(cols=20,rows=18,screen=[160,288],cell_width=8,cell_height=16,
                        records=json.loads(json.dumps(records)),emoji=emoji.identity,planes=graphics.records)
                    ref=reconstruct(doc,HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',
                        VIDEO/'weights/neuronte-video-masks-v0.nrt',weights,manifest)
                    np.testing.assert_array_equal(frame,ref)


class StyleTests(unittest.TestCase):
    def test_two_colour_emoji_halves_themes_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); weights=root/'emoji.nrt'; weights.write_bytes(fixture())
            manifest=root/'emoji.json'; manifest.write_text(json.dumps(dict(cell_columns=2,glyphs=[dict(scalar='U+1F600')])))
            t=d.Terminal(20,18); self.addCleanup(t.close); t.feed('😀Z'.encode())
            records,_=t.snapshot()
            self.assertTrue(records[0][3]&512); self.assertTrue(records[1][3]&1024)
            cache=d.MaskCache(HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',root/'cache')
            self.addCleanup(cache.close)
            if not hasattr(cache.lib,'neuronte_draw_half'): self.skipTest('half API required')
            for theme,level in (('dark',255),('light',128)):
                emoji=Emoji(weights,manifest,HOME/'build/libneuronte.so',8,16,theme); self.addCleanup(emoji.close)
                rendered=d.Renderer(cache,emoji).render(records,20,18)
                self.assertTrue(np.all(rendered[:16,:8]==[level,0,0]))
                self.assertTrue(np.all(rendered[:16,8:10]==[0,level,0]))
                self.assertTrue(np.all(rendered[:16,10:16]==d.colour(records[1][2])))
                self.assertIsNone(emoji.selected(((0x1f600,0x200d,0x1f600),0,0,512)))
                self.assertIsNone(emoji.selected(((0x1f600,0xfe0e),0,0,512)))
                self.assertIsNone(emoji.selected((0x1f601,0,0,512)))
                doc=dict(cols=20,rows=18,screen=[160,288],cell_width=8,cell_height=16,
                         records=json.loads(json.dumps(records)),emoji=emoji.identity)
                ref=reconstruct(doc,HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',emoji_weights=weights,emoji_manifest=manifest)
                np.testing.assert_array_equal(rendered,ref)

    def test_styles_change_masks_and_survive_cache_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=d.MaskCache(HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',directory)
            self.addCleanup(cache.close)
            if not hasattr(cache.lib,'neuronte_coverage_styled'): self.skipTest('styled library required')
            regular=cache.get(65)
            self.assertTrue(all(np.any(cache.get(65,style)!=regular) for style in (1,2,3)))
            self.assertGreater(cache.get(65,1).sum(),regular.sum())
            other=d.MaskCache(HOME/'weights/neuronte-split-v4.nrt',HOME/'build/libneuronte.so',directory)
            self.addCleanup(other.close)
            for style in (0,1,2,3): np.testing.assert_array_equal(cache.get(65,style),other.get(65,style))
            self.assertEqual(other.stats['inferences'],0)


class ProvisionTests(unittest.TestCase):
    def test_opt_in_and_boot_override_preserve_x(self):
        module=importlib.machinery.SourceFileLoader('generator',str(ROOT/'provision/neuronte-generator')).load_module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'etc/pleb').mkdir(parents=True)
            env=root/'etc/pleb/session.env'; env.write_text('PLEB_DISPLAY=x\nUSER_SETTING=keep\n')
            module.generate(root/'default',root,'')
            self.assertFalse((root/'default').exists())
            module.generate(root/'neuronte',root,'quiet pleb.display=neuronte')
            self.assertEqual(os.readlink(root/'neuronte/display-manager.service'),'/dev/null')
            env.write_text('PLEB_DISPLAY=neuronte\nUSER_SETTING=keep\n')
            module.generate(root/'x',root,'pleb.display=x')
            self.assertFalse((root/'x').exists())
            self.assertIn('USER_SETTING=keep',env.read_text())

    def test_addon_install_preserves_existing_selection_and_boot_override(self):
        module=importlib.machinery.SourceFileLoader('install_generator',str(ROOT/'provision/neuronte-generator')).load_module()
        for selected in ('neuronte','x',None):
            with self.subTest(selected=selected),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);payload=root/'payload';target=root/'target'
                shutil.copytree(ROOT/'provision',payload/'provision')
                (target/'etc/pleb').mkdir(parents=True)
                env=target/'etc/pleb/session.env'
                if selected:env.write_text('PLEB_DISPLAY='+selected+'\nUSER_SETTING=keep\n')
                subprocess.run(['sh',str(payload/'provision/install.sh'),str(target),'pleb'],check=True)
                module.generate(root/'normal',target,'')
                self.assertEqual((root/'normal/graphical.target.wants/neuronte-display.service').is_symlink(),selected=='neuronte')
                module.generate(root/'x',target,'pleb.display=x');self.assertFalse((root/'x').exists())
                module.generate(root/'neural',target,'pleb.display=neuronte')
                self.assertTrue((root/'neural/graphical.target.wants/neuronte-display.service').is_symlink())
                if selected:self.assertEqual(env.read_text(),'PLEB_DISPLAY='+selected+'\nUSER_SETTING=keep\n')

    def test_normal_installer_keeps_partition_choice(self):
        source='d-i pkgsel/include string xorg fonts-example \\\n lightdm\nd-i partman-auto/disk string /dev/sda\nd-i preseed/late_command string old\n'
        normal=preseed(source,['python3'],False)
        self.assertNotIn('partman',normal); self.assertNotIn('xorg',normal); self.assertNotIn('fonts-example',normal)
        test=preseed(source,['python3'],True)
        self.assertIn('grep -qx QEMU',test); self.assertIn('/dev/vda',test)


if __name__=='__main__': unittest.main()
