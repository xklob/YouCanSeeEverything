import hashlib
import json
from pathlib import Path
import re
import sqlite3

import pytest

from biography import snapshots
from biography.db import init_db
from biography.enrichment import ensure_schema as ensure_enrichment_schema


def seed(root: Path):
    vault = root / "old-vault"
    vault.mkdir()
    database = vault / "catalog.sqlite3"
    init_db(database)
    payload = b"original media must stay outside the metadata snapshot"
    digest = hashlib.sha256(payload).hexdigest()
    original = vault / "objects" / "sha256" / digest[:2] / digest
    original.parent.mkdir(parents=True)
    original.write_bytes(payload)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_enrichment_schema(connection)
    with connection:
        connection.execute("INSERT INTO sources(id,name,account_key,roots,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           ("source-1", "Synthetic journal", "example-owner", '["/original/export"]', "partial", "2024-01-01", "2024-01-01"))
        connection.execute("INSERT INTO imports(id,source_id,status,started_at) VALUES(?,?,?,?)",
                           ("import-1", "source-1", "ready", "2024-01-01"))
        connection.execute("INSERT INTO objects VALUES(?,?,?,?)", (digest, len(payload), str(original), "2024-01-01"))
        for index in range(9):
            fid, eid = f"file-{index}", f"event-{index}"
            connection.execute("INSERT INTO files(id,source_id,import_id,original_path,object_hash,bytes,parse_status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               (fid, "source-1", "import-1", f"note-{index}.txt", digest, len(payload), "parsed", "2024-01-01"))
            connection.execute("INSERT INTO events(id,source_id,title,body,occurred_at,category,created_at) VALUES(?,?,?,?,?,?,?)",
                               (eid, "source-1", f"Synthetic note {index}", "searchable evidence " * 100,
                                "2024-01-01T12:00:00Z" if index else None, "note", "2024-01-01"))
            connection.execute("INSERT INTO evidence VALUES(?,?,?,?)", (eid, fid, "line:1", "fixture.v1"))
        connection.execute("INSERT INTO jobs(id,kind,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           ("job-1", "question", "complete", "A synthetic question", "2024-01-01", "2024-01-01"))
    connection.close()
    ledgers = vault / "manifests"
    ledgers.mkdir()
    records = [{"kind": "header", "source": "/original/export"},
               {"kind": "source_ready", "objects": [{"sha256": digest, "size": len(payload)}] * 30},
               {"kind": "footer", "status": "complete"}]
    (ledgers / "synthetic-import.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    return database, ledgers, original, digest, records


def read_tables(path):
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                for table in snapshots.TABLES}


def rewrite_manifest(path, mutator):
    manifest = json.loads((path / "manifest.json").read_text())
    mutator(manifest)
    payload = snapshots._json(manifest)
    (path / "manifest.json").write_bytes(payload)
    (path / "manifest.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n")


def test_roundtrip_keeps_records_evidence_and_coverage_and_rebuilds_search(tmp_path):
    database, ledgers, _, digest, records = seed(tmp_path)
    output = tmp_path / "private-snapshot"
    manifest = snapshots.export_snapshot(database, output, manifests_dir=ledgers,
                                         app_revision="synthetic-revision", max_shard_bytes=1024)
    restored = tmp_path / "new-vault/catalog.sqlite3"
    new_ledgers = tmp_path / "restored-ledgers"
    result = snapshots.restore_snapshot(output, restored, manifests_dir=new_ledgers)
    before, after = read_tables(database), read_tables(restored)
    for table in snapshots.TABLES:
        if table != "objects":
            assert sorted(before[table]) == sorted(after[table])
    assert after["objects"][0][2] == str(restored.parent / "objects/sha256" / digest[:2] / digest)
    assert result["tables"]["events"] == result["tables"]["evidence"] == 9
    assert result["raw_objects_backed_up"] is result["raw_objects_restored"] is False
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT count(*) FROM events_fts WHERE events_fts MATCH 'searchable'").fetchone()[0] == 9
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [json.loads(line) for line in (new_ledgers / "synthetic-import.jsonl").read_text().splitlines()] == records
    assert manifest["coverage"][0]["undated_events"] == 1
    assert manifest["coverage"][0]["continuous_coverage_asserted"] is False
    assert all(p.stat().st_size <= 1024 for p in output.rglob("*.jsonl"))


def test_snapshot_is_deterministic_portable_and_does_not_copy_raw_objects(tmp_path):
    database, ledgers, original, digest, _ = seed(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    snapshots.export_snapshot(database, first, manifests_dir=ledgers)
    snapshots.export_snapshot(database, second, manifests_dir=ledgers)
    assert {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()} == {
        p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    combined = b"".join(p.read_bytes() for p in first.rglob("*") if p.is_file())
    assert original.read_bytes() not in combined
    assert str(original).encode() not in combined
    assert f"objects/sha256/{digest[:2]}/{digest}".encode() in combined
    restored = tmp_path / "elsewhere/catalog.sqlite3"
    relocated_vault = tmp_path / "different-drive"
    snapshots.restore_snapshot(first, restored, vaultroot=relocated_vault)
    assert not relocated_vault.exists()
    assert not list(first.rglob("*.sqlite3*"))
    assert not any(p.name.startswith("events_fts") for p in first.rglob("*"))
    assert original.exists()


def test_committed_wal_is_included_and_uncommitted_rows_are_excluded(tmp_path):
    database, _, _, _, _ = seed(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                   ("committed", "source-1", "Committed in WAL", "note", "2024-01-01"))
    writer.commit()
    writer.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,?,?,?)",
                   ("uncommitted", "source-1", "Not committed", "note", "2024-01-01"))
    try:
        output = tmp_path / "snapshot"
        manifest = snapshots.export_snapshot(database, output)
        assert manifest["tables"]["events"]["rows"] == 10
        snapshots.restore_snapshot(output, tmp_path / "restored.sqlite3")
        with sqlite3.connect(tmp_path / "restored.sqlite3") as connection:
            assert connection.execute("SELECT title FROM events WHERE id='committed'").fetchone()
            assert connection.execute("SELECT title FROM events WHERE id='uncommitted'").fetchone() is None
    finally:
        writer.rollback()
        writer.close()


def test_corrupt_shard_is_rejected_without_publishing_database(tmp_path):
    database, _, _, _, _ = seed(tmp_path)
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output)
    shard = next((output / "tables/events").glob("*.jsonl"))
    shard.write_bytes(shard.read_bytes().replace(b"Synthetic", b"Corrupted", 1))
    destination = tmp_path / "must-not-exist.sqlite3"
    with pytest.raises(snapshots.SnapshotError, match="integrity"):
        snapshots.restore_snapshot(output, destination)
    assert not destination.exists()


@pytest.mark.parametrize("path", ["../../outside.jsonl", "/tmp/outside.jsonl", "tables/../outside.jsonl", "tables\\outside.jsonl"])
def test_unsafe_manifest_paths_are_rejected_even_with_recomputed_manifest_hash(tmp_path, path):
    database, _, _, _, _ = seed(tmp_path)
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output)
    def mutate(manifest):
        manifest["tables"]["sources"]["groups"][0]["shards"][0]["path"] = path
    rewrite_manifest(output, mutate)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.restore_snapshot(output, tmp_path / "must-not-exist.sqlite3")
    assert not (tmp_path / "must-not-exist.sqlite3").exists()


def test_symlink_shard_and_existing_restore_destination_are_rejected(tmp_path):
    database, _, _, _, _ = seed(tmp_path)
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output)
    with pytest.raises(snapshots.SnapshotError, match="already exists"):
        snapshots.restore_snapshot(output, database)
    shard = next((output / "tables/events").glob("*.jsonl"))
    external = tmp_path / "external"
    shard.rename(external)
    shard.symlink_to(external)
    with pytest.raises(snapshots.SnapshotError, match="symlink"):
        snapshots.restore_snapshot(output, tmp_path / "new.sqlite3")


