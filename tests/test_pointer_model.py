"""Dedicated artwork routing and exact C output at manifest-approved sizes."""
import copy
import ctypes as C
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import neuronte_display as d
from display_pointer import PointerModel
from display_graphics import PointerFrame, pointer
from tools.reference import reconstruct
from tools.package import selected_pointer

HOME = Path(os.environ.get('NEURONTE_HOME', Path.home()/'projects/neuronte'))
ART = Path(os.environ.get('NEURONTE_POINTER_HOME', Path.home()/'projects/neuronte-pointer'))
WEIGHTS = ART/'weights/neuronte-pointer-v0.nrt'
MANIFEST = ART/'weights/neuronte-pointer-v0.manifest.json'
LIBRARY = HOME/'build/libneuronte.so'


class PointerModelTests(unittest.TestCase):
    def model(self, w=16, h=32):
        model = PointerModel(WEIGHTS, MANIFEST, LIBRARY, w, h)
        self.addCleanup(model.close)
        return model

    def test_native_pixels_samples_hotspots_clipping_and_motion_restore(self):
        lib = C.CDLL(str(LIBRARY))
        lib.neuronte_open_file.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        lib.neuronte_close.argtypes = [C.c_void_p]
        lib.neuronte_set_samples.argtypes = [C.c_void_p, C.c_int]
        lib.neuronte_draw_scalar.argtypes = [C.c_void_p, C.c_void_p]+[C.c_int]*7+[C.c_uint32, C.c_uint32]
        handle = C.c_void_p()
        self.assertEqual(lib.neuronte_open_file(os.fsencode(WEIGHTS), C.byref(handle)), 0)
        self.addCleanup(lib.neuronte_close, handle)
        for w, h, samples, hotspot in ((16, 32, 2, (1, 1)), (32, 64, 1, (3, 3))):
            model = self.model(w, h)
            self.assertEqual((model.samples, model.hotspot), (samples, hotspot))
            lib.neuronte_set_samples(handle, samples)
            base = np.random.default_rng(w).integers(0, 256, (h+17, w+27, 3), dtype=np.uint8)
            cache = types.SimpleNamespace(cw=w, ch=h)
            # Explicit pointer wins over an icon bundle; the latter is never
            # consulted and no model scalar can enter the text renderer.
            overlay = PointerFrame(cache, icons=object(), model=model)
            overlay.replace(base.copy())
            for position in ((0, 0), (7, 9), (8, 10), (w+26, h+16), None):
                packed = np.full(base.shape[:2], 0xff000000, np.uint32)
                for channel, shift in enumerate((16, 8, 0)):
                    packed |= base[..., channel].astype(np.uint32)<<shift
                if position is not None:
                    self.assertEqual(lib.neuronte_draw_scalar(handle, packed.ctypes.data,
                        packed.shape[1], packed.shape[1], packed.shape[0],
                        position[0]-hotspot[0], position[1]-hotspot[1], w, h, 0x100001, 0xffffffff), 0)
                expected = np.stack([packed>>shift&255 for shift in (16, 8, 0)], axis=2).astype(np.uint8)
                with patch.object(model.art, 'unstyled', wraps=model.art.unstyled) as draw:
                    overlay.move(position)
                    for call in draw.call_args_list: self.assertEqual(call.args[0], PointerModel.SCALAR)
                np.testing.assert_array_equal(overlay.frame, expected)

    def test_reject_hash_routing_palette_size_hotspot_and_provenance_changes(self):
        original = json.loads(MANIFEST.read_text())
        mutations = [
            lambda doc: doc.update(model_sha256='0'*64),
            lambda doc: doc.update(scalar=65),
            lambda doc: doc.update(crop=[0, 0, 16, 16]),
            lambda doc: doc.update(other_scalars='supported'),
            lambda doc: doc.update(palette_entries=[0, 1]),
            lambda doc: doc.update(palette_rgb=[[255, 0, 0], [255, 255, 255]]),
            lambda doc: doc.update(model='../outside.nrt'),
            lambda doc: doc.update(source_sha256={'source': 'bad'}),
            lambda doc: doc.update(sizes=[]),
            lambda doc: doc['sizes'].append(copy.deepcopy(doc['sizes'][0])),
            lambda doc: doc['sizes'][0].update(hotspot=[-1, 0]),
            lambda doc: doc['sizes'][0].update(hotspot=[16, 0]),
            lambda doc: doc['sizes'][0].update(hotspot=[True, 0]),
            lambda doc: doc['sizes'][0].update(supersampling=0),
            lambda doc: doc['sizes'][0].update(width=0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'pointer.json'
            for change in mutations:
                doc = copy.deepcopy(original)
                change(doc)
                manifest.write_text(json.dumps(doc))
                with self.subTest(doc=doc), self.assertRaises(ValueError):
                    PointerModel(WEIGHTS, manifest, LIBRARY, 16, 32)
            with self.assertRaisesRegex(ValueError, 'cell size'):
                PointerModel(WEIGHTS, MANIFEST, LIBRARY, 8, 16)

    def test_snapshot_uses_presented_position_and_independent_model_oracle(self):
        model = self.model()
        with tempfile.TemporaryDirectory() as directory:
            cache = d.MaskCache(HOME/'weights/neuronte-split-v4.nrt', LIBRARY, directory, 16, 32)
            self.addCleanup(cache.close)
            renderer = d.Renderer(cache)
            records = [(ord('A'), 0xabcdef, 0x123456, 0)]*12
            frame = renderer.render(records, 4, 3).copy()
            pointer(frame, (5, 7), cache, model=model)
            workspace = types.SimpleNamespace(cols=4, rows=3, pointer=(55, 77), presented_pointer=(5, 7))
            path = Path(directory)/'frame.ppm'
            d.save_snapshot(path, frame, records, workspace, cache, renderer.last, pointer_model=model)
            doc = json.loads(path.with_suffix('.json').read_text())
            self.assertEqual(doc['pointer'], [5, 7])
            self.assertEqual(doc['pointer_model'], model.identity)
            expected = reconstruct(doc, HOME/'weights/neuronte-split-v4.nrt', LIBRARY,
                                   pointer_weights=WEIGHTS, pointer_manifest=MANIFEST)
            np.testing.assert_array_equal(frame, expected)
            with self.assertRaisesRegex(ValueError, 'dedicated pointer'):
                reconstruct(doc, HOME/'weights/neuronte-split-v4.nrt', LIBRARY)
            with patch.object(model.art, 'unstyled', side_effect=AssertionError('text routed to pointer')):
                renderer.render([(PointerModel.SCALAR, 0xffffff, 0, 0)]*12, 4, 3)

    def test_optional_package_pair_is_exact_hash_bound_and_cannot_replace_other_files(self):
        pair = selected_pointer(WEIGHTS, MANIFEST)
        self.assertEqual(pair, {'weights/neuronte-pointer-v0.nrt': WEIGHTS.read_bytes(),
                               'weights/neuronte-pointer-v0.manifest.json': MANIFEST.read_bytes()})
        with tempfile.TemporaryDirectory() as directory:
            weights, manifest = Path(directory)/'pointer.nrt', Path(directory)/'pointer.json'
            weights.write_bytes(WEIGHTS.read_bytes()+b'changed')
            manifest.write_bytes(MANIFEST.read_bytes())
            with self.assertRaisesRegex(ValueError, 'hash'):
                selected_pointer(weights, manifest)
            weights.write_bytes(WEIGHTS.read_bytes())
            doc = json.loads(MANIFEST.read_bytes())
            doc['model'] = 'README.md'
            manifest.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ValueError, 'package model path'):
                selected_pointer(weights, manifest)


if __name__ == '__main__': unittest.main()
