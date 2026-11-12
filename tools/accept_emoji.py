#!/usr/bin/env python3
"""Opt-in colour acceptance on an owned guest; restore its session afterward."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
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
    p.add_argument('--analytic-fixture',action='store_true',help='explicit single-scalar analytic test, not real300 acceptance')
    p.add_argument('--analytic-negative',action='store_true',help='known red/green half fixture with a blue image behind it')
    p.add_argument('--installed-assets',action='store_true',help='require the locked pair already installed under /opt/neuronte-display; upload no assets')
    p.add_argument('--timeout',type=float,default=120,help='maximum seconds for each cold-render wait')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if args.timeout<=0:p.error('--timeout must be positive')
    command=['ssh','-i',args.key,'-p',args.port,'-o','BatchMode=yes','-o','UserKnownHostsFile='+args.known_hosts,'pleb@127.0.0.1']
    def ssh(script,data=None):return subprocess.check_output(command+[script],input=data)
    original=ssh('cat /etc/default/neuronte-display')
    manifest=json.loads(args.manifest.read_text())
    assert manifest['version']==1 and manifest['cell_columns']==2
    scalars=[int(row['scalar'][2:],16) for row in manifest['glyphs']]
    analytic=args.analytic_fixture or args.analytic_negative
    assert len(scalars)==(1 if analytic else 300),'real acceptance requires exactly300 manifest scalars'
    assert len(set(scalars))==len(scalars),'manifest scalars must be unique'
    weight_hash=hashlib.sha256(args.weights.read_bytes()).hexdigest()
    manifest_hash=hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    guest_weights='/tmp/neuronte-colour.nrt';guest_manifest='/tmp/neuronte-colour.json'
    if args.installed_assets:
        if analytic:p.error('--installed-assets requires the accepted real300 bundle')
        local_lock=json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']['emoji']
        guest_lock=json.loads(ssh('cat /opt/neuronte-display/deps.lock.json'))['neuronte']['emoji']
        assert local_lock==guest_lock,'installed emoji lock differs from reference payload'
        assert local_lock['weights_sha256']==weight_hash and local_lock['manifest_sha256']==manifest_hash
        guest_payload=json.loads(ssh('cat /opt/neuronte-display/manifest.json'))
        asset_proof=[]
        for kind in ('weights','manifest'):
            relative='weights/'+local_lock[kind];path='/opt/neuronte-display/'+relative
            digest=ssh('sha256sum -- '+shlex.quote(path)).decode().split()[0]
            assert digest==local_lock[kind+'_sha256']==guest_payload[relative]
            asset_proof.append(dict(kind=kind,path=path,sha256=digest))
        guest_weights='/opt/neuronte-display/weights/'+local_lock['weights']
        guest_manifest='/opt/neuronte-display/weights/'+local_lock['manifest']
        (args.output/'installed-assets.json').write_text(json.dumps(dict(lock=local_lock,assets=asset_proof),indent=2)+'\n')
    source="import sys\nsys.stdout.write('\\x1b[?2027h\\x1b[2J\\x1b[H\\x1b[?25lCOLOUR_EMOJI_ACCEPT\\r\\n')\n"
    # The application opts into grapheme width mode, then VS16 requests wide
    # presentation. Bare text-presentation scalars remain narrow by default.
    source+='values='+repr(scalars)+'\n'
    source+="for start in range(0,len(values),20):\n sys.stdout.write(''.join(chr(cp)+'\\ufe0f ' for cp in values[start:start+20])+'\\r\\n')\n"
    source+="sys.stdout.write('END_EMOJI\\r\\n');sys.stdout.flush()\n"
    if args.analytic_negative:
        assert scalars==[0x1f600],'analytic check requires its single-scalar fixture'
        source+="import base64\nsys.stdout.write('\\x1b[2;1H\\x1b_Ga=T,f=24,s=16,v=16,z=-1,q=2;'+base64.b64encode(bytes([0,0,255])*256).decode()+'\\x1b\\\\');sys.stdout.flush()\n"
    q=QMP(args.qmp);results=[]
    try:
        if not args.installed_assets:
            ssh('cat > /tmp/neuronte-colour.nrt',args.weights.read_bytes())
            ssh('cat > /tmp/neuronte-colour.json',args.manifest.read_bytes())
        ssh('cat > /tmp/neuronte-colour.py',source.encode())
        for theme in ('dark','light'):
            addition=('\nNEURONTE_EMOJI_WEIGHTS='+guest_weights+'\nNEURONTE_EMOJI_MANIFEST='+guest_manifest+'\nNEURONTE_THEME='+theme+'\n').encode()
            ssh('sudo tee /etc/default/neuronte-display >/dev/null',original+addition)
            before_restart=float(ssh("cut -d' ' -f1 /proc/uptime"))
            ssh('sudo systemctl restart neuronte-display')
            deadline=time.monotonic()+args.timeout
            while time.monotonic()<deadline:
                time.sleep(.1)
                doc=json.loads(ssh('cat /tmp/neuronte-frame.json'))
                if (doc.get('emoji') and doc['emoji']['theme']==theme
                    and doc['emoji']['weights']==weight_hash
                    and doc['timing']['presented_monotonic']>=before_restart):break
            else:raise AssertionError('optional colour mode did not start')
            q.text('python3 /tmp/neuronte-colour.py\n')
            deadline=time.monotonic()+args.timeout
            while time.monotonic()<deadline:
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
            observed=[];half_differences=0;half_cells=[]
            cw,ch=doc['cell_width'],doc['cell_height']
            for i,record in enumerate(doc['records']):
                if not record[3]&512:continue
                value=record[0][0] if isinstance(record[0],list) else record[0]
                if value not in scalars:continue
                observed.append(value);y,x=divmod(i,doc['cols'])
                assert x+1<doc['cols'] and doc['records'][i+1][3]&1024,'right half is not reserved'
                left=actual[y*ch:(y+1)*ch,x*cw:(x+1)*cw]
                right=actual[y*ch:(y+1)*ch,(x+1)*cw:(x+2)*cw]
                for half,pixels in enumerate((left,right)):
                    assert pixels.shape==(ch,cw,3),'half is clipped'
                    half_cells.append(dict(scalar=f'U+{value:04X}',half=half,column=x+half,row=y,
                        sha256=hashlib.sha256(pixels.tobytes()).hexdigest()))
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
                observed_half_cells=len(half_cells),left_half_cells=sum(h['half']==0 for h in half_cells),
                right_half_cells=sum(h['half']==1 for h in half_cells),grapheme_mode=2027,
                pixels=actual.shape[0]*actual.shape[1],identity=doc['emoji'],analytic_fixture=analytic,
                analytic_negative=args.analytic_negative,installed_assets=args.installed_assets)
            (args.output/(theme+'-half-cells.json')).write_text(json.dumps(half_cells,indent=2)+'\n')
            results.append(result);print(json.dumps(result),flush=True)
            (args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            assert result['equal'] and sorted(observed)==sorted(scalars)
            assert len(half_cells)==(2 if analytic else 600)
        (args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    finally:
        q.close()
        ssh('sudo tee /etc/default/neuronte-display >/dev/null',original)
        ssh('sudo systemctl restart neuronte-display')


if __name__=='__main__':main()
