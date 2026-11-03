"""Backup safety tests use synthetic records and original-byte fixtures only."""

import hashlib
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from biography import backups
from biography.db import init_db
from biography.storage import ImportPaused, IntegrityError


def seed(root):
    vault = root / "source-vault"
    vault.mkdir()
    database = vault / "catalog.sqlite3"
    init_db(database)
    objects = []
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                           ("source", "Synthetic source", "2024-01-01", "2024-01-01"))
        connection.execute("INSERT INTO imports(id,source_id,status,started_at) VALUES(?,?,?,?)",
                           ("import", "source", "running", "2024-01-01"))
        for index, payload in enumerate((b"synthetic original photograph\x00\xff", b"synthetic original export")):
            digest = hashlib.sha256(payload).hexdigest()
            # Historical CAS layout differences remain supported within objects/.
            path = vault / "objects" / ("legacy-layout" if index else "sha256") / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            objects.append((digest, path, payload))
            connection.execute("INSERT INTO objects VALUES(?,?,?,?)", (digest, len(payload), str(path), "2024-01-01"))
            connection.execute("INSERT INTO files(id,source_id,import_id,original_path,object_hash,bytes,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (f"file-{index}", "source", "import", f"export-{index}",
                                                       digest, len(payload), "2024-01-01"))
            connection.execute("INSERT INTO events(id,source_id,title,body,category,created_at) VALUES(?,?,?,?,?,?)",
                               (f"event-{index}", "source", "Synthetic event", "searchable original evidence", "note", "2024-01-01"))
            connection.execute("INSERT INTO evidence VALUES(?,?,?,?)", (f"event-{index}", f"file-{index}", "line:1", "fixture.v1"))
    return database, vault, objects


def create(database, vault, output, **options):
    return backups.create_backup(database, vault, output, reserve_bytes=0, capacity_paths=(), **options)


def cas(root, digest):
    return root / "objects" / "sha256" / digest[:2] / digest


def write_ledger(vault, objects, name="synthetic.jsonl", complete=True):
    directory = vault / "manifests"
    directory.mkdir(exist_ok=True)
    rows = [{"kind": "header", "import_id": "synthetic-import", "source": "synthetic-source"}]
    for digest, path, payload in objects:
        rows.append({"kind": "object", "sha256": digest, "size": len(payload), "path": str(path)})
    rows.append({"kind": "archive_entry", "type": "2", "linkname": "synthetic-target",
                 "pax_headers": {"comment": "retained header evidence"}})
    # A discarded outer container is recorded but intentionally not a CAS object.
    rows.append({"kind": "source_ready", "sha256": "0" * 64, "objects": []})
    if complete:
        rows.append({"kind": "footer", "status": "complete"})
    path = directory / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return directory, path, rows


def test_complete_ledgers_are_bound_read_back_and_restored_as_original_evidence(tmp_path):
    database, vault, objects = seed(tmp_path)
    ledger_root, ledger, rows = write_ledger(vault, objects)
    output = tmp_path / "backup"
    result = create(database, vault, output, manifests_dir=ledger_root)
    assert result["status"] == "complete"
    assert result["retained_evidence_complete"] is True
    assert result["ledgers"]["catalog_sha256"] == result["catalog"]["sha256"]
    assert result["ledgers"]["catalog_objects_without_complete_ledger"] == 0
    assert backups.verify_backup(output)["ledgers"]["records_verified"] == len(rows)
    expected = ledger.read_bytes()
    ledger.unlink()
    for _, path, _ in objects:
        path.unlink()
    restored = tmp_path / "restored"
    report = backups.restore_backup(output, restored, reserve_bytes=0, capacity_paths=())
    assert report["ledgers"]["files_verified"] == 1
    assert report["retained_evidence_complete"] is True
    assert (restored / "manifests" / ledger.name).read_bytes() == expected
    assert b"synthetic-target" in expected


def test_active_ledger_is_deferred_and_backup_and_restore_are_explicitly_partial(tmp_path):
    database, vault, objects = seed(tmp_path)
    ledger_root, ledger, _ = write_ledger(vault, objects, complete=False)
    output = tmp_path / "backup"
    result = create(database, vault, output, manifests_dir=ledger_root)
    assert result["status"] == "partial"
    assert result["catalog_and_objects_complete"] is True
    assert result["retained_evidence_complete"] is False
    assert result["ledgers"]["deferred"] == [{"name": ledger.name, "reason": "ledger_not_complete"}]
    assert result["ledgers"]["included"] == []
    assert backups.verify_backup(output)["status"] == "partial_verified"
    restored = tmp_path / "restored"
    report = backups.restore_backup(output, restored, reserve_bytes=0, capacity_paths=())
    assert report["status"] == "partial_restored_and_verified"
    assert not (restored / "manifests" / ledger.name).exists()


