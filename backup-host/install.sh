#!/usr/bin/env bash
# Sets up this machine as a local backup host for a cellar client: an append-only restic REST server
# and a daily prune. Safe to run again. See docs/backup-host.md.
#
#   sudo backup-host/install.sh --client NAME --listen ADDR:PORT --allow CLIENT_IP \
#       [--data DIR] [--max-size BYTES] [--password-stdin]
#
# --client          the client's login and repository name (<data dir>/<client>/)
# --listen          address to listen on; use one only the client can reach (a VPN address, say)
# --allow           the client's source address; everything else is refused (repeatable)
# --data            where repositories live (default /srv/backups); must be its own mount or directory
# --max-size        refuse uploads once the data directory holds this many bytes (default 400 GB)
# --password-stdin  read the client's REST login password from stdin and (re)write its htpasswd entry
set -euo pipefail
cd "$(dirname "$0")"

client="" listen="" data=/srv/backups max_size=400000000000 password_stdin=0
allow=()
while [ $# -gt 0 ]; do
    case "$1" in
        --client) client=$2; shift 2 ;;
        --listen) listen=$2; shift 2 ;;
        --allow) allow+=("$2"); shift 2 ;;
        --data) data=$2; shift 2 ;;
        --max-size) max_size=$2; shift 2 ;;
        --password-stdin) password_stdin=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
if [ "$(id -u)" != 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi
if [ -z "$client" ] || [ -z "$listen" ] || [ "${#allow[@]}" = 0 ]; then
    echo "usage: sudo $0 --client NAME --listen ADDR:PORT --allow CLIENT_IP [--data DIR] [--password-stdin]" >&2
    exit 2
fi
case "$client" in *[!a-z0-9-]*|"") echo "--client: lowercase letters, digits and dashes only" >&2; exit 2 ;; esac

missing=()
command -v restic >/dev/null || missing+=(restic)
command -v restic-rest-server >/dev/null || missing+=(restic-rest-server)
command -v mkpasswd >/dev/null || missing+=(whois)
if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update && apt-get install -y "${missing[@]}"
fi
# The package's own unit stays off; cellar-rest-server.service replaces it.
systemctl disable --now restic-rest-server.service 2>/dev/null || true
user=restic-rest-server
id "$user" >/dev/null

install -d -m 700 /etc/cellar-server
install -d -m 700 -o "$user" -g "$user" "$data"
touch /etc/cellar-server/htpasswd
chmod 600 /etc/cellar-server/htpasswd

if [ "$password_stdin" = 1 ]; then
    IFS= read -r password
    [ -n "$password" ] || { echo "empty password on stdin" >&2; exit 2; }
    hash=$(printf '%s' "$password" | mkpasswd -m bcrypt -s)
    tmp=$(mktemp /etc/cellar-server/.htpasswd-XXXXXX)
    grep -v "^$client:" /etc/cellar-server/htpasswd > "$tmp" || true
    echo "$client:$hash" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" /etc/cellar-server/htpasswd
    echo "set the REST login for $client"
fi
grep -q "^$client:" /etc/cellar-server/htpasswd || {
    echo "no REST login for $client yet; run again with --password-stdin" >&2
    exit 1
}

cat > /etc/cellar-server/server.env <<EOF
CELLAR_DATA=$data
CELLAR_LISTEN=$listen
CELLAR_ARGS=--max-size $max_size
EOF
chmod 644 /etc/cellar-server/server.env

install -m 644 cellar-rest-server.service cellar-prune@.service cellar-prune@.timer /etc/systemd/system/
install -d /etc/systemd/system/cellar-rest-server.service.d /etc/systemd/system/cellar-prune@.service.d
{
    echo "# Written by cellar's backup-host/install.sh."
    echo "[Unit]"
    echo "RequiresMountsFor=$data"
    echo "[Service]"
    echo "ReadWritePaths=$data"
    for ip in "${allow[@]}"; do echo "IPAddressAllow=$ip"; done
} > /etc/systemd/system/cellar-rest-server.service.d/site.conf
{
    echo "# Written by cellar's backup-host/install.sh."
    echo "[Unit]"
    echo "RequiresMountsFor=$data"
    echo "[Service]"
    echo "ReadWritePaths=$data"
} > /etc/systemd/system/cellar-prune@.service.d/site.conf
systemctl daemon-reload
systemctl enable cellar-rest-server.service
# Restart only when something the server reads changed, so re-running this (to enable the prune, say)
# doesn't cut off a backup that is uploading.
fingerprint=$(cat /etc/systemd/system/cellar-rest-server.service /etc/systemd/system/cellar-rest-server.service.d/site.conf \
    /etc/cellar-server/server.env /etc/cellar-server/htpasswd | sha256sum)
if [ "$fingerprint" != "$(cat /etc/cellar-server/.installed 2>/dev/null)" ] || ! systemctl is-active --quiet cellar-rest-server.service; then
    systemctl restart cellar-rest-server.service
    echo "$fingerprint" > /etc/cellar-server/.installed
fi

# The prune needs the client's repository password, copied from the client's /etc/cellar/local.pass.
if [ -s "/etc/cellar-server/$client.pass" ]; then
    chmod 600 "/etc/cellar-server/$client.pass"
    systemctl enable --now "cellar-prune@$client.timer"
    prune="cellar-prune@$client.timer enabled"
else
    prune="prune NOT enabled: copy the client's /etc/cellar/local.pass to /etc/cellar-server/$client.pass (root, 600) and run this again"
fi

sleep 2
echo
echo "Self-test:"
if systemctl is-active --quiet cellar-rest-server.service; then
    echo "  ok    cellar-rest-server running on $listen"
else
    echo "  FAIL  cellar-rest-server isn't running: journalctl -u cellar-rest-server -n 30"
    exit 1
fi
echo "  $prune"
