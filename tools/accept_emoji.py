#!/usr/bin/env python3
"""Opt-in colour acceptance on an owned guest; restore its session afterward."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import numpy as np
from qmp import QMP
from reference import read_ppm,reconstruct


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qmp',required=True);p.add_argument('--port',required=True)
    p.add_argument('--key',required=True);p.add_argument('--known-hosts',required=True)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--weights',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--analytic-negative',action='store_true',help='known red/green half fixture with a blue image behind it')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    command=['ssh','-i',args.key,'-p',args.port,'-o','BatchMode=yes','-o','UserKnownHostsFile='+args.known_hosts,'pleb@127.0.0.1']
    def ssh(script,data=None):return subprocess.check_output(command+[script],input=data)
    original=ssh('cat /etc/default/neuronte-display')
    scalars=[int(row['scalar'][2:],16) for row in json.loads(args.manifest.read_text())['glyphs']]
    assert len(scalars)<=300,'acceptance fixture supports at most 300 scalars'
    source="import sys\nsys.stdout.write('\\x1b[2J\\x1b[H\\x1b[?25lCOLOUR_EMOJI_ACCEPT\\r\\n')\n"
    # VS16 requests wide presentation for otherwise textual single scalars.
    source+='values='+repr(scalars)+'\n'
    source+="for start in range(0,len(values),20):\n sys.stdout.write(''.join(chr(cp)+'\\ufe0f ' for cp in values[start:start+20])+'\\r\\n')\n"
    source+="sys.stdout.write('END_EMOJI\\r\\n');sys.stdout.flush()\n"
    if args.analytic_negative:
        assert scalars==[0x1f600],'analytic check requires its single-scalar fixture'
        source+="import base64\nsys.stdout.write('\\x1b[2;1H\\x1b_Ga=T,f=24,s=16,v=16,z=-1,q=2;'+base64.b64encode(bytes([0,0,255])*256).decode()+'\\x1b\\\\');sys.stdout.flush()\n"
    q=QMP(args.qmp);results=[]
    try:
        ssh('cat > /tmp/neuronte-colour.nrt',args.weights.read_bytes())
        ssh('cat > /tmp/neuronte-colour.json',args.manifest.read_bytes())
        ssh('cat > /tmp/neuronte-colour.py',source.encode())
        for theme in ('dark','light'):
            addition=('\nNEURONTE_EMOJI_WEIGHTS=/tmp/neuronte-colour.nrt\nNEURONTE_EMOJI_MANIFEST=/tmp/neuronte-colour.json\nNEURONTE_THEME='+theme+'\n').encode()
            ssh('sudo tee /etc/default/neuronte-display >/dev/null',original+addition)
            ssh('sudo systemctl restart neuronte-display')
            for _ in range(100):
                time.sleep(.1)
                doc=json.loads(ssh('cat /tmp/neuronte-frame.json'))
                if doc.get('emoji') and doc['emoji']['theme']==theme:break
            else:raise AssertionError('optional colour mode did not start')
            q.text('python3 /tmp/neuronte-colour.py\n')
            for _ in range(100):
                time.sleep(.1)
                data=ssh('cat /tmp/neuronte-frame.json');doc=json.loads(data)
                text=''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'])
                if 'END_EMOJI' in text:break
            else:raise AssertionError('emoji fixture did not complete')
            q.execute('screendump',filename=str((args.output/(theme+'.ppm')).resolve()))
            after=ssh('cat /tmp/neuronte-frame.json');assert data==after,'frame changed during capture'
            (args.output/(theme+'.json')).write_bytes(data)
            weights=args.runtime/'weights'/json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']['weights']
            expected=reconstruct(doc,weights,args.runtime/'build/libneuronte.so',
                args.runtime/'weights/neuronte-video-masks-v0.nrt',args.weights,args.manifest)
            actual=read_ppm(args.output/(theme+'.ppm'))
            observed=[];half_differences=0
            cw,ch=doc['cell_width'],doc['cell_height']
            for i,record in enumerate(doc['records']):
                if not record[3]&512:continue
                value=record[0][0] if isinstance(record[0],list) else record[0]
                if value not in scalars:continue
                observed.append(value);y,x=divmod(i,doc['cols'])
                left=actual[y*ch:(y+1)*ch,x*cw:(x+1)*cw]
                right=actual[y*ch:(y+1)*ch,(x+1)*cw:(x+2)*cw]
                half_differences+=int(not np.array_equal(left,right))
            delta=np.abs(expected.astype(int)-actual.astype(int))
            if args.analytic_negative:
                assert len(doc['planes'])==1
                index=next(i for i,c in enumerate(doc['records']) if c[3]&512)
                y,x=divmod(index,doc['cols']);assert (cw,ch)==(8,16)
                known=np.full((16,16,3),(0,0,255),np.uint8)
                level=255 if theme=='dark' else 128
                known[:,:8]=[level,0,0];known[:,8:10]=[0,level,0]
                np.testing.assert_array_equal(actual[y*ch:(y+1)*ch,x*cw:(x+2)*cw],known)
            result=dict(theme=theme,equal=bool(np.array_equal(expected,actual)),max_error=int(delta.max()),
                requested=len(scalars),observed=len(observed),distinct_half_pairs=half_differences,
                pixels=actual.shape[0]*actual.shape[1],identity=doc['emoji'],analytic_negative=args.analytic_negative)
            results.append(result);print(json.dumps(result),flush=True)
            assert result['equal'] and set(observed)==set(scalars)
        (args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    finally:
        q.close()
        ssh('sudo tee /etc/default/neuronte-display >/dev/null',original)
        ssh('sudo systemctl restart neuronte-display')


if __name__=='__main__':main()
