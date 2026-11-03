"""Safe retained-media lookup and conservative explicit attachment association.

Files are indexed once. Event resolution uses indexed paths within the same
source and export scope, never a scan of the media library per event.
Remote references remain unresolved and are never downloaded.
"""

from __future__ import annotations

import hashlib
import csv
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
from urllib.parse import unquote, urlsplit

from .config import settings
from .locations import _connection

INDEX_VERSION = 1
ASSOCIATION_VERSION = 3
MANIFEST_VERSION = 1
MANIFEST_BYTE_LIMIT = 8 * 1024 * 1024
SAFE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
    "video/mp4", "video/quicktime", "video/webm", "video/ogg",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/flac", "audio/aac", "audio/amr",
}


def ensure_schema(conn_or_path=None):
    with _connection(conn_or_path) as conn:
        statements = [
            """CREATE TABLE IF NOT EXISTS media_object_index (
                object_hash TEXT PRIMARY KEY REFERENCES objects(sha256) ON DELETE CASCADE,
                mime TEXT,kind TEXT,probe_status TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS media_file_index (
                file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,import_id TEXT,scope TEXT NOT NULL,
                normalized_path TEXT NOT NULL,basename TEXT NOT NULL,
                object_hash TEXT NOT NULL,mime TEXT NOT NULL,kind TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS media_file_exact ON media_file_index(source_id,scope,normalized_path)",
            "CREATE INDEX IF NOT EXISTS media_file_hash ON media_file_index(source_id,scope,object_hash)",
            "CREATE INDEX IF NOT EXISTS media_file_name ON media_file_index(source_id,scope,basename COLLATE NOCASE)",
            """CREATE TABLE IF NOT EXISTS media_path_suffixes (
                file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,scope TEXT NOT NULL,suffix TEXT NOT NULL,
                PRIMARY KEY(file_id,suffix))""",
            "CREATE INDEX IF NOT EXISTS media_suffix_lookup ON media_path_suffixes(source_id,scope,suffix)",
            "CREATE TABLE IF NOT EXISTS media_file_state(file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,version INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS media_source_generation(source_id TEXT PRIMARY KEY,generation INTEGER NOT NULL)",
            """CREATE TABLE IF NOT EXISTS event_media_refs (
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,reference TEXT NOT NULL,caption TEXT,
                file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
                status TEXT NOT NULL,match_basis TEXT,
                PRIMARY KEY(event_id,ordinal))""",
            "CREATE INDEX IF NOT EXISTS event_media_file ON event_media_refs(file_id)",
            """CREATE TABLE IF NOT EXISTS media_manifest_state (
                file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,probe_status TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS media_manifest_refs (
                manifest_file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,source_id TEXT NOT NULL,native_id TEXT NOT NULL,
                reference TEXT NOT NULL,object_hash TEXT,status TEXT NOT NULL,locator TEXT NOT NULL,
                PRIMARY KEY(manifest_file_id,ordinal,native_id))""",
            "CREATE INDEX IF NOT EXISTS media_manifest_native ON media_manifest_refs(source_id,native_id)",
            """CREATE TABLE IF NOT EXISTS media_event_state (
                event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,generation INTEGER NOT NULL,
                needs_refresh INTEGER NOT NULL,metadata_sha256 TEXT NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 0)""",
        ]
        for statement in statements:
            conn.execute(statement)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(media_event_state)")}
        if "evidence_count" not in columns:
            conn.execute("ALTER TABLE media_event_state ADD COLUMN evidence_count INTEGER NOT NULL DEFAULT 0")


def sniff_media(path: Path, *, original_path="") -> tuple[str | None,str | None]:
    """Allow inert browser media by byte signatures; HTML/SVG stay downloads."""
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(4096)
    except OSError:
        return None,None
    mime = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"GIF87a",b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        mime = "audio/wav"
    elif data.startswith(b"BM") and len(data) >= 26:
        mime = "image/bmp"
    elif len(data) >= 12 and data[4:8] == b"ftyp":
        brands = data[8:64]
        if b"avif" in brands or b"avis" in brands:
            mime = "image/avif"
        elif any(brand in brands for brand in (b"heic",b"heix",b"hevc",b"hevx",b"mif1")):
            return None,None
        elif b"M4A " in brands or b"M4B " in brands or PurePosixPath(original_path).suffix.lower() in {".m4a",".m4b"}:
            mime = "audio/mp4"
        elif b"qt  " in brands:
            mime = "video/quicktime"
        else:
            mime = "video/mp4"
    elif data.startswith(b"\x1aE\xdf\xa3") and b"webm" in data.lower():
        mime = "video/webm"
    elif data.startswith(b"OggS"):
        mime = "video/ogg" if b"theora" in data else "audio/ogg"
    elif data.startswith(b"fLaC"):
        mime = "audio/flac"
    elif data.startswith(b"#!AMR"):
        mime = "audio/amr"
    elif data.startswith(b"ID3") or (len(data)>1 and data[0]==255 and data[1]&0xE6==0xE2):
        mime = "audio/mpeg"
    elif len(data)>1 and data[0]==255 and data[1] in {0xF1,0xF9}:
        mime = "audio/aac"
    return (mime,mime.split("/",1)[0]) if mime in SAFE_MIME_TYPES else (None,None)


def _logical_path(value: str) -> tuple[str,str,str]:
    value = unquote(str(value)).replace("\\","/")
    prefix,separator,inner = value.rpartition("!")
    if not separator:
        prefix,inner = "",value
    inner = posixpath.normpath(inner).lstrip("/")
    parts = [part for part in inner.split("/") if part and part != "."]
    markers = {"data","messages","your_facebook_activity","your_instagram_activity",
               "photos","videos","audio","audio_files","attachments","files","tweets_media"}
    marker = next((i for i,part in enumerate(parts) if part.casefold() in markers),None)
    if marker is None:
        export_prefix = parts[:-1] if len(parts)<=2 else parts[:1]
    else:
        export_prefix = parts[:marker]
    scope = prefix + "!" if prefix else ""
    if export_prefix:
        scope += "/".join(export_prefix) + "/"
    return scope,inner,posixpath.basename(inner)


def index_media_files(path=None, *, source_id=None, file_ids=None, refresh=False) -> dict:
    """Probe each new canonical object once and build indexed virtual paths."""
    with _connection(path) as conn:
        ensure_schema(conn)
        clauses,args = [],[]
        if source_id:
            clauses.append("f.source_id=?")
            args.append(source_id)
        if file_ids is not None:
            file_ids = list(dict.fromkeys(file_ids))
            if not file_ids:
                return {"files":0,"media_files":0}
            if len(file_ids)>800:
                result = {"files":0,"media_files":0}
                for start in range(0,len(file_ids),800):
                    part = index_media_files(conn,source_id=source_id,file_ids=file_ids[start:start+800],refresh=refresh)
                    for key in result:
                        result[key] += part[key]
                return result
            clauses.append("f.id IN (" + ",".join("?" for _ in file_ids) + ")")
            args.extend(file_ids)
        elif not refresh:
            clauses.append("(st.file_id IS NULL OR st.version<>?)")
            args.append(INDEX_VERSION)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute("""SELECT f.id,f.source_id,f.import_id,f.original_path,f.object_hash,o.path
            FROM files f JOIN objects o ON o.sha256=f.object_hash
            LEFT JOIN media_file_state st ON st.file_id=f.id""" + where,args)
        media_count = 0
        file_count = 0
        touched_sources = set()
        for row in rows:
            file_count += 1
            available = Path(row["path"]).is_file()
            probe = conn.execute("SELECT mime,kind,probe_status FROM media_object_index WHERE object_hash=?",(row["object_hash"],)).fetchone()
            if probe is None or refresh or probe["probe_status"] == "unavailable" or not available:
                mime,kind = sniff_media(Path(row["path"]),original_path=row["original_path"])
                conn.execute("""INSERT INTO media_object_index(object_hash,mime,kind,probe_status) VALUES(?,?,?,?)
                    ON CONFLICT(object_hash) DO UPDATE SET mime=excluded.mime,kind=excluded.kind,probe_status=excluded.probe_status""",
                    (row["object_hash"],mime,kind,"safe_media" if mime else "not_previewable" if available else "unavailable"))
            else:
                mime,kind = probe["mime"],probe["kind"]
            conn.execute("DELETE FROM media_file_index WHERE file_id=?",(row["id"],))
            conn.execute("DELETE FROM media_path_suffixes WHERE file_id=?",(row["id"],))
            if mime:
                scope,normalized,basename = _logical_path(row["original_path"])
                conn.execute("""INSERT INTO media_file_index
                    (file_id,source_id,import_id,scope,normalized_path,basename,object_hash,mime,kind)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(row["id"],row["source_id"],row["import_id"],scope,normalized,basename,row["object_hash"],mime,kind))
                parts = normalized.split("/")
                for length in range(2,min(len(parts),8)+1):
                    conn.execute("INSERT OR IGNORE INTO media_path_suffixes(file_id,source_id,scope,suffix) VALUES(?,?,?,?)",
                                 (row["id"],row["source_id"],scope,"/".join(parts[-length:])))
                touched_sources.add(row["source_id"])
                media_count += 1
            if available:
                conn.execute("INSERT OR REPLACE INTO media_file_state(file_id,version) VALUES(?,?)",(row["id"],INDEX_VERSION))
            else:
                # Metadata-only restores can mount the immutable objects later.
                # Missing bytes are retryable, never an unsupported-format verdict.
                conn.execute("DELETE FROM media_file_state WHERE file_id=?",(row["id"],))
        for source in touched_sources:
            conn.execute("""INSERT INTO media_source_generation(source_id,generation) VALUES(?,1)
                ON CONFLICT(source_id) DO UPDATE SET generation=generation+1""",(source,))
        return {"files":file_count,"media_files":media_count}


def _filename_list(value) -> list[str]:
    """Read an export's explicit filename list without guessing whitespace splits.

    X CSV exports use semicolon-space separators. JSON arrays, quoted CSV,
    newlines and common list delimiters are also accepted. A comma inside an
    unquoted filename is preserved when splitting would produce a nonfilename.
    """
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except (ValueError, TypeError):
            items = None
        if isinstance(items, list):
            return _filename_list(items)
    # Find delimiters outside CSV quotes. Filenames can legitimately contain
    # spaces, commas, semicolons and pipes when the export quotes them.
    counts = {delimiter: 0 for delimiter in (";", "\t", "|", ",")}
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in counts:
            counts[char] += 1
    delimiter = next((item for item in counts if counts[item]), ",")
    try:
        rows = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True, strict=True)
        result = [item.strip() for row in rows for item in row if item.strip()]
    except csv.Error:
        return [text]
    if len(result) > 1 and not ('"' in text or "\n" in text or "\r" in text):
        if any(not PurePosixPath(item.replace("\\", "/")).suffix for item in result):
            return [text]
    return result


