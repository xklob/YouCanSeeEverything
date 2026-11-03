"""Deterministic private Git LFS staging. No Git, network, or credential access.

Original bytes only enter content-addressed files beneath lfs/. The small Git
manifest points to a compressed LFS inventory, never to uncensored regular-Git
records. Local presence does not prove an LFS object was uploaded.
"""

from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zlib

from . import backups
from .storage import CHUNK_BYTES, DEFAULT_RESERVE_BYTES, FileSnapshot, IntegrityError, hash_file, fsync_directory


FORMAT = "biography-git-lfs-backup"
VERSION = 1
MAX_CHUNK_BYTES = 512 * 1024**2
MAX_INDEX_BYTES = 512 * 1024**2


class LFSBackupError(RuntimeError):
    pass


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _chunk_path(root, digest):
    backups._digest_name(digest)
    return backups._member(root, f"lfs/sha256/{digest[:2]}/{digest}")


def _chunk_descriptor(digest, size):
    return {"sha256": digest, "bytes": size, "path": f"lfs/sha256/{digest[:2]}/{digest}"}


class _Chunks:
    def __init__(self, root, maximum, guard):
        self.root, self.maximum, self.guard = root, maximum, guard
        self.handle = self.temporary = None
        self.length = self.total = 0
        self.digest = None
        self.chunks = []

    def write(self, data):
        original = len(data)
        view = memoryview(data)
        while view:
            if self.handle is None:
                descriptor, name = tempfile.mkstemp(prefix="chunk-", suffix=".part", dir=self.root)
                self.temporary = Path(name)
                self.handle = os.fdopen(descriptor, "wb")
                self.digest = hashlib.sha256()
            amount = min(len(view), self.maximum - self.length)
            self.guard(amount)
            self.handle.write(view[:amount])
            self.digest.update(view[:amount])
            self.length += amount
            self.total += amount
            view = view[amount:]
            if self.length == self.maximum:
                self._finish()
        return original

    def flush(self):
        if self.handle:
            self.handle.flush()

    def _finish(self):
        if self.handle is None:
            return
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.handle = None
        digest = self.digest.hexdigest()
        backups._verify_object(self.temporary, digest, self.length)
        target = backups._member(self.root, digest)
        if target.exists():
            backups._verify_object(target, digest, self.length)
        else:
            os.link(self.temporary, target)
            fsync_directory(self.root)
        self.temporary.unlink()
        self.temporary = None
        self.chunks.append(_chunk_descriptor(digest, self.length))
        self.length = 0

    def finish(self):
        self._finish()
        return self.chunks

    def abort(self):
        if self.handle:
            self.handle.close()
            self.handle = None
        if self.temporary:
            self.temporary.unlink(missing_ok=True)


def _encode(source, name, digest, size, encoding, work, chunk_bytes, level, guard):
    writer = _Chunks(work, chunk_bytes, guard)
    before = FileSnapshot.capture(source)
    raw_hash, length = hashlib.sha256(), 0
    try:
        with source.open("rb") as handle:
            encoded = gzip.GzipFile(filename="", fileobj=writer, mode="wb", compresslevel=level, mtime=0) if encoding == "gzip" else writer
            try:
                while data := handle.read(CHUNK_BYTES):
                    length += len(data)
                    if length > size:
                        raise IntegrityError("LFS source grew during encoding")
                    raw_hash.update(data)
                    encoded.write(data)
            finally:
                if encoding == "gzip":
                    encoded.close()
        chunks = writer.finish()
        if FileSnapshot.capture(source) != before or (raw_hash.hexdigest(), length) != (digest, size):
            raise IntegrityError("LFS source changed or differs from its backup inventory")
        return {"path": name, "sha256": digest, "bytes": size, "encoding": encoding,
                "encoded_bytes": writer.total, "chunks": chunks}
    finally:
        writer.abort()


def _sources(root, manifest):
    path = backups._member(root, "manifest.json")
    digest, size = hash_file(path)
    yield "manifest.json", digest, size, "raw"
    yield "catalog.sqlite3", manifest["catalog"]["sha256"], manifest["catalog"]["bytes"], "gzip"
    for ledger in backups._ledger_descriptors(manifest):
        yield ledger["path"], ledger["sha256"], ledger["bytes"], "raw"
    with closing(backups._connect(root / "catalog.sqlite3")) as catalog:
        for digest, size, _ in backups._inventory(catalog):
            yield backups._digest_name(digest), digest, size, "raw"


