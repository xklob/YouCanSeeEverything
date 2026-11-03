"""Disposable private index of saved narratives, without catalog/model access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime,timezone

MAX_DISCOVERY_FILES = 10_000
MAX_IMPORT_FILES = 64
MAX_IMPORT_BYTES = 8 * 1024**2
KEY = re.compile(r"[a-f0-9]{64}")


@contextmanager
def _connect(root):
    path = root / "library.sqlite3"
    if path.is_symlink():
        raise ValueError("The saved-summary index must be a regular private file.")
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS entries(key TEXT PRIMARY KEY,generated_at TEXT NOT NULL,metadata TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS discovered(key TEXT PRIMARY KEY)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _metadata(value):
    context = value.get("trip_context") or {}
    return {"key": value["cache"]["key"], "kind": value["kind"],
            "title": str(value.get("title", "Saved summary"))[:300],
            "period": value.get("period"), "trip_id": context.get("trip_id"),
            "focus": str(value.get("focus", ""))[:4000],
            "model": value.get("model"),
            "source_ids": value.get("selection", {}).get("source_ids", []),
            "generated_at": value.get("cache", {}).get("generated_at", ""),
            "citation_count": len(value.get("citations", [])),
            "evidence_count": len(value.get("evidence_ids", [])),
            "freshness": "not_checked"}


def register(root: Path, value: dict):
    metadata = _metadata(value)
    with _connect(root) as conn:
        conn.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?)", (metadata["key"], metadata["generated_at"], json.dumps(metadata, ensure_ascii=False)))
        conn.execute("INSERT OR IGNORE INTO discovered VALUES(?)", (metadata["key"],))


def _sort_order(sort,direction):
    keys={'generated':"summary_instant_key(generated_at)",'period':"NULLIF(json_extract(metadata,'$.period'),'')",
          'title':"NULLIF(trim(json_extract(metadata,'$.title')),'') COLLATE NOCASE"}
    if sort not in keys or direction not in {'asc','desc'}:raise ValueError('Unknown saved-summary sort or direction')
    value=keys[sort]
    return value+' IS NULL,'+value+' '+direction.upper()+',key ASC'


def _instant_key(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds') if parsed.tzinfo else None
    except (ValueError,TypeError,AttributeError,OverflowError):return None


def list_saved(root: Path, *, limit=20, offset=0,sort='generated',direction='desc'):
    from .synthesis import _read_cache
    order=_sort_order(sort,direction)
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    names, discovery_limited = [], False
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DISCOVERY_FILES:
                discovery_limited = True
                break
            if entry.name.endswith(".json") and KEY.fullmatch(entry.name[:-5]) and not entry.is_symlink():
                names.append(entry.name[:-5])
    imported, byte_count = 0, 0
    with _connect(root) as conn:
        conn.create_function('summary_instant_key',1,_instant_key,deterministic=True)
        known = {row[0] for row in conn.execute("SELECT key FROM discovered LIMIT ?", (MAX_DISCOVERY_FILES,))}
        for key in sorted(names):
            if key in known:
                continue
            if imported >= MAX_IMPORT_FILES:
                discovery_limited = True
                break
            path = root / (key + ".json")
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if byte_count + min(size, 2_000_000) > MAX_IMPORT_BYTES:
                discovery_limited = True
                break
            imported += 1
            byte_count += min(size, 2_000_000)
            value = _read_cache(root, key)
            if value:
                metadata = _metadata(value)
                conn.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?)", (key, metadata["generated_at"], json.dumps(metadata, ensure_ascii=False)))
            conn.execute("INSERT OR IGNORE INTO discovered VALUES(?)", (key,))
        rows = conn.execute("SELECT metadata FROM entries ORDER BY "+order+" LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
    items = [json.loads(row[0]) for row in rows[:limit]]
    return {"items": items, "limit": limit, "offset": offset,
            "has_more": len(rows) > limit, "discovery_limited": discovery_limited,
            "freshness": "not_checked",'sort':sort,'direction':direction,
            "notice": "Saved narratives describe their original selected evidence. Current coverage and freshness have not been checked."}
