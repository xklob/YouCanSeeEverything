"""Synthetic-only Git LFS packaging and recovery safety tests."""

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from biography import backups, lfs_backup
from biography.db import init_db
from biography.storage import ImportPaused, IntegrityError


def fixture(tmp_path):
    vault = tmp_path / "source"
    vault.mkdir()
    database = vault / "catalog.sqlite3"
    init_db(database)
    payload = bytes(range(256)) * 20
    digest = hashlib.sha256(payload).hexdigest()
    original = vault / backups._digest_name(digest)
    original.parent.mkdir(parents=True)
    original.write_bytes(payload)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO objects VALUES(?,?,?,?)", (digest, len(payload), str(original), "2024-01-01"))
    backup = tmp_path / "backup"
    backups.create_backup(database, vault, backup, reserve_bytes=0, capacity_paths=())
    return backup, digest, payload


def stage(backup, output, **options):
    return lfs_backup.stage_lfs_backup(backup, output, chunk_bytes=1024, reserve_bytes=0,
                                      capacity_paths=(), **options)


def test_deterministic_chunked_staging_reassembles_and_verifies_original_bytes(tmp_path):
    backup, digest, payload = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    result = stage(backup, output, max_new_lfs_bytes=10**7)
    assert result["status"] == "complete"
    assert result["remote_upload_verified"] is False
    assert result["physical_independence_asserted"] is False
    assert result["components_deferred"] == 0
    assert result["full_candidate_new_lfs_bytes"] == result["new_lfs_bytes"]
    assert all(path.stat().st_size <= 1024 for path in (output / "lfs").rglob("*") if path.is_file())
    assert result["total_lfs_bytes"] == sum(path.stat().st_size for path in (output / "lfs").rglob("*") if path.is_file())
    package_path = output / result["package_manifest"]
    package_bytes = package_path.read_bytes()
    assert b"source_database" not in package_bytes
    assert b"source_vault" not in package_bytes
    assert b"lfs/** filter=lfs" in (output / ".gitattributes").read_bytes()
    assert b".lfs-backup-work/" in (output / ".gitignore").read_bytes()
    again = stage(backup, output, max_new_lfs_bytes=10**7)
    assert again["package_manifest"] == result["package_manifest"]
    assert again["newly_written_lfs_bytes"] == 0
    assert again["new_lfs_bytes"] == result["new_lfs_bytes"]
    assert package_path.read_bytes() == package_bytes
    restored = tmp_path / "reassembled"
    report = lfs_backup.reassemble_lfs_backup(output, result["package_manifest"], restored,
                                             reserve_bytes=0, capacity_paths=())
    assert report["status"] == "reassembled_and_verified"
    assert (restored / backups._digest_name(digest)).read_bytes() == payload
    assert (restored / "catalog.sqlite3").read_bytes() == (backup / "catalog.sqlite3").read_bytes()
    assert backups.verify_backup(restored)["objects_verified"] == 1


