#!/usr/bin/env python3
"""Build a self-contained display payload from selected, already-built inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--neuronte-home',type=Path,required=True)
    parser.add_argument('--video-home',type=Path,required=True)
    parser.add_argument('--ghostty',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--emoji-weights',type=Path)
    parser.add_argument('--emoji-manifest',type=Path)
    parser.add_argument('--weights',type=Path,help='approved text bundle matching deps.lock.json')
    args=parser.parse_args()
    if bool(args.emoji_weights)!=bool(args.emoji_manifest):
        parser.error('emoji weights and manifest must be supplied together')
    if args.output.exists(): parser.error('output exists; choose a new artifact path')
    root=Path(__file__).resolve().parents[1]
    closure=json.loads((root/'deps.lock.json').read_text())
    lock=closure['ghostty-vt']
    if (args.ghostty/'revision').read_text().strip()!=lock['revision']:
        raise ValueError('parser revision differs from the lock')
    if (args.ghostty/'patches.sha256').read_text().strip()!=lock['patches_sha256']:
        raise ValueError('parser graphics patches differ from the lock')
    # Read video inputs from the locked Git object, never a mutable working file.
    video_revision=closure['neuronte']['video_revision']
    video_paths=('weights/'+closure['neuronte']['video_weights'],
                 'tools/neuronte_video.py','train/glyphs.py','train/strokes.py','font/octants.txt')
    video={name:subprocess.check_output(['git','-C',str(args.video_home),'show',video_revision+':'+name]) for name in video_paths}
    net=closure['neuronte']
    for name,source,digest in (
        (net['weights'],args.weights or args.neuronte_home/'weights'/net['weights'],net['weights_sha256']),
        (net['baseline_weights'],args.neuronte_home/'weights'/net['baseline_weights'],net['baseline_weights_sha256'])):
        data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('text bundle differs from lock: '+name)
        video['weights/'+name]=data
    if hashlib.sha256(video['weights/'+net['video_weights']]).hexdigest()!=net['video_weights_sha256']:
        raise ValueError('video weights differ from lock')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
        staging=Path(directory)/'neuronte-display'
        files={name:root/name for name in ('neuronte_display.py','display_io.py','display_graphics.py','display_emoji.py','display_graphemes.py','graphemes.json','GRAPHEMES.md','cell_desktop.py','LICENSE','README.md','deps.lock.json','Makefile','native.c','kms.c','tools/build_parser.py','tools/package.py','build/libneuronte-display.so','build/libneuronte-kms.so')}
        files.update({str(p.relative_to(root)):p for p in (root/'provision').iterdir() if p.is_file()})
        files.update({str(p.relative_to(root)):p for p in (root/'vendor/ghostty').rglob('*') if p.is_file()})
        files.update({'build/libghostty-vt.so.0':args.ghostty/'lib/libghostty-vt.so.0',
                      'licenses/ghostty.txt':args.ghostty/'LICENSE',
                      'provenance/ghostty-revision':args.ghostty/'revision',
                      'provenance/ghostty-patches.sha256':args.ghostty/'patches.sha256',
                      'build/libneuronte.so':args.neuronte_home/'build/libneuronte.so',
                      'licenses/neuronte.txt':args.neuronte_home/'LICENSE'})
        if args.emoji_weights:
            files.update({'weights/emoji.nrt':args.emoji_weights,'weights/emoji-manifest.json':args.emoji_manifest})
        manifest={}
        for name in sorted(set(files)|set(video)):
            target=staging/name
            target.parent.mkdir(parents=True,exist_ok=True)
            if name in video: target.write_bytes(video[name])
            else: shutil.copyfile(files[name],target)
            target.chmod(0o755 if name.endswith('.so') or '.so.' in name or name.startswith('provision/') and not name.endswith(('.json','.service')) else 0o644)
            manifest[name]=hashlib.sha256(target.read_bytes()).hexdigest()
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        with tarfile.open(args.output,'w:gz') as archive:
            archive.add(staging,arcname='neuronte-display')
    print(json.dumps({'output':str(args.output),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'files':len(manifest)}))


if __name__=='__main__': main()