def test_incomplete_ledger_fails_without_publishing_snapshot(tmp_path):
    database, ledgers, _, _, _ = seed(tmp_path)
    (ledgers / "torn.jsonl").write_bytes(b'{"kind":"header"}\n{"kind":')
    output = tmp_path / "snapshot"
    with pytest.raises(snapshots.SnapshotError, match="incomplete"):
        snapshots.export_snapshot(database, output, manifests_dir=ledgers)
    assert not output.exists()
    assert not list(tmp_path.glob(".snapshot-build-*"))


def test_ledger_object_references_relocate_without_copying_payloads(tmp_path):
    database, ledgers, original, digest, _ = seed(tmp_path)
    rows = [{"kind": "header"}, {"kind": "object", "sha256": digest, "path": str(original)},
            {"kind": "footer", "status": "complete"}]
    (ledgers / "object.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output, manifests_dir=ledgers)
    destination = tmp_path / "relocated/catalog.sqlite3"
    manifest_destination = tmp_path / "restored-ledgers"
    snapshots.restore_snapshot(output, destination, manifests_dir=manifest_destination)
    restored = [json.loads(line) for line in (manifest_destination / "object.jsonl").read_text().splitlines()]
    assert restored[1]["path"] == str(destination.parent / "objects/sha256" / digest[:2] / digest)
    assert not Path(restored[1]["path"]).exists()
    assert str(original).encode() not in b"".join(p.read_bytes() for p in output.rglob("*.jsonl"))


def test_changed_application_insertion_default_does_not_break_explicit_row_restore(tmp_path, monkeypatch):
    database, _, _, _, _ = seed(tmp_path)
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output)
    replacement = re.sub(r"(account_key TEXT NOT NULL DEFAULT )'[^']*'",
                         r"\1'changed-owner'", snapshots.SCHEMA)
    monkeypatch.setattr(snapshots, "SCHEMA", replacement)
    restored = tmp_path / "restored.sqlite3"
    snapshots.restore_snapshot(output, restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT account_key FROM sources").fetchone()[0] == "example-owner"


def test_changed_ledger_during_catalog_copy_aborts_export(tmp_path, monkeypatch):
    database, ledgers, _, _, _ = seed(tmp_path)
    real_coverage = snapshots._coverage
    def mutate_after_backup(connection):
        with (ledgers / "synthetic-import.jsonl").open("a") as handle:
            handle.write('{"kind":"late_record"}\n')
        return real_coverage(connection)
    monkeypatch.setattr(snapshots, "_coverage", mutate_after_backup)
    output = tmp_path / "snapshot"
    with pytest.raises(snapshots.SnapshotError, match="ledgers changed"):
        snapshots.export_snapshot(database, output, manifests_dir=ledgers)
    assert not output.exists()


def test_durable_enrichment_survives_and_known_derived_tables_are_rebuilt(tmp_path):
    from biography import locations, media
    from biography.enrichment import _apply, reapply
    database, _, _, digest, _ = seed(tmp_path)
    payload = {"occurred_at": "2024-02-02", "end_at": None, "time_precision": "day",
               "original_values": {"occurred_at": None, "end_at": None, "time_precision": "unknown"},
               "confidence": "high", "provenance": [{"url": "https://example.test/fixture"}]}
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        locations.ensure_schema(connection)
        media.ensure_schema(connection)
        connection.execute("INSERT INTO event_enrichments VALUES(?,?,?)",
                           ("event-0", json.dumps(payload), "2024-02-03"))
        _apply(connection, "event-0", payload)
        connection.execute("INSERT INTO media_object_index VALUES(?,?,?,?)", (digest, "image/png", "image", "ready"))
        connection.execute("UPDATE events SET metadata=? WHERE id='event-1'",
                           (json.dumps({"coordinates": {"latitude": 0, "longitude": 0}}),))
        connection.execute("INSERT INTO event_locations(id,event_id,point_index,latitude,longitude,kind,locator) VALUES(?,?,?,?,?,?,?)",
                           ("derived-point", "event-1", 0, 0, 0, "location", "$.coordinates"))
    output = tmp_path / "snapshot"
    manifest = snapshots.export_snapshot(database, output)
    assert manifest["tables"]["event_enrichments"]["rows"] == 1
    assert set(manifest["tables"]).isdisjoint(snapshots.DERIVED_TABLES)
    restored = tmp_path / "restored.sqlite3"
    snapshots.restore_snapshot(output, restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT occurred_at FROM events WHERE id='event-0'").fetchone()[0] == "2024-02-02"
        assert connection.execute("SELECT payload FROM event_enrichments").fetchone()[0] == json.dumps(payload)
        for table in snapshots.DERIVED_TABLES:
            assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
        connection.execute("UPDATE events SET occurred_at=NULL,time_precision='unknown',metadata='{}' WHERE id='event-0'")
    assert reapply(restored) == 1
    assert locations.reconcile_locations(restored)["points"] == 1
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT occurred_at FROM events WHERE id='event-0'").fetchone()[0] == "2024-02-02"
        assert connection.execute("SELECT count(*) FROM event_locations").fetchone()[0] == 1


def test_unknown_persistent_table_is_never_silently_omitted(tmp_path):
    database, _, _, _, _ = seed(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unexpected_private_notes(body TEXT)")
        connection.execute("INSERT INTO unexpected_private_notes VALUES('retain this')")
    with pytest.raises(snapshots.SnapshotError, match="unsupported application tables"):
        snapshots.export_snapshot(database, tmp_path / "snapshot")


@pytest.mark.parametrize("table", ["event_locations", "media_manifest_state", "media_manifest_refs",
                                   "media_candidates", "media_native_keys", "media_candidate_backfill"])
def test_misnamed_custom_table_cannot_masquerade_as_a_derived_cache(tmp_path, table):
    database, _, _, _, _ = seed(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(f'CREATE TABLE "{table}"(private_notes TEXT)')
    with pytest.raises(snapshots.SnapshotError, match="unsupported schema"):
        snapshots.export_snapshot(database, tmp_path / "snapshot")


def test_known_legacy_media_cache_is_omitted_without_migrating_live_catalog(tmp_path):
    database, _, _, _, _ = seed(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE media_event_state (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,generation INTEGER NOT NULL,
            needs_refresh INTEGER NOT NULL,metadata_sha256 TEXT NOT NULL)""")
        connection.execute("INSERT INTO media_event_state VALUES(?,?,?,?,?)", ("event-1", 1, 0, 0, "a" * 64))
    output = tmp_path / "snapshot"
    snapshots.export_snapshot(database, output)
    with sqlite3.connect(database) as connection:
        assert "evidence_count" not in {row[1] for row in connection.execute("PRAGMA table_info(media_event_state)")}
        assert connection.execute("SELECT count(*) FROM media_event_state").fetchone()[0] == 1
    restored = tmp_path / "restored.sqlite3"
    snapshots.restore_snapshot(output, restored)
    with sqlite3.connect(restored) as connection:
        assert "evidence_count" in {row[1] for row in connection.execute("PRAGMA table_info(media_event_state)")}
        assert connection.execute("SELECT count(*) FROM media_event_state").fetchone()[0] == 0


def test_sparse_media_discovery_is_omitted_and_restored_as_incomplete(tmp_path):
    from biography import media
    database,_,_,_,_=seed(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.row_factory=sqlite3.Row
        media.ensure_schema(connection)
        connection.execute("UPDATE events SET native_id='synthetic-native',metadata=? WHERE id='event-0'",
                           (json.dumps({'photos':[{'uri':'missing.png'}]}),))
    media.backfill_media_candidates(database,max_batches=10)
    before=media.media_candidate_status(database)
    assert before['discovery_complete_for_watermark']
    output=tmp_path/'snapshot'
    snapshots.export_snapshot(database,output)
    manifest=json.loads((output/'manifest.json').read_text())
    sparse={'media_candidates','media_native_keys','media_candidate_backfill'}
    assert sparse.issubset(manifest['derived_tables_not_exported'])
    restored=tmp_path/'restored.sqlite3'
    snapshots.restore_snapshot(output,restored)
    after=media.media_candidate_status(restored)
    assert after['status']=='not_started' and not after['discovery_complete_for_watermark']
    with sqlite3.connect(restored) as connection:
        for table in sparse:
            assert connection.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert connection.execute("SELECT native_id FROM events WHERE id='event-0'").fetchone()[0]=='synthetic-native'
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT count(*) FROM media_candidates').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM media_native_keys').fetchone()[0]==1


def test_media_manifest_caches_are_omitted_and_older_snapshots_still_restore(tmp_path):
    from biography.media import ensure_schema
    database, _, _, digest, _ = seed(tmp_path)
    with sqlite3.connect(database) as connection:
        ensure_schema(connection)
        connection.execute("INSERT INTO media_manifest_state VALUES(?,?,?)", ("file-0", 1, "parsed"))
        connection.execute("INSERT INTO media_manifest_refs VALUES(?,?,?,?,?,?,?,?)",
                           ("file-0", 0, "source-1", "synthetic-native-id", "attachment.png", digest,
                            "downloaded", "$.items[0]"))
    output = tmp_path / "snapshot"
    manifest = snapshots.export_snapshot(database, output)
    new_caches = {"media_manifest_state", "media_manifest_refs"}
    assert new_caches.issubset(manifest["derived_tables_not_exported"])
    assert new_caches.isdisjoint(manifest["tables"])
    assert not (output / "tables/media_manifest_refs").exists()
    # Earlier format-v1 manifests enumerate only caches known at export time.
    # This descriptive list must not prevent rebuilding newer optional caches.
    rewrite_manifest(output, lambda value: value.update(
        derived_tables_not_exported=[name for name in value["derived_tables_not_exported"] if name not in new_caches]))
    restored = tmp_path / "restored.sqlite3"
    snapshots.restore_snapshot(output, restored)
    with sqlite3.connect(restored) as connection:
        for table in new_caches:
            assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 9
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 9
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM media_manifest_refs").fetchone()[0] == 1


def test_partial_counters_are_omitted_and_restore_rebuilds_for_new_rowids(tmp_path):
    from contextlib import closing
    from biography import catalog_counts
    from biography.db import connect
    database, _, _, _, _ = seed(tmp_path)
    # Simulate a sparse old catalog and a partly completed counting migration.
    with closing(connect(database)) as conn, conn:
        conn.execute("UPDATE events SET rowid=100 WHERE id='event-8'")
    result = catalog_counts.build_counts(database, reset=True, batch_size=2)
    assert result['stats']['status'] == 'counting'
    assert result['stats']['counted_events'] == 2
    output = tmp_path / 'snapshot-with-partial-derived-counts'
    manifest = snapshots.export_snapshot(database, output)
    assert set(catalog_counts.TABLES).isdisjoint(manifest['tables'])
    restored = tmp_path / 'restored-counts.sqlite3'
    snapshots.restore_snapshot(output, restored)
    status = catalog_counts.count_status(restored)
    assert status['total_events'] is None
    assert status['stats']['error'] == 'counts_not_initialized'
    with closing(connect(restored)) as conn:
        assert conn.execute('SELECT max(rowid) FROM events').fetchone()[0] == 9
        assert not conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'catalog_counts_%'").fetchall()
    # No copied partial prefix may be interpreted using the newly assigned IDs.
    init_db(restored)
    assert catalog_counts.count_status(restored)['stats']['counted_events'] == 0
    completed = catalog_counts.build_counts(restored, batch_size=20)
    assert completed['total_events'] == 9
    assert completed['undated_count'] == 1
    assert completed['by_source']['source-1']['total_events'] == 9
