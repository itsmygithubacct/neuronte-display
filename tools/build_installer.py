#!/usr/bin/env python3
"""RC9 Neuronte installer derivative, with an offline local display payload.

Consumes the existing Plebian-OS preseed without editing its source tree. The
default artifact leaves partitioning interactive and SSH absent. Disposable
QEMU acceptance must be explicitly selected; it checks DMI before partitioning.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def sha(path):
    with open(path,'rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def preseed(source, packages, acceptance):
    # Join preseed continuation lines before replacing the owning directives.
    text=re.sub(r'\\\n\s*',' ',source)
    lines=[]
    for line in text.splitlines():
        if line.startswith(('d-i pkgsel/include ','d-i preseed/late_command ','tasksel tasksel/first ',
                            'd-i pkgsel/upgrade ')):
            continue
        if not acceptance and line.startswith('d-i partman'):
            continue
        lines.append(line)
    lines += ['tasksel tasksel/first multiselect',
              'd-i pkgsel/include string '+' '.join(packages),
              'd-i pkgsel/upgrade select none',
              'd-i preseed/late_command string sh /cdrom/neuronte/late-install.sh']
    if acceptance:
        lines += [
            'd-i preseed/early_command string grep -qx QEMU /sys/class/dmi/id/sys_vendor || { echo "QEMU-only acceptance installer" >&2; exit 1; }',
            'd-i partman-auto/disk string /dev/vda',
            'd-i partman/early_command string test -b /dev/vda',
            'd-i grub-installer/bootdev string /dev/vda',
            'd-i grub-installer/force-efi-extra-removable boolean true']
    return '\n'.join(lines)+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-iso',type=Path,required=True)
    p.add_argument('--os-source',type=Path,required=True)
    p.add_argument('--payload',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--acceptance-vm',action='store_true')
    p.add_argument('--ssh-public-key',type=Path)
    args=p.parse_args()
    if args.output.exists(): p.error('output exists; choose a new artifact path')
    if args.acceptance_vm != bool(args.ssh_public_key): p.error('acceptance VM requires its public SSH key; keys are forbidden in ordinary installers')
    root=Path(__file__).resolve().parents[1]
    deps=json.loads((root/'provision/dependencies.json').read_text())
    packages=deps['runtime']+['sudo','ca-certificates']+(deps['acceptance'] if args.acceptance_vm else [])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temp:
        stage=Path(temp); payload=stage/'neuronte'; payload.mkdir()
        shutil.copyfile(args.payload,payload/'display.tar.gz')
        if args.ssh_public_key:
            public=args.ssh_public_key.read_text().strip()
            if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\n]*)?',public): p.error('expected one Ed25519 public key')
            (payload/'authorized_keys').write_text(public+'\n')
        (stage/'preseed.cfg').write_text(preseed((args.os_source/'preseed/preseed.cfg').read_text(),sorted(set(packages)),args.acceptance_vm))
        late='''#!/bin/sh
set -eu
mkdir -p /target/var/tmp/neuronte-install
tar -xzf /cdrom/neuronte/display.tar.gz -C /target/var/tmp/neuronte-install
in-target sh /var/tmp/neuronte-install/neuronte-display/provision/install.sh / pleb
printf 'PLEB_DISPLAY=neuronte\\n' > /target/etc/pleb/display.env
printf 'PLEB_DESKTOP=0\\n' > /target/etc/default/neuronte-display
mkdir -p /target/etc/systemd/system.conf.d /target/etc/default/grub.d /target/etc/plebian-os
printf '[Manager]\\nShowStatus=no\\n' > /target/etc/systemd/system.conf.d/neuronte-quiet.conf
printf 'GRUB_TIMEOUT=0\\nGRUB_TERMINAL=console\\nGRUB_CMDLINE_LINUX_DEFAULT="quiet console=ttyS0 loglevel=0 systemd.show_status=false vt.global_cursor_default=0"\\n' > /target/etc/default/grub.d/neuronte.cfg
# Debian installs console and GRUB fonts even with an empty desktop task.
# This policy is confined to the fresh Neuronte image. Recovery uses the
# kernel's built-in VGA font; no external font files are needed.
in-target apt-get -y purge console-setup console-setup-linux
for fonts in /target/usr/share/grub /target/boot/grub /target/etc/console-setup; do
    if [ -d "$fonts" ]; then find "$fonts" -type f \\( -name '*.pf2' -o -name '*.psf*' \\) -exec rm -f '{}' \\;; fi
done
mkdir -p /target/etc/dpkg/dpkg.cfg.d
printf 'path-exclude=/usr/share/grub/*.pf2\\npath-exclude=/usr/share/consolefonts/*\\n' > /target/etc/dpkg/dpkg.cfg.d/neuronte-no-fonts
in-target update-grub
in-target systemctl set-default graphical.target
in-target systemctl enable getty@tty2.service
cp /cdrom/neuronte/build-info.json /target/etc/plebian-os/neuronte-rc9.json
'''
        if args.acceptance_vm:
            late+='''mkdir -p /target/home/pleb/.ssh /target/etc/ssh/sshd_config.d
printf 'NEURONTE_MODE=640x480\\nNEURONTE_SNAPSHOT=/tmp/neuronte-frame.ppm\\nNEURONTE_HISTORY=/tmp/neuronte-frame-history\\n' >> /target/etc/default/neuronte-display
cp /cdrom/neuronte/authorized_keys /target/home/pleb/.ssh/authorized_keys
chmod 700 /target/home/pleb/.ssh
chmod 600 /target/home/pleb/.ssh/authorized_keys
in-target chown -R pleb:pleb /home/pleb/.ssh
printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPermitRootLogin no\\n' > /target/etc/ssh/sshd_config.d/neuronte-acceptance.conf
printf 'pleb ALL=(ALL) NOPASSWD: ALL\\n' > /target/etc/sudoers.d/neuronte-acceptance
chmod 440 /target/etc/sudoers.d/neuronte-acceptance
'''
        (payload/'late-install.sh').write_text(late)
        manifest=dict(release_track='RC9',display='neuronte',source_iso_sha256=sha(args.source_iso),
                      payload_sha256=sha(args.payload),preseed_sha256=sha(stage/'preseed.cfg'),
                      acceptance_vm=args.acceptance_vm,packages=sorted(set(packages)))
        (payload/'build-info.json').write_text(json.dumps(manifest,indent=2)+'\n')
        options='auto=true priority=critical locale=en_US.UTF-8 keymap=us file=/cdrom/preseed.cfg'
        (stage/'isolinux.cfg').write_text('default neuronte\nprompt 0\ntimeout '+('1' if args.acceptance_vm else '0')+'\nlabel neuronte\n  kernel /install.amd/vmlinuz\n  append initrd=/install.amd/initrd.gz '+options+' --- quiet\n')
        (stage/'grub.cfg').write_text('set default=0\nset timeout='+('0' if args.acceptance_vm else '-1')+'\nmenuentry "RC9 Neuronte install" {\n linux /install.amd/vmlinuz '+options+' --- quiet\n initrd /install.amd/initrd.gz\n}\n')
        subprocess.run(['xorriso','-indev',str(args.source_iso),'-outdev',str(args.output),
                        '-boot_image','any','replay','-map',str(stage/'preseed.cfg'),'/preseed.cfg',
                        '-map',str(payload),'/neuronte','-map',str(stage/'isolinux.cfg'),'/isolinux/isolinux.cfg',
                        '-map',str(stage/'grub.cfg'),'/boot/grub/grub.cfg','-commit'],check=True)
    manifest['iso_sha256']=sha(args.output)
    args.output.with_suffix('.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest))


if __name__=='__main__': main()
