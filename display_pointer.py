"""Explicit single-scalar pointer asset; never participates in text routing."""
import ctypes as C
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

import numpy as np
from display_art import ColourArt


POINTER_SCALAR = 0x100001


def validate_manifest(data, raw):
    """Validate one in-memory model/manifest pair for runtime and packaging."""
    doc = json.loads(raw)
    if (not isinstance(doc, dict) or doc.get('format') != 'neuronte-pointer-manifest' or type(doc.get('version')) != int or
        doc['version'] != 1 or type(doc.get('scalar')) != int or doc['scalar'] != POINTER_SCALAR or
        doc.get('crop', False) is not None or doc.get('other_scalars') != 'unsupported' or
        doc.get('palette_entries') != [2, 3] or doc.get('palette_rgb') != [[0, 0, 0], [255, 255, 255]]):
        raise ValueError('invalid dedicated pointer manifest')
    def relative(path):
        return isinstance(path, str) and bool(path) and not PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts
    sources = doc.get('source_sha256')
    if (not relative(doc.get('model')) or not isinstance(sources, dict) or not sources or
        any(not relative(path) or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
            for path, sha in sources.items())):
        raise ValueError('invalid pointer provenance')
    digest = hashlib.sha256(data).hexdigest()
    if doc.get('model_sha256') != digest:
        raise ValueError('pointer model hash differs from manifest')
    sizes = doc.get('sizes')
    if not isinstance(sizes, list) or not sizes:
        raise ValueError('missing pointer sizes')
    seen = set()
    for size in sizes:
        if not isinstance(size, dict): raise ValueError('invalid pointer size')
        w, h, ss, hotspot = (size.get(key) for key in ('width', 'height', 'supersampling', 'hotspot'))
        if (any(type(n) != int for n in (w, h, ss)) or not 1 <= w <= 256 or
            not 1 <= h <= 512 or not 1 <= ss <= 8 or not isinstance(hotspot, list) or
            len(hotspot) != 2 or any(type(n) != int for n in hotspot) or
            not 0 <= hotspot[0] < w or not 0 <= hotspot[1] < h or (w, h) in seen):
            raise ValueError('invalid pointer size or hotspot')
        seen.add((w, h))
    return doc


class PointerModel:
    SCALAR = POINTER_SCALAR

    def __init__(self, weights, manifest, library, width, height):
        data, raw = Path(weights).read_bytes(), Path(manifest).read_bytes()
        doc = validate_manifest(data, raw)
        digest = doc['model_sha256']
        selected = next((size for size in doc['sizes'] if
                         (size['width'], size['height']) == (width, height)), None)
        if selected is None:
            raise ValueError('selected cell size is not accepted by the pointer manifest')
        self.cw, self.ch = width, height
        self.hotspot = tuple(selected['hotspot'])
        self.samples = selected['supersampling']
        self.art = ColourArt(weights, manifest, library, width, height, 1, 'dark')
        try:
            if self.art.layer_count != 2 or any(
                palette[[2, 3]].tolist() != doc['palette_rgb'] for palette in self.art.palettes):
                raise ValueError('pointer model palette differs from manifest')
            values = np.empty((2, height, width), np.float32)
            labels = np.empty(2, np.uint8)
            if self.art.lib.neuronte_coverage_layers(self.art.ptr, self.SCALAR, width, height, self.samples,
                    values.ctypes.data_as(C.POINTER(C.c_float)), labels.ctypes.data_as(C.POINTER(C.c_uint8))):
                raise ValueError('cannot infer dedicated pointer')
            if labels.tolist() != doc['palette_entries'] or not np.isfinite(values).all():
                raise ValueError('pointer model layers differ from manifest')
            self.art.lib.neuronte_set_samples(self.art.ptr, self.samples)
        except Exception:
            self.art.close()
            raise
        self.identity = dict(weights=digest, manifest=hashlib.sha256(raw).hexdigest(),
                             scalar=self.SCALAR, width=width, height=height,
                             supersampling=self.samples, hotspot=list(self.hotspot), source_sha256=doc['source_sha256'])

    def paint(self, frame, position):
        if position is None: return frame
        px, py = position
        h, w = frame.shape[:2]
        if not 0 <= px < w or not 0 <= py < h: return frame
        x, y = px-self.hotspot[0], py-self.hotspot[1]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x+self.cw), min(h, y+self.ch)
        background = np.zeros((self.ch, self.cw, 3), np.uint8)
        background[y0-y:y1-y, x0-x:x1-x] = frame[y0:y1, x0:x1]
        # The model is invoked only with its one accepted scalar. Fixed black
        # and white layers retain C's original blending/rounding at both sizes.
        painted = self.art.unstyled(self.SCALAR, 0xffffff, background, 0)
        frame[y0:y1, x0:x1] = painted[y0-y:y1-y, x0-x:x1-x]
        return frame

    def close(self):
        self.art.close()
