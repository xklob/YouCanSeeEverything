"""Read-only historical snapshots from existing Git repositories and Ethereum.

These collectors never modify source repositories, sign transactions, read
credential files, or use paid APIs. Missing provider pages mean unknown coverage.

Configuration contract: callers load their private JSON profile and pass its
``collectors.local_git`` keys (repositories, manifest_path, excluded_repositories,
author_aliases, infer_author_aliases), ``collectors.github`` keys (repositories,
author_login, excluded_repositories), or ``collectors.ethereum`` keys (address,
ens_name, provider_root) as explicit keyword arguments. Output locations remain
explicit. No profile owner, wallet, personal path, or exclusion is built in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLOCKSCOUT_ROOT = "https://eth.blockscout.com/api/v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(_json_bytes(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        for row in rows:
            output.write(_json_bytes(row))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _git(path: Path | None, *arguments: str, timeout: int = 120,
         input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", "--no-optional-locks"]
    if path is not None:
        command.extend(["-C", str(path)])
    command.extend(arguments)
    return subprocess.run(command, input=input_bytes, capture_output=True,
                          timeout=timeout, check=check)


def _safe_remote(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host += ":" + str(parsed.port)
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    # Drop SSH user information, including token-shaped user fields.
    return value.split("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]


def _repository_identity(remote: str) -> str | None:
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote, re.I)
    return match.group(1) if match else None


def _identity(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    match = re.fullmatch(r"(.*) <([^<>]*)> (-?\d+) ([+-]\d{4})", text)
    if not match:
        return {"name": None, "email": None, "date": None, "raw": text}
    name, email, epoch, offset = match.groups()
    minutes = int(offset[1:3]) * 60 + int(offset[3:])
    minutes *= -1 if offset[0] == "-" else 1
    try:
        date = datetime.fromtimestamp(int(epoch), timezone(timedelta(minutes=minutes))).isoformat()
    except (ValueError, OverflowError, OSError):
        date = None
    return {"name": name, "email": email, "date": date,
            "epoch": int(epoch), "timezone_offset": offset}


def _parse_commit(commit_hash: str, data: bytes, refs: list[str]) -> dict:
    headers, _, message = data.partition(b"\n\n")
    author, committer = {}, {}
    for line in headers.splitlines():
        if line.startswith(b"author "):
            author = _identity(line[7:])
        elif line.startswith(b"committer "):
            committer = _identity(line[10:])
    body = message.decode("utf-8", "replace")
    return {"kind": "git_commit", "commit_hash": commit_hash,
            "stable_id": "git:" + commit_hash, "author": author,
            "committer": committer, "subject": body.splitlines()[0] if body else "",
            "body": body, "refs": refs, "repositories": []}


def _parse_batch(data: bytes, expected: list[str]) -> dict[str, bytes]:
    result = {}
    offset = 0
    for wanted in expected:
        end = data.find(b"\n", offset)
        if end < 0:
            raise ValueError("Truncated Git batch header")
        fields = data[offset:end].split()
        if len(fields) != 3 or fields[0].decode() != wanted or fields[1] != b"commit":
            raise ValueError("Unexpected Git batch object")
        size = int(fields[2])
        start = end + 1
        if start + size >= len(data) or data[start + size:start + size + 1] != b"\n":
            raise ValueError("Truncated Git batch object")
        result[wanted] = data[start:start + size]
        offset = start + size + 1
    if offset != len(data):
        raise ValueError("Unaccounted Git batch bytes")
    return result


def collect_local_git(output: Path, *, repositories: list[dict] | None = None,
                      manifest_path: Path | None = None,
                      excluded_repositories: list[str] | tuple[str, ...] = (),
                      author_aliases: dict | None = None,
                      infer_author_aliases: bool = True,
                      max_commits_per_repository: int = 200_000) -> dict:
    """Snapshot configured repositories; infer identity only when requested.

    No repositories, owner identities, or personal exclusions are built in.
    Supply a repository list or an explicit JSON manifest. Explicit aliases use
    ``{"names": [...], "emails": [...]}``; set infer_author_aliases=False to
    avoid augmenting them from local/global Git configuration.
    """
    output = Path(output).expanduser()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if repositories is None:
        repositories = json.loads(Path(manifest_path).expanduser().read_text()) if manifest_path else []
    excluded = {str(name).casefold() for name in excluded_repositories}
    aliases = {key: {str(value).casefold() for value in (author_aliases or {}).get(key, [])} for key in ("names", "emails")}
    identity_paths = ([None] + [Path(item["path"]).expanduser() for item in repositories
                         if (item.get("repository") or "").casefold() not in excluded]) if infer_author_aliases and repositories else []
    for path in identity_paths:
        for key, group in [("user.name", "names"), ("user.email", "emails")]:
            try:
                value = _git(path, "config", "--get", key, timeout=10, check=False)
                if value.returncode == 0 and value.stdout.strip():
                    aliases[group].add(value.stdout.decode("utf-8", "replace").strip().casefold())
            except (OSError, subprocess.SubprocessError):
                pass
    report = {"service": "local_git", "started_at": _now(), "status": "running",
              "scope": "commits reachable from local --all refs at snapshot time; no code or diffs",
              "author_aliases": {key: sorted(values) for key, values in aliases.items()},
              "repositories": [], "excluded": [], "errors": [], "files": []}
    commits: dict[str, dict] = {}
    for item in repositories:
        path = Path(item["path"]).expanduser()
        repository = item.get("repository")
        evidence = {"path": str(path), "repository": repository,
                    "source_type": item.get("source_type", "local_repository")}
        if repository and repository.casefold() in excluded:
            report["excluded"].append({**evidence, "reason": "explicit_user_exclusion"})
            continue
        try:
            remote = _git(path, "remote", "get-url", "origin", timeout=10, check=False)
            if remote.returncode == 0:
                evidence["remote"] = _safe_remote(remote.stdout.decode("utf-8", "replace"))
                repository = repository or _repository_identity(evidence["remote"])
                evidence["repository"] = repository
            if repository and repository.casefold() in excluded:
                report["excluded"].append({**evidence, "reason": "explicit_user_exclusion"})
                continue
            refs_before = _git(path, "show-ref", "--head", "--dereference", check=False)
            refs_map: dict[str, list[str]] = {}
            for line in refs_before.stdout.decode("utf-8", "replace").splitlines():
                digest, _, ref = line.partition(" ")
                refs_map.setdefault(digest, []).append(ref)
            hashes = _git(path, "rev-list", "--all",
                          f"--max-count={max_commits_per_repository + 1}").stdout.decode().splitlines()
            truncated = len(hashes) > max_commits_per_repository
            hashes = hashes[:max_commits_per_repository]
            raw = _git(path, "cat-file", "--batch",
                       input_bytes=("\n".join(hashes) + ("\n" if hashes else "")).encode())
            parsed = _parse_batch(raw.stdout, hashes)
            personal = 0
            for digest in hashes:
                row = _parse_commit(digest, parsed[digest], refs_map.get(digest, []))
                author = row["author"]
                email_match = (author.get("email") or "").casefold() in aliases["emails"]
                name_match = (author.get("name") or "").casefold() in aliases["names"]
                row["personal_attribution"] = email_match or name_match
                row["attribution_reason"] = ("configured_author_email" if email_match else
                                               "configured_author_name" if name_match else
                                               "other_author_retained_as_repository_context")
                personal += int(row["personal_attribution"])
                if digest in commits:
                    previous = commits[digest]
                    previous["refs"] = sorted(set(previous["refs"] + row["refs"]))
                    previous["repositories"].append(dict(evidence))
                else:
                    row["repositories"].append(dict(evidence))
                    commits[digest] = row
            refs_after = _git(path, "show-ref", "--head", "--dereference", check=False)
            changed = refs_before.stdout != refs_after.stdout
            report["repositories"].append({**evidence, "commits": len(hashes),
                                            "personal_author_commits": personal,
                                            "other_author_commits": len(hashes) - personal,
                                            "status": "partial" if truncated or changed else "complete",
                                            "truncated": truncated, "live_refs_changed": changed,
                                            "ref_snapshot_sha256": hashlib.sha256(refs_before.stdout).hexdigest(),
                                            "refs": {ref: digest for digest, refs in refs_map.items() for ref in refs}})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # Command output can contain arbitrary source text; record type and
            # exit code without leaking source messages or URLs into logs.
            report["errors"].append({**evidence, "error_type": type(error).__name__,
                                      "returncode": getattr(error, "returncode", None)})
    rows = sorted(commits.values(), key=lambda row: (row["author"].get("date") or "", row["commit_hash"]))
    records_path = output / "commits.jsonl"
    _write_rows(records_path, rows)
    report.update({"finished_at": _now(), "unique_commits": len(rows),
                   "personal_author_commits": sum(row["personal_attribution"] for row in rows),
                   "other_author_commits": sum(not row["personal_attribution"] for row in rows),
                   "files": [str(records_path)],
                   "status": ("not_configured" if not repositories else "partial" if report["errors"] or any(
                       row["status"] != "complete" for row in report["repositories"]) else "complete")})
    _write_json(output / "report.json", report)
    return report


def collect_github(output: Path, *, repositories: list[dict] | None = None,
                   author_login: str | None = None,
                   excluded_repositories: list[str] | tuple[str, ...] = (),
                   fetcher: Callable[[str, int], list] | None = None,
                   max_pages_per_repository: int = 10,
                   max_seconds: float = 600) -> dict:
    """Bounded metadata-only pass using existing gh authentication and owner list.

    The GitHub commits endpoint covers each configured default branch. A
    configured author_login filters requests and enables linked-account
    attribution; an absent account leaves commits as project context.
    """
    output = Path(output).expanduser()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    repositories = repositories or []
    excluded = {str(name).casefold() for name in excluded_repositories}
    if author_login is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", author_login):
        raise ValueError("Invalid configured GitHub author login")

    def gh_fetch(repository: str, page: int) -> list:
        command = ["gh", "api", "--method", "GET", f"repos/{repository}/commits",
                   "-F", "per_page=100", "-F", f"page={page}"]
        if author_login:
            command.extend(["-f", f"author={author_login}"])
        response = subprocess.run(command,
            capture_output=True, check=True, timeout=30)
        return json.loads(response.stdout)

    fetch = fetcher or gh_fetch
    report = {"service": "github", "started_at": _now(), "status": "running",
              "scope": "configured default-branch commit metadata; no code cloned",
              "author_filter": author_login,
              "limits": {"max_seconds_total": max_seconds,
                         "max_pages_per_repository": max_pages_per_repository},
              "repositories": [], "errors": [], "excluded": [], "files": []}
    commits = {}
    started = time.monotonic()
    names = sorted({item.get("name") or item.get("repository") for item in repositories
                    if item.get("name") or item.get("repository")})
    for name in names:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", name)
                or name.split("/")[-1] in {".", ".."}):
            report["excluded"].append({"repository": name, "reason": "invalid_repository_identifier"})
            continue
        if name.casefold() in excluded:
            report["excluded"].append({"repository": name, "reason": "explicit_user_exclusion"})
            continue
        coverage = {"repository": name, "pages": 0, "records": 0,
                    "status": "partial", "next_page": 1, "reason": None}
        report["repositories"].append(coverage)
        for page in range(1, max_pages_per_repository + 1):
            if time.monotonic() - started >= max_seconds:
                coverage["reason"] = "time_budget"
                break
            try:
                payload = fetch(name, page)
                if not isinstance(payload, list):
                    raise ValueError("GitHub commit response is not a list")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                coverage["reason"] = "provider_error"
                coverage["status"] = "failed" if page == 1 else "partial"
                failure = {"repository": name, "page": page, "captured_at": _now(),
                           "error_type": type(error).__name__,
                           "returncode": getattr(error, "returncode", None)}
                if isinstance(error, subprocess.CalledProcessError):
                    try:
                        failure["provider_response"] = json.loads(error.stdout)
                    except (ValueError, TypeError):
                        pass
                    status = re.search(rb"HTTP (\d{3})", error.stderr or b"")
                    failure["http_status"] = int(status.group(1)) if status else None
                report["errors"].append(failure)
                _write_json(output / "raw" / name / f"error-{page:04d}.json", failure)
                break
            raw_path = output / "raw" / name / f"page-{page:04d}.json"
            _write_json(raw_path, {"provider": "https://api.github.com",
                                   "endpoint": f"repos/{name}/commits", "page": page,
                                   "author_filter": author_login, "captured_at": _now(),
                                   "response": payload})
            coverage["pages"] += 1
            for item in payload:
                digest = item.get("sha") if isinstance(item, dict) else None
                if not digest:
                    coverage["reason"] = "invalid_item"
                    continue
                metadata = item.get("commit") or {}
                body = metadata.get("message") or ""
                author_account = (item.get("author") or {}).get("login")
                evidence = {"repository": name, "remote": f"https://github.com/{name}",
                            "source_type": "github_default_branch", "source_page": str(raw_path.relative_to(output))}
                if digest in commits:
                    commits[digest]["repositories"].append(evidence)
                    continue
                commits[digest] = {"kind": "git_commit", "commit_hash": digest,
                                   "stable_id": "git:" + digest,
                                   "author": metadata.get("author") or {},
                                   "committer": metadata.get("committer") or {},
                                   "subject": body.splitlines()[0] if body else "", "body": body,
                                   "refs": [], "repositories": [evidence],
                                   "personal_attribution": bool(author_login) and (author_account or "").casefold() == author_login.casefold(),
                                   "attribution_reason": ("github_author_account" if author_login and (author_account or "").casefold() == author_login.casefold()
                                                          else "author_not_linked_to_configured_account"),
                                   "github_author_login": author_account, "url": item.get("html_url")}
            coverage["records"] += len(payload)
            coverage["next_page"] = page + 1
            if len(payload) < 100:
                coverage["status"] = "complete" if coverage["reason"] is None else "partial"
                coverage["next_page"] = None
                break
        else:
            coverage["reason"] = "page_limit"
        _write_json(output / "report.json", report)
    rows = sorted(commits.values(), key=lambda row: (row["author"].get("date") or "", row["commit_hash"]))
    records_path = output / "commits.jsonl"
    _write_rows(records_path, rows)
    report.update({"finished_at": _now(), "unique_commits": len(rows),
                   "personal_author_commits": sum(row["personal_attribution"] for row in rows),
                   "files": [str(records_path)],
                   "status": "not_configured" if not repositories else "complete" if all(row["status"] == "complete" for row in report["repositories"]) else "partial"})
    _write_json(output / "report.json", report)
    return report


def _fetch_json(url: str, *, timeout: float = 20, retries: int = 3) -> dict:
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "BiographyOfYou/0.1 read-only archive", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            retryable = not isinstance(error, urllib.error.HTTPError) or error.code == 429 or error.code >= 500
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if attempt >= retries or not retryable:
                raise
            time.sleep(min(2**attempt, 4))
    raise RuntimeError("Unreachable retry state")


def _ethereum_identity(kind: str, item: dict) -> str | None:
    tx_hash = item.get("hash") if kind == "transactions" else item.get("transaction_hash")
    if not tx_hash:
        transaction = item.get("transaction")
        tx_hash = transaction.get("hash") if isinstance(transaction, dict) else None
    if not tx_hash:
        return None
    if kind == "transactions":
        return "1:tx:" + str(tx_hash).lower()
    if kind == "token-transfers":
        index = item.get("log_index")
        return None if index is None else f"1:log:{str(tx_hash).lower()}:{index}"
    trace = item.get("trace_address", item.get("index"))
    return None if trace is None else f"1:trace:{str(tx_hash).lower()}:{json.dumps(trace, separators=(',', ':'))}"


def _retain_ethereum_items(payload: dict, endpoint: str, kind: str, address: str,
                           raw_record: dict, raw_path: Path, output: Path,
                           coverage: dict, rows: list[dict], seen_payloads: set,
                           provider_root: str = BLOCKSCOUT_ROOT) -> None:
    for item in payload["items"]:
        if not isinstance(item, dict):
            coverage["reason"] = "invalid_item"
            continue
        identity = _ethereum_identity(endpoint, item)
        digest = hashlib.sha256(_json_bytes(item)).hexdigest()
        # Preserve distinct source variants sharing an event identity.
        if digest in seen_payloads:
            coverage["duplicate_records"] += 1
            continue
        seen_payloads.add(digest)
        coverage["missing_identity_records"] += int(identity is None)
        timestamp = item.get("timestamp")
        if isinstance(timestamp, str):
            coverage["first_timestamp"] = min(coverage["first_timestamp"] or timestamp, timestamp)
            coverage["last_timestamp"] = max(coverage["last_timestamp"] or timestamp, timestamp)
        rows.append({**item, "kind": kind, "chain_id": 1, "address": address,
                     "stable_id": identity, "variant_sha256": digest,
                     "source_page": str(raw_path.relative_to(output)),
                     "provider": provider_root, "captured_at": raw_record["received_at"]})


def collect_ethereum(output: Path, address: str, *,
                     ens_name: str | None = None,
                     provider_root: str = BLOCKSCOUT_ROOT,
                     fetcher: Callable[[str], dict] | None = None,
                     max_pages: int = 1000, max_seconds: float = 600,
                     timeout: float = 20, retries: int = 3,
                     resume: bool = False) -> dict:
    """Capture public chain-1 pages with raw provenance and honest completeness."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("Ethereum address must have 40 hexadecimal characters")
    provider_root = provider_root.rstrip("/")
    parsed_provider = urllib.parse.urlsplit(provider_root)
    if parsed_provider.scheme not in {"https", "http"} or not parsed_provider.netloc or parsed_provider.username or parsed_provider.password or parsed_provider.query or parsed_provider.fragment:
        raise ValueError("Provider root must be an HTTP(S) URL without credentials, query, or fragment")
    output = Path(output).expanduser()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    fetch = fetcher or (lambda url: _fetch_json(url, timeout=timeout, retries=retries))
    report = {"service": "ethereum", "chain_id": 1, "address": address,
              "ens_name": ens_name,
              "provider": provider_root, "started_at": _now(), "status": "running",
              "scope": "provider-indexed address transactions, token-transfer logs, and internal transactions",
              "limits": {"max_pages_per_endpoint": max_pages, "max_seconds_total": max_seconds},
              "resume": resume,
              "coverage": {}, "errors": [], "files": []}
    start = time.monotonic()
    _write_json(output / "report.json", report)
    kinds = {"transactions": "ethereum_transaction", "token-transfers": "ethereum_token_transfer",
             "internal-transactions": "ethereum_internal_transaction"}
    for endpoint, kind in kinds.items():
        base = f"{provider_root}/addresses/{address}/{endpoint}"
        cursor = None
        seen_cursors = set()
        seen_payloads = set()
        rows = []
        coverage = {"status": "partial", "pages": 0, "records": 0,
                    "duplicate_records": 0, "missing_identity_records": 0,
                    "first_timestamp": None, "last_timestamp": None,
                    "next_page_params": None, "reason": None}
        report["coverage"][endpoint] = coverage
        next_page_number = 1
        already_complete = False
        if resume:
            # Replay raw evidence, including pages durable before a crash but not
            # yet represented in normalized JSONL. Never refetch/overwrite them.
            for raw_path in sorted((output / "raw" / endpoint).glob("page-*.json")):
                if raw_path.name != f"page-{next_page_number:06d}.json":
                    raise ValueError("Cannot resume a raw page sequence with a gap")
                if already_complete:
                    raise ValueError("Cannot resume pages after a completed endpoint")
                raw_record = json.loads(raw_path.read_text())
                if (raw_record.get("chain_id") != 1 or raw_record.get("address", "").lower() != address.lower()
                        or not raw_record.get("url", "").startswith(base)):
                    raise ValueError("Resume evidence belongs to another address or endpoint")
                payload = raw_record["response"]
                _retain_ethereum_items(payload, endpoint, kind, address, raw_record,
                                        raw_path, output, coverage, rows, seen_payloads, provider_root)
                cursor = payload.get("next_page_params")
                if cursor is not None and not isinstance(cursor, dict):
                    raise ValueError("Cannot resume an invalid provider cursor")
                coverage["pages"] += 1
                coverage["next_page_params"] = cursor
                next_page_number += 1
                if not cursor:
                    already_complete = True
                else:
                    seen_cursors.add(json.dumps(cursor, sort_keys=True))
            if already_complete:
                coverage["status"] = "complete" if not coverage["reason"] else "partial"
        for page_number in range(next_page_number, max_pages + 1):
            if already_complete:
                break
            if time.monotonic() - start >= max_seconds:
                coverage["reason"] = "time_budget"
                break
            url = base + ("?" + urllib.parse.urlencode(cursor, doseq=True) if cursor else "")
            requested_at = _now()
            try:
                payload = fetch(url)
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise ValueError("Provider response has no item list")
            except (OSError, ValueError, TimeoutError, urllib.error.URLError) as error:
                coverage["reason"] = "provider_error"
                coverage["status"] = "failed" if coverage["pages"] == 0 else "partial"
                failure = {"endpoint": endpoint, "url": url, "requested_at": requested_at,
                           "error_type": type(error).__name__,
                           "http_status": getattr(error, "code", None),
                           "next_page_params": cursor}
                report["errors"].append(failure)
                _write_json(output / "raw" / endpoint / f"error-{page_number:06d}.json", failure)
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                break
            raw_path = output / "raw" / endpoint / f"page-{page_number:06d}.json"
            raw_record = {"url": url, "requested_at": requested_at, "received_at": _now(),
                          "chain_id": 1, "address": address, "response": payload}
            _write_json(raw_path, raw_record)
            coverage["pages"] += 1
            _retain_ethereum_items(payload, endpoint, kind, address, raw_record,
                                    raw_path, output, coverage, rows, seen_payloads, provider_root)
            cursor = payload.get("next_page_params")
            coverage["next_page_params"] = cursor
            coverage["records"] = len(rows)
            if page_number % 10 == 0:
                _write_json(output / "report.json", report)
            if not cursor:
                coverage["status"] = "complete" if not coverage["reason"] else "partial"
                break
            if not isinstance(cursor, dict):
                coverage["reason"] = "invalid_cursor"
                break
            key = json.dumps(cursor, sort_keys=True)
            if key in seen_cursors:
                coverage["reason"] = "repeated_cursor"
                break
            seen_cursors.add(key)
        else:
            if not already_complete:
                coverage["reason"] = "page_limit"
        coverage["records"] = len(rows)
        record_path = output / (endpoint + ".jsonl")
        _write_rows(record_path, rows)
        report["files"].append(str(record_path))
        report["coverage"][endpoint] = coverage
        _write_json(output / "report.json", report)
    statuses = [item["status"] for item in report["coverage"].values()]
    report["status"] = "complete" if all(status == "complete" for status in statuses) else (
        "failed" if all(status == "failed" for status in statuses) else "partial")
    report["finished_at"] = _now()
    _write_json(output / "report.json", report)
    return report


__all__ = ["collect_local_git", "collect_github", "collect_ethereum", "BLOCKSCOUT_ROOT"]
