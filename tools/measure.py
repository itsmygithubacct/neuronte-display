#!/usr/bin/env python3
"""Renderer timings, unknown-scalar routes, and external QMP key visibility."""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neuronte_display import MaskCache,Renderer
from qmp import QMP


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',type=Path,required=True); p.add_argument('--grid',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True); p.add_argument('--qmp')
    args=p.parse_args(); args.output.parent.mkdir(parents=True,exist_ok=True)
    doc=json.loads(args.grid.read_text()); runtime=args.runtime
    weights=runtime/'weights'/json.loads((runtime/'deps.lock.json').read_text())['neuronte']['weights']
    library=runtime/'build/libneuronte.so'
    with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
        cache=MaskCache(weights,library,directory,doc['cell_width'],doc['cell_height'])
        try:
            renderer=Renderer(cache); renderer.render(doc['records'],doc['cols'],doc['rows'])
            cold=renderer.last['seconds']; warm=[]
            for _ in range(10):
                renderer.render(doc['records'],doc['cols'],doc['rows'],force=True)
                warm.append(renderer.last['seconds'])
            renderer.render(doc['records'],doc['cols'],doc['rows'])
            unchanged=renderer.last['seconds']
            cache.lib.neuronte_route.argtypes=[C.c_void_p,C.c_uint32]
            unknown=[]
            for cp in (0x4e2d,0x10fffd,0xefff):
                # Only include the actual scalar cells, not a UTF-8 source string.
                indices=[i for i,cell in enumerate(doc['records']) if cell[0]==cp]
                mask=cache.infer(cp)
                unknown.append(dict(scalar=f'U+{cp:04X}',route=cache.lib.neuronte_route(cache.ptr,cp),
                    visible_cell_indices=indices,nonzero_pixels=int(np.count_nonzero(mask)),
                    mask_sha256=hashlib.sha256(mask.tobytes()).hexdigest()))
            result=dict(weights_sha256=hashlib.sha256(weights.read_bytes()).hexdigest(),
                        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                        grid=[doc['cols'],doc['rows']],cell=[doc['cell_width'],doc['cell_height']],
                        renderer_cold_seconds=cold,warm_full_seconds=warm,
                        warm_full_median_seconds=statistics.median(warm),unchanged_seconds=unchanged,
                        unknown_scalars=unknown,unknown_gate=all(r['visible_cell_indices'] and r['nonzero_pixels'] and r['route']>=0 for r in unknown),
                        limits='Renderer timings are host CPU text rendering; QMP is guest external visibility, includes transport/capture, no physical display latency.')
        finally: cache.close()
    if args.qmp:
        q=QMP(args.qmp); readings=[]
        try:
            for _ in range(5):
                path=args.output.with_suffix('.latency.ppm')
                q.execute('screendump',filename=str(path.resolve())); before=path.read_bytes()
                start=time.monotonic()
                q.execute('send-key',keys=[dict(type='qcode',data='x')],**{'hold-time':25})
                polls=0
                while time.monotonic()-start<3:
                    q.execute('screendump',filename=str(path.resolve())); polls+=1
                    if path.read_bytes()!=before: break
                    time.sleep(.005)
                else: raise AssertionError('QMP key never changed pixels')
                readings.append(dict(seconds=time.monotonic()-start,polls=polls))
                q.keys('backspace');time.sleep(.15)
            result['qmp_key_to_first_changed_capture']=readings
            result['qmp_key_median_seconds']=statistics.median(row['seconds'] for row in readings)
        finally:q.close()
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if result['unknown_gate'] else 1


if __name__=='__main__':raise SystemExit(main())
