import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest

from tools.package import locked_emoji,locked_icons,write_archive


class PayloadPermissionTests(unittest.TestCase):
    def test_archive_and_extraction_modes_do_not_inherit_private_umask(self):
        expected = {
            'neuronte-display': 0o755,
            'neuronte-display/weights': 0o755,
            'neuronte-display/weights/pointer.nrt': 0o644,
            'neuronte-display/vendor': 0o755,
            'neuronte-display/vendor/parser': 0o755,
            'neuronte-display/vendor/parser/library.so': 0o755,
            'neuronte-display/manifest.json': 0o644,
        }
        for mask in (0o077, 0o027, 0o022):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as temp:
                previous = os.umask(mask)
                try:
                    root = Path(temp)
                    staging = root/'staging'
                    (staging/'weights').mkdir(parents=True)
                    (staging/'vendor/parser').mkdir(parents=True)
                    model = staging/'weights/pointer.nrt'
                    model.write_bytes(b'unchanged model bytes')
                    model.chmod(0o644)  # Existing payload file policy.
                    library = staging/'vendor/parser/library.so'
                    library.write_bytes(b'executable library fixture')
                    library.chmod(0o755)
                    (staging/'manifest.json').write_text('{}\n')
                    if mask == 0o077:
                        self.assertEqual(stat.S_IMODE((staging/'weights').stat().st_mode), 0o700)
                    target = root/'payload.tar.gz'
                    write_archive(staging, target)
                    with tarfile.open(target) as archive:
                        self.assertEqual({m.name: m.mode for m in archive}, expected)
                        self.assertEqual(archive.extractfile('neuronte-display/weights/pointer.nrt').read(),
                                         b'unchanged model bytes')
                    installed = root/'installed'
                    installed.mkdir()
                    # Match privileged tar's permission restoration without
                    # root or chown. Python's data extraction filter purposely
                    # discards directory modes, so it cannot model this install.
                    subprocess.run(['tar', '-xzf', str(target), '-C', str(installed),
                                    '--same-permissions', '--no-same-owner'], check=True)
                    # Even with a restrictive umask, every payload ancestor is
                    # traversable and the model is readable by another account.
                    for path, mode in expected.items():
                        self.assertEqual(stat.S_IMODE((root/'installed'/path).stat().st_mode), mode)
                finally:
                    os.umask(previous)


class EmojiClosureTests(unittest.TestCase):
    def test_icon_inventory_is_bound_to_immutable_labelled_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            def git(*args):return subprocess.check_output(['git','-C',temp,*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init','-q');(root/'weights').mkdir()
            labels=[f'U+{0xe000+i:04X}' for i in range(167)]
            data=b'icon fixture, not trained weights';metadata=json.dumps(dict(training=dict(labelled=' '.join(labels)))).encode()
            (root/'weights/icons.nrt').write_bytes(data);(root/'weights/icons.json').write_bytes(metadata)
            git('add','weights');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            entry=dict(revision=git('rev-parse','HEAD'),weights='icons.nrt',weights_source='weights/icons.nrt',weights_sha256=hashlib.sha256(data).hexdigest(),metadata_source='weights/icons.json',metadata_sha256=hashlib.sha256(metadata).hexdigest(),manifest='inventory.json')
            manifest=dict(version=1,cell_columns=1,weights_sha256=entry['weights_sha256'],source=dict(revision=entry['revision'],metadata=entry['metadata_source'],metadata_sha256=entry['metadata_sha256']),glyphs=[dict(scalar=s) for s in labels],pointer=dict(scalar=labels[0]))
            path=root/'inventory.json'
            def save():
                path.write_text(json.dumps(manifest));entry['manifest_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            save();(root/'weights/icons.nrt').write_bytes(b'working-tree replacement')
            self.assertEqual(locked_icons(root,entry,path)['weights/icons.nrt'],data)
            manifest['pointer']['scalar']='U+F8FF';save()
            with self.assertRaisesRegex(ValueError,'all167'):locked_icons(root,entry,path)
            manifest['pointer']['scalar']=labels[0];manifest['glyphs'][1]=dict(scalar=labels[0]);save()
            with self.assertRaisesRegex(ValueError,'all167'):locked_icons(root,entry,path)

    def test_immutable_pair_and_inventory_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            def git(*args):return subprocess.check_output(['git','-C',temp,*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init','-q')
            (root/'weights').mkdir();(root/'font').mkdir()
            weights=b'package provenance fixture, not a trained NRT'
            manifest=json.dumps(dict(version=1,cell_columns=2,glyphs=[dict(scalar=f'U+{cp:04X}') for cp in range(0x1f600,0x1f600+300)])).encode()
            (root/'weights/emoji.nrt').write_bytes(weights)
            (root/'font/emoji.json').write_bytes(manifest)
            git('add','weights','font')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            entry=dict(revision=git('rev-parse','HEAD'),weights='emoji.nrt',weights_source='weights/emoji.nrt',
                       weights_sha256=hashlib.sha256(weights).hexdigest(),manifest='emoji.json',manifest_source='font/emoji.json',
                       manifest_sha256=hashlib.sha256(manifest).hexdigest())
            # A working-tree replacement cannot alter the locked package pair.
            (root/'weights/emoji.nrt').write_bytes(b'unapproved working file')
            self.assertEqual(locked_emoji(root,entry),{'weights/emoji.nrt':weights,'weights/emoji.json':manifest})
            with self.assertRaisesRegex(ValueError,'differs from lock'):
                locked_emoji(root,dict(entry,weights_sha256='0'*64))
            with self.assertRaisesRegex(ValueError,'immutable'):
                locked_emoji(root,dict(entry,revision='HEAD'))
            invalid=json.dumps(dict(version=1,cell_columns=2,glyphs=[dict(scalar='U+1F600')]*300)).encode()
            (root/'font/emoji.json').write_bytes(invalid)
            git('add','font/emoji.json');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','invalid inventory')
            with self.assertRaisesRegex(ValueError,'exactly300'):
                locked_emoji(root,dict(entry,revision=git('rev-parse','HEAD'),manifest_sha256=hashlib.sha256(invalid).hexdigest()))