def index_explicit_manifests(path=None, *, source_id=None) -> dict:
    """Index bounded retained manifests that explicitly name event IDs and files.

    A supported entry has references[].fullname/native_id/source_native_id and a
    file or URL; downloaded bytes additionally need a declared SHA256. No URLs
    are fetched, and a manifest file path is never opened as a filesystem path.
    """
    with _connection(path) as conn:
        ensure_schema(conn)
        clauses = ["lower(f.original_path) LIKE '%manifest.json'", "(st.file_id IS NULL OR st.version<>? OR st.probe_status='unavailable')"]
        args = [MANIFEST_VERSION]
        if source_id:
            clauses.append("f.source_id=?")
            args.append(source_id)
        rows = conn.execute("""SELECT f.id,f.source_id,f.original_path,o.path FROM files f
            JOIN objects o ON o.sha256=f.object_hash
            LEFT JOIN media_manifest_state st ON st.file_id=f.id WHERE """ + " AND ".join(clauses), args)
        counts = {"manifest_files": 0, "manifest_references": 0}
        touched = set()
        for row in rows:
            if posixpath.basename(row["original_path"].replace("\\", "/")) != "manifest.json":
                continue
            physical = Path(row["path"])
            if not physical.is_file() or physical.is_symlink():
                conn.execute("INSERT OR REPLACE INTO media_manifest_state VALUES(?,?,?)", (row["id"], MANIFEST_VERSION, "unavailable"))
                continue
            counts["manifest_files"] += 1
            entries = []
            status = "unsupported"
            try:
                if physical.stat().st_size > MANIFEST_BYTE_LIMIT:
                    raise ValueError("bounded manifest limit")
                payload = json.loads(physical.read_text(encoding="utf-8-sig"))
                items = payload.get("items") if isinstance(payload, dict) else None
                if isinstance(items, list):
                    for ordinal, item in enumerate(items):
                        if not isinstance(item, dict) or not isinstance(item.get("references"), list):
                            continue
                        local = item.get("file")
                        remote = item.get("url")
                        reference = local if isinstance(local, str) and local.strip() else remote if isinstance(remote, str) else None
                        if not reference or len(reference) > 8_000:
                            continue
                        digest = item.get("sha256")
                        digest = digest.lower() if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) else None
                        declared = "downloaded" if item.get("status") == "downloaded" and isinstance(local, str) and local.strip() and digest else "unavailable"
                        natives = set()
                        for ref in item["references"]:
                            if isinstance(ref, dict):
                                native = ref.get("fullname") or ref.get("native_id") or ref.get("source_native_id")
                                if isinstance(native, str):
                                    natives.add(native)
                        for native in natives:
                            if isinstance(native, str) and native.strip() and len(native) <= 300:
                                entries.append((row["id"], ordinal, row["source_id"], native, reference.strip(), digest, declared, f"$.items[{ordinal}]"))
                    if entries:
                        status = "explicit_references"
            except (OSError, UnicodeError, ValueError, TypeError):
                # Source content and error payloads stay private. Unsupported
                # manifests are retained; they never authorize a guessed link.
                entries = []
            conn.execute("DELETE FROM media_manifest_refs WHERE manifest_file_id=?", (row["id"],))
            conn.executemany("INSERT OR IGNORE INTO media_manifest_refs VALUES(?,?,?,?,?,?,?,?)", entries)
            conn.execute("INSERT OR REPLACE INTO media_manifest_state VALUES(?,?,?)", (row["id"], MANIFEST_VERSION, status))
            counts["manifest_references"] += len(entries)
            if entries:
                touched.add(row["source_id"])
        for source in touched:
            conn.execute("""INSERT INTO media_source_generation(source_id,generation) VALUES(?,1)
                ON CONFLICT(source_id) DO UPDATE SET generation=generation+1""", (source,))
        return counts


