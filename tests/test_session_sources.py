import hashlib
import json
from pathlib import Path

from biography.parsers import parse_file
from biography.session_sources import _lines, collect_agent_history, collect_shell_history, default_shell_sources, main, sanitize_visible

SID = "01234567-1234-1234-1234-0123456789ab"


def write_rows(path, rows, tail=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows) + tail
    path.write_bytes(raw)
    return raw


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def response(text, role="user", channel=None):
    return {"timestamp": "2024-01-01T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": role, "channel": channel, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


def test_codex_visible_preferred_representation_and_hidden_exclusion(tmp_path):
    source = tmp_path / "sessions" / f"rollout-{SID}.jsonl"
    original = write_rows(source, [response("Visible question"), response("Secret hidden reasoning", "assistant", "analysis"), response("Visible answer", "assistant", "final"), {"type": "event_msg", "payload": {"type": "user_message", "message": "Visible question"}}, {"type": "response_item", "payload": {"type": "function_call_output", "output": "Secret tool output"}}])
    out = tmp_path / "out"
    report = collect_agent_history(out, codex_roots=[source.parent], claude_roots=[])
    records = rows(out / "codex.jsonl")
    assert [record["body"] for record in records] == ["Visible question", "Visible answer"]
    assert report["source_files"][0]["fallback_messages_not_duplicated"] == 1
    assert source.read_bytes() == original
    assert (out / "codex.jsonl").stat().st_mode & 0o777 == 0o600


def test_event_msg_fallback_and_genuine_repeat_identity(tmp_path):
    source = tmp_path / f"rollout-{SID}.jsonl"
    fallback = {"timestamp": "2024-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "repeat"}}
    write_rows(source, [fallback, fallback])
    out = tmp_path / "out"
    collect_agent_history(out, codex_roots=[tmp_path], claude_roots=[])
    records = rows(out / "codex.jsonl")
    assert len(records) == 2
    assert records[0]["stable_id"] != records[1]["stable_id"]


def test_uuid_overlap_chooses_larger_primary_without_copying_backup(tmp_path):
    live, archive = tmp_path / "live", tmp_path / "archive"
    write_rows(live / f"rollout-{SID}.jsonl", [response("one"), response("two")])
    write_rows(archive / f"rollout-{SID}.jsonl", [response("one")])
    out = tmp_path / "out"
    report = collect_agent_history(out, codex_roots=[live, archive], claude_roots=[])
    assert len(rows(out / "codex.jsonl")) == 2
    assert len(report["duplicate_session_copies_skipped"]) == 1
    assert report["backups_not_read"] is True


def test_claude_text_blocks_only_not_thinking_tool_results_or_tools(tmp_path):
    source = tmp_path / "claude" / f"{SID}.jsonl"
    write_rows(source, [{"type": "assistant", "uuid": "message-id", "timestamp": "2024-01-01T00:00:00Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "tool_use", "input": "secret output"}, {"type": "text", "text": "Visible answer"}]}}, {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "hidden result"}]}}])
    out = tmp_path / "out"
    collect_agent_history(out, codex_roots=[], claude_roots=[source.parent])
    assert [r["body"] for r in rows(out / "claude.jsonl")] == ["Visible answer"]


def test_fixed_cutoff_and_torn_line_do_not_consume_new_appends(tmp_path):
    path = tmp_path / "active.jsonl"
    prefix = b'{"complete":true}\n{"torn":'
    path.write_bytes(prefix)
    snapshot = {"path": path, "cutoff_bytes": len(prefix)}
    with path.open("ab") as handle:
        handle.write(b'true}\n{"new":true}\n')
    stats = {}
    extracted = list(_lines(snapshot, stats))
    assert extracted == [(1, b'{"complete":true}\n')]
    assert stats["source_prefix_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert stats["torn_last_line_skipped"] is True
    assert stats["grew_after_cutoff"] is True


def test_strong_contextual_redaction_preserves_ordinary_words():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.abcdefghijklmnop"
    text = f'''export PRIVATE_KEY="{'a' * 64}"\ncurl -H 'Authorization: Bearer bearer-credential' --api-key flag-credential https://user:url-password@example.com\nSESSION_TOKEN=env-credential\n{jwt}\nseed phrase: alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu\nOrdinary public discussion and commit hashes.'''
    cleaned, changed = sanitize_visible(text)
    assert changed
    for secret in ("a" * 64, "bearer-credential", "flag-credential", "url-password", "env-credential", jwt, "alpha beta gamma"):
        assert secret not in cleaned
    assert "Ordinary public discussion and commit hashes." in cleaned
    assert sanitize_visible(cleaned)[0] == cleaned


def test_redacts_quoted_json_credentials_and_headers():
    text = '{"api_key": "quoted-key-secret", "Authorization": "Bearer quoted-header-secret", "Cookie": "session=quoted-cookie-secret"}'
    cleaned, changed = sanitize_visible(text)
    assert changed
    assert "quoted-key-secret" not in cleaned
    assert "quoted-header-secret" not in cleaned
    assert "quoted-cookie-secret" not in cleaned


def test_redacts_unlabelled_token_shapes_and_non_http_basic_auth():
    text = "123456789:" + "aBz1_" * 7 + "\npostgres://user:database-password@localhost/db\nkey='" + "aBz1_" * 8 + "ABCD'\n" + "0123456789abcdef" * 4
    cleaned, changed = sanitize_visible(text)
    assert changed
    assert "123456789:" not in cleaned
    assert "database-password" not in cleaned
    assert "aBz1_" * 8 not in cleaned
    assert "0123456789abcdef" * 4 not in cleaned


def test_conservative_hex_projection_redaction_preserves_short_common_values():
    value, _ = sanitize_visible('"1234567890abcdef" "deadbeef" PASS] ' + 'aBz1_' * 8 + 'ABCD')
    assert '1234567890abcdef' not in value
    assert '"deadbeef"' in value
    assert 'aBz1_' * 8 not in value


def test_shell_zsh_epochs_repeated_commands_and_undated_bash(tmp_path):
    zsh, bash = tmp_path / "zsh", tmp_path / "bash"
    zsh.write_text(': 1704067200:0;echo public\n: 1704067200:0;echo public\n: 1704067201:0;export API_KEY=secret-value\n')
    bash.write_text("echo bash\n")
    originals = [zsh.read_bytes(), bash.read_bytes()]
    out = tmp_path / "out"
    report = collect_shell_history(out, sources={"zsh": zsh, "bash": bash})
    records = rows(out / "commands.jsonl")
    assert len(records) == 4
    assert records[0]["timestamp"] == 1704067200
    assert records[0]["stable_id"] != records[1]["stable_id"]
    assert records[-1]["timestamp"] is None
    assert "secret-value" not in (out / "commands.jsonl").read_text()
    assert [zsh.read_bytes(), bash.read_bytes()] == originals
    assert report["status"] == "complete"
    parsed = parse_file(out / "commands.jsonl", "commands.jsonl", "Shell history")
    assert parsed.events[0]["occurred_at"] == "2024-01-01T00:00:00Z"
    assert parsed.events[-1]["occurred_at"] is None
    assert parsed.events[0]["source_native_id"] == records[0]["stable_id"]


def test_configured_directory_exclusions_and_generic_os_shell_defaults(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    write_rows(root / "excluded-backup" / f"rollout-{SID}.jsonl", [response("Excluded")])
    output = tmp_path / "out"
    report = collect_agent_history(output, codex_roots=[root], claude_roots=[], excluded_directory_names=["EXCLUDED-BACKUP"])
    assert report["files"][0]["records"] == 0
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("APPDATA", str(appdata))
    sources = default_shell_sources()
    assert sources["zsh"] == home / ".zsh_history"
    assert sources["powershell"].is_relative_to(appdata)


def test_cli_uses_external_profile_and_inherits_global_directory_policy(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sessions"
    write_rows(root / "excluded-backup" / f"rollout-{SID}.jsonl", [response("Excluded")])
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"excluded_directory_names": ["excluded-backup"], "collectors": {"agent_history": {"codex_roots": [str(root)], "claude_roots": []}}}))
    output = tmp_path / "out"
    monkeypatch.setenv("BIOGRAPHY_PROFILE", str(profile))
    monkeypatch.setattr("sys.argv", ["collector", "--output-root", str(output), "--agents-only"])
    main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["agents"]["files"][0]["records"] == 0
    assert not (output / "shell-history").exists()
