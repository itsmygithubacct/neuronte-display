#!/bin/sh
# Stage into a target root. Does not restart services or change boot defaults.
set -eu
root=${1:?usage: install.sh TARGET_ROOT USER}
user=${2:?target login user required}
case "$user" in ''|*[!a-zA-Z0-9_-]*) echo 'invalid user' >&2; exit 1;; esac
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install -d "$root/opt/neuronte-display" "$root/usr/local/bin" "$root/etc/systemd/system" "$root/etc/pleb" "$root/etc/default" "$root/etc/systemd/system-generators"
cp -a "$here/." "$root/opt/neuronte-display/"
find "$root/opt/neuronte-display" -type d -exec chmod 755 {} +
if [ "$(id -u)" = 0 ]; then chown -R 0:0 "$root/opt/neuronte-display"; fi
install -m755 "$here/provision/neuronte-session" "$root/usr/local/bin/neuronte-session"
install -m755 "$here/provision/neuronte-generator" "$root/etc/systemd/system-generators/neuronte-generator"
sed "s/^User=pleb$/User=$user/" "$here/provision/neuronte-display.service" > "$root/etc/systemd/system/neuronte-display.service"
chmod 644 "$root/etc/systemd/system/neuronte-display.service"
if [ ! -e "$root/etc/default/neuronte-display" ]; then
    printf 'PLEB_DESKTOP=0\n' > "$root/etc/default/neuronte-display"
fi
# PLEB_DISPLAY is the policy knob. Existing session.env is never overwritten.
# Fresh installer sets this file to neuronte explicitly; add-on installs leave
# X as the default and can test using a one-boot pleb.display=neuronte argument.
if [ ! -e "$root/etc/pleb/display.env" ]; then
    printf '# Set PLEB_DISPLAY=x or neuronte here to override session.env.\n# Unset inherits session.env; otherwise the default is x.\n' > "$root/etc/pleb/display.env"
fi