def _manifest_references(conn, event, evidence):
    if not event["native_id"]:
        return []
    containers = {row["original_path"].rpartition("!")[0] for row in evidence}
    rows = conn.execute("""SELECT r.*,f.original_path AS manifest_path FROM media_manifest_refs r
        JOIN files f ON f.id=r.manifest_file_id WHERE r.source_id=? AND r.native_id=?
        ORDER BY r.manifest_file_id,r.ordinal""", (event["source_id"], event["native_id"])).fetchall()
    return [
        {"reference": row["reference"], "caption": None, "manifest_hash": row["object_hash"], "manifest_status": row["status"], "manifest_file_id": row["manifest_file_id"], "locator": row["locator"]}
        for row in rows if row["manifest_path"].rpartition("!")[0] in containers
    ]


def _resolve_manifest(conn, event, reference):
    if reference["manifest_status"] != "downloaded" or not reference["manifest_hash"]:
        return None, "manifest_unavailable"
    rows = conn.execute("""SELECT * FROM media_file_index WHERE source_id=? AND object_hash=?
        ORDER BY file_id LIMIT 21""", (event["source_id"], reference["manifest_hash"])).fetchall()
    chosen, basis = _choose(rows, "explicit_manifest_native_id_and_hash")
    return chosen, basis or "manifest_missing_retained_media"