def test_ledger_ahead_of_fixed_inventory_is_deferred_not_falsely_complete(tmp_path):
    database, vault, objects = seed(tmp_path)
    unknown = ("0" * 64, vault / "objects" / "not-in-cut", b"unknown")
    ledger_root, ledger, _ = write_ledger(vault, objects + [unknown])
    result = create(database, vault, tmp_path / "backup", manifests_dir=ledger_root)
    assert result["status"] == "partial"
    assert result["ledgers"]["deferred"] == [{"name": ledger.name, "reason": "object_outside_fixed_catalog"}]
    assert result["ledgers"]["catalog_objects_without_complete_ledger"] == 2


def test_missing_ledger_coverage_does_not_claim_complete_retained_evidence(tmp_path):
    database, vault, objects = seed(tmp_path)
    ledger_root, _, _ = write_ledger(vault, objects[:1])
    result = create(database, vault, tmp_path / "backup", manifests_dir=ledger_root)
    assert result["status"] == "partial"
    assert result["ledgers"]["catalog_objects_without_complete_ledger"] == 1


def test_ledger_changed_after_hash_is_deferred_safely(tmp_path, monkeypatch):
    database, vault, objects = seed(tmp_path)
    ledger_root, ledger, _ = write_ledger(vault, objects)
    original = backups.hash_file
    changed = False
    def mutate(path, **options):
        nonlocal changed
        result = original(path, **options)
        if path == ledger and not changed:
            with ledger.open("ab") as handle:
                handle.write(b'{"kind":"later"}\n')
            changed = True
        return result
    monkeypatch.setattr(backups, "hash_file", mutate)
    result = create(database, vault, tmp_path / "backup", manifests_dir=ledger_root)
    assert result["status"] == "partial"
    assert "ledger_changed" in result["ledgers"]["deferred"][0]["reason"]


def test_corrupt_backed_up_ledger_fails_verification_and_restore_without_catalog(tmp_path):
    database, vault, objects = seed(tmp_path)
    ledger_root, ledger, _ = write_ledger(vault, objects)
    output = tmp_path / "backup"
    create(database, vault, output, manifests_dir=ledger_root)
    saved = output / "manifests" / ledger.name
    saved.write_bytes(saved.read_bytes().replace(b"synthetic-target", b"corrupted-target"))
    with pytest.raises(IntegrityError, match="Ledger failed"):
        backups.verify_backup(output)
    restored = tmp_path / "restored"
    with pytest.raises(IntegrityError):
        backups.restore_backup(output, restored, reserve_bytes=0, capacity_paths=())
    assert not (restored / "catalog.sqlite3").exists()


def test_large_ledger_inventory_remains_readable_on_verify_resume_and_restore(tmp_path, monkeypatch):
    database, vault, _ = seed(tmp_path)
    ledgers = vault / "manifests"
    ledgers.mkdir()
    def large_plan(root, catalog, catalog_hash):
        return {"requested": True, "catalog_sha256": catalog_hash, "included": [],
                "deferred": [{"name": f"synthetic-{index:05d}.jsonl", "reason": "incomplete_" + "x" * 200}
                             for index in range(6000)],
                "complete": False, "catalog_objects_without_complete_ledger": 2}
    monkeypatch.setattr(backups, "_ledger_plan", large_plan)
    output = tmp_path / "backup"
    result = create(database, vault, output, manifests_dir=ledgers)
    assert (output / "manifest.json").stat().st_size > 1024**2
    assert len(backups.verify_backup(output)["ledgers"]["deferred"]) == 6000
    assert create(database, vault, output, manifests_dir=ledgers)["snapshot_id"] == result["snapshot_id"]
    restored = tmp_path / "restored"
    report = backups.restore_backup(output, restored, reserve_bytes=0, capacity_paths=())
    assert len(report["ledgers"]["deferred"]) == 6000


