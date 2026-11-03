# Private Git LFS backup packages

`biography.lfs_backup` stages a previously verified retained-original backup for
Git LFS. It runs no Git commands, uploads nothing, reads no credentials, and
creates no paid usage. Account quota and remote acceptance must be checked
separately before any push. A local package does not prove remote recovery.

```python
from pathlib import Path
from biography.lfs_backup import stage_lfs_backup, reassemble_lfs_backup

result = stage_lfs_backup(
    Path("/private/backups/fixed-cut"),
    Path("/private/lfs-repository"),
    max_new_lfs_bytes=10 * 1024**3,
    reserve_bytes=20 * 1024**3,
    capacity_paths=(),
)
```

The private destination contains content-addressed immutable payloads beneath
`lfs/sha256/`. Each is at most 512 MiB by default. Original CAS objects use raw
chunks, allowing reuse across snapshots. The catalog streams through gzip with
`mtime=0` and no embedded filename before chunking. Included import ledgers and
the original private backup manifest also become LFS payloads.

Small regular-Git manifests beneath `packages/` point to a compressed LFS
component index. That index maps safe original member names, hashes and sizes
to encoded chunk hashes and sizes. Private records and the original catalog
never become regular-Git JSON exports. The helper preserves existing rules and
adds `lfs/** filter=lfs diff=lfs merge=lfs -text` to `.gitattributes` and ignores
`.lfs-backup-work/`, which holds private resumable staging state.

The caller must install and configure Git LFS before staging Git changes, then
verify that actual Git blobs for `lfs/` contain LFS pointers. Merely having a
`.gitattributes` file does not prove that Git's LFS filter ran. Never use a public
repository for these artifacts, and review credentials before private Git
publication. Packaging does not remove canonical originals or change the
separate verified source-move policy.

The byte budget is enforced before payload publication. It counts unique
required LFS hashes, including the compressed index. Local files remain charged
on retries because local presence does not prove prior upload. Only hashes
explicitly supplied in `known_lfs_hashes` are considered already published and
quota-exempt. Supply them only from verified remote evidence. Returned
`new_lfs_bytes` and `total_lfs_bytes` are exact; transient staging space is a
separate concern governed by the disk reserve. `full_candidate_new_lfs_bytes`
reports the exact upload size needed to cover every component, even when the
configured budget produces a partial package.

If the budget cannot cover everything, complete individual components are
deferred and the package reports `partial`. The index records the exact deferred
components and their reasons. If even the index cannot fit, nothing is published
and the result is `budget_exceeded`. No partial package claims recoverability of
the full backup. The source backup's own evidence coverage is separate: a fully
packaged backup can still report deferred active import ledgers.

Re-running against the same source cut reuses verified immutable chunks and
cached encoding plans. Cached compressed mappings are decoded and checked
against their original hash. Existing mismatched chunks fail without overwrite.
Partial temporary chunks are retried. Published package manifests are themselves
content addressed and atomically published. Work directories may be large and
must remain private and ignored; encoded source candidates can be retained there
even when excluded by the upload budget.

```python
report = reassemble_lfs_backup(
    Path("/private/lfs-repository"),
    Path(result["package_manifest"]),
    Path("/private/new-reassembled-backup"),
    reserve_bytes=20 * 1024**3,
    capacity_paths=(),
)
```

Reassembly requires a complete package, downloaded LFS payloads and a new private
directory. It checks the small manifest's content address, index hash and count,
all chunk paths, hashes and sizes, decoded component hashes and lengths, then
calls `verify_backup` over the entire reassembled backup. There is no tar
extraction or execution of imported data. The catalog is written last, and any
failed reassembly removes that new catalog. Original files are copied into new
inodes; the engine never hardlinks canonical CAS source bytes into staging.

`remote_upload_verified` and `physical_independence_asserted` remain false. A
separate remote download and reassembly test is required to establish that the
server actually retained a recoverable copy.