def explicit_references(metadata: dict) -> list[dict]:
    """Extract declared media/file fields without treating arbitrary URLs as files."""
    result,seen = [],set()
    if not isinstance(metadata,dict):
        return result
    groups = {"photos","videos","audio_files","gifs","files","attachments","sticker",
              "media_refs","content_parts","message_metadata","extended_entities","entities","media"}
    reference_keys = {"uri","path","file_path","local_path","filename","file_name","asset_pointer","audio_asset_pointer",
                      "image_asset_pointer","file_id","sha256","media_url","media_url_https"}
    def visit(value,trail,active=False):
        if isinstance(value,list):
            for index,item in enumerate(value):
                visit(item,f"{trail}[{index}]",active)
        elif isinstance(value,dict):
            for key,item in value.items():
                normalized_key = re.sub(r"[\s_-]+", " ", str(key)).casefold()
                if normalized_key in {"media file names", "media filenames", "media file name", "media filename"}:
                    for index, reference in enumerate(_filename_list(item)):
                        if reference not in seen:
                            seen.add(reference)
                            result.append({"reference": reference, "locator": trail + "." + str(key) + f"[{index}]", "caption": None})
                elif key in reference_keys and active and isinstance(item,str):
                    reference = item.strip()
                    if reference and reference not in seen:
                        seen.add(reference)
                        result.append({"reference":reference,"locator":trail+"."+key,
                                       "caption":value.get("caption") or value.get("name") or value.get("title")})
                elif key == "id" and active and isinstance(item,str) and item.startswith("file-"):
                    if item not in seen:
                        seen.add(item)
                        result.append({"reference":item,"locator":trail+".id","caption":value.get("name")})
                elif key == "url" and active and ".video_info.variants[" in trail and isinstance(item,str):
                    if urlsplit(item).hostname == "video.twimg.com" and item not in seen:
                        seen.add(item)
                        result.append({"reference":item,"locator":trail+".url","caption":None})
                elif isinstance(item,(dict,list)):
                    visit(item,trail+"."+key,active or key in groups)
    visit(metadata,"$")
    return result


