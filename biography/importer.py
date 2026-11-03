"""Verified local import and narrowly scoped, manifest-backed source cleanup.

Archive names are evidence strings, never extraction destinations. Every entry
has its ordinal in a durable ledger, so duplicate names do not overwrite each
other. A paused/interrupted run can be repeated: immutable objects deduplicate
only after fresh readback, and originals survive until final reconciliation.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import stat
import tarfile
import uuid
import zipfile
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from .storage import (
    DEFAULT_RESERVE_BYTES, FileSnapshot, ImportPaused, IntegrityError,
    ObjectStore, SourceChangedError, StoredObject, fsync_directory, hash_file,
)


Progress = Callable[[dict], None]
MAX_ARCHIVE_DEPTH = 16
EXCLUDED_FILENAMES = {".env", "credentials", "credentials.json", "secrets.json",
                      "id_rsa", "id_ed25519", "wallet.dat"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressCallbackError(Exception):
    """Catalog/progress failures must never be mistaken for archive damage."""


@dataclass
class StoredFile:
    sha256: str
    size: int
    path: Path
    original_path: str
    source_path: str
    archive_chain: list[dict] = field(default_factory=list)
    media_type: str | None = None
    disposition: str = "retained_leaf"
    deduplicated: bool = False

    @property
    def object_path(self) -> Path:
        return self.path

    def to_dict(self) -> dict:
        return {**asdict(self), "path": str(self.path)}


@dataclass
class IngestReport:
    import_id: str
    source: str
    vaultroot: str
    manifest_path: Path
    files: list[StoredFile] = field(default_factory=list)
    source_reports: list[dict] = field(default_factory=list)
    status: str = "running"
    counts: dict = field(default_factory=dict)
    exceptions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"import_id": self.import_id, "source": self.source,
                "vaultroot": self.vaultroot, "manifest_path": str(self.manifest_path),
                "files": [item.to_dict() for item in self.files],
                "source_reports": self.source_reports, "status": self.status,
                "counts": self.counts, "exceptions": self.exceptions}


class _Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.file = path.open("x", encoding="utf-8")
        os.chmod(path, 0o600)
        fsync_directory(path.parent)

    def append(self, kind: str, **data: object) -> None:
        self.file.write(json.dumps({"kind": kind, "at": utcnow(), **data},
                                   ensure_ascii=True, sort_keys=True) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self) -> None:
        self.file.close()


def _unsafe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return ("\x00" in name or PurePosixPath(normalized).is_absolute()
            or bool(PureWindowsPath(name).drive)
            or ".." in PurePosixPath(normalized).parts)


def _excluded_name(name: str, excluded_directory_names: set[str]) -> bool:
    parts = [p.casefold() for p in name.replace("\\", "/").split("/")]
    return (bool(set(parts) & excluded_directory_names)
            or (bool(parts) and (parts[-1] in EXCLUDED_FILENAMES
                                 or parts[-1].endswith((".pem", ".key", ".p12", ".pfx"))
                                 or parts[-1].startswith(".env."))))


def _normalized_member(name: str) -> str:
    return posixpath.normpath(name.replace("\\", "/"))


def _archive_kind(path: Path) -> str | None:
    # ZIP permits arbitrary prefixes and scans backwards for its end record.
    # A TAR with a ZIP attachment near the end can therefore also pass
    # is_zipfile(). Its real TAR header must take precedence or sibling files
    # could be silently dropped when a disposable outer export is processed.
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except (OSError, tarfile.TarError):
        pass
    if zipfile.is_zipfile(path):
        return "zip"
    return None


def _under_root(path: Path, allowed_root: Path) -> None:
    """Reject symlink components and require a strict descendant regular file."""
    path = Path(os.path.abspath(path))
    allowed_root = Path(os.path.abspath(allowed_root))
    try:
        relative = path.relative_to(allowed_root)
    except ValueError as error:
        raise IntegrityError("Source is outside the authorized cleanup root") from error
    if not relative.parts:
        raise IntegrityError("Cleanup requires an exact source file below its root")
    current = allowed_root
    if current.is_symlink():
        raise IntegrityError("Cleanup root cannot be a symlink")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise IntegrityError("Cleanup path cannot contain a symlink")


def _verify_source(path: Path, snapshot: FileSnapshot, sha256: str,
                   progress: Callable[[int], None] | None = None) -> None:
    actual, _ = hash_file(path, snapshot=snapshot, progress=progress)
    if actual != sha256:
        raise SourceChangedError(f"Source content changed before cleanup: {path}")


class _Importer:
    def __init__(self, report: IngestReport, store: ObjectStore, ledger: _Ledger,
                 progress: Progress | None, delete_source: bool,
                 allowed_root: Path | None, discard_container: bool = False,
                 excluded_directory_names: tuple[str, ...] = ()):
        self.report, self.store, self.ledger = report, store, ledger
        self.progress = progress
        self.delete_source = delete_source
        self.allowed_root = allowed_root
        self.discard_container = discard_container
        self.excluded_directory_names = {name.casefold() for name in excluded_directory_names}

    def emit(self, event: str, **data: object) -> None:
        if self.progress:
            try:
                self.progress({"event": event, **data})
            except Exception as error:
                raise ProgressCallbackError(f"Import callback failed during {event}") from error

    def retain(self, obj: StoredObject, logical: str, source: Path,
               chain: list[dict], disposition: str = "retained_leaf") -> StoredFile:
        result = StoredFile(obj.sha256, obj.size, obj.path, logical, str(source),
                            list(chain), mimetypes.guess_type(logical)[0],
                            disposition, obj.deduplicated)
        self.report.files.append(result)
        self.ledger.append("object", **result.to_dict())
        self.emit("object_verified", sha256=obj.sha256, size=obj.size,
                  original_path=logical, disposition=disposition,
                  file=result.to_dict())
        return result

    def bytes_progress(self, source: Path) -> Callable[[int], None]:
        return lambda size: self.emit("bytes_written", source_path=str(source), bytes=size)

    def read_progress(self, source: Path, phase: str) -> Callable[[int], None]:
        return lambda size: self.emit("bytes_read", source_path=str(source),
                                      phase=phase, bytes=size)

    def archive(self, path: Path, kind: str, source: Path, logical: str,
                chain: list[dict], depth: int = 0) -> bool:
        """Return true only when every member has a reconciled disposition."""
        if depth >= MAX_ARCHIVE_DEPTH:
            self.ledger.append("archive_limit", original_path=logical, depth=depth)
            return False
        complete = True
        count = 0
        self.ledger.append("archive_begin", original_path=logical, format=kind,
                           archive_chain=chain)
        try:
            if kind == "zip":
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    # An unreadable/encrypted member must not hide the remaining
                    # central-directory entries from the audit ledger.
                    for ordinal, info in enumerate(infos):
                        self.ledger.append("archive_entry_inventory", archive=logical,
                                           path=info.filename, ordinal=ordinal,
                                           size=info.file_size, crc32=info.CRC,
                                           unsafe_path=_unsafe_member(info.filename))
                    for ordinal, info in enumerate(infos):
                        count += 1
                        name = info.filename
                        entry = {"path": name, "ordinal": ordinal,
                                 "size": info.file_size, "compressed_size": info.compress_size,
                                 "crc32": info.CRC, "date_time": list(info.date_time),
                                 "external_attr": info.external_attr,
                                 "unsafe_path": _unsafe_member(name)}
                        member_chain = chain + [{"path": name, "ordinal": ordinal}]
                        member_logical = logical + "!" + name
                        mode = info.external_attr >> 16
                        if info.is_dir():
                            disposition = "directory"
                        elif stat.S_ISLNK(mode):
                            disposition = "link_container_required"
                            complete = False
                        elif _excluded_name(name, self.excluded_directory_names):
                            disposition = "excluded_container_required"
                            complete = False
                        else:
                            # Passing ZipInfo is essential: duplicate names remain distinct.
                            with archive.open(info) as stream:
                                obj = self.store.put_stream(
                                    stream, expected_size=info.file_size,
                                    progress=self.bytes_progress(source))
                            nested = _archive_kind(obj.path)
                            if nested:
                                self.retain(obj, member_logical, source, member_chain,
                                            "verified_container")
                                reconciled = self.archive(obj.path, nested, source,
                                                          member_logical, member_chain,
                                                          depth + 1)
                                # The complete native attachment bytes are already
                                # retained and independently verified. Parsing its
                                # children is separate from retaining this member.
                                disposition = ("nested_archive_verified" if reconciled
                                               else "nested_archive_retained_unparsed")
                            else:
                                self.retain(obj, member_logical, source, member_chain)
                                disposition = "retained_leaf"
                            entry["sha256"] = obj.sha256
                        if entry["unsafe_path"]:
                            complete = False
                        self.ledger.append("archive_entry", archive=logical,
                                           archive_chain=chain, disposition=disposition, **entry)
            else:
                # Paths are lookup keys only. Never materialize either link type
                # on disk. Duplicate names make a hardlink target ambiguous.
                target_objects: dict[str, list[tuple[str, int]]] = {}
                hardlinks: list[dict] = []
                path_occurrences: dict[str, int] = {}
                with tarfile.open(path, "r:*") as archive:
                    for ordinal, info in enumerate(archive):
                        count += 1
                        name = info.name
                        key = _normalized_member(name)
                        path_occurrences[key] = path_occurrences.get(key, 0) + 1
                        entry = {"path": name, "ordinal": ordinal, "size": info.size,
                                 "type": info.type.decode("ascii", "replace"),
                                 "linkname": info.linkname, "mode": info.mode,
                                 "uid": info.uid, "gid": info.gid, "mtime": info.mtime,
                                 "pax_headers": info.pax_headers,
                                 "unsafe_path": _unsafe_member(name)}
                        member_chain = chain + [{"path": name, "ordinal": ordinal}]
                        member_logical = logical + "!" + name
                        if _excluded_name(name, self.excluded_directory_names):
                            disposition = "excluded_container_required"
                            complete = False
                        elif info.isdir():
                            disposition = "directory"
                        elif info.issym():
                            disposition = "retained_symlink_metadata"
                        elif info.islnk():
                            disposition = "hardlink_pending"
                            hardlinks.append({"entry": entry, "chain": member_chain,
                                              "logical": member_logical})
                        elif not info.isfile():
                            disposition = "special_container_required"
                            complete = False
                        else:
                            stream = archive.extractfile(info)
                            if stream is None:
                                raise IntegrityError("TAR regular member has no content stream")
                            with stream:
                                obj = self.store.put_stream(
                                    stream, expected_size=info.size,
                                    progress=self.bytes_progress(source))
                            nested = _archive_kind(obj.path)
                            if nested:
                                self.retain(obj, member_logical, source, member_chain,
                                            "verified_container")
                                reconciled = self.archive(obj.path, nested, source,
                                                          member_logical, member_chain,
                                                          depth + 1)
                                disposition = ("nested_archive_verified" if reconciled
                                               else "nested_archive_retained_unparsed")
                            else:
                                self.retain(obj, member_logical, source, member_chain)
                                disposition = "retained_leaf"
                            entry["sha256"] = obj.sha256
                            target_objects.setdefault(_normalized_member(name), []).append(
                                (obj.sha256, obj.size))
                        if entry["unsafe_path"]:
                            complete = False
                        self.ledger.append("archive_entry", archive=logical,
                                           archive_chain=chain, disposition=disposition, **entry)
                    # tarfile stops at the first end marker or invalid header.
                    # A concatenated archive or nonzero tail must not silently
                    # disappear when an outer export container is discarded.
                    archive.fileobj.seek(archive.offset)
                    tail_bytes = 0
                    nonzero_tail = False
                    while tail := archive.fileobj.read(1024 * 1024):
                        tail_bytes += len(tail)
                        nonzero_tail = nonzero_tail or any(tail)
                        self.emit("bytes_read", source_path=str(source),
                                  phase="archive_tail_verification", bytes=len(tail))
                    if nonzero_tail:
                        complete = False
                        self.ledger.append("archive_unparsed_tail", archive=logical,
                                           bytes=tail_bytes, offset=archive.offset,
                                           disposition="container_required")
                # Resolve chains and forward references after the complete member
                # inventory is known. Ambiguous duplicate target paths are blocked.
                pending_links: dict[str, list[dict]] = {}
                resolved_ordinals: set[int] = set()
                verified_targets: dict[tuple[str, int], StoredObject] = {}
                for link in hardlinks:
                    target = _normalized_member(link["entry"]["linkname"])
                    if (not _unsafe_member(link["entry"]["linkname"])
                            and path_occurrences.get(target, 0) == 1):
                        pending_links.setdefault(target, []).append(link)
                available = deque(target for target, values in target_objects.items()
                                  if len(values) == 1 and path_occurrences.get(target) == 1)
                while available:
                    target = available.popleft()
                    candidates = target_objects.get(target, [])
                    if len(candidates) != 1 or path_occurrences.get(target) != 1:
                        continue
                    sha256, size = candidates[0]
                    for link in pending_links.pop(target, []):
                        entry = link["entry"]
                        identity = (sha256, size)
                        if identity not in verified_targets:
                            verified_targets[identity] = self.store.verify_object(sha256, size)
                        obj = verified_targets[identity]
                        self.retain(obj, link["logical"], source, link["chain"])
                        key = _normalized_member(entry["path"])
                        target_objects.setdefault(key, []).append((sha256, size))
                        available.append(key)
                        resolved_ordinals.add(entry["ordinal"])
                        self.ledger.append("hardlink_resolved", archive=logical,
                                           path=entry["path"], ordinal=entry["ordinal"],
                                           linkname=entry["linkname"], sha256=sha256,
                                           size=size, disposition="retained_hardlink_alias")
                for link in hardlinks:
                    if link["entry"]["ordinal"] in resolved_ordinals:
                        continue
                    complete = False
                    self.ledger.append("hardlink_unresolved", archive=logical,
                                       path=link["entry"]["path"],
                                       ordinal=link["entry"]["ordinal"],
                                       linkname=link["entry"]["linkname"],
                                       disposition="container_required")
        except (ImportPaused, IntegrityError, ProgressCallbackError):
            raise
        except (OSError, EOFError, RuntimeError, ValueError, tarfile.TarError,
                zipfile.BadZipFile, NotImplementedError) as error:
            # A whole verified opaque copy is required before any later cleanup.
            self.ledger.append("archive_error", archive=logical,
                               error_type=type(error).__name__, error=str(error))
            self.report.exceptions.append({"source": logical,
                                           "error_type": type(error).__name__,
                                           "error": str(error)})
            complete = False
        self.ledger.append("archive_end", original_path=logical, entries=count,
                           reconciled=complete)
        return complete

    def source_file(self, path: Path, logical: str) -> None:
        if _excluded_name(logical, self.excluded_directory_names):
            self.ledger.append("excluded_source", source_path=str(path),
                               original_path=logical, disposition="retained_source",
                               reason="excluded_directory_or_credential_filename")
            self.report.exceptions.append({"source": str(path), "status": "excluded"})
            return
        before = FileSnapshot.capture(path)
        self.ledger.append("source_begin", source_path=str(path), original_path=logical,
                           snapshot=before.to_dict())
        self.emit("source_started", source_path=str(path), size=before.size)
        first_file = len(self.report.files)
        kind = _archive_kind(path)
        if kind:
            source_hash, _ = hash_file(path, snapshot=before,
                                      progress=self.read_progress(path, "source_hash"))
            # Header precedes member processing and survives interruption.
            self.ledger.append("source_archive_header", source_path=str(path),
                               original_path=logical, sha256=source_hash, format=kind,
                               snapshot=before.to_dict())
            reconciled = self.archive(path, kind, path, logical, [])
            _verify_source(path, before, source_hash,
                           self.read_progress(path, "source_reconciliation"))
            if not reconciled or not self.discard_container:
                opaque = self.store.put_path(path, snapshot=before,
                                             progress=self.bytes_progress(path))
                if opaque.sha256 != source_hash:
                    raise SourceChangedError("Archive changed before opaque retention")
                self.retain(opaque, logical, path, [],
                            "verified_container" if reconciled else "opaque_container")
        else:
            obj = self.store.put_path(path, snapshot=before,
                                      progress=self.bytes_progress(path))
            source_hash = obj.sha256
            self.retain(obj, logical, path, [])
            reconciled = True
        after = FileSnapshot.capture(path)
        if before != after:
            raise SourceChangedError(f"Source changed during import: {path}")
        source_record = {"source_path": str(path), "original_path": logical,
                         "sha256": source_hash, "size": before.size,
                         "snapshot_before": before.to_dict(), "snapshot_after": after.to_dict(),
                         "archive_format": kind, "reconciled": reconciled,
                         "deletion_eligible": reconciled,
                         "objects": [{"sha256": digest, "size": size}
                                     for digest, size in dict.fromkeys(
                                         (item.sha256, item.size)
                                         for item in self.report.files[first_file:])],
                         "deleted": False}
        self.ledger.append("source_ready", **source_record)
        self.report.source_reports.append(source_record)
        if self.delete_source and reconciled:
            if self.allowed_root is None:
                raise IntegrityError("Source deletion requires an explicit allowed_root")
            self.delete_verified(path, source_record)
        self.emit("source_finished", source_path=str(path), reconciled=reconciled,
                  deleted=source_record["deleted"])

    def delete_verified(self, path: Path, record: dict) -> None:
        assert self.allowed_root is not None
        _under_root(path, self.allowed_root)
        # Reopen every canonical object, including duplicates, immediately before
        # deletion. A previous successful copy cannot authorize deletion now.
        for item in record["objects"]:
            self.store.verify_object(item["sha256"], item["size"])
        snapshot = FileSnapshot(**record["snapshot_before"])
        _verify_source(path, snapshot, record["sha256"],
                       self.read_progress(path, "source_cleanup_verification"))
        self.ledger.append("delete_authorized", source_path=str(path),
                           sha256=record["sha256"], snapshot=snapshot.to_dict())
        # lstat again after durable intent, then unlink only this exact file.
        if FileSnapshot.capture(path) != snapshot:
            raise SourceChangedError(f"Source changed at deletion boundary: {path}")
        path.unlink()
        fsync_directory(path.parent)
        record["deleted"] = True
        self.ledger.append("source_deleted", source_path=str(path), sha256=record["sha256"])


def ingest_path(path: Path, vaultroot: Path, *, delete_source: bool = False,
                allowed_root: Path | None = None, progress: Progress | None = None,
                logical_path: str | None = None, discard_container: bool = False,
                reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                windows_reserve_bytes: int | None = None,
                excluded_directory_names: tuple[str, ...] = (),
                space_guard: Callable[[int], None] | None = None) -> IngestReport:
    """Import one completed export or directory, with deletion disabled by default.

    Re-running after interruption is safe and verifies already-existing objects.
    Deletion requires both an explicit opt-in and an exact authorized source root.
    A source containing unsafe/unresolved entries is retained for review.
    Archive bytes are retained by default, including native ZIP attachments.
    Only a caller-classified outer export may set discard_container=True.
    """
    path = Path(os.path.abspath(path))
    vaultroot = Path(os.path.abspath(vaultroot))
    if delete_source and allowed_root is None:
        raise IntegrityError("Source deletion requires an explicit allowed_root")
    if path.is_symlink():
        raise IntegrityError("Import source cannot be a symlink")
    # Never recurse into the destination if callers accidentally place it inside
    # an import directory. Live repositories/vaults must be snapshotted upstream.
    if path.is_dir() and vaultroot.is_relative_to(path):
        raise IntegrityError("The canonical vault cannot be inside an import source")
    import_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    manifest = vaultroot / "manifests" / (import_id + ".jsonl")
    store = ObjectStore(vaultroot, reserve_bytes=reserve_bytes, space_guard=space_guard,
                        windows_reserve_bytes=windows_reserve_bytes)
    store.guard(0)
    ledger = _Ledger(manifest)
    report = IngestReport(import_id, str(path), str(vaultroot), manifest)
    runner = _Importer(report, store, ledger, progress, delete_source,
                       Path(allowed_root) if allowed_root is not None else None,
                       discard_container=discard_container,
                       excluded_directory_names=excluded_directory_names)
    ledger.append("header", version=1, import_id=import_id, source=str(path),
                  vaultroot=str(vaultroot), delete_source=delete_source,
                  allowed_root=str(allowed_root) if allowed_root else None,
                  logical_path=logical_path, discard_container=discard_container,
                  reserve_bytes=reserve_bytes, windows_reserve_bytes=windows_reserve_bytes)
    directories: list[Path] = []
    try:
        if path.is_dir():
            # os.walk does not follow links; excluded roots never get traversed.
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                directories.append(current_path)
                ledger.append("source_directory", source_path=str(current_path))
                kept = []
                for name in sorted(dirnames):
                    child = current_path / name
                    if child.is_symlink() or name.casefold() in runner.excluded_directory_names:
                        ledger.append("excluded_source", source_path=str(child),
                                      disposition="retained_source", reason="symlink_or_excluded_directory")
                    else:
                        kept.append(name)
                dirnames[:] = kept
                for name in sorted(filenames):
                    child = current_path / name
                    if child.is_symlink() or not child.is_file():
                        ledger.append("excluded_source", source_path=str(child),
                                      disposition="retained_source", reason="non_regular_source")
                        continue
                    runner.source_file(child, str(child.relative_to(path)))
        else:
            runner.source_file(path, logical_path or path.name)
        if delete_source:
            # rmdir removes empty manifest-seen directories only. Never rmtree.
            for directory in reversed(directories):
                if directory == Path(allowed_root):
                    continue
                try:
                    directory.rmdir()
                    ledger.append("empty_directory_removed", source_path=str(directory))
                except OSError:
                    pass
        report.status = "complete" if not report.exceptions and all(
            item["reconciled"] for item in report.source_reports) else "needs_review"
    except ImportPaused as error:
        report.status = "paused"
        report.exceptions.append(error.to_dict())
        ledger.append("paused", **error.to_dict())
        error.manifest_path = manifest
        error.report = report
        raise
    except BaseException as error:
        report.status = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
        ledger.append("error", error_type=type(error).__name__, error=str(error), status=report.status)
        raise
    finally:
        report.counts = {"files": len(report.files),
                         "unique_objects": len({item.sha256 for item in report.files}),
                         "source_files": len(report.source_reports),
                         "deleted_sources": sum(item["deleted"] for item in report.source_reports),
                         "logical_bytes": sum(item.size for item in report.files)}
        ledger.append("footer", status=report.status, counts=report.counts,
                      exceptions=report.exceptions)
        ledger.close()
    return report


def read_manifest(path: Path) -> list[dict]:
    """Read complete durable records, tolerating only an interrupted last line."""
    lines = Path(path).read_bytes().splitlines(keepends=True)
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise IntegrityError("Manifest contains a corrupt record")
    return records


__all__ = ["ingest_path", "read_manifest", "StoredFile", "IngestReport",
           "ImportPaused", "IntegrityError", "SourceChangedError"]