def _cached_chunks(descriptor, work, destination):
    for chunk in descriptor["chunks"]:
        path = _chunk_path(destination, chunk["sha256"])
        if not path.exists():
            path = backups._member(work, chunk["sha256"])
        if not path.exists():
            return False
    digest, length = hashlib.sha256(), 0
    for data in _decoded(destination, descriptor, work=work):
        digest.update(data)
        length += len(data)
        if length > descriptor["bytes"]:
            raise IntegrityError("Cached LFS component exceeds its original size")
    if (digest.hexdigest(), length) != (descriptor["sha256"], descriptor["bytes"]):
        raise IntegrityError("Cached LFS mapping differs from its original component")
    return True


def _append_rule(path, rule):
    path = backups._path(path)
    original = path.read_bytes() if path.exists() else b""
    if len(original) > 1024**2:
        raise LFSBackupError("Git rule file exceeds its size bound")
    if rule.encode() not in original.splitlines():
        payload = original + (b"\n" if original and not original.endswith(b"\n") else b"") + rule.encode() + b"\n"
        temporary = path.with_name(path.name + ".lfs-backup.part")
        temporary = backups._path(temporary)
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)


def _index(plan, work, chunk_bytes, level, guard):
    writer = _Chunks(work, chunk_bytes, guard)
    digest, length = hashlib.sha256(), 0
    try:
        with gzip.GzipFile(filename="", fileobj=writer, mode="wb", compresslevel=level, mtime=0) as compressor:
            for name, value, selected in plan.execute("SELECT name,descriptor,selected FROM members ORDER BY ordinal"):
                row = {"included": bool(selected), "member": json.loads(value)}
                if not selected:
                    row["reason"] = "new_lfs_byte_budget"
                data = _json(row)
                length += len(data)
                if length > MAX_INDEX_BYTES:
                    raise LFSBackupError("Component index exceeds its size bound")
                digest.update(data)
                compressor.write(data)
        chunks = writer.finish()
        return {"sha256": digest.hexdigest(), "bytes": length, "encoding": "gzip",
                "encoded_bytes": writer.total, "chunks": chunks}
    finally:
        writer.abort()


def _charged(plan):
    return plan.execute("SELECT coalesce(sum(bytes),0) FROM chunks WHERE refs>0 AND charged=1").fetchone()[0]


