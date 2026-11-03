"""Private content-addressed storage with independent readback verification.

Source content is never trusted because a filename or a previous ledger says it
was copied. Both new and existing objects must pass a fresh SHA-256 readback.
"""

from __future__ import annotations

import hashlib
import errno
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable


CHUNK_BYTES = 1024 * 1024
DEFAULT_RESERVE_BYTES = 20 * 1024**3


class IntegrityError(RuntimeError):
    """An object failed its independent readback or expected length check."""


class SourceChangedError(IntegrityError):
    """A source changed while being read. It must never be deleted."""


class ImportPaused(RuntimeError):
    """A resumable pause, normally because either disk has too little space."""

    def __init__(self, reason: str, **details: object):
        self.reason = reason
        self.details = details
        super().__init__(reason)

    def to_dict(self) -> dict:
        return {"status": "paused", "reason": self.reason, **self.details}


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int

    @classmethod
    def capture(cls, path: Path) -> "FileSnapshot":
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityError(f"Source is not a regular file: {path}")
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                   info.st_ctime_ns, info.st_mode)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size: int
    path: Path
    deduplicated: bool = False


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def hash_file(path: Path, *, snapshot: FileSnapshot | None = None,
              progress: Callable[[int], None] | None = None) -> tuple[str, int]:
    """Hash a regular file and prove the path and opened inode remained stable."""
    path = Path(path)
    before = snapshot or FileSnapshot.capture(path)
    digest = hashlib.sha256()
    length = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.device, before.inode):
            raise SourceChangedError(f"Source was replaced before reading: {path}")
        while data := source.read(CHUNK_BYTES):
            digest.update(data)
            length += len(data)
            if progress:
                progress(len(data))
        after_open = os.fstat(source.fileno())
    after = FileSnapshot.capture(path)
    if before != after or (after_open.st_size, after_open.st_mtime_ns,
                            after_open.st_ctime_ns) != (
                                before.size, before.mtime_ns, before.ctime_ns):
        raise SourceChangedError(f"Source changed while reading: {path}")
    if length != before.size:
        raise SourceChangedError(f"Source length changed while reading: {path}")
    return digest.hexdigest(), length


class SpaceGuard:
    """Check both the WSL filesystem and its Windows C: backing disk.

    Free space on ext4 alone is misleading when the expanding WSL VHDX lives
    on C:. These are independent upper bounds, never amounts to add together.
    """

    def __init__(self, root: Path, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                 windows_root: Path | None = Path("/mnt/c"),
                 windows_reserve_bytes: int | None = None):
        self.root = Path(root)
        self.reserve_bytes = reserve_bytes
        self.windows_root = Path(windows_root) if windows_root is not None else None
        self.windows_reserve_bytes = (reserve_bytes if windows_reserve_bytes is None
                                      else windows_reserve_bytes)

    def __call__(self, additional_bytes: int = 0) -> None:
        ancestor = self.root
        while not ancestor.exists():
            ancestor = ancestor.parent
        roots = [("vault_filesystem", ancestor, self.reserve_bytes)]
        if self.windows_root is not None and self.windows_root.is_dir():
            roots.append(("windows_c_backing_disk", self.windows_root,
                          self.windows_reserve_bytes))
        for label, root, reserve in roots:
            available = shutil.disk_usage(root).free
            required = reserve + max(0, additional_bytes)
            if available < required:
                raise ImportPaused("insufficient_disk_space", disk=label,
                                   path=str(root), free_bytes=available,
                                   required_bytes=required,
                                   reserve_bytes=reserve)


class ObjectStore:
    def __init__(self, vaultroot: Path, *, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                 space_guard: Callable[[int], None] | None = None,
                 windows_reserve_bytes: int | None = None):
        self.root = Path(vaultroot)
        self.objects = self.root / "objects" / "sha256"
        self.guard = space_guard or SpaceGuard(
            self.root, reserve_bytes, windows_reserve_bytes=windows_reserve_bytes)

    def object_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("Invalid SHA-256 object name")
        return self.objects / sha256[:2] / sha256

    def verify_object(self, sha256: str, size: int | None = None) -> StoredObject:
        path = self.object_path(sha256)
        actual, length = hash_file(path)
        if actual != sha256 or (size is not None and size != length):
            raise IntegrityError(f"Canonical object failed readback: {sha256}")
        return StoredObject(sha256, length, path, True)

    def put_stream(self, source: BinaryIO, *, expected_size: int | None = None,
                   progress: Callable[[int], None] | None = None) -> StoredObject:
        """Stream directly to an atomic object, fsync, reopen, and rehash it."""
        self.guard(expected_size or 0)
        temporary_root = self.root / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix="object-", suffix=".part",
                                             dir=temporary_root)
        temporary = Path(name)
        digest = hashlib.sha256()
        length = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while data := source.read(CHUNK_BYTES):
                    self.guard(len(data))
                    destination.write(data)
                    digest.update(data)
                    length += len(data)
                    if progress:
                        progress(len(data))
                destination.flush()
                os.fsync(destination.fileno())
            if expected_size is not None and length != expected_size:
                raise IntegrityError("Stream length differs from declared member length")
            sha256 = digest.hexdigest()
            destination_path = self.object_path(sha256)
            destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            duplicate = destination_path.exists()
            if not duplicate:
                try:
                    # Atomic no-overwrite publication also handles another importer.
                    os.link(temporary, destination_path)
                except FileExistsError:
                    duplicate = True
                fsync_directory(destination_path.parent)
            verified = self.verify_object(sha256, length)
            return StoredObject(sha256, length, verified.path, duplicate)
        except OSError as error:
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                raise ImportPaused("filesystem_full", path=str(self.root),
                                   bytes_streamed=length, errno=error.errno) from error
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def put_path(self, path: Path, *, snapshot: FileSnapshot | None = None,
                 progress: Callable[[int], None] | None = None) -> StoredObject:
        path = Path(path)
        before = snapshot or FileSnapshot.capture(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != (before.device, before.inode):
                raise SourceChangedError(f"Source was replaced before reading: {path}")
            result = self.put_stream(source, expected_size=before.size, progress=progress)
            opened_after = os.fstat(source.fileno())
        if FileSnapshot.capture(path) != before or (
            opened_after.st_size, opened_after.st_mtime_ns, opened_after.st_ctime_ns
        ) != (before.size, before.mtime_ns, before.ctime_ns):
            raise SourceChangedError(f"Source changed while reading: {path}")
        return result


__all__ = ["ObjectStore", "StoredObject", "FileSnapshot", "SpaceGuard",
           "IntegrityError", "SourceChangedError", "ImportPaused", "hash_file"]
