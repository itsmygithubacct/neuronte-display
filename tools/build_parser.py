#!/usr/bin/env python3
"""Build the locked MIT parser and vendored graphics patches in new storage."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import shutil


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True,help='existing Git repository containing the pinned commit')
    p.add_argument('--zig',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--prefix',type=Path,required=True)
    args=p.parse_args(); root=Path(__file__).resolve().parents[1]
    lock=json.loads((root/'deps.lock.json').read_text())['ghostty-vt']
    if subprocess.check_output([str(args.zig),'version'],text=True).strip()!=lock['zig']:
        p.error('wrong Zig version')
    if args.work.exists() or args.prefix.exists(): p.error('work/prefix must be new directories')
    patches=sorted((root/'vendor/ghostty/patches').glob('*.patch'))
    digest=hashlib.sha256()
    for path in patches:
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=lock['patches'].get(path.name): p.error('patch checksum differs')
        digest.update(path.name.encode()+b'\0'+data+b'\0')
    if digest.hexdigest()!=lock['patches_sha256']: p.error('patch set differs')
    args.work.mkdir(parents=True)
    archive=args.work/'source.tar'
    with archive.open('wb') as stream:
        subprocess.run(['git','-C',str(args.source),'archive',lock['revision']],stdout=stream,check=True)
    source=args.work/'source'; source.mkdir()
    with tarfile.open(archive) as stream: stream.extractall(source,filter='data')
    for patch in patches: subprocess.run(['git','apply',str(patch.resolve())],cwd=source,check=True)
    subprocess.run([str(args.zig.resolve()),'build','-Demit-lib-vt','-Doptimize=ReleaseFast','-j2',
                    '--cache-dir',str((args.work/'zig-local').resolve()),'--global-cache-dir',str((args.work/'zig-global').resolve()),
                    '-p',str(args.prefix.resolve())],cwd=source,check=True)
    shutil.copyfile(source/'LICENSE',args.prefix/'LICENSE')
    (args.prefix/'revision').write_text(lock['revision']+'\n')
    (args.prefix/'patches.sha256').write_text(lock['patches_sha256']+'\n')
    print(json.dumps(dict(revision=lock['revision'],patches_sha256=lock['patches_sha256'],
                         library_sha256=hashlib.sha256((args.prefix/'lib/libghostty-vt.so').read_bytes()).hexdigest())))


if __name__=='__main__': main()