def stage_lfs_backup(backup: Path, destination: Path, *, max_new_lfs_bytes: int,
                     chunk_bytes: int = MAX_CHUNK_BYTES, compression_level: int = 6,
                     known_lfs_hashes=frozenset(), reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                     capacity_paths: tuple[Path, ...] = (), space_guard=None, progress=None) -> dict:
    """Stage immutable LFS files and a small ordinary-Git package manifest.

    Local files are still quota-charged on resume. Only explicitly supplied
    known_lfs_hashes are treated as previously published remote objects.
    No original component is partially published to satisfy the byte budget.
    """
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES or not 0 <= compression_level <= 9 or max_new_lfs_bytes < 0:
        raise ValueError("Invalid LFS chunk, compression, or byte-budget option")
    known = frozenset(known_lfs_hashes)
    for digest in known:
        backups._digest_name(digest)
    backup, destination = backups._path(backup), backups._path(destination)
    verification = backups.verify_backup(backup)
    source_manifest = backups._manifest(backup)
    source_hash, _ = hash_file(backup / "manifest.json")
    backups._mkdir(destination, False)
    _append_rule(destination / ".gitattributes", "lfs/** filter=lfs diff=lfs merge=lfs -text")
    _append_rule(destination / ".gitignore", ".lfs-backup-work/")
    work = backups._member(destination, f".lfs-backup-work/{source_hash}/{chunk_bytes}-{compression_level}")
    backups._mkdir(work, False)
    guard = space_guard or backups._space_guard(destination, reserve_bytes, capacity_paths)
    with backups._writer_lock(work, False), closing(sqlite3.connect(work / "plan.sqlite3")) as plan:
        os.chmod(work / "plan.sqlite3", 0o600)
        plan.executescript("""CREATE TABLE IF NOT EXISTS members(name TEXT PRIMARY KEY,ordinal INTEGER,descriptor TEXT,selected INTEGER DEFAULT 0);
          DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS member_chunks;
          CREATE TABLE chunks(hash TEXT PRIMARY KEY,bytes INTEGER,refs INTEGER DEFAULT 0,charged INTEGER);
          CREATE TABLE member_chunks(name TEXT,hash TEXT,PRIMARY KEY(name,hash));""")
        for partial in work.glob("chunk-*.part"):
            backups._path(partial).unlink()
        total_members = selected_charge = 0
        for ordinal, (name, digest, size, encoding) in enumerate(_sources(backup, source_manifest)):
            cached = plan.execute("SELECT descriptor FROM members WHERE name=?", (name,)).fetchone()
            descriptor = json.loads(cached[0]) if cached else None
            direct = {"path": name, "sha256": digest, "bytes": size, "encoding": encoding,
                      "encoded_bytes": size, "chunks": [_chunk_descriptor(digest, size)] if size else []}
            cheap_raw = encoding == "raw" and size <= chunk_bytes
            already_selected = cheap_raw and plan.execute("SELECT 1 FROM chunks WHERE hash=? AND refs>0", (digest,)).fetchone()
            skip_encoding = cheap_raw and digest not in known and not already_selected and size > max_new_lfs_bytes - selected_charge
            if skip_encoding:
                descriptor = direct  # An explicitly deferred component needs no temporary copy.
            elif cheap_raw and _chunk_path(destination, digest).exists():
                backups._verify_object(_chunk_path(destination, digest), digest, size)
                descriptor = direct
            elif (descriptor is None or descriptor.get("sha256") != digest or descriptor.get("bytes") != size
                    or descriptor.get("encoding") != encoding or not _cached_chunks(descriptor, work, destination)):
                descriptor = _encode(backups._member(backup, name), name, digest, size, encoding,
                                     work, chunk_bytes, compression_level, guard)
            with plan:
                plan.execute("INSERT OR REPLACE INTO members VALUES(?,?,?,0)", (name, ordinal, _json(descriptor).decode()))
                for chunk in descriptor["chunks"]:
                    plan.execute("INSERT OR IGNORE INTO chunks(hash,bytes,charged) VALUES(?,?,?)",
                                 (chunk["sha256"], chunk["bytes"], int(chunk["sha256"] not in known)))
                    plan.execute("INSERT OR IGNORE INTO member_chunks VALUES(?,?)", (name, chunk["sha256"]))
                cost = plan.execute("SELECT coalesce(sum(bytes),0) FROM chunks WHERE refs=0 AND charged=1 AND hash IN "
                                    "(SELECT hash FROM member_chunks WHERE name=?)", (name,)).fetchone()[0]
                if selected_charge + cost <= max_new_lfs_bytes:
                    plan.execute("UPDATE members SET selected=1 WHERE name=?", (name,))
                    plan.execute("UPDATE chunks SET refs=refs+1 WHERE hash IN (SELECT hash FROM member_chunks WHERE name=?)", (name,))
                    selected_charge += cost
            total_members += 1
            if progress:
                progress({"stage": "lfs_encode", "components_processed": total_members,
                          "new_lfs_bytes_selected": selected_charge})
        plan.execute("SAVEPOINT full_candidate")
        try:
            plan.execute("UPDATE members SET selected=1")
            full_index = _index(plan, work, chunk_bytes, compression_level, guard)
            full_new_bytes = plan.execute("SELECT coalesce(sum(bytes),0) FROM chunks WHERE charged=1").fetchone()[0]
            full_extra = {chunk["sha256"]: chunk["bytes"] for chunk in full_index["chunks"]
                          if chunk["sha256"] not in known and not plan.execute(
                              "SELECT 1 FROM chunks WHERE hash=?", (chunk["sha256"],)).fetchone()}
            full_new_bytes += sum(full_extra.values())
        finally:
            plan.execute("ROLLBACK TO full_candidate")
            plan.execute("RELEASE full_candidate")
        while True:
            index = _index(plan, work, chunk_bytes, compression_level, guard)
            extra = {chunk["sha256"]: chunk["bytes"] for chunk in index["chunks"] if chunk["sha256"] not in known
                     and not plan.execute("SELECT 1 FROM chunks WHERE hash=? AND refs>0", (chunk["sha256"],)).fetchone()}
            required = _charged(plan) + sum(extra.values())
            if required <= max_new_lfs_bytes:
                break
            removed = False
            for name, in plan.execute("SELECT name FROM members WHERE selected=1 ORDER BY ordinal DESC").fetchall():
                with plan:
                    plan.execute("UPDATE members SET selected=0 WHERE name=?", (name,))
                    plan.execute("UPDATE chunks SET refs=refs-1 WHERE hash IN (SELECT hash FROM member_chunks WHERE name=?)", (name,))
                removed = True
                if _charged(plan) + sum(extra.values()) <= max_new_lfs_bytes:
                    break
            if not removed:
                return {"status": "budget_exceeded", "manifest_published": False,
                        "new_lfs_bytes_required": required, "max_new_lfs_bytes": max_new_lfs_bytes,
                        "full_candidate_new_lfs_bytes": full_new_bytes,
                        "components_deferred": total_members, "physical_independence_asserted": False}
        selected = plan.execute("SELECT count(*) FROM members WHERE selected=1").fetchone()[0]
        chunks = {digest: size for digest, size in plan.execute("SELECT hash,bytes FROM chunks WHERE refs>0")}
        chunks.update({chunk["sha256"]: chunk["bytes"] for chunk in index["chunks"]})
        newly_written = 0
        for digest, size in sorted(chunks.items()):
            target = _chunk_path(destination, digest)
            if target.exists():
                backups._verify_object(target, digest, size)
                continue
            source = backups._member(work, digest)
            backups._verify_object(source, digest, size)
            guard(0)  # Publication links private staged copies, never canonical CAS.
            backups._mkdir(target.parent, False)
            os.link(source, target)
            os.chmod(target, 0o600)
            fsync_directory(target.parent)
            newly_written += size
        raw_bytes = plan.execute("SELECT coalesce(sum(json_extract(descriptor,'$.bytes')),0) FROM members WHERE selected=1").fetchone()[0]
        result = {"format": FORMAT, "format_version": VERSION, "source_manifest_sha256": source_hash,
                  "source_snapshot_id": source_manifest["snapshot_id"], "source_backup_status": source_manifest["status"],
                  "status": "complete" if selected == total_members else "partial", "index": index,
                  "components_included": selected, "components_deferred": total_members - selected,
                  "original_bytes_included": raw_bytes, "total_lfs_bytes": sum(chunks.values()),
                  "new_lfs_bytes": sum(size for digest, size in chunks.items() if digest not in known),
                  "full_candidate_new_lfs_bytes": full_new_bytes,
                  "max_new_lfs_bytes": max_new_lfs_bytes, "chunk_bytes": chunk_bytes,
                  "catalog_compression": {"format": "gzip", "mtime": 0, "level": compression_level},
                  "retained_evidence_complete": verification["retained_evidence_complete"],
                  "remote_upload_verified": False, "physical_independence_asserted": False}
        payload = _json(result)
        package_hash = hashlib.sha256(payload).hexdigest()
        name = f"packages/{source_hash}/{package_hash}.json"
        path = backups._member(destination, name)
        backups._mkdir(path.parent, False)
        if path.exists():
            if path.read_bytes() != payload:
                raise IntegrityError("Existing package manifest differs from its content address")
        else:
            temporary = backups._path(path.with_name(path.name + ".part"))
            temporary.unlink(missing_ok=True)
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            fsync_directory(path.parent)
        return {**result, "package_manifest": name, "manifest_published": True,
                "newly_written_lfs_bytes": newly_written}


