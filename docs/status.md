# status.json

`/var/lib/cellar/status.json` is world-readable and holds no secrets;
[Hearth](https://github.com/anthonyradke/hearth) reads it. The top level is `schema` (currently `1`), `updated`, and these sections, each present once that job has
run at least once:

```jsonc
{
  "schema": 1,
  "updated": "2026-09-24T10:41:02+00:00",      // UTC, ISO 8601, every timestamp
  "last_success": "…",                          // last backup that ended ok or partial
  "backup": {
    "started": "…", "finished": "…",            // finished is null while running
    "result": "running | ok | partial | failed",
    "error": null,                              // text when partial or failed
    "tag": "nightly",
    "snapshot": "3f9c…",                        // full snapshot id
    "bytes_added": 0, "bytes_uploaded": 0,      // new data, before and after compression
    "bytes_total": 0,                           // everything the backup read
    "files_new": 0, "files_changed": 0, "files_total": 0,
    "missing_sources": []                       // sources.txt lines that matched nothing
  },
  "repo":    { "snapshots": 0, "size_bytes": 0, "measured": "…" },   // size_bytes = stored size in the bucket
  "sources": [ { "path": "/etc", "bytes": 0 } ],                      // per source, from the latest snapshot
  "check":   { "started": "…", "finished": "…", "result": "running | ok | failed", "error": null, "subset": "10%" },
  "drill":   { "started": "…", "finished": "…", "result": "running | ok | failed", "error": null,
               "snapshot": "3f9c1a2b",
               "files": [ { "path": "…", "bytes": 0, "sha256": "…", "integrity": "ok" } ] }  // integrity only for .db/.sqlite
}
```

Adding fields is fine. Renaming or removing one means bumping `schema` and updating Hearth.
