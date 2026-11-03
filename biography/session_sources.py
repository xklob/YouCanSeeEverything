"""Read-only, fixed-cutoff projections of visible agent and shell history.

Only sanitized visible text is written. Hidden reasoning, tool output, credential
files, and backup copies are never projected. Original active logs remain the
authoritative evidence and are never modified or copied wholesale.

The CLI accepts --profile or BIOGRAPHY_PROFILE pointing to private JSON. Supported
sections are ``collectors.agent_history`` with codex_roots, claude_roots, and
excluded_directory_names, and ``collectors.shell_history`` with a sources mapping
from shell labels to paths. Sections may also live at the top level. Global
excluded_directory_names provides the fallback agent policy. Library callers
pass those same fields explicitly. Relative/home paths resolve in the caller's
environment; profiles and their private content are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .parsers.common import clean_text, preserve_public_token_query, redact_auth_query_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / ".local/history-staging"
MAX_LINE_BYTES = 32 * 1024 * 1024
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
VALUE = r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s;&|,}\n]+)'''
SECRET_LABEL = r"(?:[\w-]*(?:api[_-]?key|token|password|passwd|passphrase|secret|private[_-]?key|mnemonic|seed[_-]?phrase)[\w-]*)"
ASSIGNMENT = re.compile(r"(?im)(\b" + SECRET_LABEL + r'''(?:\\?["'])?\s*[:=]\s*)''' + VALUE)
FLAG = re.compile(r"(?i)(--(?:password|passwd|passphrase|token|access-token|refresh-token|api-key|secret|private-key|mnemonic|seed-phrase)(?:=|\s+))" + VALUE)
HEADER = re.compile(r'''(?im)(\b(?:authorization|proxy-authorization|x-api-key)(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?)[^\r\n"']+''')
COOKIE = re.compile(r'''(?im)(\b(?:cookie|set-cookie)(?:\\?["'])?\s*:\s*(?:\\?["'])?)[^\r\n"']+''')
JWT = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{10,}(?![\w-])")
URL_AUTH = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
PHRASE = re.compile(r'''(?im)(\b(?:mnemonic|seed[ _-]?phrase|recovery[ _-]?phrase)(?:\\?["'])?\s*(?::|=|is)\s*)[^\r\n]+''')
KNOWN_TOKEN = re.compile(r"\b(?:hf_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{15,}|AIza[A-Za-z0-9_-]{30,})\b")
PRIVATE_KEY = re.compile(r"(?i)(\b(?:private[ _-]?key|secret[ _-]?key)\s*(?::|=|is)\s*)(?:0x)?[a-f0-9]{64}\b")
TELEGRAM = re.compile(r"(?<![\w-])\d{8,10}:[0-9A-Za-z_-]{35}(?![\w-])")
OPAQUE_KEY = re.compile(r'''(?im)(\b(?:key|pwd|pass|token)(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?)[a-zA-Z0-9_\-]{32,}(?![\w-])''')
HEX_KEY = re.compile(r"(?<![\w])(?:0x)?[a-fA-F0-9]{64}(?![\w])")
ENTROPY_TOKEN = re.compile(r"(?<![A-Za-z0-9_/+=-])[A-Za-z0-9_/+=-]{32,}(?![A-Za-z0-9_/+=-])")
QUOTED_HEX = re.compile(r'''(?<=["'])[a-fA-F0-9]{12,31}(?=["'])''')
PASS_MARKER = re.compile(r"(?i)(\bPASS\]\s+)[A-Za-z0-9_-]{44}(?![\w-])")


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize_visible(text: str) -> tuple[str, bool]:
    """Redact credential contexts without blanket-redacting ordinary short words."""
    value = redact_auth_query_values(str(text))
    # Credential headers can contain spaces and JWTs. Redact them before more
    # general assignment patterns consume only the scheme/first word.
    for pattern in (HEADER, COOKIE, PHRASE, PRIVATE_KEY, ASSIGNMENT, FLAG, OPAQUE_KEY, PASS_MARKER):
        value = pattern.sub(lambda match: match[0] if "[REDACTED" in match[0] or preserve_public_token_query(match) else match[1] + "[REDACTED CREDENTIAL]", value)
    value = URL_AUTH.sub(r"\1[REDACTED CREDENTIAL]@", value)
    value = JWT.sub("[REDACTED JWT]", value)
    value = KNOWN_TOKEN.sub("[REDACTED CREDENTIAL]", value)
    value = TELEGRAM.sub("[REDACTED CREDENTIAL]", value)
    # Unlabelled high-entropy values in visible text can be encoded credentials.
    # Conservative redaction affects projections only; exact original logs and
    # their line hashes stay authoritative. Structured provenance hashes are not
    # passed through this text sanitizer.
    value = HEX_KEY.sub("[REDACTED OPAQUE VALUE]", value)
    def redact_entropy(match):
        token = match[0]
        if re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_-]*=)?0x[0-9a-fA-F]{40}", token, re.I):
            return token
        counts = Counter(token)
        entropy = -sum((count/len(token))*math.log2(count/len(token)) for count in counts.values())
        return "[REDACTED OPAQUE VALUE]" if entropy >= 4.5 or (re.fullmatch("[a-fA-F0-9]+", token) and entropy >= 3) else token
    value = ENTROPY_TOKEN.sub(redact_entropy, value)
    value = QUOTED_HEX.sub(redact_entropy, value)
    value = clean_text(value, max(len(value), 1))
    value = value.encode("utf-8", "replace").decode("utf-8")
    return value, value != text


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".part")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Projection:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary = path.with_name(path.name + ".part")
        self.handle = os.fdopen(os.open(self.temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        self.count = 0
        self.redacted = 0

    def write(self, record):
        self.handle.write((json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode())
        self.count += 1
        self.redacted += bool(record.get("redacted"))

    def finish(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        os.replace(self.temporary, self.path)
        return {"path": str(self.path), "records": self.count, "redacted_records": self.redacted, "bytes": self.path.stat().st_size}


def _inventory(roots, excluded_directory_names=()):
    files = []
    excluded = {str(name).casefold() for name in excluded_directory_names} | {"backups", "sessions-backup"}
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir() or root.is_symlink() or root.name.casefold() in excluded:
            continue
        for base, directories, names in os.walk(root, followlinks=False):
            directories[:] = sorted(d for d in directories if d.casefold() not in excluded and not Path(base, d).is_symlink())
            for name in sorted(names):
                path = Path(base, name)
                if path.suffix == ".jsonl" and not path.is_symlink():
                    info = path.stat()
                    files.append({"path": path, "cutoff_bytes": info.st_size, "initial_mtime_ns": info.st_mtime_ns})
    return files


def _lines(snapshot: dict, stats: dict):
    digest = hashlib.sha256()
    remaining = snapshot["cutoff_bytes"]
    line_number = 0
    with snapshot["path"].open("rb") as handle:
        while remaining:
            raw = handle.readline(min(remaining, MAX_LINE_BYTES + 1))
            if not raw:
                stats["source_truncated_during_read"] = True
                break
            remaining -= len(raw)
            digest.update(raw)
            line_number += 1
            if len(raw) > MAX_LINE_BYTES:
                stats["oversized_lines"] = stats.get("oversized_lines", 0) + 1
                while raw and not raw.endswith(b"\n") and remaining:
                    raw = handle.readline(min(remaining, 1024 * 1024))
                    remaining -= len(raw)
                    digest.update(raw)
                continue
            if not raw.endswith(b"\n"):
                stats["torn_last_line_skipped"] = True
                break
            yield line_number, raw
    stats["source_prefix_sha256"] = digest.hexdigest()
    stats["source_bytes_read"] = snapshot["cutoff_bytes"] - remaining
    stats["source_lines_seen"] = line_number
    try:
        stats["final_size_bytes"] = snapshot["path"].stat().st_size
        stats["grew_after_cutoff"] = stats["final_size_bytes"] > snapshot["cutoff_bytes"]
    except OSError:
        stats["source_unavailable_after_read"] = True


def _session_id(snapshot, tool):
    path = snapshot["path"]
    found = UUID.findall(path.stem)
    if found:
        return found[-1].casefold()
    # Metadata is inspected only inside an approved session JSONL, never auth or
    # config files. Unusual Claude agent files retain their own path identity.
    if tool == "codex":
        try:
            with path.open("rb") as handle:
                head = handle.readline(min(snapshot["cutoff_bytes"], 1024 * 1024))
            first = json.loads(head)
            payload = first.get("payload") or {}
            candidate = payload.get("id") or payload.get("session_id")
            if candidate and UUID.fullmatch(str(candidate)):
                return str(candidate).casefold()
        except (OSError, ValueError, TypeError):
            pass
    return "path-" + hashlib.sha256(str(path).encode()).hexdigest()


def _text_blocks(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str))


def _record(tool, session_id, role, text, timestamp, message_id, snapshot, line, raw, occurrences):
    body, redacted = sanitize_visible(text)
    if not body.strip():
        return None
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    identity = f"{role}:{timestamp or 'undated'}:{message_id or body_hash}:{body_hash}"
    ordinal = occurrences[identity]
    occurrences[identity] += 1
    native = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "kind": "session_message", "stable_id": f"session:{tool}:{session_id}:{native}:{ordinal}",
        "title": f"{tool.title()} · {role}", "body": body, "timestamp": timestamp,
        "role": role, "session_id": session_id, "agent": tool,
        "source_path": str(snapshot["path"]), "source_line": line,
        "source_record_sha256": hashlib.sha256(raw).hexdigest(), "redacted": redacted,
        "message_id": str(message_id) if message_id else None,
    }


def _project_session(snapshot, tool, output):
    stats = {"path": str(snapshot["path"]), "cutoff_bytes": snapshot["cutoff_bytes"], "session_id": snapshot["session_id"], "malformed_lines": 0}
    counts = {"preferred": 0, "fallback": 0}
    occurrences = {"preferred": Counter(), "fallback": Counter()}
    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as preferred, tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as fallback:
        for line, raw in _lines(snapshot, stats):
            # Most Codex bytes are tool results or other non-message payloads.
            # Inspect a fixed prefix before JSON decoding those very large rows.
            if tool == "codex" and not re.search(rb'"type"\s*:\s*"(?:response_item|event_msg)"', raw[:2048]):
                continue
            if tool == "codex" and re.search(rb'"type"\s*:\s*"(?:function_call|function_call_output|custom_tool_call|custom_tool_call_output|reasoning|web_search_call|image_generation_call)"', raw[:512]):
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                stats["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                continue
            representation = "preferred"
            if tool == "codex":
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role not in {"user", "assistant"} or payload.get("channel") not in {None, "final", "commentary"}:
                        continue
                    text = _text_blocks(payload.get("content"))
                elif record.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                    if payload.get("channel") in {"analysis", "reasoning"}:
                        continue
                    role = "user" if payload["type"] == "user_message" else "assistant"
                    text = payload.get("message")
                    representation = "fallback"
                else:
                    continue
                message_id = payload.get("id") or payload.get("message_id")
            else:
                payload = record.get("message")
                if record.get("type") not in {"user", "assistant"} or not isinstance(payload, dict):
                    continue
                role = payload.get("role") or record["type"]
                if role not in {"user", "assistant"}:
                    continue
                text = _text_blocks(payload.get("content"))
                message_id = record.get("uuid") or payload.get("id")
            if not isinstance(text, str):
                continue
            projected = _record(tool, snapshot["session_id"], role, text, record.get("timestamp"), message_id, snapshot, line, raw, occurrences[representation])
            if projected:
                target = preferred if representation == "preferred" else fallback
                target.write((json.dumps(projected, ensure_ascii=True) + "\n").encode())
                counts[representation] += 1
        selected = preferred if counts["preferred"] else fallback
        selected.seek(0)
        for row in selected:
            output.write(json.loads(row))
    stats["visible_messages"] = counts["preferred"] or counts["fallback"]
    stats["representation"] = "response_item" if tool == "codex" and counts["preferred"] else "event_msg_fallback" if tool == "codex" else "claude_text_blocks"
    stats["fallback_messages_not_duplicated"] = counts["fallback"] if counts["preferred"] else 0
    return stats


def collect_agent_history(output_dir: Path | None = None, *, codex_roots=None, claude_roots=None, excluded_directory_names=(), progress=None):
    """Project configured roots, or the current OS user's conventional CLI roots.

    Personal locations and policy names belong in caller configuration. These
    defaults never name a particular user, machine, or backup collection.
    """
    output_dir = Path(output_dir or DEFAULT_ROOT / "agent-history").expanduser()
    home = Path.home()
    codex = _inventory(codex_roots if codex_roots is not None else [home / ".codex/sessions", home / ".codex/archived_sessions"], excluded_directory_names)
    claude = _inventory(claude_roots if claude_roots is not None else [home / ".claude/projects"], excluded_directory_names)
    report = {"started_at": now(), "mode": "sanitized_visible_text_fixed_byte_cutoff", "raw_sources_preserved": True, "backups_not_read": True, "files": [], "source_files": [], "errors": [], "duplicate_session_copies_skipped": []}
    for tool, candidates in (("codex", codex), ("claude", claude)):
        selected = {}
        for snapshot in candidates:
            snapshot["session_id"] = _session_id(snapshot, tool)
            key = snapshot["session_id"]
            old = selected.get(key)
            score = (snapshot["cutoff_bytes"], "archived_sessions" not in snapshot["path"].parts)
            old_score = (old["cutoff_bytes"], "archived_sessions" not in old["path"].parts) if old else (-1, False)
            if old is None or score > old_score:
                if old:
                    report["duplicate_session_copies_skipped"].append({"path": str(old["path"]), "session_id": key})
                selected[key] = snapshot
            else:
                report["duplicate_session_copies_skipped"].append({"path": str(snapshot["path"]), "session_id": key})
        writer = Projection(output_dir / f"{tool}.jsonl")
        for index, snapshot in enumerate(sorted(selected.values(), key=lambda value: str(value["path"]))):
            try:
                report["source_files"].append({"agent": tool, **_project_session(snapshot, tool, writer)})
            except (OSError, ValueError, TypeError) as error:
                report["errors"].append({"path": str(snapshot["path"]), "error_type": type(error).__name__})
            if progress:
                progress({"agent": tool, "completed_files": index + 1, "total_files": len(selected), "messages": writer.count})
        report["files"].append(writer.finish())
    partial = report["errors"] or any(item.get("malformed_lines") or item.get("oversized_lines") or item.get("source_truncated_during_read") for item in report["source_files"])
    report.update({"finished_at": now(), "status": "partial" if partial else "complete", "coverage": "Visible user and assistant text only; no hidden reasoning, tool results, raw backup copies, or credential files."})
    _write_json(output_dir / "report.json", report)
    return report


def _shell_rows(snapshot, shell, stats):
    pending = []
    pending_line = None
    pending_stamp = None
    pending_raw = []
    for line, raw in _lines(snapshot, stats):
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        match = re.match(r"^: (\d+):\d+;(.*)$", text) if shell == "zsh" else None
        if match:
            if pending:
                yield pending_line, pending_stamp, "\n".join(pending), b"".join(pending_raw)
            pending_line, pending_stamp = line, int(match[1])
            pending, pending_raw = [match[2]], [raw]
        elif shell == "zsh" and pending and pending[-1].endswith("\\"):
            pending[-1] = pending[-1][:-1]
            pending.append(text)
            pending_raw.append(raw)
        else:
            if pending:
                yield pending_line, pending_stamp, "\n".join(pending), b"".join(pending_raw)
                pending = []
            if text.strip():
                # Bash/PSReadLine timestamps and filesystem mtimes are not event
                # dates. Only an explicit zsh extended-history epoch is used.
                yield line, None, text, raw
    if pending:
        yield pending_line, pending_stamp, "\n".join(pending), b"".join(pending_raw)


def default_shell_sources():
    """Use the current OS profile; WSL/remote Windows paths require configuration."""
    sources = {"zsh": Path.home() / ".zsh_history", "bash": Path.home() / ".bash_history"}
    appdata = os.environ.get("APPDATA")
    if appdata and Path(appdata).is_dir():
        sources["powershell"] = Path(appdata) / "Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt"
    return sources


def collect_shell_history(output_dir: Path | None = None, *, sources=None, progress=None):
    """Project explicit shell paths or discover them from the current OS user."""
    output_dir = Path(output_dir or DEFAULT_ROOT / "shell-history").expanduser()
    sources = sources if sources is not None else default_shell_sources()
    report = {"started_at": now(), "mode": "sanitized_projection_fixed_byte_cutoff", "raw_sources_preserved": True, "source_files": [], "files": [], "errors": []}
    snapshots = []
    for shell, source in sources.items():
        source = Path(source).expanduser()
        if source.is_file() and not source.is_symlink():
            snapshots.append((shell, {"path": source, "cutoff_bytes": source.stat().st_size}))
    writer = Projection(output_dir / "commands.jsonl")
    for shell, snapshot in snapshots:
        stats = {"path": str(snapshot["path"]), "shell": shell, "cutoff_bytes": snapshot["cutoff_bytes"]}
        occurrences = Counter()
        try:
            for line, timestamp, text, raw in _shell_rows(snapshot, shell, stats):
                body, redacted = sanitize_visible(text)
                digest = hashlib.sha256(body.encode()).hexdigest()
                identity = f"{timestamp or 'undated'}:{digest}"
                ordinal = occurrences[identity]
                occurrences[identity] += 1
                source_id = hashlib.sha256(str(snapshot["path"]).encode()).hexdigest()[:16]
                writer.write({"kind": "shell_command", "stable_id": f"shell:{shell}:{source_id}:{identity}:{ordinal}", "title": f"{shell} command", "body": body, "timestamp": timestamp, "role": "user", "shell": shell, "source_path": str(snapshot["path"]), "source_line": line, "source_record_sha256": hashlib.sha256(raw).hexdigest(), "redacted": redacted})
        except (OSError, ValueError, TypeError) as error:
            report["errors"].append({"path": str(snapshot["path"]), "error_type": type(error).__name__})
        report["source_files"].append(stats)
        if progress:
            progress({"shell": shell, "commands": writer.count})
    report["files"].append(writer.finish())
    report.update({"finished_at": now(), "status": "partial" if report["errors"] else "complete", "coverage": "Only explicit zsh extended-history epochs are dated; bash and PSReadLine commands remain undated."})
    _write_json(output_dir / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--shell-only", action="store_true")
    parser.add_argument("--agents-only", action="store_true")
    parser.add_argument("--profile", type=Path, default=os.environ.get("BIOGRAPHY_PROFILE"), help="Private JSON profile; supports agent_history and shell_history sections, optionally under collectors")
    arguments = parser.parse_args()
    profile = json.loads(arguments.profile.expanduser().read_text()) if arguments.profile else {}
    collector_profile = profile.get("collectors", profile)
    agent_profile = collector_profile.get("agent_history", {})
    agent_options = {key: agent_profile[key] for key in ("codex_roots", "claude_roots", "excluded_directory_names") if key in agent_profile}
    agent_options.setdefault("excluded_directory_names", profile.get("excluded_directory_names", []))
    shell_profile = collector_profile.get("shell_history", {})
    shell_options = {"sources": shell_profile["sources"]} if "sources" in shell_profile else {}
    reports = {}
    if not arguments.shell_only:
        report = collect_agent_history(arguments.output_root / "agent-history", **agent_options)
        reports["agents"] = {"status": report["status"], "files": report["files"]}
    if not arguments.agents_only:
        report = collect_shell_history(arguments.output_root / "shell-history", **shell_options)
        reports["shell"] = {"status": report["status"], "files": report["files"]}
    print(json.dumps(reports, sort_keys=True))


if __name__ == "__main__":
    main()
