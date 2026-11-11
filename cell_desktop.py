#!/usr/bin/env python3
"""Small terminal-native desktop. All visible shapes are terminal cells."""
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import termios
import tty


def clean(value):
    return ''.join(c if c.isprintable() else '?' for c in str(value))


def main():
    saved=termios.tcgetattr(0)
    selected=0
    mode='home'
    directory=Path.home()
    dirty=True
    def resize(*_):
        nonlocal dirty
        dirty=True
    signal.signal(signal.SIGWINCH,resize)
    try:
        tty.setraw(0)
        sys.stdout.write('\x1b[?1049h\x1b[?25l')
        while True:
            cols,rows=shutil.get_terminal_size((80,24))
            if dirty:
                items=['Terminal','Files','System status','Exit desktop']
                if mode=='files':
                    try: entries=[directory.parent]+sorted(directory.iterdir(),key=lambda p:(not p.is_dir(),p.name))[:max(1,rows-8)]
                    except OSError: entries=[directory.parent]
                    items=['[..]']+[('['+p.name+']' if p.is_dir() else p.name) for p in entries[1:]]
                selected=min(selected,len(items)-1)
                lines=['NEURONTE  |  Character-cell desktop','',
                       'Arrows: select   Enter: open   Escape: home   q: quit','']
                if mode=='files': lines.append(clean(directory))
                if mode=='status':
                    lines += ['Load: '+clean(Path('/proc/loadavg').read_text().strip()),
                              'Memory: '+clean(Path('/proc/meminfo').read_text().splitlines()[0]),
                              'Display: neural masks / direct KMS or Kitty', '']
                for i,item in enumerate(items):
                    lines.append(('> ' if i==selected else '  ')+clean(item))
                sys.stdout.write('\x1b[0m\x1b[2J\x1b[H'+ '\r\n'.join(line[:max(1,cols-1)] for line in lines[:rows-1]))
                sys.stdout.flush()
                dirty=False
            if not select.select([0],[],[],0.2)[0]: continue
            key=os.read(0,64)
            if key in (b'q',b'\x03'): break
            if key==b'\x1b': mode='home'; selected=0
            elif key in (b'\x1b[A',b'k'): selected=max(0,selected-1)
            elif key in (b'\x1b[B',b'j'): selected=min(len(items)-1,selected+1)
            elif key in (b'\r',b'\n'):
                if mode=='files':
                    if entries[selected].is_dir(): directory=entries[selected]; selected=0
                    else:
                        termios.tcsetattr(0,termios.TCSANOW,saved)
                        subprocess.run(['more',str(entries[selected])])
                        tty.setraw(0)
                elif selected==0:
                    sys.stdout.write('\x1b[?25h\x1b[2J\x1b[H'); sys.stdout.flush()
                    termios.tcsetattr(0,termios.TCSANOW,saved)
                    subprocess.run([os.environ.get('SHELL','/bin/bash')])
                    tty.setraw(0)
                    sys.stdout.write('\x1b[?25l')
                elif selected==1: mode='files'; selected=0
                elif selected==2: mode='status'
                else: break
            dirty=True
    finally:
        sys.stdout.write('\x1b[0m\x1b[?25h\x1b[?1049l'); sys.stdout.flush()
        termios.tcsetattr(0,termios.TCSANOW,saved)


if __name__=='__main__': main()
