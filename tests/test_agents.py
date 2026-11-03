import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from biography import agents, revisions


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / ".local" / "state"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    def install(*, response=None, event=None, status="Logged in using ChatGPT", extra=""):
        binary = tmp_path / "bin" / "codex"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text(
            "#!/usr/bin/python3\nimport json, os, pathlib, sys, time\n"
            f"status = {status!r}\n"
            "if sys.argv[1:] == ['login', 'status']:\n print(status)\n sys.exit(0)\n"
            "prompt = sys.stdin.read()\n"
            + extra + "\n"
            + f"response = {response!r}\n"
            "target = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
            "target.write_text(json.dumps(response))\n"
            "print(json.dumps({'type':'turn.started'}))\n"
            + f"print(json.dumps({event or {'type': 'item.completed', 'item': {'type': 'agent_message'}}!r}))\n"
            "print(json.dumps({'type':'turn.completed'}))\n"
            "print('private diagnostic must not survive', file=sys.stderr)\n"
        )
        binary.chmod(0o700)
        monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
        return binary
    return install


def test_saved_chatgpt_auth_and_no_api_or_proxy_environment(fake_cli, state, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-never-inherit")
    monkeypatch.setenv("CODEX_API_KEY", "test-secret-never-inherit")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-never-inherit")
    fake_cli(response={"answer": "Supported [record-1]", "citations": ["record-1"]}, extra="assert not any(k in os.environ for k in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'HTTPS_PROXY', 'GITHUB_TOKEN'))")
    result = agents.ask("When?", [{"id": "record-1", "body": "A real record"}], state)
    assert result["citations"] == ["record-1"]
    artifact = Path(result["artifact_dir"])
    assert not (artifact / "events.raw").exists()
    assert not (artifact / "stderr.raw").exists()
    assert "private diagnostic" not in "".join(p.read_text() for p in artifact.iterdir() if p.is_file())
    command = agents._command(artifact, agents.DEFAULT_MODEL)
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--json", "read-only", "forced_login_method=\"chatgpt\"", "shell_tool", "apps", "plugins", "hooks"):
        assert flag in command
    assert "danger-full-access" not in command


def test_api_key_login_rejected_without_model_call(fake_cli, state):
    fake_cli(status="Logged in using an API key")
    with pytest.raises(agents.AgentError, match="API-key") as error:
        agents.ask("Question", [], state)
    assert error.value.code == "chatgpt_login_required"
    assert list(state.iterdir()) == []


def test_tool_response_is_rejected(fake_cli, state):
    fake_cli(response={"answer": "Unsafe", "citations": []}, event={"type": "item.completed", "item": {"type": "mcp_tool_call"}})
    with pytest.raises(agents.AgentError) as error:
        agents.ask("Question", [], state)
    assert error.value.code == "unexpected_tool"
    assert json.loads((Path(error.value.artifact_dir) / "status.json").read_text())["status"] == "failed"


def test_failed_turn_is_rejected_even_with_zero_cli_exit(fake_cli, state):
    fake_cli(response={"answer": "Partial", "citations": []}, event={"type": "turn.failed", "error": {"message": "usage limit"}})
    with pytest.raises(agents.AgentError) as error:
        agents.ask("Question", [], state)
    assert error.value.code == "cli_failed"


def test_pre_turn_diagnostic_errors_are_not_tool_calls(fake_cli, state):
    fake_cli(response={"answer": "Supported", "citations": []}, extra="print(json.dumps({'type':'item.completed','item':{'type':'error','message':'startup warning'}}))")
    result = agents.ask("Question", [], state)
    status = json.loads((Path(result["artifact_dir"]) / "status.json").read_text())
    assert status["status"] == "succeeded"
    assert status["startup_diagnostics"] == 1