def _decoded(root, descriptor, *, work=None):
    if descriptor.get("encoding") not in ("raw", "gzip"):
        raise LFSBackupError("Unsupported component encoding")
    decoder = zlib.decompressobj(31) if descriptor["encoding"] == "gzip" else None
    encoded = 0
    for chunk in descriptor["chunks"]:
        digest, size = chunk["sha256"], chunk["bytes"]
        if not isinstance(size, int) or not 0 < size <= MAX_CHUNK_BYTES or chunk.get("path") != _chunk_descriptor(digest, size)["path"]:
            raise LFSBackupError("Unsafe LFS chunk descriptor")
        path = _chunk_path(root, digest)
        if not path.exists() and work is not None:
            path = backups._member(work, digest)
        backups._verify_object(path, digest, size)
        with path.open("rb") as handle:
            while data := handle.read(CHUNK_BYTES):
                encoded += len(data)
                if decoder is None:
                    yield data
                else:
                    while data:
                        value = decoder.decompress(data, CHUNK_BYTES)
                        data = decoder.unconsumed_tail
                        if decoder.unused_data:
                            raise IntegrityError("Trailing data after compressed component")
                        if value:
                            yield value
    if encoded != descriptor["encoded_bytes"] or (decoder is not None and not decoder.eof):
        raise IntegrityError("Truncated encoded component")


def _safe_member(name, digest):
    if name in ("manifest.json", "catalog.sqlite3"):
        return
    if name.startswith("objects/") and name == backups._digest_name(digest):
        return
    if name.startswith("manifests/") and Path(name).parent.as_posix() == "manifests" and name.endswith(".jsonl"):
        backups._member(Path("/"), name)
        return
    raise LFSBackupError("Unsafe original member path")


