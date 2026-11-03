"""Private, portable metadata snapshots. Original source/media bytes are excluded.

Export only to a private destination. JSONL contains personal text and provenance,
not merely anonymous metadata. Restore always creates a new database and never
executes SQL supplied by a snapshot. Callers should quiesce import workers while
including their ledgers; concurrent ledger changes cause a safe export failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
from typing import Iterator

from .db import SCHEMA
from .storage import fsync_directory


FORMAT = "biography-private-metadata"
VERSION = 1
MAX_SHARD_BYTES = 20 * 1024 * 1024
TABLES = ("sources", "imports", "objects", "files", "events", "evidence", "jobs",
          "event_enrichments")
DERIVED_TABLES = frozenset({"event_locations", "location_index_state", "media_object_index",
                          "media_file_index", "media_path_suffixes", "media_file_state",
                          "media_source_generation", "event_media_refs", "media_event_state"})
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class SnapshotError(RuntimeError):
    """A snapshot is incomplete, incompatible, unsafe, or failed verification."""


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _no_links(path: Path) -> Path:
    result = Path(os.path.abspath(path))
    for part in (result, *result.parents):
        if part.is_symlink():
            raise SnapshotError("Snapshot paths cannot contain symlinks")
    return result


def _member(root: Path, name: str) -> Path:
    if not isinstance(name, str) or "\\" in name or "\x00" in name:
        raise SnapshotError("Unsafe snapshot member path")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or ":" in name or relative.as_posix() != name):
        raise SnapshotError("Unsafe snapshot member path")
    return _no_links(root.joinpath(*relative.parts))


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _columns(connection: sqlite3.Connection) -> dict:
    result = {}
    present = {row[1] for row in connection.execute("PRAGMA table_list")
               if row[2] == "table" and not row[1].startswith("sqlite_")}
    if not set(TABLES).issubset(present) or present - set(TABLES) - DERIVED_TABLES:
        raise SnapshotError("Catalog contains missing or unsupported application tables")
    # Only actual installed cache schemas may be omitted. A custom table using
    # a familiar cache name must not silently lose persistent data.
    with sqlite3.connect(":memory:") as expected:
        _initialize_schema(expected)
        for table in present & DERIVED_TABLES:
            actual_columns = [list(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            expected_columns = [list(row) for row in expected.execute(f'PRAGMA table_info("{table}")')]
            actual_contract = _schema_contract({table: actual_columns})
            expected_contract = _schema_contract({table: expected_columns})
            # media.ensure_schema has one known additive cache migration. The
            # old cache is safe to omit only when every original column still
            # matches exactly; arbitrary missing/extra columns remain errors.
            legacy_media_cache = (
                table == "media_event_state"
                and len(expected_columns) == 6
                and expected_columns[-1][1:] == ["evidence_count", "INTEGER", 1, "0", 0]
                and actual_contract == _schema_contract({table: expected_columns[:-1]})
            )
            if actual_contract != expected_contract and not legacy_media_cache:
                raise SnapshotError("A derived table has an unsupported schema")
    for table in TABLES:
        result[table] = [list(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
    return result


def _initialize_schema(connection: sqlite3.Connection) -> None:
    from .enrichment import ensure_schema as ensure_enrichment_schema
    from .locations import ensure_schema as locations_schema
    from .media import ensure_schema as media_schema
    connection.executescript(SCHEMA)
    ensure_enrichment_schema(connection)
    locations_schema(connection)
    media_schema(connection)


def _stat(path: Path) -> tuple:
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _object_path(digest: str) -> str:
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SnapshotError("Invalid object hash")
    return f"objects/sha256/{digest[:2]}/{digest}"


def _schema_contract(schema: dict) -> dict:
    # Every column is supplied explicitly on restore. A changed insertion
    # default (such as the configured account label) is not a format migration.
    return {table: [column[:4] + column[5:] for column in columns]
            for table, columns in schema.items()}


class _Shards:
    def __init__(self, root: Path, prefix: str, limit: int):
        self.root, self.prefix, self.limit = root, prefix, limit
        self.descriptors: list[dict] = []
        self.handle = None
        self.path: Path | None = None
        self.bytes = self.lines = self.rows = 0

    def _close(self) -> None:
        if self.handle is None:
            return
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        assert self.path is not None
        self.descriptors.append({"path": self.path.relative_to(self.root).as_posix(),
                                 "bytes": self.bytes, "lines": self.lines,
                                 "sha256": _digest(self.path)})
        self.handle = None
        self.bytes = self.lines = 0

    def _line(self, line: bytes) -> None:
        if len(line) > self.limit:
            raise SnapshotError("A snapshot fragment exceeds its shard bound")
        if self.handle is not None and self.bytes + len(line) > self.limit:
            self._close()
        if self.handle is None:
            self.path = self.root / f"{self.prefix}-{len(self.descriptors):05d}.jsonl"
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.handle = self.path.open("xb")
            os.chmod(self.path, 0o600)
        self.handle.write(line)
        self.bytes += len(line)
        self.lines += 1

    def add(self, record: dict) -> None:
        line = _json({"kind": "row", "value": record})
        if len(line) <= self.limit:
            self._line(line)
        else:
            # A huge source_ready ledger entry can exceed the file-size bound.
            # Lossless fragments keep each JSONL shard bounded without truncation.
            raw = _json(record)
            digest = hashlib.sha256(raw).hexdigest()
            chunk_bytes = ((self.limit - 512) // 4) * 3
            parts = (len(raw) + chunk_bytes - 1) // chunk_bytes
            for index, start in enumerate(range(0, len(raw), chunk_bytes)):
                self._line(_json({"kind": "fragment", "index": index, "parts": parts,
                                  "sha256": digest, "bytes": len(raw),
                                  "data": base64.b64encode(raw[start:start + chunk_bytes]).decode("ascii")}))
        self.rows += 1

    def finish(self) -> dict:
        self._close()
        return {"rows": self.rows, "shards": self.descriptors}


def _coverage(connection: sqlite3.Connection) -> list[dict]:
    result = []
    for source in connection.execute("SELECT id,name,status FROM sources ORDER BY id"):
        sid, name, status = source
        row = connection.execute("SELECT count(*),min(occurred_at),max(occurred_at),"
                                 "sum(occurred_at IS NULL) FROM events WHERE source_id=?", (sid,)).fetchone()
        states = dict(connection.execute("SELECT parse_status,count(*) FROM files WHERE source_id=? "
                                         "GROUP BY parse_status ORDER BY parse_status", (sid,)))
        result.append({"source_id": sid, "name": name, "status": status,
                       "events": row[0], "first_observed": row[1], "last_observed": row[2],
                       "undated_events": row[3] or 0, "files_by_parse_status": states,
                       "continuous_coverage_asserted": False})
    return result


def export_snapshot(database: Path, destination: Path, *, manifests_dir: Path | None = None,
                    app_revision: str | None = None,
                    max_shard_bytes: int = MAX_SHARD_BYTES) -> dict:
    """Create a new private snapshot directory; never copy or read raw CAS files.

    SQLite's backup API fixes the catalog cut, including committed WAL contents.
    Existing destinations are never replaced. Snapshot contents are deterministic
    for unchanged catalog/ledger inputs, including shard names and manifest hash.
    """
    if not 1024 <= max_shard_bytes <= MAX_SHARD_BYTES:
        raise SnapshotError("Shard bound must be between 1 KiB and 20 MiB")
    database, destination = _no_links(database), _no_links(destination)
    if not database.is_file() or destination.exists():
        raise SnapshotError("Source database must exist and snapshot destination must be new")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_root = _no_links(manifests_dir) if manifests_dir is not None else None
    if ledger_root is not None and not ledger_root.is_dir():
        raise SnapshotError("Ledger directory does not exist")
    ledger_paths = sorted(ledger_root.glob("*.jsonl")) if ledger_root else []
    ledger_stats = {p.name: _stat(_no_links(p)) for p in ledger_paths}
    with tempfile.TemporaryDirectory(prefix=".snapshot-build-", dir=destination.parent) as temporary:
        work = Path(temporary)
        output = work / "snapshot"
        output.mkdir(mode=0o700)
        backup_path = work / "catalog-cut.sqlite3"
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        cut = sqlite3.connect(backup_path)
        os.chmod(backup_path, 0o600)
        try:
            source.backup(cut)
        except BaseException:
            cut.close()
            raise
        finally:
            source.close()
        try:
            from .enrichment import ensure_schema as ensure_enrichment_schema
            ensure_enrichment_schema(cut)
            cut.commit()
            if cut.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SnapshotError("Source catalog failed integrity_check")
            if cut.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SnapshotError("Source catalog has broken evidence references")
            schema = _columns(cut)
            manifest = {"format": FORMAT, "format_version": VERSION,
                        "private_content": True, "raw_objects_backed_up": False,
                        "excluded": ["original_source_and_media_bytes", "credentials", "dashboard_builds"],
                        "derived_tables_not_exported": sorted(DERIVED_TABLES | {"events_fts"}),
                        "app_revision": app_revision,
                        "max_shard_bytes": max_shard_bytes,
                        "schema": {"user_version": cut.execute("PRAGMA user_version").fetchone()[0],
                                   "tables": schema}, "tables": {}, "ledgers": [],
                        "coverage": _coverage(cut)}
            for table in TABLES:
                columns = [column[1] for column in schema[table]]
                primary = [column[1] for column in sorted(schema[table], key=lambda c: c[5]) if column[5]]
                order = ",".join(f'"{name}"' for name in (primary or columns))
                writers: dict[str, _Shards] = {}
                for row in cut.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
                    record = dict(zip(columns, row))
                    if table == "objects":
                        record["path"] = _object_path(record["sha256"])
                    key = _json([record[name] for name in (primary or columns)])
                    bucket = hashlib.sha256(key).hexdigest()[0]
                    writer = writers.setdefault(bucket, _Shards(output, f"tables/{table}/{bucket}", max_shard_bytes))
                    writer.add(record)
                groups = [writers[bucket].finish() for bucket in sorted(writers)]
                manifest["tables"][table] = {"columns": columns, "rows": sum(g["rows"] for g in groups),
                                              "groups": groups}
            object_count, object_bytes = cut.execute("SELECT count(*),coalesce(sum(bytes),0) FROM objects").fetchone()
            manifest["raw_object_inventory"] = {"table": "objects", "count": object_count,
                                                 "bytes": object_bytes, "path_base": "vaultroot",
                                                 "content_included": False}
            for number, path in enumerate(ledger_paths):
                writer = _Shards(output, f"ledgers/{number:06d}", max_shard_bytes)
                last = None
                try:
                    with path.open("rb") as handle:
                        for line in handle:
                            if not line.endswith(b"\n"):
                                raise SnapshotError("Ledger has an incomplete final record")
                            last = json.loads(line)
                            if not isinstance(last, dict):
                                raise SnapshotError("Ledger contains a non-object record")
                            if last.get("kind") == "object":
                                last = {**last, "path": _object_path(last.get("sha256"))}
                            writer.add(last)
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise SnapshotError("Ledger contains invalid JSON") from error
                descriptor = writer.finish()
                manifest["ledgers"].append({"name": path.name, "source_sha256": _digest(path),
                                            "source_bytes": ledger_stats[path.name][2],
                                            "complete": bool(last and last.get("kind") == "footer"),
                                            **descriptor})
            if ledger_root is not None:
                current = sorted(ledger_root.glob("*.jsonl"))
                if ({p.name: _stat(_no_links(p)) for p in current} != ledger_stats):
                    raise SnapshotError("Import ledgers changed during snapshot; retry with the worker stopped")
            manifest["schema"]["sha256"] = hashlib.sha256(_json(manifest["schema"])).hexdigest()
            payload = _json(manifest)
            _write(output / "manifest.json", payload)
            _write(output / "manifest.sha256", (hashlib.sha256(payload).hexdigest() + "\n").encode())
            fsync_directory(output)
            os.rename(output, destination)
            fsync_directory(destination.parent)
            return manifest
        finally:
            cut.close()


def _descriptors(manifest: dict) -> Iterator[dict]:
    for table in TABLES:
        for group in manifest["tables"][table]["groups"]:
            yield from group["shards"]
    for ledger in manifest["ledgers"]:
        yield from ledger["shards"]


def _load_manifest(root: Path) -> dict:
    root = _no_links(root)
    payload = _member(root, "manifest.json").read_bytes()
    expected = _member(root, "manifest.sha256").read_text().strip()
    if not _HASH.fullmatch(expected) or hashlib.sha256(payload).hexdigest() != expected:
        raise SnapshotError("Snapshot manifest hash does not match")
    try:
        result = json.loads(payload)
        if (result["format"] != FORMAT or result["format_version"] != VERSION
                or result["raw_objects_backed_up"] is not False
                or set(result["tables"]) != set(TABLES)
                or not 1024 <= result["max_shard_bytes"] <= MAX_SHARD_BYTES):
            raise SnapshotError("Unsupported snapshot format")
        schema = dict(result["schema"])
        schema_hash = schema.pop("sha256")
        if hashlib.sha256(_json(schema)).hexdigest() != schema_hash:
            raise SnapshotError("Snapshot schema checksum does not match")
        seen = set()
        for descriptor in _descriptors(result):
            name = descriptor["path"]
            if name in seen or not name.endswith(".jsonl") or not name.startswith(("tables/", "ledgers/")):
                raise SnapshotError("Duplicate or invalid snapshot shard")
            seen.add(name)
            path = _member(root, name)
            if (not path.is_file() or not 0 < descriptor["bytes"] <= result["max_shard_bytes"]
                    or path.stat().st_size != descriptor["bytes"]
                    or not _HASH.fullmatch(descriptor["sha256"])
                    or _digest(path) != descriptor["sha256"]):
                raise SnapshotError("Snapshot shard failed integrity verification")
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotError("Malformed snapshot manifest") from error


def _records(root: Path, descriptor: dict) -> Iterator[dict]:
    count = 0
    fragments: list[bytes] = []
    fragment_info = None
    for shard in descriptor["shards"]:
        lines = 0
        size = 0
        digest = hashlib.sha256()
        with _member(root, shard["path"]).open("rb") as handle:
            for line in handle:
                lines += 1
                size += len(line)
                digest.update(line)
                if not line.endswith(b"\n"):
                    raise SnapshotError("Snapshot shard has an incomplete record")
                try:
                    envelope = json.loads(line)
                    if envelope["kind"] == "row" and not fragments:
                        record = envelope["value"]
                    elif envelope["kind"] == "fragment":
                        info = (envelope["parts"], envelope["sha256"], envelope["bytes"])
                        if (envelope["index"] != len(fragments) or envelope["parts"] <= 0
                                or fragment_info is not None and info != fragment_info):
                            raise SnapshotError("Snapshot fragments are out of order")
                        fragment_info = info
                        fragments.append(base64.b64decode(envelope["data"], validate=True))
                        if len(fragments) < info[0]:
                            continue
                        raw = b"".join(fragments)
                        if len(raw) != info[2] or hashlib.sha256(raw).hexdigest() != info[1]:
                            raise SnapshotError("Snapshot fragment hash does not match")
                        record = json.loads(raw)
                        fragments, fragment_info = [], None
                    else:
                        raise SnapshotError("Malformed snapshot row envelope")
                    if not isinstance(record, dict):
                        raise SnapshotError("Snapshot record is not an object")
                except (KeyError, TypeError, ValueError) as error:
                    raise SnapshotError("Invalid snapshot JSONL record") from error
                count += 1
                yield record
        if lines != shard["lines"] or size != shard["bytes"] or digest.hexdigest() != shard["sha256"]:
            raise SnapshotError("Snapshot shard changed or its line count does not match")
    if fragments or count != descriptor["rows"]:
        raise SnapshotError("Snapshot record count does not match")


def restore_snapshot(snapshot_dir: Path, database: Path, *, vaultroot: Path | None = None,
                     manifests_dir: Path | None = None) -> dict:
    """Validate a snapshot and atomically publish a new database; no raw copies.

    Restored object references point into vaultroot. They may legitimately be
    missing until the independent local archive is mounted or copied separately.
    Never overwrite an existing database, even an apparently empty one.
    """
    root, database = _no_links(snapshot_dir), _no_links(database)
    vault = _no_links(vaultroot or database.parent)
    ledger_dest = _no_links(manifests_dir) if manifests_dir is not None else None
    if any(Path(str(database) + suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise SnapshotError("Restore destination database already exists")
    if ledger_dest is not None and ledger_dest.exists():
        raise SnapshotError("Restore ledger destination already exists")
    manifest = _load_manifest(root)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".snapshot-restore-", dir=database.parent) as temporary:
        stage = Path(temporary)
        tempdb = stage / "catalog.sqlite3"
        connection = sqlite3.connect(tempdb)
        os.chmod(tempdb, 0o600)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            _initialize_schema(connection)
            schema = _columns(connection)
            if (_schema_contract(schema) != _schema_contract(manifest["schema"]["tables"]) or
                    connection.execute("PRAGMA user_version").fetchone()[0] != manifest["schema"]["user_version"]):
                raise SnapshotError("Snapshot schema is incompatible with this application")
            with connection:
                for table in TABLES:
                    descriptor = manifest["tables"][table]
                    columns = [column[1] for column in schema[table]]
                    if columns != descriptor["columns"]:
                        raise SnapshotError("Snapshot column contract does not match")
                    names = ",".join(f'"{name}"' for name in columns)
                    sql = f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})'
                    rows = 0
                    for group in descriptor["groups"]:
                        for record in _records(root, group):
                            if set(record) != set(columns):
                                raise SnapshotError("Snapshot row fields do not match schema")
                            if table == "objects":
                                digest = record["sha256"]
                                expected = _object_path(digest)
                                if record["path"] != expected:
                                    raise SnapshotError("Unsafe or nonportable object path")
                                record["path"] = str(_member(vault, expected))
                            connection.execute(sql, [record[name] for name in columns])
                            rows += 1
                    if rows != descriptor["rows"]:
                        raise SnapshotError("Snapshot table count does not match")
                connection.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SnapshotError("Restored catalog has broken references")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SnapshotError("Restored catalog failed integrity_check")
            if _coverage(connection) != manifest["coverage"]:
                raise SnapshotError("Restored coverage differs from snapshot")
            count, size = connection.execute("SELECT count(*),coalesce(sum(bytes),0) FROM objects").fetchone()
            if manifest["raw_object_inventory"] != {"table": "objects", "count": count,
                    "bytes": size, "path_base": "vaultroot", "content_included": False}:
                raise SnapshotError("Restored raw-object inventory differs from snapshot")
            stage_ledgers = stage / "manifests"
            stage_ledgers.mkdir(mode=0o700)
            names = set()
            for ledger in manifest["ledgers"]:
                name = ledger["name"]
                if name in names or PurePosixPath(name).name != name or not name.endswith(".jsonl"):
                    raise SnapshotError("Unsafe ledger name")
                names.add(name)
                target = _member(stage_ledgers, name)
                with target.open("xb") as handle:
                    os.chmod(target, 0o600)
                    for record in _records(root, ledger):
                        if record.get("kind") == "object":
                            expected = _object_path(record.get("sha256"))
                            if record.get("path") != expected:
                                raise SnapshotError("Unsafe ledger object path")
                            record = {**record, "path": str(_member(vault, expected))}
                        handle.write(_json(record))
                    handle.flush()
                    os.fsync(handle.fileno())
        except sqlite3.Error as error:
            raise SnapshotError("Snapshot records violate the database schema") from error
        finally:
            connection.close()
        with tempdb.open("rb") as handle:
            os.fsync(handle.fileno())
        # link() publishes without replacing an existing database created by a race.
        os.link(tempdb, database)
        try:
            if ledger_dest is not None:
                ledger_dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(stage_ledgers, ledger_dest)
                fsync_directory(ledger_dest.parent)
            fsync_directory(database.parent)
        except BaseException:
            database.unlink()
            raise
    return {"database": str(database), "tables": {t: manifest["tables"][t]["rows"] for t in TABLES},
            "raw_objects_backed_up": False, "raw_objects_restored": False,
            "raw_object_inventory": manifest["raw_object_inventory"],
            "manifests_restored": len(manifest["ledgers"]) if ledger_dest is not None else 0}
