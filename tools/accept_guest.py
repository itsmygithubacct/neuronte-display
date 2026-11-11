#!/usr/bin/env python3
"""Drive an owned QEMU guest externally and reconstruct every captured screen."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import numpy as np
from qmp import QMP
from reference import reconstruct,read_ppm


FIXTURE=r'''import base64,sys,time
out='\x1b[2J\x1b[H\x1b[?25lQMP ACCEPTANCE\r\n'
for attr,label in ((0,'regular'),(1,'bold'),(3,'italic'),(4,'underline'),(9,'strike'),(7,'reverse')):
    out+=f'\x1b[{attr}m{label}: ABC xyz 012\x1b[0m\r\n'
out+='clusters: e\u0301 x\u0301\u0327 \U0001f468\u200d\U0001f469\u200d\U0001f467\r\n'
out+='scripts: \u0391\u03bb\u03c6\u03ac \u0416\u044f\u0451\u0439\r\n'
out+='unknown: \u4e2d \U0010fffd \uefff\r\n'
pixels=bytes(channel for y in range(48) for x in range(64) for channel in ((x*13+y*7)%256,(x*3+y*19)%256,(x*17+y*5)%256))
out+='\x1b_Ga=T,f=24,s=64,v=48,i=8,q=2;'+base64.b64encode(pixels).decode()+'\x1b\\'
sys.stdout.write(out+'\r\nIMAGE_OK\r\n');sys.stdout.flush()
'''

MOUSE=r'''import os,termios,tty
saved=termios.tcgetattr(0)
try:
 tty.setraw(0)
 os.write(1,b'\x1b[2J\x1b[HMOUSE_WAIT\x1b[?1000h\x1b[?1006h')
 data=b''
 while not data.endswith(b'M'): data+=os.read(0,1)
 os.write(1,b'\x1b[?1000l\x1b[?1006l\r\nMOUSE_OK '+repr(data).encode()+b'\r\n')
finally: termios.tcsetattr(0,termios.TCSANOW,saved)
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qmp',required=True); p.add_argument('--port',type=int,required=True)
    p.add_argument('--key',required=True); p.add_argument('--known-hosts',required=True); p.add_argument('--host-alias')
    p.add_argument('--user',default='pleb'); p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--strict',action='store_true',help='require empty fonts, no other display, and bundled parser')
    args=p.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    command=['ssh','-i',args.key,'-p',str(args.port),'-o','BatchMode=yes','-o','ConnectTimeout=8',
             '-o','UserKnownHostsFile='+args.known_hosts]
    if args.host_alias: command+=['-o','HostKeyAlias='+args.host_alias]
    command+=[args.user+'@127.0.0.1']
    def ssh(script,input=None):
        return subprocess.run(command+[script],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
    weights=args.runtime/'weights'/json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']['weights']
    q=QMP(args.qmp); results=[]
    def capture(name,marker=None):
        time.sleep(.3)
        for _ in range(20):
            before=ssh('cat /tmp/neuronte-frame.json')
            q.execute('screendump',filename=str((args.output/(name+'.ppm')).resolve()))
            after=ssh('cat /tmp/neuronte-frame.json')
            if before==after: break
            time.sleep(.15)
        else: raise AssertionError('display did not settle')
        doc=json.loads(after); (args.output/(name+'.json')).write_bytes(after)
        text=''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'])
        if marker and marker not in text: raise AssertionError(name+' missing marker: '+marker)
        expected=reconstruct(doc,weights,args.runtime/'build/libneuronte.so',args.runtime/'weights/neuronte-video-masks-v0.nrt')
        actual=read_ppm(args.output/(name+'.ppm'))
        delta=np.abs(expected.astype(int)-actual.astype(int))
        result=dict(name=name,equal=bool(np.array_equal(expected,actual)),max_error=int(delta.max()),
                    differing_pixels=int(np.count_nonzero(np.any(delta,axis=2))),pixels=actual.shape[0]*actual.shape[1],
                    timing=doc['timing'],pointer=doc.get('pointer'),planes=len(doc.get('planes',[])))
        results.append(result)
        (args.output/'screens.json').write_text(json.dumps(results,indent=2)+'\n')
        print(json.dumps(result),flush=True)
        assert result['equal'],name
        return doc
    try:
        audit=subprocess.run(command+['sudo python3 -'],input=Path(__file__).with_name('guest_audit.py').read_bytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        (args.output/'strict-audit.json').write_bytes(audit.stdout)
        if args.strict: assert audit.returncode==0,audit.stdout.decode()+audit.stderr.decode()
        ssh('cat > /tmp/neuronte-fixture.py',FIXTURE.encode())
        ssh('cat > /tmp/neuronte-mouse.py',MOUSE.encode())
        capture('shell')
        q.text('printf QMP_INPUT_OK\n'); capture('keyboard','QMP_INPUT_OK')
        q.text('python3 /tmp/neuronte-fixture.py\n'); doc=capture('styles-graphics','IMAGE_OK')
        assert doc['timing']['styled_cells']>0 and doc['timing']['unsupported_grapheme_cells']>0
        assert len(doc['planes'])==1
        q.keys('ctrl','a'); q.keys('s'); q.text('stty size\n'); capture('split')
        q.keys('ctrl','a'); q.keys('shift','equal'); capture('split-resized')
        q.keys('ctrl','a'); q.keys('x')
        q.text('vi -u NONE -n /tmp/neuronte-fixture.py\n'); capture('vim','base64')
        q.keys('esc'); q.text(':q!\n')
        q.text('tmux -L neuronte-accept new-session\n'); q.keys('ctrl','b'); q.keys('shift','5')
        q.text('printf TMUX_OK\n'); capture('tmux-split','TMUX_OK')
        q.text('tmux resize-pane -R 3\n'); capture('tmux-resized')
        q.text('tmux kill-server\n')
        q.text('python3 /tmp/neuronte-mouse.py\n'); time.sleep(.4)
        q.execute('input-send-event',events=[
            {'type':'abs','data':{'axis':'x','value':10000}},
            {'type':'abs','data':{'axis':'y','value':12000}}])
        time.sleep(.1)
        q.execute('input-send-event',events=[{'type':'btn','data':{'down':True,'button':'left'}}])
        q.execute('input-send-event',events=[{'type':'btn','data':{'down':False,'button':'left'}}])
        capture('mouse','MOUSE_OK')
        q.text('python3 /opt/neuronte-display/cell_desktop.py\n'); capture('desktop','Character-cell desktop')
        q.keys('down'); q.keys('ret'); capture('desktop-files','[..]')
        q.keys('esc'); q.keys('q')
        # Recovery is deliberately last, after all neural-frame checks.
        journal=ssh('sudo journalctl -t neuronte-session -b --no-pager').decode()
        assert 'vt-release' not in journal,'recovery was used before the final check'
        ssh('sudo systemctl start getty@tty2.service; sudo chvt 2')
        time.sleep(.4)
        q.execute('screendump',filename=str((args.output/'recovery.ppm').resolve()))
        assert ssh('sudo fgconsole').strip()==b'2'
        ssh('sudo chvt 1'); time.sleep(.4); capture('reacquired')
        journal=ssh('sudo journalctl -t neuronte-session -b --no-pager').decode()
        assert 'vt-release' in journal and 'vt-acquire' in journal
        (args.output/'vt-journal.log').write_text(journal)
        audit=ssh("sudo sh -c 'printf \"CMDLINE\\n\"; cat /proc/cmdline; printf \"PROCESSES\\n\"; ps -eo comm; printf \"FONTS\\n\"; find / -path /proc -prune -o -path /sys -prune -o -path /dev -prune -o -path /run -prune -o -type f \\( -iname \"*.ttf*\" -o -iname \"*.otf*\" -o -iname \"*.pcf*\" -o -iname \"*.psf*\" -o -iname \"*.bdf*\" -o -iname \"*.woff*\" -o -iname \"*.pf2\" \\) -print; printf \"FC_LIST\\n\"; if command -v fc-list >/dev/null; then fc-list; fi'")
        (args.output/'guest-audit.txt').write_bytes(audit)
    finally: q.close()


if __name__=='__main__': main()
