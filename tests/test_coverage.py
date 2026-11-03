import json
import sqlite3

from biography.coverage import coverage_overview
from biography.db import init_db


def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    init_db(path)
    connection = sqlite3.connect(path)
    connection.executemany("INSERT INTO sources(id,name,status,queue_order,snapshot_bytes,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", [
        ("source-a", "Example journal", "ready", 2, 10000, '["Originals retained; extraction needs review."]', "2025-01-01", "2025-01-01"),
        ("source-b", "Example queued archive", "queued", 1, 20000, "[]", "2025-01-01", "2025-01-01"),
    ])
    states = ["parsed", "metadata_only", "retained", "error", "pending", "partial", "empty", "excluded", "needs_fallback"]
    for index, state in enumerate(states, 1):
        digest = f"{index:064x}"
        connection.execute("INSERT INTO objects VALUES(?,?,?,?)", (digest, index * 100, f"/not-opened/object-{index}", "2025-01-01"))
        connection.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,parse_status,created_at) VALUES(?,?,?,?,?,?,?)", (f"file-{index}", "source-a", f"example-{index}.json", digest, index * 100, state, "2025-01-01"))
    connection.executemany("INSERT INTO events(id,source_id,title,occurred_at,category,time_precision,created_at) VALUES(?,?,?,?,?,?,?)", [
        ("event-day", "source-a", "A fictional day", "2024-01-05", "note", "day", "2025-01-01"),
        ("event-undated", "source-a", "An undated note", None, "note", "unknown", "2025-01-01"),
        ("event-year", "source-a", "A year-only note", "2023-01-01", "note", "year", "2025-01-01"),
    ])
    connection.execute("INSERT INTO imports(id,source_id,status,total_files,completed_files,total_bytes,completed_bytes,events,error,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("import-running", "source-a", "running", 4, 1, 1000, 900, 7, None, "2025-01-02"))
    connection.execute("INSERT INTO imports(id,source_id,status,total_files,completed_files,total_bytes,completed_bytes,events,error,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("import-paused", "source-b", "paused", 0, 0, 0, 0, 0, "RuntimeError: PRIVATE_DIAGNOSTIC_SENTINEL", "2025-01-01"))
    connection.commit(); connection.close()
    return path


def read(path, **options):
    return coverage_overview(path, wait=True, index_status={"status": "not_built", "watermark_rowid": None}, **options)


def test_observed_coverage_distinguishes_retention_parsing_and_absence(tmp_path):
    path = catalog(tmp_path)
    report = read(path)
    assert report["scope"] == "observed_imported_records"
    assert report["continuous_coverage_asserted"] is False
    assert report["totals"] == {"sources": 2, "files": 9, "bytes": 4500, "parsed_files": 1, "metadata_only_files": 1, "retained_only_files": 1, "review_files": 3, "events": 3, "dated_events": 2, "undated_events": 1}
    source = next(row for row in report["sources"] if row["id"] == "source-a")
    assert source["status"] == "partial"
    assert source["files"]["by_parse_status"]["error"] == 1
    assert source["records"]["first_observed"] == "2023-01-01"
    assert source["records"]["last_observed"] == "2024-01-05"
    assert source["extraction"] == {"review_files": 3, "pending_files": 1, "history_absence_established": False}
    queued = next(row for row in report["sources"] if row["id"] == "source-b")
    assert queued["records"]["total"] == 0
    assert queued["records"]["first_observed"] is None
    assert queued["extraction"]["history_absence_established"] is False
    assert report["backup"] == {"status": "not_supplied"}


def test_import_file_and_byte_progress_have_distinct_denominators_and_safe_errors(tmp_path):
    report = read(catalog(tmp_path))
    active = report["active_imports"][0]
    assert active["files"]["fraction"] == 0.25
    assert active["bytes"]["fraction"] == 0.9
    assert active["events_reported"] == 7
    assert report["totals"]["events"] == 3
    assert report["queue"][0]["source_id"] == "source-b"
    assert report["queue"][0]["error"]["code"] == "RuntimeError"
    assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in json.dumps(report)


def test_cache_watermark_reports_stale_counts_until_explicit_refresh(tmp_path):
    path = catalog(tmp_path)
    first = read(path, cache_ttl_seconds=600)
    assert first["indexing"]["coverage_stats"]["status"] == "current"
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO events(id,source_id,title,occurred_at,category,time_precision,created_at) VALUES('event-new','source-a','New fictional note','2025-02-03','note','day','2025-02-03')")
    connection.commit(); connection.close()
    stale = read(path, cache_ttl_seconds=600)
    assert stale["totals"]["events"] == 3
    assert stale["indexing"]["coverage_stats"]["status"] == "stale"
    assert stale["indexing"]["catalog_max_event_rowid"] > stale["indexing"]["coverage_stats"]["catalog_max_event_rowid"]
    fresh = read(path, cache_ttl_seconds=600, refresh=True)
    assert fresh["totals"]["events"] == 4
    assert fresh["indexing"]["coverage_stats"]["status"] == "current"


def test_new_source_has_unknown_counts_until_it_enters_measured_snapshot(tmp_path):
    path = catalog(tmp_path)
    read(path, cache_ttl_seconds=600)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO sources(id,name,status,created_at,updated_at) VALUES('new-source','Fictional new source','queued','2025-02-03','2025-02-03')")
    connection.commit(); connection.close()
    cached = read(path, cache_ttl_seconds=600)
    source = next(row for row in cached["sources"] if row["id"] == "new-source")
    assert source["records"]["total"] is None
    assert source["records"]["dated"] is None
    assert source["records"]["status"] == "stale"
    measured = read(path, refresh=True)
    source = next(row for row in measured["sources"] if row["id"] == "new-source")
    assert source["records"]["total"] == 0


def test_month_presence_is_bounded_and_does_not_turn_year_precision_into_january(tmp_path):
    path = catalog(tmp_path)
    connection = sqlite3.connect(path)
    for number, month in enumerate(("2024-02", "2024-03")):
        connection.execute("INSERT INTO events(id,source_id,title,occurred_at,category,time_precision,created_at) VALUES(?,?,?,?,?,?,?)", (f"month-{number}", "source-a", "Another fictional note", month + "-10", "note", "day", "2025-01-01"))
    connection.commit(); connection.close()
    report = read(path, include_months=True, month_limit=2)
    source = next(row for row in report["sources"] if row["id"] == "source-a")
    assert source["records"]["months"]["items"] == [{"month": "2024-03", "records": 1}, {"month": "2024-02", "records": 1}]
    assert source["records"]["months"]["truncated"] is True


def test_missing_covering_index_returns_unknown_counts_without_schema_or_data_changes(tmp_path):
    path = catalog(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX events_source"); connection.commit()
    before = connection.execute("SELECT count(*) FROM events").fetchone()[0]
    report = read(path)
    assert report["totals"]["events"] is None
    assert all(row["records"]["total"] is None for row in report["sources"])
    assert report["indexing"]["coverage_stats"]["status"] == "unavailable"
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == before
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='events_source'").fetchone()[0] == 0
    connection.close()


def test_supplied_projection_classification_and_verified_backup_are_explicit(tmp_path):
    path = catalog(tmp_path)
    projection = {"status": "building", "watermark_rowid": 100, "checkpoint_rowid": 20, "processed_records": 18, "current_catalog_max_rowid": 120}
    backup = {"status": "verified", "last_verified_at": "2025-01-02T00:00:00Z"}
    report = coverage_overview(path, wait=True, index_status=projection, classifications={"source-a": {"status": "reviewed_projection", "label": "Example classified source"}}, backup_status=backup)
    assert report["indexing"]["daily"] == projection
    assert report["backup"] == backup
    assert next(row for row in report["sources"] if row["id"] == "source-a")["classification"]["status"] == "reviewed_projection"