def test_only_numeric_usage_counters_retained(fake_cli, state):
    fake_cli(response={"answer": "Supported", "citations": []}, event={"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 12, "credential": "must-not-be-retained", "total_tokens": True}})
    result = agents.ask("Question", [], state)
    status = json.loads((Path(result["artifact_dir"]) / "status.json").read_text())
    assert status["usage"] == {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 12}
    assert "must-not-be-retained" not in (Path(result["artifact_dir"]) / "events.json").read_text()


def test_error_item_after_turn_started_fails(fake_cli, state):
    fake_cli(response={"answer": "Partial", "citations": []}, event={"type": "item.completed", "item": {"type": "error"}})
    with pytest.raises(agents.AgentError) as error:
        agents.ask("Question", [], state)
    assert error.value.code == "cli_failed"


def test_failure_category_retained_without_private_diagnostic_text(fake_cli, state):
    secret = "private-token-shaped-value-never-log"
    fake_cli(response={"answer": "Partial", "citations": []}, event={"type": "turn.failed", "error": {"message": "You have hit your usage limit. " + secret}})
    with pytest.raises(agents.AgentError) as error:
        agents.ask("Question", [], state)
    artifact = Path(error.value.artifact_dir)
    assert json.loads((artifact / "status.json").read_text())["failure_classification"] == "usage_limit"
    assert "usage limit" in str(error.value)
    assert all(secret not in path.read_text() for path in artifact.iterdir() if path.is_file())


@pytest.mark.parametrize("message,category", [
    ("rate_limit_exceeded", "rate_limited"), ("Unauthorized HTTP 401", "authentication"),
    ("stream disconnected before completion", "network"), ("model_not_found", "model_unavailable"),
    ("invalid_json_schema", "invalid_schema"), ("unrecognized failure", "unknown"),
])
def test_failure_classification_is_a_closed_category(message, category):
    assert agents._failure_classification(message) == category


def test_timeout_stops_job_and_persists_failed_status(fake_cli, state):
    fake_cli(extra="time.sleep(30)")
    with pytest.raises(agents.AgentError) as error:
        agents._run("Question", agents._object({"answer": agents._string()}), state, kind="test", timeout=0.05)
    assert error.value.code == "timeout"
    artifact = Path(error.value.artifact_dir)
    assert json.loads((artifact / "status.json").read_text())["code"] == "timeout"
    assert not (artifact / "events.raw").exists()


@pytest.mark.parametrize("response", [
    {"answer": "Unsupported", "citations": ["fabricated"]},
    {"answer": "Duplicate", "citations": ["one", "one"]},
    {"answer": "Wrong shape", "citations": "one"},
    {"answer": "Extra", "citations": [], "command": "do something"},
])
def test_untrusted_model_output_validated(fake_cli, state, response):
    fake_cli(response=response)
    with pytest.raises(agents.AgentError) as error:
        agents.ask("Question", [{"id": "one", "body": "Support"}], state)
    assert error.value.code in {"invalid_citation", "invalid_output"}


def test_evidence_is_bounded_deduplicated_and_excludes_arbitrary_metadata():
    evidence = [{"id": "one", "body": "x" * 40_000, "metadata": {"private": "never needed"}, "path": "/secret"}, {"id": "one", "body": "duplicate"}]
    evidence.extend({"id": f"other-{i}", "body": "x" * 40_000} for i in range(100))
    bounded = agents.bounded_evidence(evidence)
    assert len(bounded) <= agents.MAX_EVIDENCE
    assert len({r["id"] for r in bounded}) == len(bounded)
    assert len(bounded[0]["body"]) == agents.MAX_EXCERPT
    assert all("metadata" not in r and "path" not in r for r in bounded)
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in bounded) <= agents.MAX_CONTEXT


def test_astra_explicit_only_and_unknown_models_rejected(state):
    assert "model_reasoning_effort=\"xhigh\"" in agents._command(state, "gpt-6-astra")
    assert "model_reasoning_effort=\"high\"" in agents._command(state, agents.DEFAULT_MODEL)
    with pytest.raises(agents.AgentError, match="Choose"):
        agents._command(state, "some-other-provider")


def test_artifacts_cannot_be_written_outside_local(tmp_path):
    with pytest.raises(agents.AgentError, match=".local"):
        agents._job_dir(tmp_path, "test")


def mapping(**updates):
    return {"kind": "mapping", "format": "json", "record_path": "entries", "fields": {"id": "id", "title": "title", "body": "text", "occurred_at": "date", "end_at": "", "category": ""}, "events": [], "notes": "", **updates}


def test_adapt_declarative_mapping_and_rejects_expressions(fake_cli, state):
    fake_cli(response=mapping())
    result = agents.adapt({"format": "json", "sample": {"entries": [{"id": 1, "date": "2020-01-01", "title": "Hello", "text": "World"}]}}, state)
    assert result["fields"]["body"] == "text"
    fake_cli(response=mapping(record_path="__import__('os').system('bad')"))
    with pytest.raises(agents.AgentError) as error:
        agents.adapt({"sample": {}}, state)
    assert error.value.code == "invalid_mapping"
    assert json.loads((Path(error.value.artifact_dir) / "status.json").read_text())["status"] == "failed"


def test_adapt_refuses_oversized_source_sample(state):
    with pytest.raises(agents.AgentError) as error:
        agents.adapt({"text": "x" * 64_000}, state)
    assert error.value.code == "profile_too_large"


def make_revision(state, identity, previous=None):
    directory = state / "revisions" / identity
    (directory / "dist").mkdir(parents=True)
    (directory / "dist/index.html").write_text(f"<title>{identity}</title>")
    manifest = {"revision_id": identity, "status": "passed", "summary": identity}
    if previous:
        manifest["previous_revision"] = previous
    (directory / "manifest.json").write_text(json.dumps(manifest))


def test_activate_undo_changes_only_ui_pointer(state):
    make_revision(state, "original")
    make_revision(state, "changed", "original")
    revisions.activate(state, "original")
    active = revisions.activate(state, "changed")
    assert active["can_undo"]
    # Imports completed after a UI change remain present after undo.
    imported_file = state / "new-import.json"
    imported_file.write_text('{"event":"later import"}')
    restored = revisions.undo(state)
    assert restored["revision_id"] == "original"
    assert not restored["can_undo"]
    assert imported_file.read_text() == '{"event":"later import"}'
    assert revisions.active_dist(state, Path("fallback")) == state / "revisions/original/dist"


def test_unchecked_or_stale_revision_never_becomes_active(state):
    make_revision(state, "original")
    make_revision(state, "first", "original")
    make_revision(state, "stale", "original")
    revisions.activate(state, "original")
    revisions.activate(state, "first")
    with pytest.raises(agents.AgentError) as error:
        revisions.activate(state, "stale")
    assert error.value.code == "stale_revision"
    with pytest.raises(agents.AgentError):
        revisions.activate(state, "missing")
    assert revisions.current(state)["revision_id"] == "first"


@pytest.mark.parametrize("name", ["web/src/../../biography/db.py", "web/package.json", "/web/src/main.tsx", "web/src/../main.tsx", "web\\src\\main.tsx", "web/src/.secret.ts", "web/src/run.sh"])
def test_generated_file_paths_cannot_escape_src(name):
    with pytest.raises(agents.AgentError):
        revisions._safe_path(name)


@pytest.mark.parametrize("code", [
    "import x from '../../biography/private'", "import x from 'node:fs'", "import('react')", "fetch('https://outside.invalid')", "new WebSocket('/api')", "localStorage.setItem('x','secret')", "eval('code')",
])
def test_generated_browser_code_restricted(code):
    with pytest.raises(agents.AgentError):
        revisions._validate_code("web/src/main.tsx", code)


def test_isolated_change_keeps_source_unchanged_until_explicit_activation(tmp_path, state, monkeypatch):
    project = tmp_path / "project"
    (project / "web/src").mkdir(parents=True)
    original = "export const value = 1;\n"
    (project / "web/src/main.tsx").write_text(original)
    (project / "web/index.html").write_text('<div id="root"></div>')
    (project / "web/package.json").write_text('{"dependencies":{"react":"19.0.0"}}')

    def built(web, node_modules, dist, artifact):
        dist.mkdir(parents=True)
        (dist / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>')
        (dist / "assets").mkdir()
        (dist / "assets/app.js").write_text("console.log('built')")
        return [{"name": "mocked_fixed_build", "passed": True}]

    monkeypatch.setattr(revisions, "_build", built)
    monkeypatch.setattr(revisions, "_run", lambda *a, **k: ({"summary": "Changed value", "files": [{"path": "web/src/main.tsx", "content": "export const value = 2;\n"}]}, state))
    result = agents.change_dashboard("Change the displayed value", project, state)
    assert (project / "web/src/main.tsx").read_text() == original
    assert revisions.current(state)["baseline"]
    assert revisions.current(state)["revision_id"] != result["revision_id"]
    revisions.activate(state, result["revision_id"])
    assert revisions.current(state)["revision_id"] == result["revision_id"]
    assert "+export const value = 2;" in Path(result["patch_path"]).read_text()

    # Repeated changes start from the active snapshot even if the source checkout
    # differs. They never silently replace an earlier applied UI with stale code.
    (project / "web/src/main.tsx").write_text("export const value = 999;\n")
    def second_change(instruction, *args, **kwargs):
        assert "export const value = 2;" in instruction
        assert "export const value = 999;" not in instruction
        return {"summary": "Second change", "files": [{"path": "web/src/main.tsx", "content": "export const value = 3;\n"}]}, state
    monkeypatch.setattr(revisions, "_run", second_change)
    second = agents.change_dashboard("Another change", project, state)
    assert second["previous_revision"] == result["revision_id"]
    assert "-export const value = 2;" in Path(second["patch_path"]).read_text()
    assert (project / "web/src/main.tsx").read_text() == "export const value = 999;\n"


def test_declared_font_dependencies_and_safe_subpaths_accepted(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text(json.dumps({"dependencies": {"react": "19", "react-dom": "19", "@fontsource-variable/dm-sans": "5", "@fontsource-variable/manrope": "5"}, "devDependencies": {"vite": "6"}}))
    allowed = revisions._declared_dependencies(tmp_path)
    assert "vite" not in allowed
    revisions._validate_code("web/src/main.tsx", "import '@fontsource-variable/dm-sans';\nimport '@fontsource-variable/manrope/index.css';\nimport { jsx } from 'react/jsx-runtime';\nimport ReactDOM from 'react-dom/client';", allowed)


def test_import_scan_does_not_treat_from_string_or_comments_as_modules():
    code = """import {useState}from'react';
const params = new URLSearchParams();
if (from) params.set('from', from);
if (to) params.set('to', to);
const description = 'ordinary text from somewhere';
// import 'not-an-actual-package';
export {useState}from'react';
"""
    assert revisions._module_specifiers(code) == ["react", "react"]
    revisions._validate_code("web/src/App.tsx", code, {"react"})


@pytest.mark.parametrize("module", ["fs", "node:fs", "https://outside.invalid/x.js", "@fontsource-variable/dm-sans/../../private", "@fontsource-variable/dm-sans-evil", "@fontsource-variable/dm-sans//index.css", "@fontsource-variable/dm-sans/%2e%2e/private", "@fontsource-variable/dm-sans/index.css?url", "@fontsource-variable/dm-sans/./index.css", "vite"])
def test_package_imports_reject_undeclared_builtin_remote_and_unsafe_subpaths(module):
    with pytest.raises(agents.AgentError):
        revisions._validate_code("web/src/main.tsx", f"import '{module}';", {"react", "@fontsource-variable/dm-sans"})


def browser_report(state, *, revision="candidate", **updates):
    local = next(parent for parent in state.parents if parent.name == ".local")
    checker = local.parent / "scripts/check_dashboard.py"
    checker.parent.mkdir(exist_ok=True)
    checker.write_text("# Trusted fixed browser checker fixture\n")
    report = local / "revision-checks" / revision / "result.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    value = {"passed": True, "dist_path": str(state / "revisions" / revision / "dist"), "mode": "fixture", "started_at": 100, "checks": [{"name": "browser_render", "passed": True}], "page_errors": [], "browser_console_errors": [], "blocked_requests": [], "server_violations": [], **updates}
    report.write_text(json.dumps(value))
    return report


def test_browser_verification_persisted_before_activation_and_survives_undo(state):
    make_revision(state, "original")
    make_revision(state, "candidate", "original")
    report = browser_report(state)
    project = next(parent for parent in state.parents if parent.name == ".local").parent
    result = revisions.mark_browser_verified(state, "candidate", report, project_root=project)
    assert result["browser_verified"] is True
    assert result["browser_check"]["checker_path"].endswith("scripts/check_dashboard.py")
    assert len(result["browser_check"]["report_sha256"]) == 64
    revisions.activate(state, "original")
    revisions.activate(state, "candidate")
    assert revisions.current(state)["browser_verified"] is True
    revisions.undo(state)
    revisions.activate(state, "candidate")
    assert revisions.current(state)["browser_verified"] is True


def test_browser_checker_root_independent_of_external_private_state(tmp_path):
    private_state = tmp_path / "private-data/.local/state"
    private_state.mkdir(parents=True)
    make_revision(private_state, "candidate")
    report = browser_report(private_state)
    # The state directory's neighboring script is unrelated to the public app.
    private_checker = tmp_path / "private-data/scripts/check_dashboard.py"
    private_checker.write_text("# unrelated private script")
    project = tmp_path / "public-app"
    checker = project / "scripts/check_dashboard.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# the trusted public application browser checker")
    result = revisions.mark_browser_verified(private_state, "candidate", report, project_root=project)
    assert result["browser_check"]["checker_path"] == str(checker)
    assert result["browser_check"]["checker_path"] != str(private_checker)


@pytest.mark.parametrize("updates", [
    {"passed": False}, {"dist_path": "/wrong/candidate"}, {"checks": []}, {"checks": [{"passed": False}]}, {"blocked_requests": [{"method": "POST"}]}, {"page_errors": ["failed"]}, {"mode": "made_up"}, {"started_at": "not a timestamp"},
])
def test_failed_or_unbound_browser_report_cannot_mark_revision_verified(state, updates):
    make_revision(state, "candidate")
    with pytest.raises(agents.AgentError) as error:
        report = browser_report(state, **updates)
        project = next(parent for parent in state.parents if parent.name == ".local").parent
        revisions.mark_browser_verified(state, "candidate", report, project_root=project)
    assert error.value.code == "invalid_browser_report"
    assert revisions.get(state, "candidate").get("browser_verified") is not True


def test_real_build_sandbox_has_no_credentials_vault_or_network(tmp_path, state):
    if not shutil.which("bwrap") or not shutil.which("node"):
        pytest.skip("Bubblewrap and Node required")
    web = state / "sandbox/web"
    web.mkdir(parents=True)
    modules = state / "sandbox/modules"
    for entry in ("typescript/bin/tsc", "vite/bin/vite.js"):
        target = modules / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    dist = state / "sandbox/dist"
    dist.mkdir()
    vault = tmp_path / "private-vault"
    vault.write_text("not readable in build")
    program = (
        "const fs=require('fs'); const net=require('net'); "
        f"if(fs.existsSync({json.dumps(str(vault))})) process.exit(10); "
        "if(process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || process.env.HOME !== '/tmp') process.exit(11); "
        "fs.writeFileSync('/out/proof.txt','isolated'); "
        "try { fs.writeFileSync('/app/forbidden','x'); process.exit(12); } catch {} "
        "const interfaces=require('os').networkInterfaces(); "
        "if(Object.keys(interfaces).some(name=>name!=='lo')) process.exit(13); console.log('isolated');"
    )
    completed = subprocess.run(revisions._sandbox_command(web, modules, dist, ["-e", program]), capture_output=True, text=True, timeout=15, env={"PATH": os.defpath, "OPENAI_API_KEY": "test-do-not-inherit", "CODEX_API_KEY": "test-do-not-inherit"})
    assert completed.returncode == 0, completed.stderr
    assert (dist / "proof.txt").read_text() == "isolated"
    assert not (web / "forbidden").exists()
