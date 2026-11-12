#!/usr/bin/env python3
"""Paced changing-frame Kitty transport through the actual neural image plane.

Run after the primary suite's recovery check. This checks frame replacement,
not animation-protocol extensions or a real-time throughput target.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import numpy as np
from qmp import QMP
from reference import read_ppm,reconstruct


def source_frame(index,width=64,height=48):
    y,x=np.indices((height,width))
    # A moving, asymmetric RGB pattern; every pixel has an independent expected
    # value before any parser, video encoder or C inference is involved.
    return np.stack(((x*13+y*7+index*31)%256,
                     (x*3+y*19+index*47)%256,
                     (x*17+y*5+index*61)%256),axis=-1).astype(np.uint8)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qmp','port','key','known-hosts'):p.add_argument('--'+name,required=True)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--frames',type=int,default=8)
    args=p.parse_args()
    if not 2<=args.frames<=32:p.error('--frames must be2..32')
    args.output.mkdir(parents=True,exist_ok=True)
    command=['ssh','-i',args.key,'-p',args.port,'-o','BatchMode=yes','-o','UserKnownHostsFile='+args.known_hosts,'pleb@127.0.0.1']
    def ssh(script,data=None):return subprocess.check_output(command+[script],input=data)
    nonce=f'{time.time_ns():x}'
    source='nonce='+repr(nonce)+'\n'+r'''
import base64,sys,time
from pathlib import Path
control=Path('/tmp/neuronte-video-step')
sys.stdout.write('\x1b_Ga=d,d=A;\x1b\\\x1b[2J\x1b[H\x1b[?25l')
sys.stdout.flush();last=-1;deadline=time.monotonic()+120
try:
 while time.monotonic()<deadline:
  index=int(control.read_text())
  if index<0:break
  if index==last:time.sleep(.02);continue
  pixels=bytes(channel for y in range(48) for x in range(64) for channel in
    ((x*13+y*7+index*31)%256,(x*3+y*19+index*47)%256,(x*17+y*5+index*61)%256))
  out=f'\x1b[1;1HVIDEO_{nonce}_{index}\x1b[5;8H'
  # One fixed image/placement ID is replaced on every frame; no delete/recreate
  # between frames and no animation frames/timing extensions.
  out+='\x1b_Ga=T,f=24,s=64,v=48,i=97531,p=1,C=1,q=2;'+base64.b64encode(pixels).decode()+'\x1b\\'
  sys.stdout.write(out);sys.stdout.flush();last=index
finally:
 sys.stdout.write('\x1b_Ga=d,d=A;\x1b\\\x1b[10;1HVIDEO_SEQUENCE_DONE\r\n');sys.stdout.flush()
'''
    ssh('cat > /tmp/neuronte-video-sequence.py',source.encode())
    ssh('printf 0 > /tmp/neuronte-video-step')
    q=QMP(args.qmp);results=[]
    lock=json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']
    try:
        q.text('python3 /tmp/neuronte-video-sequence.py\n')
        for index in range(args.frames):
            if index:ssh(f'printf {index} > /tmp/neuronte-video-step.next && mv /tmp/neuronte-video-step.next /tmp/neuronte-video-step')
            expected_source=source_frame(index)
            (args.output/f'source-{index:02d}.rgb').write_bytes(expected_source.tobytes())
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                time.sleep(.05);raw=ssh('cat /tmp/neuronte-frame.json');doc=json.loads(raw)
                text=''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'])
                if f'VIDEO_{nonce}_{index}' not in text:continue
                q.execute('screendump',filename=str((args.output/f'frame-{index:02d}.ppm').resolve()))
                if ssh('cat /tmp/neuronte-frame.json')==raw:break
            else:raise AssertionError('frame update did not settle')
            (args.output/f'frame-{index:02d}.json').write_bytes(raw)
            assert len(doc['planes'])==1,'replacement accumulated or lost image placements'
            plane=doc['planes'][0];x,y=plane['x'],plane['y']
            assert (plane['width'],plane['height'])==(64,48) and plane['clip']==[x,y,x+64,y+48]
            actual=read_ppm(args.output/f'frame-{index:02d}.ppm')
            crop=actual[y:y+48,x:x+64]
            expected=reconstruct(doc,args.runtime/'weights'/lock['weights'],args.runtime/'build/libneuronte.so',
                                 args.runtime/'weights'/lock['video_weights'])
            result=dict(frame=index,source_sha256=hashlib.sha256(expected_source.tobytes()).hexdigest(),
                actual_image_sha256=hashlib.sha256(crop.tobytes()).hexdigest(),
                actual_frame_sha256=hashlib.sha256(actual.tobytes()).hexdigest(),
                source_exact=bool(np.array_equal(crop,expected_source)),
                independent_frame_exact=bool(np.array_equal(actual,expected)),
                max_error=int(np.abs(actual.astype(int)-expected.astype(int)).max()),
                pixels=actual.shape[0]*actual.shape[1],image=[x,y,64,48],plane_count=1,
                presented_monotonic=doc['timing']['presented_monotonic'])
            results.append(result)
            (args.output/'frames.json').write_text(json.dumps(results,indent=2)+'\n')
            print(json.dumps(result),flush=True)
            assert result['source_exact'] and result['independent_frame_exact']
        distinct=len({item['actual_image_sha256'] for item in results})
        assert distinct==args.frames
        summary=dict(passed=True,requested_frames=args.frames,observed_frames=len(results),distinct_image_frames=distinct,
            distinct_full_frames=len({item['actual_frame_sha256'] for item in results}),
            all_source_exact=True,all_independent_frames_exact=True,image_id=97531,placement_id=1,
            source_size=[64,48],transport='Kitty a=T replacement at a fixed image/placement ID',
            timing='paced by capture acknowledgements; no real-time frame-rate claim',
            release='RC9',video_revision=lock['video_revision'],video_weights_sha256=lock['video_weights_sha256'])
        (args.output/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
    finally:
        ssh('printf -- -1 > /tmp/neuronte-video-step.next && mv /tmp/neuronte-video-step.next /tmp/neuronte-video-step')
        q.close()


if __name__=='__main__':main()