def _choose(rows,basis):
    if not rows:
        return None,None
    if len(rows)>20 or len({row["object_hash"] for row in rows}) != 1:
        return None,"ambiguous"
    return rows[0],basis


def _resolve_for_evidence(conn,event,evidence,reference):
    raw = reference.strip()
    parsed = urlsplit(raw)
    remote = parsed.scheme in {"http","https"}
    pointer = parsed.scheme in {"file-service", "sediment"}
    if parsed.scheme and not remote and not pointer and parsed.scheme != "file":
        return None,"unsupported_reference"
    asset = None
    modern_asset = False
    if pointer:
        if parsed.query or parsed.fragment or (parsed.netloc and parsed.path not in {"", "/"}):
            return None, "unsupported_reference"
        asset = parsed.netloc or parsed.path.lstrip("/")
        modern_asset = bool(re.fullmatch(r"file_[0-9a-fA-F]{32}", asset))
        if parsed.scheme == "sediment" and not modern_asset:
            return None, "unsupported_reference"
        if not modern_asset and not re.fullmatch(r"file-[A-Za-z0-9_-]+", asset):
            return None, "unsupported_reference"
    elif re.fullmatch(r"file_[0-9a-fA-F]{32}", raw):
        asset, modern_asset = raw, True
    elif re.fullmatch(r"file-[A-Za-z0-9_-]+", raw):
        asset = raw
    value = unquote(parsed.path if parsed.scheme == "file" else raw).replace("\\","/")
    if "\x00" in value:
        return None,"invalid_reference"
    for source_file in evidence:
        scope,document,_ = _logical_path(source_file["original_path"])
        args = [event["source_id"],scope]
        base = "m.source_id=? AND m.scope=?"
        # import_id identifies an attempt, not an export. A safely resumed
        # import can index the document and its image in different attempts.
        def query(condition,extra):
            return conn.execute("SELECT m.* FROM media_file_index m WHERE " + base + " AND " + condition + " ORDER BY m.file_id LIMIT 21",args+extra).fetchall()
        if remote:
            if parsed.hostname not in {"pbs.twimg.com","video.twimg.com"}:
                continue
            native = event["native_id"] or ""
            basename = posixpath.basename(parsed.path)
            if native and basename:
                candidates = query("m.basename=?",[str(native)+"-"+basename])
                chosen,basis = _choose(candidates,"explicit_twitter_export_name")
                if chosen is not None or basis == "ambiguous":
                    return chosen,basis
            continue
        if asset:
            candidates = query("m.basename LIKE ? ESCAPE '\\'", [asset.replace("_", "\\_") + "%"])
            if modern_asset:
                # Current exports retain sediment assets as file_<32hex>.<ext>.
                # Exact stem equality avoids treating a longer ID or a similar
                # filename as evidence for this pointer.
                candidates = [row for row in candidates if PurePosixPath(row["basename"]).stem == asset]
            else:
                candidates = [row for row in candidates if row["basename"] == asset or (row["basename"].startswith(asset) and row["basename"][len(asset):len(asset)+1] in {"-", "_", "."})]
            chosen,basis = _choose(candidates, "explicit_sediment_asset_id" if parsed.scheme == "sediment" else "explicit_asset_id")
            if chosen is not None or basis == "ambiguous":
                return chosen,basis
            continue
        digest = re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",value,re.I)
        if digest:
            chosen,basis = _choose(query("m.object_hash=?",[digest.group().lower()]),"explicit_content_hash")
            if chosen is not None:
                return chosen,basis
        normalized = posixpath.normpath(value.lstrip("/"))
        if normalized == ".." or normalized.startswith("../"):
            # Relative parent references are allowed only after resolving against
            # the evidence document, still inside its virtual export root.
            normalized = None
        ancestors = list(PurePosixPath(document).parents)
        exact = []
        if normalized:
            exact.append(normalized)
        for ancestor in ancestors:
            candidate = posixpath.normpath(posixpath.join(str(ancestor),value))
            if candidate != ".." and not candidate.startswith("../"):
                exact.append(candidate.lstrip("/"))
        exact_matches = {}
        for candidate in dict.fromkeys(exact):
            for row in query("m.normalized_path=?",[candidate]):
                exact_matches[row["file_id"]] = row
        chosen,basis = _choose(list(exact_matches.values()),"explicit_local_path")
        if chosen is not None or basis == "ambiguous":
            return chosen,basis
        if normalized and "/" not in normalized and not pointer:
            # A bare filename explicitly supplied by an export is exact evidence.
            # Keep it inside the same source and virtual export, and reject
            # competing bytes instead of choosing an arbitrary same-name file.
            chosen,basis = _choose(query("m.basename=?", [normalized]), "explicit_export_basename")
            if chosen is not None or basis == "ambiguous":
                return chosen,basis
        if normalized and "/" in normalized:
            rows = conn.execute("""SELECT m.* FROM media_path_suffixes x
                JOIN media_file_index m ON m.file_id=x.file_id WHERE """ + base +
                " AND x.suffix=? ORDER BY m.file_id LIMIT 21",args+[normalized]).fetchall()
            chosen,basis = _choose(rows,"unique_export_suffix")
            if chosen is not None or basis == "ambiguous":
                return chosen,basis
    return None,"remote_reference" if remote else "not_found"


