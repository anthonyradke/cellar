#!/usr/bin/env bash
# Installs or updates cellar on this machine. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" != 0 ]; then
    echo "run with sudo: sudo ./install.sh" >&2
    exit 1
fi

command -v restic >/dev/null || apt-get install -y restic
install -m 755 bin/cellar /usr/local/bin/cellar
install -m 644 systemd/cellar-*.service systemd/cellar-*.timer /etc/systemd/system/
install -d -m 700 /etc/cellar /var/cache/cellar
install -d -m 755 /var/lib/cellar
install -d -m 700 /var/lib/cellar/staging

# Starter config from the examples, never overwriting real files. b2.env is written by hand.
for example in etc/*.example; do
    name=$(basename "$example" .example)
    [ "$name" = b2.env ] && continue
    if [ ! -e "/etc/cellar/$name" ]; then
        install -m 600 "$example" "/etc/cellar/$name"
        echo "created /etc/cellar/$name from the example; edit it for this machine"
    fi
done

if [ ! -s /etc/cellar/b2.env ]; then
    echo
    echo "/etc/cellar/b2.env is missing. Create it (see etc/b2.env.example):"
    echo "  sudo nano /etc/cellar/b2.env && sudo chmod 600 /etc/cellar/b2.env"
    echo "then run this again."
    exit 1
fi
chmod 600 /etc/cellar/b2.env

if [ ! -s /etc/cellar/restic.pass ]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/cellar/restic.pass)
    echo
    echo "Generated the repository password in /etc/cellar/restic.pass."
    if [ -t 1 ]; then
        echo "Save it in your password manager now, with the B2 key and bucket name:"
        echo
        echo "  $(cat /etc/cellar/restic.pass)"
    else
        echo "Show it in a terminal and save it in your password manager: sudo cat /etc/cellar/restic.pass"
    fi
    echo "Without it the backups can't be read."
fi

cellar init

systemctl daemon-reload
systemctl enable --now cellar-backup.timer cellar-check.timer cellar-drill.timer

echo
echo "Self-test:"
fail=0
check() {
    if "$@" >/dev/null 2>&1; then echo "  ok    $desc"; else echo "  FAIL  $desc"; fail=1; fi
}
desc="repository reachable"; check cellar restic cat config
desc="all sources exist";    check cellar sources
for t in backup check drill; do
    desc="cellar-$t.timer active"; check systemctl is-active --quiet "cellar-$t.timer"
done
[ "$fail" = 0 ] || { cellar sources || true; exit 1; }
echo
systemctl list-timers 'cellar-*' --no-pager
