"""Deterministic, bounded source/date-aware retrieval for the local assistant.

Queries never grant tools to a model. The catalog is opened read-only; imported
text stays untrusted evidence. Ranking is lexical, with source/category/time
diversity rather than a promise of exhaustive semantic search.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
import calendar
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from .config import settings
from .parsers.common import clean_text


POLICY_VERSION = 1
MAX_CANDIDATES = 640
MAX_EVIDENCE = 48
MAX_CONTEXT_CHARS = 64_000
MAX_EXCERPT_CHARS = 2_000
MAX_SOURCE_SELECTION = 32
QUERY_SECONDS = 12
RAW_CATEGORIES = {"sensor", "sensor_sample", "health_sample", "raw_health", "heart_rate_sample", "step_sample", "gps_sample"}
HIGH_VOLUME = {"location", "listening", "usage", "media", "health", "sensor"}
_MONTHS = {name.casefold(): index for index in range(1, 13) for name in (calendar.month_name[index], calendar.month_abbr[index])}
_MONTH_PATTERN = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_STOP = set("what when where which have with from about that this were would could please tell show summarize summary give does into been your my the and for you how did was are can much many some more most any life doing during between through happened happen month year day days months years source sources evidence records compare versus also again work based selected everything all in on at to of a an is it as by be i me we our us do not".split()) | set(_MONTHS)
_STOP.discard("work")
_EXPANSIONS = {"job": ("employment", "interview", "career"), "jobs": ("job", "employment", "interview"), "projects": ("project", "repository", "commit", "release", "deployment"), "work": ("project", "job", "career", "engineering"), "moved": ("moving", "relocated", "move"), "travel": ("trip", "visit", "traveling"), "music": ("listening", "song", "playlist"), "exercise": ("workout", "training", "fitness")}


@contextmanager
def _connection(path=None):
    database = Path(path or settings.database).expanduser().resolve()
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + QUERY_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2_000)
    try:
        conn.execute("BEGIN")
        yield conn
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise ValueError("Evidence retrieval exceeded its local query budget; choose a narrower date or source range.") from exc
        raise
    finally:
        conn.close()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _period_bounds(period: str) -> tuple[str, str]:
    if re.fullmatch(r"\d{4}", period):
        year = int(period)
        return date(year, 1, 1).isoformat(), date(year + 1, 1, 1).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}", period):
        year, month = map(int, period.split("-"))
        first = date(year, month, 1)
        last = date(year + (month == 12), month % 12 + 1, 1)
        return first.isoformat(), last.isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
        first = date.fromisoformat(period)
        return first.isoformat(), (first + timedelta(days=1)).isoformat()
    raise ValueError("Choose an exact YYYY, YYYY-MM or YYYY-MM-DD period.")


def selection(prompt: str = "", *, period=None, source_ids=None, start=None, end=None) -> dict:
    """Infer only unambiguous written calendar periods, never filesystem dates."""
    if period and (start or end):
        raise ValueError("Choose a period or a date range, not both.")
    if source_ids is not None and not isinstance(source_ids, (list, tuple)):
        raise ValueError("Source selections must be a list of catalog source IDs.")
    ids = sorted(set(str(value) for value in (source_ids or [])))
    if len(ids) > MAX_SOURCE_SELECTION or any(not value or len(value) > 200 for value in ids):
        raise ValueError("The source selection is invalid or too large.")
    result = {"period": None, "start": None, "end_exclusive": None, "source_ids": ids, "date_selection_basis": "all_dates", "source_selection_basis": "explicit" if ids else "all_sources", "warnings": [], "time_basis": "Known instants use their stored UTC dates; dates without a known timezone remain local calendar-day buckets."}
    try:
        if start or end:
            result.update(start=date.fromisoformat(start).isoformat() if start else None, end_exclusive=(date.fromisoformat(end) + timedelta(days=1)).isoformat() if end else None, date_selection_basis="explicit_range")
        elif period:
            first, last = _period_bounds(str(period))
            result.update(period=str(period), start=first, end_exclusive=last, date_selection_basis="explicit_period")
        else:
            text = prompt.casefold()
            span = re.search(r"\b(?:from|between)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|through|and)\s+(\d{4}-\d{2}-\d{2})\b", text)
            if span:
                result.update(start=date.fromisoformat(span[1]).isoformat(), end_exclusive=(date.fromisoformat(span[2]) + timedelta(days=1)).isoformat(), date_selection_basis="question_range")
            else:
                candidates = set()
                # Capture named day expressions before considering month-only forms.
                for match in re.finditer(rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", text):
                    candidates.add(date(int(match[3]), _MONTHS[match[1]], int(match[2])).isoformat())
                iso_days = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
                candidates.update(iso_days)
                candidates.update(re.findall(r"\b\d{4}-\d{2}(?!-\d{2})\b", text))
                for match in re.finditer(rf"\b({_MONTH_PATTERN})\s+(\d{{4}})\b", text):
                    candidates.add(f"{int(match[2]):04d}-{_MONTHS[match[1]]:02d}")
                if not candidates:
                    years = set(re.findall(r"\b(?:19|20)\d{2}\b", text))
                    if len(years) == 1:
                        candidates = years
                if len(candidates) == 1:
                    chosen = next(iter(candidates))
                    first, last = _period_bounds(chosen)
                    result.update(period=chosen, start=first, end_exclusive=last, date_selection_basis="question_period")
                elif candidates:
                    result["warnings"].append("Several separate dates were mentioned; no single period was imposed.")
            if re.search(r"\b(?:last|this|next)\s+(?:week|month|year|day)\b|\byesterday\b|\btoday\b", text) and not result["start"]:
                result["warnings"].append("Relative date wording was not converted to a calendar period; choose exact dates to constrain the answer.")
        if result["start"] and result["end_exclusive"] and result["start"] >= result["end_exclusive"]:
            raise ValueError("The date range is reversed.")
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("The requested calendar period is invalid.") from exc
    return result


def _where(chosen: dict, *, extra=None) -> tuple[str, list]:
    clauses, args = [], []
    if chosen["source_ids"]:
        clauses.append("e.source_id IN (" + ",".join("?" for _ in chosen["source_ids"]) + ")")
        args.extend(chosen["source_ids"])
    if chosen["start"]:
        clauses.append("e.occurred_at>=?")
        args.append(chosen["start"])
    if chosen["end_exclusive"]:
        clauses.append("e.occurred_at<?")
        args.append(chosen["end_exclusive"])
    if extra:
        clauses.extend(extra)
    return (" AND ".join(clauses) or "1"), args


def _known_sources(conn, chosen: dict, prompt="") -> list[dict]:
    sources = [dict(row) for row in conn.execute("SELECT id,name,status,updated_at FROM sources ORDER BY name,id")]
    if chosen["source_ids"]:
        if set(chosen["source_ids"]) - {row["id"] for row in sources}:
            raise ValueError("A selected source is not present in the catalog.")
    elif prompt:
        matches = []
        for source in sources:
            aliases = [source["name"].casefold()]
            if "twitter" in source["name"].casefold():
                aliases.append("twitter")
            if "github" in source["name"].casefold():
                aliases.append("github")
            if "maps timeline" in source["name"].casefold():
                aliases.append("google maps")
            for alias in aliases:
                if alias == "unknown":
                    continue
                for match in re.finditer(r"\b(?:from|on|in)\s+(?:my\s+)?" + re.escape(alias) + r"(?!\w)", prompt.casefold()):
                    matches.append((match.start(), match.end(), source["id"]))
        # Prefer the complete name at a given mention: "from Google Maps
        # Timeline" should not accidentally include the separate Google source.
        matches = [item for item in matches if not any(other[0] == item[0] and other[1] > item[1] for other in matches)]
        if matches:
            chosen.update(source_ids=list(dict.fromkeys(item[2] for item in sorted(matches))), source_selection_basis="explicit_name_in_question")
    return [row for row in sources if not chosen["source_ids"] or row["id"] in chosen["source_ids"]]


def _coverage(conn, chosen: dict, known: list[dict]) -> dict:
    condition, args = _where(chosen)
    aggregate = {row["source_id"]: dict(row) for row in conn.execute("SELECT e.source_id,count(*) total_events,sum(e.occurred_at IS NOT NULL) dated_events,min(e.occurred_at) first_event,max(e.occurred_at) last_event FROM events e WHERE " + condition + " GROUP BY e.source_id", args)}
    # The catalog can contain many GiB of document bodies. The composite
    # source/time index answers these counts without fetching those bodies.
    undated = {source["id"]: conn.execute("SELECT count(*) FROM events INDEXED BY events_source WHERE source_id=? AND occurred_at IS NULL", (source["id"],)).fetchone()[0] for source in known}
    file_counts = {}
    for row in conn.execute("SELECT source_id,parse_status,count(*) n FROM files GROUP BY source_id,parse_status"):
        file_counts.setdefault(row["source_id"], {})[row["parse_status"]] = row["n"]
    sources = []
    for source in known:
        row = aggregate.get(source["id"], {})
        files = file_counts.get(source["id"], {})
        sources.append({"source_id": source["id"], "source": source["name"], "status": source["status"], "updated_at": source["updated_at"], "total_events": row.get("total_events", 0), "dated_events": row.get("dated_events", 0), "selected_events": 0, "first_event": row.get("first_event"), "last_event": row.get("last_event"), "undated_source_events": undated.get(source["id"], 0), "file_status_counts": files, "files_needing_extraction": sum(files.get(key, 0) for key in ("pending", "error", "needs_fallback", "partial"))})
    limitations = list(chosen["warnings"])
    if any(row["status"] not in {"ready", "complete", "completed"} or row["files_needing_extraction"] for row in sources):
        limitations.append("Some selected sources or retained files are incomplete; missing records are not evidence that an activity did not occur.")
    limitations.extend(["Based on selected evidence from the local catalog, not a complete biography.", "Lexical retrieval and bounded time/source sampling can omit relevant records."])
    limitations.append("Raw health and sensor samples are excluded from this narrative; deterministic daily health rollups are shown separately.")
    return {"total_events_in_scope": sum(row["total_events"] for row in sources), "selected_events": 0, "candidate_events": 0, "undated_events_excluded": sum(row["undated_source_events"] for row in sources) if chosen["start"] or chosen["end_exclusive"] else 0, "sources": sources, "retrieval_limited": True, "time_basis": chosen["time_basis"], "file_status_scope": "Whole selected sources; extraction status is not period-specific.", "limitations": limitations}


def _watermark(conn, chosen, coverage) -> dict:
    # A combined aggregate defeats SQLite's COUNT/MAX fast paths and can scan
    # the full body-bearing table. Keep independent lookups independent.
    totals = {}
    for table, count_key, row_key in (("events", "events", "event_rowid"), ("files", "files", "file_rowid"), ("evidence", "evidence", "evidence_rowid")):
        totals[count_key] = conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
        totals[row_key] = conn.execute("SELECT max(rowid) FROM " + table).fetchone()[0]
    try:
        from .classification import CLASSIFIER_VERSION
    except ImportError:
        CLASSIFIER_VERSION = 0
    value = {"policy_version": POLICY_VERSION, "classification_version": CLASSIFIER_VERSION, "catalog": totals, "selection": chosen, "sources": coverage["sources"], "total_events_in_scope": coverage["total_events_in_scope"]}
    return {"token": _digest(value), "policy_version": POLICY_VERSION, "classification_version": CLASSIFIER_VERSION, "catalog": totals}


def catalog_state(path=None, *, period=None, source_ids=None, prompt="", start=None, end=None) -> dict:
    chosen = selection(prompt, period=period, source_ids=source_ids, start=start, end=end)
    with _connection(path) as conn:
        known = _known_sources(conn, chosen, prompt)
        coverage = _coverage(conn, chosen, known)
        return {"selection": chosen, "coverage": coverage, "watermark": _watermark(conn, chosen, coverage)}


def _terms(prompt: str) -> list[str]:
    return list(dict.fromkeys(token for token in re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", prompt.casefold()) if len(token) > 1 and token not in _STOP and not token.isdigit()))[:12]


def _quoted(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _spread(values: list, count: int) -> list:
    if len(values) <= count:
        return values
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


_META_HINTS = "json_object(" + ",".join("'" + key + "',json_extract(e.metadata,'$." + key + "')" for key in ("role", "conversation_title", "document_kind", "timezone_unknown", "original_datetime", "timestamp_role")) + ")"
_COLUMNS = "e.id,e.source_id,s.name AS source,e.title,e.occurred_at,e.end_at,e.category,e.time_precision,length(e.body) AS body_characters,CASE WHEN length(e.metadata)>12000 AND json_valid(e.metadata) THEN " + _META_HINTS + " ELSE e.metadata END AS metadata"


def _provenance(conn, ids: list[str]) -> dict:
    if not ids:
        return {}
    result = {}
    for row in conn.execute("WITH ranked AS (SELECT v.event_id,v.file_id,v.locator,v.parser,f.original_path,f.object_hash,row_number() OVER(PARTITION BY v.event_id ORDER BY f.original_path) n FROM evidence v JOIN files f ON f.id=v.file_id WHERE v.event_id IN (" + ",".join("?" for _ in ids) + ")) SELECT * FROM ranked WHERE n<=3 ORDER BY event_id,n", ids):
        group = result.setdefault(row["event_id"], [])
        if len(group) < 3:
            group.append({"file_id": row["file_id"], "locator": clean_text(row["locator"], 400), "parser": row["parser"], "original_path": clean_text(row["original_path"], 500), "object_hash": row["object_hash"]})
    return result


def _evidence_state(conn, ids: list[str]) -> str:
    ids = sorted(set(ids))
    if not ids:
        return _digest([])
    conn.create_function("summary_record_hash", 8, lambda *values: _digest(values), deterministic=True)
    rows = [tuple(row) for row in conn.execute("SELECT id,summary_record_hash(title,body,occurred_at,end_at,category,time_precision,metadata,source_id) FROM events WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids)]
    return _digest(rows)


def evidence_state(path, ids: list[str]) -> str:
    if len(ids) > MAX_EVIDENCE:
        return "invalid"
    with _connection(path) as conn:
        return _evidence_state(conn, ids)


def _raw_sample(row: dict, provenance: list) -> bool:
    if row["category"] in RAW_CATEGORIES:
        return True
    try:
        from .classification import classify_event
    except ImportError:
        return False
    try:
        classified = classify_event({**row, "source_name": row["source"]}, provenance=provenance)
        return bool(classified.get("is_raw_sample") or classified.get("is_sample"))
    except (KeyError, ValueError, TypeError):
        return False


def retrieve(prompt: str, path=None, *, period=None, source_ids=None, start=None, end=None, limit=MAX_EVIDENCE) -> dict:
    if not isinstance(prompt, str) or len(prompt) > 8_000:
        raise ValueError("Questions must be at most 8,000 characters.")
    limit = max(1, min(int(limit), MAX_EVIDENCE))
    chosen = selection(prompt, period=period, source_ids=source_ids, start=start, end=end)
    terms = _terms(prompt)
    passes, pool = [], {}
    with _connection(path) as conn:
        known = _known_sources(conn, chosen, prompt)
        coverage = _coverage(conn, chosen, known)
        fixed = _watermark(conn, chosen, coverage)
        condition, args = _where(chosen)

        def collect(name, sql, params, cap, weight):
            if len(pool) >= MAX_CANDIDATES:
                return
            rows = conn.execute(sql, params + [min(cap, MAX_CANDIDATES - len(pool))]).fetchall()
            passes.append({"name": name, "records": len(rows), "limit": cap, "capped": len(rows) >= cap})
            for rank, item in enumerate(rows):
                record = dict(item)
                identity = record["id"]
                score = weight + 1 / (rank + 1)
                if identity in pool:
                    pool[identity]["score"] += min(weight / 4, 4)
                    if name not in pool[identity]["retrieval_passes"]:
                        pool[identity]["retrieval_passes"].append(name)
                else:
                    record.update(score=score, excerpt_strategy="query_centered" if name.startswith("lexical") else "leading", retrieval_passes=[name])
                    pool[identity] = record

        search_sql = "SELECT " + _COLUMNS + ",substr(snippet(events_fts,1,'','',' […] ',60),1,3000) AS body FROM events_fts JOIN events e ON e.rowid=events_fts.rowid JOIN sources s ON s.id=e.source_id WHERE events_fts MATCH ? AND " + condition + " ORDER BY rank LIMIT ?"
        if terms:
            collect("lexical_all_terms", search_sql, [" AND ".join(map(_quoted, terms))] + args, 72, 20)
            collect("lexical_any_term", search_sql, [" OR ".join(map(_quoted, terms))] + args, 96, 8)
            for term in terms[:6]:
                collect("lexical_term", search_sql, [_quoted(term)] + args, 20, 5)
            expanded = list(dict.fromkeys(value for term in terms for value in _EXPANSIONS.get(term, ())))
            if expanded:
                collect("lexical_related_terms", search_sql, [" OR ".join(map(_quoted, expanded))] + args, 48, 3)

        # Dates are selected before loading text. Each source contributes a
        # bounded pool across category/time buckets, including historical years.
        span = (date.fromisoformat(chosen["end_exclusive"]) - date.fromisoformat(chosen["start"])).days if chosen["start"] and chosen["end_exclusive"] else None
        bucket_chars = 10 if span is not None and span <= 62 else 7 if span is not None and span <= 730 else 4
        available_sources = sorted((item for item in coverage["sources"] if item["total_events"]), key=lambda item: (-item["total_events"], item["source_id"]))[:24]
        context_quota = max(4, min(40, (MAX_CANDIDATES - len(pool)) // max(1, len(available_sources))))
        for source in available_sources:
            if not source["total_events"]:
                continue
            buckets = [row[0] for row in conn.execute(f"SELECT DISTINCT substr(e.occurred_at,1,{bucket_chars}) FROM events e WHERE " + condition + " AND e.source_id=? AND e.occurred_at IS NOT NULL ORDER BY 1", args + [source["source_id"]])]
            buckets = _spread(buckets, 8)
            bucket_condition = f"substr(e.occurred_at,1,{bucket_chars}) IN (" + ",".join("?" for _ in buckets) + ")" if buckets else "0"
            if not chosen["start"] and not chosen["end_exclusive"]:
                bucket_condition = "(" + bucket_condition + " OR e.occurred_at IS NULL)"
            raw_marks = ",".join("?" for _ in RAW_CATEGORIES)
            sql = f"""WITH ranked AS (
                SELECT e.id,row_number() OVER(PARTITION BY e.category,substr(e.occurred_at,1,{bucket_chars}) ORDER BY e.occurred_at DESC,e.id) AS bucket_rank
                FROM events e WHERE {condition} AND e.source_id=? AND ({bucket_condition}) AND e.category NOT IN ({raw_marks})
                ) SELECT {_COLUMNS},substr(e.body,1,3000) AS body FROM ranked r
                JOIN events e ON e.id=r.id JOIN sources s ON s.id=e.source_id WHERE r.bucket_rank<=2
                ORDER BY r.bucket_rank,CASE WHEN e.category IN ('note','journal','post','message','event','git_commit','travel','place_visit') THEN 0 ELSE 1 END,e.occurred_at,e.id LIMIT ?"""
            collect("source_time_context", sql, args + [source["source_id"], *buckets, *sorted(RAW_CATEGORIES)], context_quota, 2)

        provenance = _provenance(conn, list(pool))
        candidates, raw_omitted = [], 0
        for record in pool.values():
            try:
                metadata = json.loads(record["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            record["metadata"] = metadata if isinstance(metadata, dict) else {}
            record["provenance"] = provenance.get(record["id"], [])
            if _raw_sample(record, record["provenance"]):
                raw_omitted += 1
                continue
            if not record["provenance"]:
                continue
            candidates.append(record)

        selected, seen_fingerprints = [], set()
        source_counts, category_counts, bucket_counts = {}, {}, {}
        remaining = MAX_CONTEXT_CHARS
        while candidates and len(selected) < limit:
            def score(item):
                source_penalty = source_counts.get(item["source_id"], 0) * 4
                category_penalty = category_counts.get(item["category"], 0) * (4 if item["category"] in HIGH_VOLUME else 1)
                bucket = (item["occurred_at"] or "undated")[:bucket_chars]
                return item["score"] - source_penalty - category_penalty - bucket_counts.get(bucket, 0) * 2
            item = max(candidates, key=lambda value: (score(value), value["id"]))
            candidates.remove(item)
            body = clean_text(item["body"], MAX_EXCERPT_CHARS)
            fingerprint = _digest([item["source_id"], item["title"], body, item["occurred_at"], item["category"]])
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            hints = {key: item["metadata"][key] for key in ("role", "conversation_title", "document_kind", "timezone_unknown", "original_datetime", "timestamp_role") if key in item["metadata"] and isinstance(item["metadata"][key], (str, bool))}
            evidence = {key: item[key] for key in ("id", "source_id", "source", "occurred_at", "end_at", "category", "time_precision")}
            evidence.update(title=clean_text(item["title"], 300), body=body, metadata=hints, provenance=[{key: entry[key] for key in ("file_id", "locator", "parser")} for entry in item["provenance"]], excerpt_strategy=item["excerpt_strategy"], retrieval_role="matched_terms" if item["excerpt_strategy"] == "query_centered" else "background_context", body_truncated=item["body_characters"] > len(body))
            size = len(json.dumps(evidence, ensure_ascii=False))
            if size > remaining:
                continue
            selected.append(evidence)
            remaining -= size
            source_counts[item["source_id"]] = source_counts.get(item["source_id"], 0) + 1
            category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
            bucket = (item["occurred_at"] or "undated")[:bucket_chars]
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        coverage.update(selected_events=len(selected), candidate_events=len(pool), retrieval_limited=coverage["total_events_in_scope"] > len(selected) or any(item["body_truncated"] for item in selected), raw_sample_candidates_omitted=raw_omitted, query_passes=passes, candidate_limit=MAX_CANDIDATES, evidence_limit=limit, context_character_limit=MAX_CONTEXT_CHARS, categories_selected=category_counts)
        for source in coverage["sources"]:
            source["selected_events"] = source_counts.get(source["source_id"], 0)
        return {"evidence": selected, "evidence_fingerprint": _evidence_state(conn, [item["id"] for item in selected]), "selection": chosen, "coverage": coverage, "watermark": fixed, "policy_version": POLICY_VERSION}