def _resolve(conn,event,evidence,reference):
    matches = []
    reason = "not_found"
    for source_file in evidence:
        matched,basis = _resolve_for_evidence(conn,event,[source_file],reference)
        if basis == "ambiguous":
            return None,"ambiguous"
        if matched is not None:
            matches.append((matched,basis))
        else:
            reason = basis
    if len({row["object_hash"] for row,_ in matches})>1:
        return None,"ambiguous"
    return matches[0] if matches else (None,reason)


def reconcile_media(path=None, *, event_ids=None, source_id=None, refresh=False,
                     index_files=True) -> dict:
    """Reconcile explicit refs after a batch/import; unresolved refs stay visible."""
    with _connection(path) as conn:
        ensure_schema(conn)
        conn.create_function("biography_media_metadata_sha256", 1,
                             lambda value: hashlib.sha256((value or "{}").encode()).hexdigest(), deterministic=True)
        indexed = index_media_files(conn,source_id=source_id) if index_files else {"files":0,"media_files":0}
        manifests = index_explicit_manifests(conn,source_id=source_id) if index_files else {"manifest_files":0,"manifest_references":0}
        indexed.update(manifests)
        clauses,args = [],[]
        if source_id:
            clauses.append("e.source_id=?")
            args.append(source_id)
        if event_ids is not None:
            event_ids = list(dict.fromkeys(event_ids))
            if not event_ids:
                return {**indexed,"events":0,"resolved":0,"unresolved":0}
            if len(event_ids)>800:
                totals = {**indexed,"events":0,"resolved":0,"unresolved":0}
                for start in range(0,len(event_ids),800):
                    part = reconcile_media(conn,event_ids=event_ids[start:start+800],source_id=source_id,index_files=False)
                    for key in ("events","resolved","unresolved"):
                        totals[key] += part[key]
                return totals
            clauses.append("e.id IN (" + ",".join("?" for _ in event_ids) + ")")
            args.extend(event_ids)
        elif not refresh:
            clauses.append("(st.event_id IS NULL OR st.version<>? OR (st.needs_refresh=1 AND st.generation<>coalesce(g.generation,0)) OR st.evidence_count<>(SELECT count(*) FROM evidence ev WHERE ev.event_id=e.id) OR st.metadata_sha256<>biography_media_metadata_sha256(e.metadata))")
            args.append(ASSOCIATION_VERSION)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute("""SELECT e.*,coalesce(g.generation,0) AS media_generation FROM events e
            LEFT JOIN media_event_state st ON st.event_id=e.id
            LEFT JOIN media_source_generation g ON g.source_id=e.source_id""" + where,args)
        counts = {**indexed,"events":0,"resolved":0,"unresolved":0}
        for event in rows:
            try:
                metadata = json.loads(event["metadata"] or "{}")
            except (TypeError,ValueError):
                metadata = {}
            evidence_count = conn.execute("SELECT count(*) FROM evidence WHERE event_id=?",(event["id"],)).fetchone()[0]
            evidence = conn.execute("""SELECT DISTINCT f.id,f.original_path,f.import_id,f.object_hash FROM evidence v
                JOIN files f ON f.id=v.file_id WHERE v.event_id=? ORDER BY f.original_path""",(event["id"],)).fetchall()
            conn.execute("DELETE FROM event_media_refs WHERE event_id=?",(event["id"],))
            refs = explicit_references(metadata) + _manifest_references(conn,event,evidence)
            resolved_hashes = set()
            ordinal = 0
            # Native-ID manifests may arrive later in the same source import.
            needs_refresh = bool(event["native_id"])
            if event["category"] == "media":
                for source_file in evidence:
                    own = conn.execute("SELECT * FROM media_file_index WHERE file_id=?",(source_file["id"],)).fetchone()
                    if own is not None and own["object_hash"] not in resolved_hashes:
                        conn.execute("INSERT INTO event_media_refs(event_id,ordinal,reference,caption,file_id,status,match_basis) VALUES(?,?,?,?,?,?,?)",
                                     (event["id"],ordinal,source_file["original_path"],event["title"],own["file_id"],"resolved","direct_source_evidence"))
                        ordinal += 1
                        resolved_hashes.add(own["object_hash"])
                        counts["resolved"] += 1
                    elif own is None:
                        probe = conn.execute("SELECT probe_status FROM media_object_index WHERE object_hash=?",(source_file["object_hash"],)).fetchone()
                        needs_refresh |= probe is None or probe["probe_status"] == "unavailable"
            for ref in refs:
                matched,basis = _resolve_manifest(conn,event,ref) if "manifest_status" in ref else _resolve(conn,event,evidence,ref["reference"])
                if matched is not None and matched["object_hash"] in resolved_hashes:
                    continue
                status = "resolved" if matched is not None else "ambiguous" if basis == "ambiguous" else "unresolved"
                conn.execute("INSERT INTO event_media_refs(event_id,ordinal,reference,caption,file_id,status,match_basis) VALUES(?,?,?,?,?,?,?)",
                             (event["id"],ordinal,ref["reference"],str(ref["caption"]) if ref["caption"] is not None else None,
                              matched["file_id"] if matched is not None else None,status,basis))
                ordinal += 1
                counts["resolved" if matched is not None else "unresolved"] += 1
                if matched is not None:
                    resolved_hashes.add(matched["object_hash"])
                needs_refresh |= matched is None or basis in {"unique_export_suffix","explicit_asset_id","explicit_sediment_asset_id","explicit_export_basename"}
            digest = hashlib.sha256((event["metadata"] or "{}").encode()).hexdigest()
            conn.execute("INSERT OR REPLACE INTO media_event_state(event_id,version,generation,needs_refresh,metadata_sha256,evidence_count) VALUES(?,?,?,?,?,?)",
                         (event["id"],ASSOCIATION_VERSION,event["media_generation"],int(needs_refresh),digest,evidence_count))
            counts["events"] += 1
        return counts