def test_different_destination_and_resumed_compression_are_byte_identical(tmp_path):
    backup, _, _ = fixture(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    a = stage(backup, first, max_new_lfs_bytes=10**7)
    b = stage(backup, second, max_new_lfs_bytes=10**7)
    assert a["package_manifest"] == b["package_manifest"]
    assert {p.relative_to(first).as_posix(): p.read_bytes() for p in (first / "lfs").rglob("*") if p.is_file()} == {
        p.relative_to(second).as_posix(): p.read_bytes() for p in (second / "lfs").rglob("*") if p.is_file()}


def test_zero_budget_publishes_no_lfs_payload_or_false_complete_manifest(tmp_path):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    result = stage(backup, output, max_new_lfs_bytes=0)
    assert result["status"] == "budget_exceeded"
    assert result["manifest_published"] is False
    assert result["components_deferred"] == 3
    assert not (output / "lfs").exists()
    assert not (output / "packages").exists()


def test_partial_budget_is_respected_and_partial_reassembly_is_refused(tmp_path):
    backup, _, _ = fixture(tmp_path)
    full = stage(backup, tmp_path / "full", max_new_lfs_bytes=10**7)
    output = tmp_path / "partial"
    budget = full["new_lfs_bytes"] - 2000
    partial = stage(backup, output, max_new_lfs_bytes=budget)
    assert partial["status"] == "partial"
    assert partial["new_lfs_bytes"] <= budget
    assert partial["components_deferred"] > 0
    with pytest.raises(lfs_backup.LFSBackupError, match="Partial LFS package"):
        lfs_backup.reassemble_lfs_backup(output, partial["package_manifest"], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_known_remote_hashes_are_explicit_quota_exemptions(tmp_path):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    full = stage(backup, output, max_new_lfs_bytes=10**7)
    hashes = frozenset(path.name for path in (output / "lfs").rglob("*") if path.is_file())
    free = stage(backup, output, max_new_lfs_bytes=0, known_lfs_hashes=hashes)
    assert free["status"] == "complete"
    assert free["new_lfs_bytes"] == 0
    assert free["total_lfs_bytes"] == full["total_lfs_bytes"]


def test_corrupt_existing_lfs_chunk_is_never_overwritten(tmp_path):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    stage(backup, output, max_new_lfs_bytes=10**7)
    path = next(path for path in (output / "lfs").rglob("*") if path.is_file())
    path.write_bytes(b"corrupted")
    with pytest.raises(IntegrityError):
        stage(backup, output, max_new_lfs_bytes=10**7)
    assert path.read_bytes() == b"corrupted"


def test_interrupted_encoding_resumes_and_keeps_all_publications_immutable(tmp_path, monkeypatch):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    original_encode = lfs_backup._encode
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return original_encode(*args, **kwargs)
    monkeypatch.setattr(lfs_backup, "_encode", interrupted)
    with pytest.raises(KeyboardInterrupt):
        stage(backup, output, max_new_lfs_bytes=10**7)
    assert not (output / "lfs").exists()
    monkeypatch.setattr(lfs_backup, "_encode", original_encode)
    result = stage(backup, output, max_new_lfs_bytes=10**7)
    assert result["status"] == "complete"
    assert not list(output.rglob("chunk-*.part"))


def test_interrupted_package_publication_resumes_without_partial_manifest(tmp_path, monkeypatch):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    original_link = lfs_backup.os.link
    def interrupt_manifest(source, target, *args, **kwargs):
        if str(target).endswith(".json"):
            raise KeyboardInterrupt()
        return original_link(source, target, *args, **kwargs)
    monkeypatch.setattr(lfs_backup.os, "link", interrupt_manifest)
    with pytest.raises(KeyboardInterrupt):
        stage(backup, output, max_new_lfs_bytes=10**7)
    assert not list((output / "packages").rglob("*.json"))
    monkeypatch.setattr(lfs_backup.os, "link", original_link)
    result = stage(backup, output, max_new_lfs_bytes=10**7)
    assert result["status"] == "complete"
    assert result["newly_written_lfs_bytes"] == 0


def test_index_traversal_and_component_corruption_never_publish_reassembled_catalog(tmp_path):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    result = stage(backup, output, max_new_lfs_bytes=10**7)
    package_path = output / result["package_manifest"]
    value = json.loads(package_path.read_bytes())
    value["index"]["chunks"][0]["path"] = "../../outside"
    package_path.write_text(json.dumps(value))
    destination = tmp_path / "restored"
    with pytest.raises(IntegrityError, match="package manifest"):
        lfs_backup.reassemble_lfs_backup(output, result["package_manifest"], destination,
                                        reserve_bytes=0, capacity_paths=())
    assert not (destination / "catalog.sqlite3").exists()
    with pytest.raises(lfs_backup.LFSBackupError, match="Unsafe original member"):
        lfs_backup._safe_member("../../outside", "0" * 64)
    with pytest.raises(lfs_backup.LFSBackupError, match="Unsafe LFS chunk"):
        list(lfs_backup._decoded(output, value["index"]))


def test_low_space_and_existing_restore_path_fail_without_touching_sources(tmp_path):
    backup, _, _ = fixture(tmp_path)
    output = tmp_path / "private-lfs"
    def full(_):
        raise ImportPaused("insufficient_disk_space")
    with pytest.raises(ImportPaused):
        stage(backup, output, max_new_lfs_bytes=10**7, space_guard=full)
    assert not (output / "lfs").exists()
    result = stage(backup, output, max_new_lfs_bytes=10**7)
    with pytest.raises(lfs_backup.LFSBackupError, match="must be new"):
        lfs_backup.reassemble_lfs_backup(output, result["package_manifest"], backup)
    assert backups.verify_backup(backup)["objects_verified"] == 1