def test_complete_backup_restores_original_bytes_remapped_paths_and_provenance(tmp_path):
    database, vault, objects = seed(tmp_path)
    output = tmp_path / "backup"
    manifest = create(database, vault, output)
    assert manifest["status"] == "complete"
    assert manifest["physical_independence_asserted"] is False
    assert manifest["watermark"]["counts"]["evidence"] == 2
    assert backups.verify_backup(output)["objects_verified"] == 2
    # Proves the restore uses backup originals rather than the active vault.
    for _, path, _ in objects:
        path.unlink()
    restored = tmp_path / "restored-vault"
    report = backups.restore_backup(output, restored, reserve_bytes=0, capacity_paths=())
    assert report["original_bytes_read_back"] is True
    assert report["all_object_paths_remapped"] is True
    for digest, _, payload in objects:
        assert cas(restored, digest).read_bytes() == payload
    with sqlite3.connect(database) as before, sqlite3.connect(restored / "catalog.sqlite3") as after:
        for table in ("sources", "imports", "files", "events", "evidence"):
            assert before.execute(f"SELECT * FROM {table}").fetchall() == after.execute(f"SELECT * FROM {table}").fetchall()
        for digest, path in after.execute("SELECT sha256,path FROM objects"):
            assert path == str(cas(restored, digest))
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []
        assert after.execute("SELECT count(*) FROM events_fts WHERE events_fts MATCH 'searchable'").fetchone()[0] == 2
    assert report["backup_catalog_sha256"] != report["restored_catalog_sha256"]
    assert all(not stat.S_IMODE(path.stat().st_mode) & 0o077 for path in output.rglob("*"))


@pytest.mark.parametrize("broken_reference", [False, True])
def test_bounded_validation_cache_keeps_catalog_immutable_and_fk_checks_enforced(tmp_path, broken_reference):
    database, _, _ = seed(tmp_path)
    if broken_reference:
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE files SET object_hash=?", ("0" * 64,))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    connection = backups._connect(database)
    try:
        if broken_reference:
            with pytest.raises(backups.BackupError, match="foreign_key_check"):
                backups._checks(connection)
        else:
            backups._checks(connection)
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -256 * 1024
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        connection.close()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_resume_retains_fixed_cut_and_independently_rehashes_existing_objects(tmp_path, monkeypatch):
    database, vault, objects = seed(tmp_path)
    output = tmp_path / "backup"
    original_copy = backups._copy_object
    calls = []
    def interrupt_second(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return original_copy(*args, **kwargs)
    monkeypatch.setattr(backups, "_copy_object", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        create(database, vault, output)
    cut_hash = hashlib.sha256((output / "catalog.sqlite3").read_bytes()).hexdigest()
    assert json.loads((output / "status.json").read_text())["status"] == "paused"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                           ("late", "source", "Imported after the cut", "note", "2024-01-01"))
    # A stale interrupted partial is safely discarded; a complete object is read back.
    remaining = next(digest for digest, _, _ in objects if not cas(output, digest).exists())
    partial = cas(output, remaining).with_name(remaining + ".part")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"interrupted")
    monkeypatch.setattr(backups, "_copy_object", original_copy)
    verified_paths = []
    original_verify = backups._verify_object
    def track(*args):
        verified_paths.append(args[0])
        return original_verify(*args)
    monkeypatch.setattr(backups, "_verify_object", track)
    result = create(database, vault, output)
    assert result["watermark"]["counts"]["events"] == 2
    assert result["catalog"]["sha256"] == cut_hash
    assert not partial.exists()
    assert any(path == cas(output, calls[0]) for path in verified_paths)
    assert json.loads((output / "status.json").read_text())["objects_newly_copied"] == 1


def test_existing_mismatched_object_is_never_overwritten(tmp_path):
    database, vault, objects = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    digest = objects[0][0]
    cas(output, digest).write_bytes(b"wrong hash")
    with pytest.raises(IntegrityError):
        create(database, vault, output)
    assert cas(output, digest).read_bytes() == b"wrong hash"
    with pytest.raises(backups.BackupError, match="not complete"):
        backups.verify_backup(output)


def test_missing_published_catalog_is_never_replaced_by_newer_source(tmp_path):
    database, vault, _ = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    (output / "catalog.sqlite3").unlink()
    with pytest.raises(backups.BackupError, match="refusing a newer cut"):
        create(database, vault, output)
    assert not (output / "catalog.sqlite3").exists()


def test_fixed_catalog_corruption_fails_before_using_object_inventory(tmp_path):
    database, vault, _ = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    with (output / "catalog.sqlite3").open("r+b") as handle:
        handle.seek(100)
        handle.write(b"bad catalog")
    with pytest.raises(IntegrityError, match="catalog|Catalog"):
        create(database, vault, output)


def test_competing_writer_pauses_without_modifying_backup(tmp_path):
    database, vault, _ = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    before = (output / "manifest.json").read_bytes()
    with backups._writer_lock(output, False):
        with pytest.raises(ImportPaused, match="backup_writer_active"):
            create(database, vault, output)
    assert (output / "manifest.json").read_bytes() == before


