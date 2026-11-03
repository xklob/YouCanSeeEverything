# Retained-original backups

`biography.backups` backs up one fixed SQLite catalog cut and every immutable
object registered in that cut's `objects` table. The catalog itself is the
object inventory, including the expected SHA-256 and byte length. This includes
registered objects without a current file reference; it excludes unregistered
CAS files and source bytes which have not yet been retained.

A metadata snapshot made by `biography.snapshots` excludes original bytes.
Neither a metadata snapshot nor a second directory on the original physical
disk provides independent recovery of original media. The backup engine always
reports `physical_independence_asserted: false`. Physical independence requires
separate, external evidence about the destination device or storage service.

## Privacy and scope

Backups contain private records, source paths, and original source bytes. Those
original bytes may themselves contain personal secrets. Keep backup and restore
directories outside public checkouts. Never publish or upload them automatically.
The engine does not inspect credential stores, environment files, or external
source directories. The database's provenance and recorded
source status are retained, including incomplete import status and uncertainty.

Directories and files use owner-only permissions. A filesystem which cannot
enforce those permissions fails by default. A caller that has independently
verified native ACLs may pass `externally_private=True` and a structured
`privacy_evidence` dictionary. That dictionary is recorded as caller-supplied
evidence; the engine does not attest to the ACL inspection.

## Creating and resuming

```python
from pathlib import Path
from biography.backups import create_backup

manifest = create_backup(
    database=Path("/private/source-vault/catalog.sqlite3"),
    vaultroot=Path("/private/source-vault"),
    destination=Path("/private/backup-device/snapshot-001"),
    reserve_bytes=20 * 1024**3,
    snapshot_timeout=600,
    max_wal_growth_bytes=512 * 1024**2,
    capacity_paths=(),
    manifests_dir=Path("/private/source-vault/manifests"),
)
```

Use real, nonsymlink paths. Historical object layouts are supported when the
catalog path remains beneath the configured vault's `objects` directory. Hashes
and sizes, rather than path spelling, establish object identity. Catalog object
paths escaping that directory, symlinks, and invalid hashes are rejected.

The SQLite backup API includes committed WAL records and excludes uncommitted
transactions. It copies in bounded page steps without pinning a long read
transaction, imposing a deadline and a maximum observed WAL growth relative to
the starting WAL size. Frequent writes may restart the copy. A timed-out cut
pauses safely; the next invocation starts that incomplete cut again. The engine
does not checkpoint the live source or stop its importer.

After publication, `catalog.sqlite3` is a fixed cut. Its SHA-256, size, timestamp,
source/import status and record counts appear in `manifest.json`. On resume,
the cut is rehashed, checked, and reused even when the source has newer records.
A missing or mismatched published cut fails rather than silently taking a newer
cut. Use a fresh destination to capture the next point in time.

When `manifests_dir` is supplied, the engine also inventories import ledgers.
These contain retained archive headers, link targets and reconciliation facts
which the catalog does not reproduce. Only stable files with a complete footer
and `status: complete` are included. Their object records must match hashes and
sizes in this exact fixed catalog cut. Each included ledger is copied byte for
byte, independently hashed and checked for the expected record count. Parsing
streams even large records without loading a whole ledger into memory.

Incomplete, changing, malformed, or inventory-ahead ledgers are listed as
deferred. The manifest counts catalog objects which lack a complete included
ledger. If anything is deferred or uncovered, the backup reports `partial`,
while separately reporting that its catalog and registered CAS bytes are
complete. `retained_evidence_complete` remains false. The frozen ledger plan
does not advance on resume; take a fresh cut after the source finishes to obtain
complete evidence coverage. Omitting `manifests_dir` provides a complete CAS-only
backup, with `retained_evidence_complete: false` explicitly recorded.

Each object streams into a private partial file, is fsynced, independently
reopened and hashed, then published without overwrite. Existing objects are
always rehashed, including on resume. A mismatched existing object fails and is
never overwritten. Interrupted partial objects are discarded and recopied; they
are never trusted as complete. Atomic no-overwrite publication requires a
filesystem that supports hard links within the destination. A nonblocking
writer lock prevents concurrent writers to the same backup.

`status.json` is an atomically updated progress record with the cut watermark,
stage, verified bytes/objects and pause or failure reason. It is a progress hint,
not an alternative to full verification. Updates are throttled, and an abrupt
power loss can leave the displayed progress behind durable completed objects.

Free-space reserve checks apply to the destination and each supplied
`capacity_paths` entry separately, never by adding their free space. Configure
the physical host volume as an additional bound when backing up an expanding
virtual disk. Generic application code cannot assume which host disk backs a
VM. The default additional capacity paths are empty. Snapshot page allocation
and every object chunk are guarded; insufficient space pauses without touching
source exports. The same reserve rules apply independently to restore.

## Verification and restoration

```python
from biography.backups import verify_backup, restore_backup

verification = verify_backup(Path("/private/backup-device/snapshot-001"))
restoration = restore_backup(
    Path("/private/backup-device/snapshot-001"),
    Path("/private/new-restored-vault"),
    reserve_bytes=20 * 1024**3,
    capacity_paths=(),
)
```

`verify_backup` checks the catalog hash, SQLite integrity, foreign keys, manifest
watermark, all object hashes and lengths, inventory totals, and included ledger
hashes, record counts and references. It needs no
access to the original source vault.

`restore_backup` requires a new directory. It checks the fixed catalog, copies
and independently reads back every original object, streams one catalog copy,
and remaps all `objects.path` values into the new CAS. SQLite integrity, foreign
keys, provenance counts and all remapped paths are checked before the restored
catalog is published. Source statuses, original file paths, evidence links,
locators, and the search index remain part of the catalog. The restored database
has a different hash because its object paths changed; both catalog hashes are
recorded in `restore-report.json`.

Included ledgers are restored into `manifests/` without changing their original
bytes or source path strings. Existing manifest replay uses hashes to locate
objects in the new CAS. Partial backups can be verified and restored, and both
reports preserve the explicit partial status and deferred evidence list.

The restored catalog is published last. A failed restore leaves only its new
private directory and no usable catalog; retry into a different new directory.
No existing destination is overwritten or deleted. Restoration uses roughly
one catalog plus the registered original bytes, with small temporary rollback
journals during bounded path-remapping batches. It does not make a second full
temporary catalog. Retain the backup, report and destination evidence together.

The engine deliberately has no cloud upload, source cleanup, Git publication,
credential provisioning, or physical-device discovery behavior. A successful
restoration proves this cut is recoverable; it does not claim that later imports
or external, unretained exports are included.
