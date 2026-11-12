#!/usr/bin/env python3
"""External QMP acceptance of preserved clusters and independently composed pixels."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import numpy as np
from qmp import QMP
from reference import read_ppm,reconstruct


MARKS=(0x300,0x301,0x302,0x303,0x304,0x306,0x307,0x308,0x30a,0x30b,0x30c,0x312,0x315,0x326,0x327,0x328)
CASES=[(f'q + {cp:04X}',[113,cp],0) for cp in MARKS]
CASES += [(f'{base} + {cp:04X}',[ord(base),cp],0) for base in 'gjy' for cp in (0x326,0x327,0x328)]
CASES += [(name,value,0) for name,value in (
    ('Q acute',[81,769]),('Greek alpha acute',[945,769]),('Russian a acute',[1072,769]),
    ('x acute cedilla',[120,769,807]),('q diaeresis acute',[113,776,769]),
    ('i acute caron',[105,769,780]),('j acute',[106,769]),('g cedilla acute',[103,807,769]),
    ('m acute',[109,769]),('m acute singleton',[0x1e3f]),('alpha singleton',[0x3ac]),
    ('trained e acute',[101,769]),('trained Russian yo',[1077,776]),('unsupported mark',[120,0x309]),
    ('unsupported Arabic',[0x627,0x64e]),('q two below',[113,806,808]))]
CASES += [(f'x acute SGR {sgr}',[120,769],sgr) for sgr in (1,3,4,7,2,8,9)]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qmp','port','key','known-hosts'):p.add_argument('--'+name,required=True)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    command=['ssh','-i',args.key,'-p',args.port,'-o','BatchMode=yes','-o','UserKnownHostsFile='+args.known_hosts,'pleb@127.0.0.1']
    def ssh(script,data=None):return subprocess.check_output(command+[script],input=data)
    initial=json.loads(ssh('cat /tmp/neuronte-frame.json'))
    assert initial.get('graphemes'),'candidate policy missing from guest'
    columns,rows=initial['cols'],initial['rows'];cw,ch=initial['cell_width'],initial['cell_height']
    assert columns>=32 and rows>=10
    # Leave room for workspace chrome, the negative-z row and the shell prompt.
    count=rows-8
    pages=[CASES[i:i+count] for i in range(0,len(CASES),count)]
    source='import base64,sys\npages='+repr(pages)+'\n'
    nonce=f'{time.time_ns():x}'
    source+='nonce='+repr(nonce)+'\n'
    source+='cw,ch='+repr((cw,ch))+'\n'
    source+=r'''
page=int(sys.argv[1]);cases=pages[page]
out='\x1b_Ga=d,d=A;\x1b\\\x1b[2J\x1b[H\x1b[?25lGP_'+nonce+'_'+str(page)
for row,(label,value,sgr) in enumerate(cases,2):
 text=''.join(map(chr,value))
 out+=f'\x1b[{row};1H{label[:20]:20s}\x1b[{row};23H'+chr(value[0])
 out+=f'\x1b[{row};26H\x1b[{sgr}m'+text+'\x1b[0mZ'
 out+=f'\x1b[{row};30H\ufffd'
row=len(cases)+3
out+=f'\x1b[{row};1Hnegative-z x acute\x1b[{row};26Hx\u0301'
out+=f'\x1b[{row};26H\x1b_Ga=T,f=24,s={cw},v={ch},z=-1,q=2;'+base64.b64encode(bytes([0,0,255])*(cw*ch)).decode()+'\x1b\\'
out+=f'\x1b[{row+1};1HGD_{nonce}_{page}\r\n'
sys.stdout.write(out);sys.stdout.flush()
'''
    ssh('cat > /tmp/neuronte-graphemes.py',source.encode())
    q=QMP(args.qmp);results=[]
    lock=json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']
    weights=args.runtime/'weights'/lock['weights'];library=args.runtime/'build/libneuronte.so'
    try:
        for page,cases in enumerate(pages):
            q.text(f'python3 /tmp/neuronte-graphemes.py {page}\n')
            deadline=time.monotonic()+60
            marker=f'GD_{nonce}_{page}'
            while time.monotonic()<deadline:
                time.sleep(.1);raw=ssh('cat /tmp/neuronte-frame.json');doc=json.loads(raw)
                text=''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'])
                if marker not in text:continue
                q.execute('screendump',filename=str((args.output/f'page-{page}.ppm').resolve()))
                if ssh('cat /tmp/neuronte-frame.json')==raw:break
            else:raise AssertionError('no stable completed grapheme page')
            (args.output/f'page-{page}.json').write_bytes(raw)
            observed=[]
            header=f'GP_{nonce}_{page}'
            header_row=next(row for row in range(rows) if ''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'][row*columns:(row+1)*columns]).startswith(header))
            for row,(label,value,sgr) in enumerate(cases,header_row+1):
                record=doc['records'][row*columns+25]
                cluster=record[0] if isinstance(record[0],list) else [record[0]]
                assert cluster==value,(label,cluster,value)
                assert doc['records'][row*columns+26][0]==90,'cluster overwrote its neighbour or changed width'
                assert not record[3]&(512|1024),'combining fixture unexpectedly became wide'
                observed.append(dict(label=label,cluster=cluster,flags=record[3],row=row,column=25))
            assert len(doc['planes'])==1 and doc['planes'][0]['z']<0
            expected=reconstruct(doc,weights,library,args.runtime/'weights'/lock['video_weights'])
            actual=read_ppm(args.output/f'page-{page}.ppm')
            delta=np.abs(actual.astype(int)-expected.astype(int))
            result=dict(page=page,cases=observed,equal=bool(np.array_equal(actual,expected)),
                max_error=int(delta.max()),pixels=actual.shape[0]*actual.shape[1],cell=[cw,ch],
                policy=doc['graphemes'],weights_sha256=hashlib.sha256(weights.read_bytes()).hexdigest(),
                composed_cells=doc['timing'].get('composed_grapheme_cells'),
                unsupported_cells=doc['timing'].get('unsupported_grapheme_cells'),negative_z=True)
            results.append(result);(args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k!='cases'}),flush=True)
            assert result['equal']
    finally:q.close()


if __name__=='__main__':main()
