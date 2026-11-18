# The local copy

cellar can keep a second repository on another machine on the same network, next to the off-site one. That gives
three copies (the live files, the local copy, the bucket), and a restore that runs at LAN speed with no download
cap. A B2 bucket on the free tier allows 1 GB of downloads a day, so a full restore from it alone takes days.

The machine that stores the copy is the backup host. It runs `restic-rest-server` in append-only mode and prunes
the repository itself once a day. Everything for it is in `backup-host/`.

## Design

- **Both repositories are filled from the live files.** Each backup job stages the SQLite databases and pauses the
  Minecraft servers once, then runs `restic backup` against the off-site repository and then against the local
  one. The two snapshots hold the same staged copies and flushed worlds. Nothing is ever copied between the
  repositories, so the local copy never reads from the bucket and never touches its download cap.
- **Each repository succeeds or fails on its own.** A backup host that is off or has lost its disk fails only the
  local run (a 10 second TCP probe catches it before restic starts); the off-site backup still runs and reports
  `ok`. The reverse holds too: when the bucket refuses writes (a storage cap, say), the local copy still gets its
  snapshot. The job exits non-zero if either failed.
- **The client can't delete or rewrite the local copy.** `--append-only` lets it add data and snapshots but
  never delete or overwrite files (stale lock files excepted). A compromised client can add junk, which
  `--max-size` bounds, but every earlier snapshot survives. The server is also limited to the client's address
  (`IPAddressAllow=`), listens on one address only, and asks for a bcrypt htpasswd login; `--private-repos` keeps
  each login inside its own repository.
- **Retention runs on the backup host.** `cellar-prune@<client>.timer` applies the same policy as the off-site
  repository every day at 07:00: frequent snapshots for 24 hours, then 7 daily, 5 weekly and 12 monthly per host
  and tag set. It needs the repository password, which systemd hands it as a credential from a root-only file. The
  client notices a prune that has stopped: its nightly run turns `partial` and alerts once frequent snapshots
  older than 48 hours pile up.
- **The local copy has its own password** (`/etc/cellar/local.pass`). It shares the off-site repository's chunker
  parameters (`restic init --copy-chunker-params`, which reads only the bucket's config and key files), so either
  repository can later refill the other with `restic copy` and full deduplication.
- **Checks and drills cost nothing locally, so they do more.** The weekly check reads all of the local copy
  (`--read-data`) instead of 1/52. The monthly drill restores the whole latest snapshot with `--verify`, compares
  its file count and bytes with the snapshot's own summary, and runs `PRAGMA integrity_check` on every staged
  database as well as the `drill.txt` files.

### Why not back up locally and copy to the bucket

The other way round (one `restic backup` to the local copy, then `restic copy` local to the bucket; uploads are
free) reads the live files once instead of twice. It was not chosen because it makes the off-site copy depend on
the backup host: with that machine off, the bucket gets nothing unless a fallback path backs up to it directly,
which is the design above with extra steps. Reading the sources twice costs little, since restic re-reads only
changed files and both runs share one staging pass and one Minecraft pause.

## Setting it up

On the backup host (Debian or Ubuntu, with the data disk mounted):

```bash
git clone https://github.com/anthonyradke/cellar && cd cellar
# REST login password for the client: generate it on the client and pipe it in, so it never sits in a file twice
sudo backup-host/install.sh --client box --listen 100.64.0.2:8470 --allow 100.64.0.1 \
    --data /srv/data/cellar --password-stdin
```

`install.sh` installs `restic` and `restic-rest-server`, keeps the package's own unit disabled, writes
`/etc/cellar-server/server.env` and a drop-in with the data directory and allowed address, and starts
`cellar-rest-server.service`. The drop-in adds `RequiresMountsFor=` on the data directory, so a missing disk stops
the server instead of letting it write into the empty mount point.

On the client, write `/etc/cellar/local.env` (see `etc/local.env.example`) and run `sudo ./install.sh`. It
generates `/etc/cellar/local.pass`, creates the repository and adds "local copy reachable" to its self-test. Then
copy that password to the backup host and run its installer again to enable the prune:

```bash
sudo cat /etc/cellar/local.pass | ssh backup-host 'sudo install -m 600 /dev/stdin /etc/cellar-server/box.pass'
ssh backup-host 'cd cellar && sudo backup-host/install.sh --client box --listen 100.64.0.2:8470 --allow 100.64.0.1 --data /srv/data/cellar'
```

The first full backup can run by hand: `sudo cellar --local backup --tag manual`.

Keep `local.pass` in a password manager next to the off-site password. Both are also inside every snapshot (all
of `/etc`), so either repository opens the other once one password is known.

## Alerts

`HC_URL_LOCAL` in `cellar.env` is a healthchecks.io check for the local copy alone: its nightly backup pings start
and success, its frequent backups, checks and drills only their failures. Without it, the local copy's nightly,
check and drill failures go to `HC_URL` as `/fail` (after the off-site success ping), and frequent failures only
reach `status.json`, so a backup host that is switched off for a day sends one alert, not 48.

## Restoring from the local copy

On the client, any `cellar` command takes `--local`:

```bash
sudo cellar --local snapshots
sudo cellar --local restore latest --target /var/tmp/r --include /srv/minecraft/world
sudo cellar --local restic find level.dat
```

For a rebuilt client, put `local.env` and `local.pass` back into `/etc/cellar/` first (see RESTORE.md). The REST
login can be reset on the backup host with `install.sh --password-stdin`. The backup host can also restore on its
own, without the client or the server, by reading the repository directory directly:

```bash
sudo restic -r /srv/data/cellar/box --password-file /etc/cellar-server/box.pass snapshots
sudo restic -r /srv/data/cellar/box --password-file /etc/cellar-server/box.pass restore latest --target /srv/data/restore
```

## Checking on it

```bash
sudo cellar status                                  # the "local copy:" block
systemctl status cellar-rest-server                 # on the backup host
systemctl list-timers 'cellar-prune@*'; journalctl -u 'cellar-prune@*' -n 50
```
