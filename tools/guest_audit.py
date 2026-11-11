#!/usr/bin/env python3
"""Run inside the owned guest as root; print the strict installed-system gates."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def audit():
    fonts=[]
    errors=[]
    def error(exc): errors.append(str(exc))
    for folder,dirs,files in os.walk('/',onerror=error):
        if folder=='/': dirs[:]=[d for d in dirs if d not in ('proc','sys','dev','run')]
        for name in files:
            if re.search(r'\.(?:ttf|otf|pcf|psf|bdf|woff|pf2)(?:\d|\.|$)',name,re.I):
                fonts.append(str(Path(folder)/name))
    names=subprocess.check_output(['ps','-eo','comm='],text=True).splitlines()
    foreign=sorted(set(names)&{'X','Xorg','Xwayland','weston','sway','kwin_wayland','gnome-shell','lightdm','LightDM','kitty','kilix','fbterm'})
    console=Path('/sys/class/tty/console/active').read_text().strip()
    fc=subprocess.check_output(['fc-list'],text=True).splitlines() if shutil.which('fc-list') else []
    root=Path('/opt/neuronte-display')
    closure=json.loads((root/'manifest.json').read_text())
    mismatches=[name for name,digest in closure.items() if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest]
    ldd=subprocess.check_output(['ldd',str(root/'build/libneuronte-display.so')],text=True)
    package_parser='libghostty-vt.so.0 => /opt/neuronte-display/build/libghostty-vt.so.0' in ldd
    result=dict(font_allowlist=[],font_files=sorted(fonts),fc_list=fc,scan_errors=errors,
                foreign_display_processes=foreign,kernel_console=console,
                closure_files=len(closure),closure_mismatches=mismatches,ldd=ldd,
                active_vt=subprocess.check_output(['fgconsole'],text=True).strip(),
                cmdline=Path('/proc/cmdline').read_text().strip(),
                installed_build=json.loads(Path('/etc/plebian-os/neuronte-rc9.json').read_text()) if Path('/etc/plebian-os/neuronte-rc9.json').exists() else None)
    result['gates']=dict(no_fonts=not fonts and not fc and not errors,
                         no_other_display=not foreign and console=='ttyS0',
                         package_closure=not mismatches and package_parser)
    return result


if __name__=='__main__':
    result=audit(); print(json.dumps(result,indent=2))
    raise SystemExit(0 if all(result['gates'].values()) else 1)
