# status.json

`/var/lib/cellar/status.json` is world-readable and holds no secrets;
[Hearth](https://github.com/anthonyradke/hearth) reads it. The top level is `schema` (currently `1`), `updated`, and these sections, each present once that job has
run at least once. Every write takes `status.lock` and replaces the file atomically, so readers never see a partial file.

```jsonc
{
  "schema": 1,
  "updated": "2026-09-24T10:41:02+00:00",      // UTC, ISO 8601, every timestamp
  "last_success": "…",                          // when the last nightly or manual backup saved a snapshot
  "backup": {                                   // the last nightly or manual backup
    "started": "…", "finished": "…",            // finished is null while running
    "result": "running | ok | partial | failed",
    "error": null,                              // text when partial or failed
    "tag": "nightly",
    // From here on, only once the snapshot is saved:
    "snapshot": "3f9c…",                        // full snapshot id
    "bytes_added": 0, "bytes_uploaded": 0,      // new data, before and after compression
    "bytes_total": 0,                           // everything the backup read
    "files_new": 0, "files_changed": 0, "files_total": 0,
    "missing_sources": []                       // sources.txt lines that matched nothing
  },
  "frequent": { /* same fields as backup, for the half-hourly runs */ "last_success": "…" },
  "repo":    { "snapshots": 0, "size_bytes": 0, "measured": "…" },   // size_bytes = stored size; nightly and manual runs only
  "sources": [ { "path": "/etc", "bytes": 0 } ],                      // per source, from the last nightly or manual snapshot
  "check":   { "started": "…", "finished": "…", "result": "running | ok | failed", "error": null, "subset": "46/52" },
  "drill":   { "started": "…", "finished": "…", "result": "running | ok | failed", "error": null,
               "snapshot": "3f9c1a2b",
               "full": false,                                                   // true for the local copy's whole-snapshot drill
               "files": [ { "path": "…", "bytes": 0, "sha256": "…", "integrity": "ok" } ] },  // integrity only for SQLite files
  "local": {                                    // only when a local copy is configured (docs/backup-host.md)
    "updated": "…",
    "last_success": "…",                        // the local copy's last nightly or manual snapshot
    "backup":   { /* same fields as the top-level backup */ },
    "frequent": { /* same fields as the top-level frequent */ },
    "repo":     { "snapshots": 0, "size_bytes": 0, "measured": "…" },
    "check":    { /* as above; subset is "all" */ },
    "drill":    { /* as above, with "full": true and "restored": { "files": 0, "bytes": 0 } */ }
  }
}
```

The top-level sections always describe the off-site repository, so readers written before the local copy existed
see exactly what they saw before. `local` has the same section names with the same fields, one level down, and has
no `sources` (sizes per source are measured off-site only). A reader that wants to show the local copy reads
`local.backup`, `local.frequent`, `local.check`, `local.drill`, `local.repo` and `local.last_success` with the same
rules it uses for the top level; the local check is expected weekly and the drill monthly, like the off-site ones.

`partial` means a snapshot was saved but something needs a look: files that couldn't be read, a Minecraft server
that couldn't be paused or resumed, retention and stats failing after the backup, or (local copy only) a backup
host whose prune has stopped. `error` says which.

A run killed before it could write its result (OOM, SIGKILL, unit timeout) is set to `failed` by the unit's
`ExecStopPost` (`cellar stopped <job>`), so `running` only lasts while a job really runs. `cellar status` also
shows a leftover `running` as `interrupted` when no job holds the lock.

Adding fields is fine. Renaming or removing one means bumping `schema` and updating Hearth.