def media_for_events(event_ids, path=None, *, include_unresolved=False) -> dict:
    event_ids = list(dict.fromkeys(event_ids))
    result = {event_id:[] for event_id in event_ids}
    if not event_ids:
        return result
    with _connection(path) as conn:
        ensure_schema(conn)
        for start in range(0,len(event_ids),800):
            batch = event_ids[start:start+800]
            rows = conn.execute("""SELECT r.*,m.mime,m.kind FROM event_media_refs r
                LEFT JOIN media_file_index m ON m.file_id=r.file_id WHERE r.event_id IN (""" +
                ",".join("?" for _ in batch) + ") ORDER BY r.event_id,r.ordinal",batch).fetchall()
            for row in rows:
                if row["status"] == "resolved" and row["mime"] in SAFE_MIME_TYPES:
                    result[row["event_id"]].append({"file_id":row["file_id"],"url":f"/api/files/{row['file_id']}/media",
                        "mime":row["mime"],"kind":row["kind"],"caption":row["caption"],"status":"resolved","match_basis":row["match_basis"]})
                elif include_unresolved:
                    result[row["event_id"]].append({"file_id":None,"url":None,"mime":None,"kind":None,
                        "caption":row["caption"],"status":row["status"],"reference":row["reference"],"match_basis":row["match_basis"]})
    return result


