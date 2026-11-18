# Restoring from cellar

These are the steps for getting everything back onto a freshly installed server. The same commands work for pulling
back a single file on a live box. Paths below use `/home/you`; put in the real home directory.

## What you need from outside the server

- The B2 key ID and application key, plus the bucket name and its S3 endpoint.
- The restic repository password (`/etc/cellar/restic.pass` on the old box).
- If there is a local copy ([docs/backup-host.md](docs/backup-host.md)): its password (`/etc/cellar/local.pass`).
  The backup host keeps a copy of it in `/etc/cellar-server/<client>.pass`.

If those are lost along with the server, the backups can't be read.

**Restore from the local copy when it's there.** It runs at LAN speed and has no download cap. The bucket is for
when the backup host is gone too. Check the daily download cap in B2 (Caps & Alerts) before restoring from it: a
full restore is several GB, and once the cap is hit B2 refuses every read until midnight UTC.

## 1. Get restic talking to the repository

Do this right after the OS, SSH and sudo are set up, before rebuilding anything else, because the backup holds the
unit files, config and notes that the rest of the rebuild depends on.

```bash
sudo apt update && sudo apt install restic
sudo install -d -m 700 /etc/cellar
sudo nano /etc/cellar/b2.env          # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_REPOSITORY
sudo nano /etc/cellar/restic.pass     # the password, on one line
sudo chmod 600 /etc/cellar/*

sudo -i
set -a; . /etc/cellar/b2.env; set +a
export RESTIC_PASSWORD_FILE=/etc/cellar/restic.pass
restic snapshots                      # should list the nightly snapshots
```

From the local copy instead, reset the client's REST login on the backup host (`backup-host/install.sh ...
--password-stdin`, see docs/backup-host.md) and use these in place of `b2.env` and `restic.pass`:

```bash
sudo nano /etc/cellar/local.env       # RESTIC_REPOSITORY=rest:http://<backup-host>:<port>/<client>/, REST user and password
sudo nano /etc/cellar/local.pass      # the local copy's password
sudo chmod 600 /etc/cellar/*
sudo -i
set -a; . /etc/cellar/local.env; set +a
export RESTIC_PASSWORD_FILE=/etc/cellar/local.pass
restic snapshots
```

Or skip the network: on the backup host, `restic -r <data dir>/<client> --password-file
/etc/cellar-server/<client>.pass restore latest --target <dir>`, then copy the result over with rsync.

**Don't run `install.sh` yet.** It enables the nightly timer, and backing up a half-rebuilt box just adds useless
snapshots. Do it last (step 5).

## 2. Restore everything into a scratch directory

Never restore straight over `/`. The backup has all of `/etc`, and a new install has its own `fstab`, `machine-id`,
netplan, SSH host keys and so on, which you don't want overwritten blindly.

```bash
restic restore latest --target /root/restore
ls /root/restore                      # etc/ home/ usr/ var/
```

To restore from an older snapshot, use `restic snapshots` to pick an ID and use it in place of `latest`.

## 3. Copy things back into place

Stop each service before putting its data back, and fix ownership afterwards (restic restores the original
owners by UID, which only matches if the user was recreated with the same UID).

- **Notes first:** the notes files in `/root/restore/home/you` and `projects/*/` have the full rebuild
  guide. Copy them back and follow the rebuild order from there.
- **Config from `/etc`:** copy over what each service needs, one directory at a time, for example
  `systemd/system/*.service`, `sudoers.d/`, `pihole/`, `dnsmasq.d/`, `caddy/`, `selkies/`, and the
  `systemd/*.conf.d/` drop-ins. Run `sudo systemctl daemon-reload` afterwards.
- **Scripts:** `/root/restore/usr/local/bin/*` and `/root/restore/usr/local/lib/*` go back to the same paths.
- **Crontabs:** `/root/restore/var/spool/cron/crontabs/<user>`. Load each with `crontab -u <user> <file>` rather
  than copying the file.
- **Minecraft:** copy the whole server directory back. `libraries/`, `versions/` and `logs/` weren't backed up; the
  server re-creates the first two on start. The world was saved with `save-all flush` before the snapshot, so it's
  consistent.
- **Valheim:** copy `.config/unity3d/IronGate/Valheim/` back. The game itself (`~/servers/valheim`) isn't backed up; install
  it again with steamcmd. The world has no save-off hook, so if the live `worlds_local/<world>/_main.<N>.*` set
  looks broken or the server refuses it, use the newest `<world>_backup_auto-<timestamp>/` folder next to it
  instead. Those are always complete.
- **SQLite databases:** the live files weren't backed up. Their clean copies are at
  `/root/restore/var/lib/cellar/staging/<original path>`. Stop the app, copy the copy to the original path, give it
  back to its owner (`chown you:you`), and delete any leftover `-wal`/`-shm` next to it before starting again.

## 4. What isn't in the backup

- Git repos that have a GitHub remote. Clone them again. Only their untracked notes files are backed up.
- `node_modules`, `.venv` and other build output.
- Docker images and volumes. For the remote desktop that means the `selkies-home` volume, so browser logins have to
  be done again.
- Game binaries (Valheim via steamcmd, Minecraft jars other than the running `server.jar`).
- Pi-hole's query history (`pihole-FTL.db`). Settings and blocklists are backed up.

## 5. Turn backups back on

Once the box is running normally:

```bash
git clone https://github.com/anthonyradke/cellar ~/projects/cellar
cd ~/projects/cellar && sudo ./install.sh
```

`/etc/cellar/` already exists, so `install.sh` keeps the keys and passwords, sees that the repositories exist,
enables the timers and runs its self-test. Restore `/etc/cellar/sources.txt` and the other lists from
`/root/restore/etc/cellar/` first if you want the old ones.

## Restoring one thing on a live box

Add `--local` after `cellar` to read the local copy instead of the bucket.

```bash
sudo cellar --local restic ls latest /home/you/minecraft/world | head
sudo cellar --local restore latest --target /var/tmp/r --include /home/you/minecraft/world
sudo cellar restic find level.dat     # which off-site snapshots have a file
```
