#!/usr/bin/env python3
"""Sample an owned guest every 100 ms; verify distinct frames from grid history."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from qmp import QMP
from reference import read_ppm,reconstruct


def capture(socket,output,seconds,resume):
    output.mkdir(parents=True,exist_ok=True)
    q=QMP(socket); frames=[]; unique={}
    start=time.monotonic()
    try:
        if resume: q.execute('cont')
        for index in range(int(seconds*10)):
            deadline=start+index/10
            time.sleep(max(0,deadline-time.monotonic()))
            before=time.monotonic()
            temporary=output/'sample.ppm'
            q.execute('screendump',filename=str(temporary.resolve()))
            actual=read_ppm(temporary)
            digest=hashlib.sha256(actual.tobytes()).hexdigest()
            black=not np.any(actual)
            if digest not in unique:
                target=output/(digest+'.ppm')
                temporary.replace(target)
                unique[digest]=dict(black=black,shape=list(actual.shape))
            frames.append(dict(index=index,seconds=before-start,hash=digest,black=black,
                               capture_seconds=time.monotonic()-before))
        document=dict(period_seconds=.1,frames=frames,unique=unique,
                      max_interval_seconds=max(b['seconds']-a['seconds'] for a,b in zip(frames,frames[1:])))
        (output/'timeline.json').write_text(json.dumps(document,indent=2)+'\n')
        print(json.dumps(dict(samples=len(frames),unique=len(unique),max_interval_seconds=document['max_interval_seconds'])))
    finally: q.close()


def verify(output,history,runtime):
    document=json.loads((output/'timeline.json').read_text())
    reconstructed={}
    weights=runtime/'weights'/json.loads((runtime/'deps.lock.json').read_text())['neuronte']['weights']
    for path in history.glob('*.json'):
        grid=json.loads(path.read_text())
        actual=reconstruct(grid,weights,runtime/'build/libneuronte.so',runtime/'weights/neuronte-video-masks-v0.nrt')
        reconstructed[hashlib.sha256(actual.tobytes()).hexdigest()]=path.name
    unknown=[digest for digest,frame in document['unique'].items() if not frame['black'] and digest not in reconstructed]
    neural=[frame for frame in document['frames'] if frame['hash'] in reconstructed]
    result=dict(pass_black_or_neural=not unknown,samples=len(document['frames']),
                black_samples=sum(frame['black'] for frame in document['frames']),
                neural_samples=len(neural),unmatched_hashes=unknown,
                independent_grid_count=len(reconstructed),first_neural_seconds=neural[0]['seconds'] if neural else None,
                max_interval_seconds=document['max_interval_seconds'])
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result))
    return 0 if not unknown and neural else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qmp'); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=float,default=25); p.add_argument('--resume',action='store_true')
    p.add_argument('--history',type=Path); p.add_argument('--runtime',type=Path)
    args=p.parse_args()
    if args.history:
        if not args.runtime: p.error('--runtime required with --history')
        raise SystemExit(verify(args.output,args.history,args.runtime))
    if not args.qmp: p.error('--qmp required to capture')
    capture(args.qmp,args.output,args.seconds,args.resume)