def test_resume_removes_only_known_partials_after_verifying_published_files(tmp_path):
    database, vault, objects = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    digest = objects[0][0]
    object_partial = cas(output, digest).with_name(digest + ".part")
    object_partial.write_bytes(b"left after publication")
    (output / "catalog.sqlite3.part").write_bytes(b"left after publication")
    create(database, vault, output)
    assert not object_partial.exists()
    assert not (output / "catalog.sqlite3.part").exists()
    assert backups.verify_backup(output)["objects_verified"] == 2


def test_owner_permissions_fail_closed_when_chmod_is_ineffective(tmp_path, monkeypatch):
    public_directory = tmp_path / "unenforceable-permissions"
    public_directory.mkdir(mode=0o755)
    public_directory.chmod(0o755)
    monkeypatch.setattr(backups.os, "chmod", lambda *args: None)
    with pytest.raises(backups.BackupError, match="Owner-only permissions"):
        backups._private(public_directory, True, False)


def test_wrong_source_hash_is_not_published(tmp_path):
    database, vault, objects = seed(tmp_path)
    digest, path, payload = objects[0]
    path.write_bytes(b"x" * len(payload))
    output = tmp_path / "backup"
    with pytest.raises(IntegrityError, match="declared hash"):
        create(database, vault, output)
    assert not cas(output, digest).exists()
    assert not list(output.rglob("*.part"))


@pytest.mark.parametrize("object_path", ["../outside", "/outside/object", "objects/../outside"])
def test_source_catalog_path_traversal_is_rejected(tmp_path, object_path):
    database, vault, _ = seed(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE objects SET path=?", (object_path,))
    with pytest.raises(backups.BackupError, match="traversal|escapes"):
        create(database, vault, tmp_path / "backup")


def test_symlink_objects_and_manifest_catalog_traversal_are_rejected(tmp_path):
    database, vault, objects = seed(tmp_path)
    digest, path, payload = objects[0]
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(backups.BackupError, match="symlink"):
        create(database, vault, tmp_path / "backup")
    path.unlink()
    path.write_bytes(payload)
    output = tmp_path / "second-backup"
    create(database, vault, output)
    manifest_path = output / "manifest.json"
    value = json.loads(manifest_path.read_text())
    value["catalog"]["path"] = "../catalog.sqlite3"
    manifest_path.write_text(json.dumps(value))
    with pytest.raises(backups.BackupError, match="Unsafe catalog"):
        backups.restore_backup(output, tmp_path / "restore", reserve_bytes=0, capacity_paths=())
    assert not (tmp_path / "restore").exists()


def test_live_wal_cut_includes_committed_but_excludes_uncommitted_rows(tmp_path):
    database, vault, _ = seed(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                   ("committed", "source", "Committed WAL event", "note", "2024-01-01"))
    writer.commit()
    writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                   ("pending", "source", "Pending event", "note", "2024-01-01"))
    try:
        output = tmp_path / "backup"
        create(database, vault, output)
        with sqlite3.connect(output / "catalog.sqlite3") as cut:
            assert cut.execute("SELECT id FROM events WHERE id='committed'").fetchone()
            assert not cut.execute("SELECT id FROM events WHERE id='pending'").fetchone()
        assert writer.in_transaction
    finally:
        writer.rollback()
        writer.close()


def test_prepared_read_snapshot_can_be_copied_after_later_writer_commit(tmp_path, monkeypatch):
    database, vault, _ = seed(tmp_path)
    original_connect = backups._connect
    prepared = original_connect(database)
    prepared.execute("BEGIN")
    assert prepared.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    with sqlite3.connect(database) as writer:
        writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                       ("later", "source", "Committed after read snapshot", "note", "2024-01-01"))
    monkeypatch.setattr(backups, "_connect", lambda path: prepared if path == database else original_connect(path))
    result = create(database, vault, tmp_path / "backup")
    assert result["watermark"]["counts"]["events"] == 2


def test_low_space_pauses_before_cut_and_resume_restarts_only_incomplete_cut(tmp_path):
    database, vault, _ = seed(tmp_path)
    output = tmp_path / "backup"
    def no_space(additional):
        raise ImportPaused("insufficient_disk_space", required_bytes=additional)
    with pytest.raises(ImportPaused):
        create(database, vault, output, space_guard=no_space)
    assert not (output / "catalog.sqlite3").exists()
    assert json.loads((output / "manifest.json").read_text())["status"] == "cut_pending"
    (output / "catalog.sqlite3.part").write_bytes(b"interrupted database")
    assert create(database, vault, output)["status"] == "complete"
    restored = tmp_path / "restore"
    with pytest.raises(ImportPaused):
        backups.restore_backup(output, restored, space_guard=no_space)
    assert not restored.exists()