def media_for_event(event_id,path=None,*,include_unresolved=False):
    return media_for_events([event_id],path,include_unresolved=include_unresolved)[event_id]


def media_file_info(file_id,path=None,*,objects_root=None):
    """Return an approved local media path for a range-capable FileResponse."""
    with _connection(path) as conn:
        ensure_schema(conn)
        row = conn.execute("""SELECT f.original_path,o.path,m.mime,m.kind FROM files f
            JOIN objects o ON o.sha256=f.object_hash JOIN media_file_index m ON m.file_id=f.id
            WHERE f.id=?""",(file_id,)).fetchone()
        if row is None:
            return None
        original = Path(row["path"])
        if original.is_symlink():
            return None
        retained = original.resolve()
        root = Path(objects_root or settings.data_dir/"objects").resolve()
        if not retained.is_relative_to(root) or not retained.is_file():
            return None
        mime,kind = sniff_media(retained,original_path=row["original_path"])
        if mime not in SAFE_MIME_TYPES:
            return None
        return {"path":retained,"mime":mime,"kind":kind,
                "filename":posixpath.basename(row["original_path"].replace("\\","/")).replace("\r","").replace("\n","")}


__all__ = ["ensure_schema","index_media_files","index_explicit_manifests","explicit_references","reconcile_media",
           "media_for_events","media_for_event","media_file_info","sniff_media","SAFE_MIME_TYPES"]
