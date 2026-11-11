#!/usr/bin/env python3
"""External QEMU screenshot and input helper; never opens guest display memory."""
import argparse
import json
import socket
import time


class QMP:
    def __init__(self,path):
        self.socket=socket.socket(socket.AF_UNIX)
        self.socket.settimeout(10)
        self.socket.connect(str(path))
        self.file=self.socket.makefile('rwb')
        self.greeting=json.loads(self.file.readline())
        self.execute('qmp_capabilities')

    def execute(self,command,**arguments):
        self.file.write(json.dumps(dict(execute=command,arguments=arguments)).encode()+b'\n')
        self.file.flush()
        while True:
            item=json.loads(self.file.readline())
            if 'error' in item: raise RuntimeError(item['error'])
            if 'return' in item: return item['return']

    def keys(self,*keys):
        self.execute('send-key',keys=[dict(type='qcode',data=key) for key in keys],**{'hold-time':25})
        time.sleep(.07)

    def text(self,text):
        mapping={' ':'spc','\n':'ret','-':'minus','=':'equal','/':'slash','.':'dot',',':'comma',
                 "'":'apostrophe',';':'semicolon','[':'bracket_left',']':'bracket_right','\\':'backslash','`':'grave'}
        shifted=dict(zip('!@#$%^&*()_+?:"{}|~<>','1234567890-= /;\'[]\\`,.'.replace(' ','')))
        for char in text:
            if char.isascii() and char.isalnum(): self.keys(*(['shift'] if char.isupper() else []),char.lower())
            elif char in mapping: self.keys(mapping[char])
            elif char in shifted:
                base=shifted[char]; self.keys('shift',mapping.get(base,base))
            else: raise ValueError('unsupported keyboard character '+repr(char))

    def close(self):
        self.file.close(); self.socket.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--socket',required=True); p.add_argument('--screenshot'); p.add_argument('--text'); p.add_argument('--keys',nargs='+')
    args=p.parse_args(); q=QMP(args.socket)
    try:
        if args.text: q.text(args.text)
        if args.keys: q.keys(*args.keys)
        if args.screenshot: q.execute('screendump',filename=args.screenshot)
    finally: q.close()