def test_snapshot_timeout_leaves_no_published_cut(tmp_path, monkeypatch):
    database, vault, _ = seed(tmp_path)
    ticks = iter([0, 0, 10, 10, 10, 10, 10, 10])
    monkeypatch.setattr(backups.time, "monotonic", lambda: next(ticks, 10))
    output = tmp_path / "backup"
    with pytest.raises(ImportPaused, match="snapshot_timeout"):
        create(database, vault, output, snapshot_timeout=1)
    assert not (output / "catalog.sqlite3").exists()


def test_snapshot_capacity_checks_are_batched_but_initial_reservation_covers_all_pages(tmp_path, monkeypatch):
    class Cursor:
        def __init__(self, value):
            self.value = value
        def fetchone(self):
            return (self.value,)
    class Source:
        def execute(self, query):
            return Cursor(4096 if "page_size" in query else 20 * 1024)
        def backup(self, target, *, pages, progress, sleep):
            for remaining in range(19 * 1024, -1, -1024):
                progress(0, remaining, 20 * 1024)
        def close(self):
            pass
    monkeypatch.setattr(backups, "_connect", lambda _: Source())
    monkeypatch.setattr(backups.time, "monotonic", lambda: 0)
    checks = []
    backups._catalog_cut(tmp_path / "source.sqlite3", tmp_path / "cut.sqlite3", checks.append,
                         10, 1024, False, None)
    assert checks[0] == 80 * 1024**2
    assert len(checks) == 2


def test_wal_growth_budget_stops_online_cut(tmp_path, monkeypatch):
    database, vault, _ = seed(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    guards = []
    def guard(additional):
        guards.append(additional)
        # The first guard runs after measuring baseline WAL, before backup.
        if len(guards) == 1:
            writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                           ("growth", "source", "WAL growth", "note", "2024-01-01"))
            writer.commit()
    try:
        with pytest.raises(ImportPaused, match="snapshot_wal_growth"):
            create(database, vault, tmp_path / "backup", space_guard=guard, max_wal_growth_bytes=0)
    finally:
        writer.close()


def test_backup_and_restore_corruption_never_publish_restored_catalog(tmp_path):
    database, vault, objects = seed(tmp_path)
    output = tmp_path / "backup"
    create(database, vault, output)
    digest = objects[0][0]
    cas(output, digest).write_bytes(b"corruption")
    with pytest.raises(IntegrityError):
        backups.verify_backup(output)
    destination = tmp_path / "restored"
    with pytest.raises(IntegrityError):
        backups.restore_backup(output, destination, reserve_bytes=0, capacity_paths=())
    assert not (destination / "catalog.sqlite3").exists()
    assert objects[0][1].read_bytes() == objects[0][2]
    with pytest.raises(backups.BackupError, match="must be new"):
        backups.restore_backup(output, vault)


def test_native_acl_requires_explicit_evidence_and_is_not_engine_attested(tmp_path):
    database, vault, _ = seed(tmp_path)
    with pytest.raises(backups.BackupError, match="privacy evidence"):
        create(database, vault, tmp_path / "missing-evidence", externally_private=True)
    manifest = create(database, vault, tmp_path / "backup", externally_private=True,
                      privacy_evidence={"method": "synthetic native ACL inspection", "verified_by": "caller"})
    assert manifest["privacy"]["caller_supplied_evidence"]["verified_by"] == "caller"
    assert manifest["physical_independence_asserted"] is False


def test_capacity_paths_are_separate_bounds_not_summed(tmp_path, monkeypatch):
    host = tmp_path / "host-backing"
    host.mkdir()
    import shutil
    usage = shutil._ntuple_diskusage
    monkeypatch.setattr("biography.storage.shutil.disk_usage",
                        lambda path: usage(1000, 950, 50) if Path(path) == host else usage(1000, 0, 1000))
    guard = backups._space_guard(tmp_path / "backup", 40, (host,))
    guard(10)
    with pytest.raises(ImportPaused):
        guard(11)
    with pytest.raises(backups.BackupError, match="capacity path is unavailable"):
        backups._space_guard(tmp_path / "backup", 40, (tmp_path / "missing-host",))