def reassemble_lfs_backup(repository: Path, package_manifest: Path, destination: Path, *,
                          reserve_bytes: int = DEFAULT_RESERVE_BYTES, capacity_paths: tuple[Path, ...] = (),
                          space_guard=None, progress=None) -> dict:
    """Reassemble a complete package into a NEW private backup and verify it."""
    root, destination = backups._path(repository), backups._path(destination)
    path = Path(package_manifest)
    path = backups._path(path) if path.is_absolute() else backups._member(root, str(path))
    package_hash, _ = hash_file(path)
    if path.stem != package_hash:
        raise IntegrityError("LFS package manifest failed its content-address readback")
    package = backups._read_json(path)
    if package.get("format") != FORMAT or package.get("format_version") != VERSION:
        raise LFSBackupError("Unsupported LFS package")
    if package.get("status") != "complete" or package.get("components_deferred") != 0:
        raise LFSBackupError("Partial LFS package cannot restore a complete backup")
    if destination.exists():
        raise LFSBackupError("Reassembly destination must be new")
    guard = space_guard or backups._space_guard(destination, reserve_bytes, capacity_paths)
    guard(package["original_bytes_included"])
    backups._mkdir(destination, False)
    plan_path = backups._member(destination, ".reassemble-plan.sqlite3.part")
    try:
        with closing(sqlite3.connect(plan_path)) as plan:
            os.chmod(plan_path, 0o600)
            plan.execute("CREATE TABLE members(name TEXT PRIMARY KEY,descriptor TEXT)")
            digest, size, buffer = hashlib.sha256(), 0, b""
            for data in _decoded(root, package["index"]):
                size += len(data)
                if size > min(package["index"]["bytes"], MAX_INDEX_BYTES):
                    raise IntegrityError("Component index exceeds its declared byte bound")
                digest.update(data)
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    record = json.loads(line)
                    if record.get("included") is not True:
                        raise LFSBackupError("Complete package index contains a deferred component")
                    member = record["member"]
                    _safe_member(member["path"], member["sha256"])
                    plan.execute("INSERT INTO members VALUES(?,?)", (member["path"], _json(member).decode()))
                if len(buffer) > 4 * 1024**2:
                    raise LFSBackupError("Component index record exceeds its size bound")
            if buffer or (digest.hexdigest(), size) != (package["index"]["sha256"], package["index"]["bytes"]):
                raise IntegrityError("Component index failed hash readback")
            if plan.execute("SELECT count(*) FROM members").fetchone()[0] != package["components_included"]:
                raise IntegrityError("Component index count mismatch")
            total_raw = plan.execute("SELECT coalesce(sum(json_extract(descriptor,'$.bytes')),0) FROM members").fetchone()[0]
            if total_raw != package["original_bytes_included"]:
                raise IntegrityError("Component index byte total mismatch")
            if not all(plan.execute("SELECT 1 FROM members WHERE name=?", (name,)).fetchone()
                       for name in ("catalog.sqlite3", "manifest.json")):
                raise LFSBackupError("Package lacks its catalog or backup manifest")
            restored = 0
            for name, raw in plan.execute("SELECT name,descriptor FROM members ORDER BY name='catalog.sqlite3',name"):
                member = json.loads(raw)
                target = backups._member(destination, name)
                backups._mkdir(target.parent, False)
                temporary = backups._path(target.with_name(target.name + ".part"))
                try:
                    length = 0
                    with temporary.open("xb") as output:
                        os.chmod(temporary, 0o600)
                        for data in _decoded(root, member):
                            length += len(data)
                            if length > member["bytes"]:
                                raise IntegrityError("Decoded component exceeds its declared size")
                            guard(len(data))
                            output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    backups._verify_object(temporary, member["sha256"], member["bytes"])
                    os.link(temporary, target)
                    fsync_directory(target.parent)
                finally:
                    temporary.unlink(missing_ok=True)
                restored += 1
                if progress:
                    progress({"stage": "lfs_reassemble", "components_restored": restored})
        if hash_file(destination / "manifest.json")[0] != package["source_manifest_sha256"]:
            raise IntegrityError("Reassembled backup manifest differs from the package")
        verification = backups.verify_backup(destination, progress=progress)
        return {"status": "reassembled_and_verified", "backup_verification": verification,
                "components_restored": restored, "remote_upload_verified": False,
                "physical_independence_asserted": False}
    except BaseException:
        (destination / "catalog.sqlite3").unlink(missing_ok=True)
        raise
    finally:
        plan_path.unlink(missing_ok=True)


__all__ = ["LFSBackupError", "stage_lfs_backup", "reassemble_lfs_backup"]
