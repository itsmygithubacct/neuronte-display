#!/usr/bin/env python3
"""Build a self-contained display payload from selected, already-built inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display_pointer import validate_manifest


def locked_emoji(home,entry):
    """Read the approved pair from one immutable revision, including its inventory."""
    if not re.fullmatch(r'[0-9a-f]{40}',entry['revision']):
        raise ValueError('emoji revision must be a full immutable Git commit')
    result={}
    for kind in ('weights','manifest'):
        name=entry[kind]
        if Path(name).name!=name:raise ValueError('emoji package name must be a basename')
        data=subprocess.check_output(['git','-C',str(home),'show',entry['revision']+':'+entry[kind+'_source']])
        if hashlib.sha256(data).hexdigest()!=entry[kind+'_sha256']:
            raise ValueError('emoji '+kind+' differs from lock')
        result['weights/'+name]=data
    manifest=json.loads(result['weights/'+entry['manifest']])
    scalars=[int(row['scalar'][2:],16) for row in manifest['glyphs']]
    if (manifest.get('version')!=1 or manifest.get('cell_columns')!=2 or len(scalars)!=300
        or len(set(scalars))!=300 or any(cp>0xeffff or cp<0 or 0xd800<=cp<=0xdfff for cp in scalars)):
        raise ValueError('approved emoji manifest must contain exactly300 unique two-cell scalars')
    return result


def locked_icons(home,entry,manifest_path):
    """Derive availability from accepted labels, never from a routing result."""
    if not re.fullmatch(r'[0-9a-f]{40}',entry['revision']):
        raise ValueError('icon revision must be a full immutable Git commit')
    blobs = {}
    for kind in ('weights','metadata'):
        data = subprocess.check_output(['git','-C',str(home),'show',entry['revision']+':'+entry[kind+'_source']])
        if hashlib.sha256(data).hexdigest()!=entry[kind+'_sha256']:
            raise ValueError('icon '+kind+' differs from lock')
        blobs[kind] = data
    raw = Path(manifest_path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=entry['manifest_sha256']:
        raise ValueError('icon manifest differs from lock')
    manifest = json.loads(raw)
    labels = json.loads(blobs['metadata'])['training']['labelled'].split()
    listed = [row['scalar'] for row in manifest['glyphs']]
    if (manifest.get('version')!=1 or manifest.get('cell_columns')!=1 or
        len(labels)!=167 or len(set(labels))!=167 or listed!=labels or
        manifest.get('weights_sha256')!=entry['weights_sha256'] or
        manifest.get('source')!=dict(revision=entry['revision'],metadata=entry['metadata_source'],
                                     metadata_sha256=entry['metadata_sha256']) or
        manifest['pointer']['scalar'] not in labels):
        raise ValueError('icon inventory must match all167 accepted labels and source identity')
    for name in (entry['weights'],entry['manifest']):
        if Path(name).name!=name:raise ValueError('icon package name must be a basename')
    return {'weights/'+entry['weights']:blobs['weights'],'weights/'+entry['manifest']:raw}


def selected_pointer(weights, manifest):
    """Only an explicitly supplied, hash-validated pair enters the payload."""
    data, raw = Path(weights).read_bytes(), Path(manifest).read_bytes()
    doc = validate_manifest(data, raw)
    # Stable names cannot overwrite another entry in the existing closure.
    if doc['model'] != 'weights/neuronte-pointer-v0.nrt':
        raise ValueError('pointer package model path must be weights/neuronte-pointer-v0.nrt')
    return {doc['model']: data, 'weights/neuronte-pointer-v0.manifest.json': raw}


def write_archive(staging, output):
    """Payload traversal/read permissions must not inherit the builder's umask."""
    def permissions(member):
        if member.isdir():
            member.mode = 0o755
        elif member.isfile():
            member.mode = 0o755 if member.mode & 0o111 else 0o644
        return member
    with tarfile.open(output, 'w:gz') as archive:
        archive.add(staging, arcname='neuronte-display', filter=permissions)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--neuronte-home',type=Path,required=True)
    parser.add_argument('--video-home',type=Path,required=True)
    parser.add_argument('--ghostty',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--emoji-home',type=Path,help='repository containing the immutable accepted emoji pair in deps.lock.json')
    parser.add_argument('--icon-home',type=Path,help='repository containing the pinned accepted icon weights and labelled metadata')
    parser.add_argument('--weights',type=Path,help='approved text bundle matching deps.lock.json')
    parser.add_argument('--pointer-weights',type=Path,help='optional dedicated pointer model; requires its manifest')
    parser.add_argument('--pointer-manifest',type=Path,help='hash-bound pointer manifest to include alongside the model')
    args=parser.parse_args()
    if args.output.exists(): parser.error('output exists; choose a new artifact path')
    if bool(args.pointer_weights)!=bool(args.pointer_manifest):
        parser.error('--pointer-weights and --pointer-manifest must be supplied together')
    root=Path(__file__).resolve().parents[1]
    closure=json.loads((root/'deps.lock.json').read_text())
    emoji=closure['neuronte'].get('emoji')
    if bool(emoji)!=bool(args.emoji_home):
        parser.error('an accepted emoji lock and --emoji-home must be supplied together')
    icons=closure['neuronte'].get('icons')
    if bool(icons)!=bool(args.icon_home):
        parser.error('an accepted icon lock and --icon-home must be supplied together')
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
    if emoji:video.update(locked_emoji(args.emoji_home,emoji))
    if icons:video.update(locked_icons(args.icon_home,icons,root/'icons.json'))
    if args.pointer_weights:video.update(selected_pointer(args.pointer_weights,args.pointer_manifest))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
        staging=Path(directory)/'neuronte-display'
        files={name:root/name for name in ('neuronte_display.py','display_io.py','display_graphics.py','display_emoji.py','display_icons.py','display_pointer.py','display_art.py','display_colour.py','COLOUR.md','display_graphemes.py','graphemes.json','GRAPHEMES.md','cell_desktop.py','LICENSE','README.md','deps.lock.json','Makefile','native.c','kms.c','tools/build_parser.py','tools/package.py','build/libneuronte-display.so','build/libneuronte-kms.so')}
        files.update({str(p.relative_to(root)):p for p in (root/'provision').iterdir() if p.is_file()})
        files.update({str(p.relative_to(root)):p for p in (root/'vendor/ghostty').rglob('*') if p.is_file()})
        files.update({'build/libghostty-vt.so.0':args.ghostty/'lib/libghostty-vt.so.0',
                      'licenses/ghostty.txt':args.ghostty/'LICENSE',
                      'provenance/ghostty-revision':args.ghostty/'revision',
                      'provenance/ghostty-patches.sha256':args.ghostty/'patches.sha256',
                      'build/libneuronte.so':args.neuronte_home/'build/libneuronte.so',
                      'licenses/neuronte.txt':args.neuronte_home/'LICENSE'})
        manifest={}
        for name in sorted(set(files)|set(video)):
            target=staging/name
            target.parent.mkdir(parents=True,exist_ok=True)
            if name in video: target.write_bytes(video[name])
            else: shutil.copyfile(files[name],target)
            target.chmod(0o755 if name.endswith('.so') or '.so.' in name or name.startswith('provision/') and not name.endswith(('.json','.service')) else 0o644)
            manifest[name]=hashlib.sha256(target.read_bytes()).hexdigest()
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        write_archive(staging, args.output)
    print(json.dumps({'output':str(args.output),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'files':len(manifest)}))


if __name__=='__main__': main()
