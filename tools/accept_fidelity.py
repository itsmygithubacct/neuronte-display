#!/usr/bin/env python3
"""Installed cursor/icon/colour-style QMP proof; never uses the recovery VT."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import time
import numpy as np
from qmp import QMP
from reference import reconstruct,read_ppm

FIXTURE=r'''import base64,json,os,select,sys,termios,time,tty
from pathlib import Path
cfg=json.loads(Path('/tmp/neuronte-fidelity-config.json').read_text())
ESC='\x1b'
def image(raw,w,h,z):
 return ESC+f'_Ga=T,f=24,s={w},v={h},i=84100,p=1,C=1,q=2,z={z};'+base64.b64encode(raw).decode()+ESC+'\\'
def draw(index):
 name=cfg['steps'][index];light=name.startswith('light');fg=0x102030 if light else 0xeff3fa;bg=0xf0f0f0 if light else 0x101820
 rgb=lambda c:f'{c>>16&255};{c>>8&255};{c&255}'
 colour=ESC+f'[38;2;{rgb(fg)};48;2;{rgb(bg)}m'
 out=ESC+'[?2027h'+ESC+'[?25l'+ESC+'[0m'+colour+ESC+'[2J'+ESC+'[H'+ESC+'_Ga=d,d=A;'+ESC+'\\'
 out+=cfg['nonce']+'_'+str(index)+' '+name+'\r\n'
 if name.endswith('inventory'):
  for first in range(0,len(cfg['icons']),20):out+=''.join(chr(c)+'  ' for c in cfg['icons'][first:first+20])+'\r\n'
 elif name.endswith(('styles','negative')):
  styles=['0','1','3','1;3','4','9','53','4;9','2','7','2;7','1;3;4;9;2','1;3;4;9;2;8']
  for sgr in styles:
   out+=ESC+'[0m'+colour+sgr.ljust(13)+ESC+'['+sgr+'m'+colour
   out+=''.join(chr(cp)+'\ufe0f ' for cp in (0x1f600,0x1f680,0x26a0))+' '+''.join(chr(cp)+' ' for cp in (0xf030c,0xf071,0xf036d))+'\x1b[0m'+colour+'\r\n'
  if name.endswith('negative'):
   w,h=32*cfg['cw'],len(styles)*cfg['ch']
   raw=bytes(c for y in range(h) for x in range(w) for c in ((17*x+3*y)%256,(7*x+23*y)%256,220))
   out+=ESC+'[2;14H'+image(raw,w,h,-1)
 elif name.startswith('cursor_'):
  out+=ESC+'[5;5H'+colour+'😀\ufe0fZ'+ESC+'[7;5HAB'
  shape=2 if 'block' in name else 4 if 'underline' in name else 6
  if name not in ('cursor_hidden','cursor_hide_restored'):
   row,col=(7,6) if name=='cursor_move_plain' else (5,6 if 'right' in name else 5)
   out+=ESC+f'[{shape} q'+ESC+f'[{row};{col}H'+ESC+'[?25h'
 elif name=='pointer_grid':
  w,h=160,96;raw=bytes(c for y in range(h) for x in range(w) for c in [255 if (x//40+y//48)%2 else 0]*3)
  out+=ESC+'[5;5H'+image(raw,w,h,0)+ESC+'[?1000h'+ESC+'[?1006h'
 sys.stdout.write(out);sys.stdout.flush()
saved=termios.tcgetattr(0);last=None;data=b''
try:
 tty.setraw(0)
 deadline=time.monotonic()+1200
 while time.monotonic()<deadline:
  try:index=int(Path('/tmp/neuronte-fidelity-step').read_text())
  except (FileNotFoundError,ValueError):index=-2
  if index==-1:break
  if index>=0 and index!=last:draw(index);last=index
  if select.select([0],[],[],.02)[0]:
   data+=os.read(0,1024)
   if data.endswith(b'm'):
    sys.stdout.write(ESC+'[24;1H'+ESC+'[0m'+ESC+'[2KCLICK_REPORT '+repr(data)+ESC+'[?25l');sys.stdout.flush();data=b''
finally:
 os.write(1,b'\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?25h\x1b_Ga=d,d=A;\x1b\\\r\nFIDELITY_DONE\r\n')
 termios.tcsetattr(0,termios.TCSANOW,saved)
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qmp','port','key','known-hosts'):p.add_argument('--'+name,required=True)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    command=['ssh','-i',args.key,'-p',args.port,'-o','BatchMode=yes','-o','UserKnownHostsFile='+args.known_hosts,'pleb@127.0.0.1']
    def ssh(script,data=None):return subprocess.check_output(command+[script],input=data)
    lock=json.loads((args.runtime/'deps.lock.json').read_text())['neuronte']
    assert json.loads(ssh('cat /opt/neuronte-display/deps.lock.json'))['neuronte']==lock
    assets={}
    for key in ('emoji','icons'):
        entry=lock[key]
        for kind in ('weights','manifest'):
            relative='weights/'+entry[kind]
            digest=ssh('sha256sum -- '+shlex.quote('/opt/neuronte-display/'+relative)).decode().split()[0]
            assert digest==entry[kind+'_sha256']==hashlib.sha256((args.runtime/relative).read_bytes()).hexdigest()
            assets[key+'_'+kind]=dict(path='/opt/neuronte-display/'+relative,sha256=digest)
    (args.output/'installed-assets.json').write_text(json.dumps(assets,indent=2)+'\n')
    original=ssh('cat /etc/default/neuronte-display');q=QMP(args.qmp);results=[];captured={}
    steps=[theme+'_'+case for case in ('inventory','styles','negative') for theme in ('dark','light')]
    steps+=['cursor_hidden','cursor_block_left','cursor_block_right','cursor_underline_left','cursor_underline_right','cursor_bar_left','cursor_bar_right','cursor_move_plain','cursor_hide_restored','pointer_grid']
    nonce='FIDELITY_'+str(time.time_ns())
    def capture(name,marker,position=None):
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            raw=ssh('cat /tmp/neuronte-frame.json');doc=json.loads(raw)
            text=''.join(chr(c[0]) if isinstance(c[0],int) else ''.join(map(chr,c[0])) for c in doc['records'])
            if marker not in text or position is not None and doc.get('pointer')!=list(position):time.sleep(.08);continue
            q.execute('screendump',filename=str((args.output/(name+'.ppm')).resolve()))
            if raw==ssh('cat /tmp/neuronte-frame.json'):break
            time.sleep(.08)
        else:raise AssertionError('no stable fixture '+name)
        (args.output/(name+'.json')).write_bytes(raw)
        expected=reconstruct(doc,args.runtime/'weights'/lock['weights'],args.runtime/'build/libneuronte.so',
            args.runtime/'weights'/lock['video_weights'],args.runtime/'weights'/lock['emoji']['weights'],
            args.runtime/'weights'/lock['emoji']['manifest'],args.runtime/'weights'/lock['icons']['weights'],
            args.runtime/'weights'/lock['icons']['manifest'])
        actual=read_ppm(args.output/(name+'.ppm'));delta=np.abs(expected.astype(int)-actual.astype(int))
        row=dict(name=name,equal=bool(np.array_equal(actual,expected)),max_error=int(delta.max()),
            pixels=actual.shape[0]*actual.shape[1],rgb_sha256=hashlib.sha256(actual.tobytes()).hexdigest(),cursor=doc.get('cursor'),pointer=doc.get('pointer'),planes=len(doc['planes']))
        results.append(row);(args.output/'screens.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(row),flush=True)
        assert row['equal'],name
        captured[name]=(doc,actual)
        return doc,actual
    try:
        addition='\nNEURONTE_THEME=auto\n'
        for key,prefix in (('emoji','EMOJI'),('icons','ICON')):
            for kind in ('weights','manifest'):addition+=f'NEURONTE_{prefix}_{kind.upper()}='+assets[key+'_'+kind]['path']+'\n'
        ssh('sudo tee /etc/default/neuronte-display >/dev/null',original+addition.encode())
        before=float(ssh("cut -d' ' -f1 /proc/uptime"));ssh('sudo systemctl restart neuronte-display')
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            time.sleep(.15);doc=json.loads(ssh('cat /tmp/neuronte-frame.json'))
            if doc.get('icons') and doc.get('emoji') and doc['timing']['presented_monotonic']>=before:break
        else:raise AssertionError('selected installed colour assets did not start')
        cfg=dict(steps=steps,nonce=nonce,cw=doc['cell_width'],ch=doc['cell_height'],icons=[int(r['scalar'][2:],16) for r in json.loads((args.runtime/'weights'/lock['icons']['manifest']).read_text())['glyphs']])
        assert len(cfg['icons'])==167 and doc['cols']>=60 and doc['rows']>=28
        ssh('cat > /tmp/neuronte-fidelity-config.json',json.dumps(cfg).encode());ssh('cat > /tmp/neuronte-fidelity.py',FIXTURE.encode())
        ssh('printf 0 > /tmp/neuronte-fidelity-step');q.text('python3 /tmp/neuronte-fidelity.py\n')
        for index,name in enumerate(steps):
            ssh('printf '+str(index)+' > /tmp/neuronte-fidelity-step')
            doc,actual=capture(name,nonce+'_'+str(index))
            if name.endswith('inventory'):
                observed=[c[0] for c in doc['records'] if isinstance(c[0],int) and c[0] in cfg['icons']]
                assert sorted(observed)==sorted(cfg['icons']),'all167 icon inventory required'
        cw,ch=cfg['cw'],cfg['ch'];ys=slice(6*ch,7*ch);left=slice(4*cw,5*cw);right=slice(5*cw,6*cw)
        baseline=captured['cursor_hidden'][1]
        for name,region,other in (('cursor_block_left',left,right),('cursor_block_right',right,left)):
            frame=captured[name][1]
            assert np.any(frame[ys,region]!=baseline[ys,region]),name+' invisible'
            np.testing.assert_array_equal(frame[ys,other],baseline[ys,other])
        for name in ('cursor_move_plain','cursor_hide_restored'):
            np.testing.assert_array_equal(captured[name][1][ys,4*cw:7*cw],baseline[ys,4*cw:7*cw])
        tablet=next(m for m in q.execute('query-mice') if m['name']=='QEMU HID Tablet')
        q.execute('human-monitor-command',**{'command-line':f"mouse_set {tablet['index']}"})
        width,height=doc['screen'];positions=[(4*cw+20,6*ch+24),(4*cw+60,6*ch+24),(0,0),(width-1,0),(0,height-1),(width-1,height-1)]
        for i,pos in enumerate(positions):
            q.execute('input-send-event',events=[dict(type='abs',data=dict(axis=axis,value=math.ceil(v*32767/(size-1)))) for axis,v,size in zip(('x','y'),pos,(width,height))])
            doc,actual=capture('pointer_'+str(i),nonce+'_'+str(len(steps)-1),pos)
            baseline_doc=dict(doc,pointer=None)
            underneath=reconstruct(baseline_doc,args.runtime/'weights'/lock['weights'],args.runtime/'build/libneuronte.so',args.runtime/'weights'/lock['video_weights'],args.runtime/'weights'/lock['emoji']['weights'],args.runtime/'weights'/lock['emoji']['manifest'],args.runtime/'weights'/lock['icons']['weights'],args.runtime/'weights'/lock['icons']['manifest'])
            assert np.any(actual!=underneath),'pointer disappeared'
            if i==0:
                q.execute('input-send-event',events=[dict(type='btn',data=dict(down=True,button='left'))])
                q.execute('input-send-event',events=[dict(type='btn',data=dict(down=False,button='left'))])
                col=pos[0]//cw+1;row=(pos[1]-2*ch)//ch+1
                marker='[<0;'+str(col)+';'+str(row)+'m'
                clickdoc,_=capture('pointer_click',marker,pos)
                (args.output/'click-alignment.json').write_text(json.dumps(dict(position=pos,hotspot=doc['icons']['pointer'],expected_release=marker,pass_=True),indent=2)+'\n')
        (args.output/'result.json').write_text(json.dumps(dict(scope='RC9',pass_=True,installed_assets=True,screens=len(results),icons=167,colour_cases=13*6*4,cursor_shapes=['block','underline','bar'],recovery_used=False),indent=2)+'\n')
    finally:
        ssh('printf -- -1 > /tmp/neuronte-fidelity-step')
        ssh('sudo tee /etc/default/neuronte-display >/dev/null',original)
        ssh('sudo systemctl restart neuronte-display');q.close()


if __name__=='__main__':main()
