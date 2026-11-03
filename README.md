# YouCanSeeEverything

A local personal-history archive: import exported data, search one timeline, explore places alongside events, and inspect the original evidence. FastAPI and SQLite provide the catalog and full-text search; React provides the dashboard. Unique source bytes live in a content-addressed vault outside Git.

This is an early, single-user application. It binds to localhost. A public source-code checkout and a separate private profile/data directory are the intended deployment arrangement.

## Run locally

Requirements: Python 3.11+, Node.js 20+, `uv`, and Linux or WSL. Codex features additionally need the Codex CLI signed into ChatGPT, Bubblewrap, and Playwright Chromium. Existing subscription authentication is used; this app does not provision or use a separately billed API key.

```sh
uv sync --extra dev
cd web
npm ci
npm run build
cd ..
cp profile.example.json /path/to/private/profile.json
# Edit the private profile to point at your export and state directories.
export BIOGRAPHY_PROFILE=/path/to/private/profile.json
uv run biography init
uv run biography serve
```

Open <http://localhost:8000>. Keep the state directory under a directory named `.local`, for example `/path/to/private/.local`. Do not serve this application on a public interface; multi-user authentication and tenant isolation are not implemented.

## Import and retain data

Put each service's exports in its own directory under `export_root`, then inspect the inventory:

```sh
uv run biography plan
uv run biography import --source "Example Service"
```

Imports copy and independently verify original bytes by SHA-256. Repeated identical bytes share one object; records retain source-file hashes and locators. Native archive attachments remain intact. Source variants are retained. Unknown dates stay unknown, and date-only values are not turned into invented precise times.

`--move` explicitly enables deletion of exact, verified source copies inside the configured export root. Never point a move import at an active notes vault, repository, or application history. Snapshot active sources first. Outer export containers are discarded only when separately classified and fully reconciled; an unresolved archive remains available for review. Space reserves pause work rather than consuming all available disk.

The parser supports JSON/JSONL, CSV, calendar, text/Markdown, HTML, several document formats, exercise tracks, and common export structures. An optional Codex fallback proposes bounded declarative mappings or evidence-quoted text records. Unrecognized content is retained and reported. Supporting a format does not imply complete coverage of every service or every field.

## Explore and change the dashboard

- Search and filter the timeline by source, category and date; open records to inspect provenance and download retained originals.
- Use the map for explicit source coordinates. Same-day context is a temporal association, not a claim that a post was written at that place. Map tiles are optional and requested from OpenStreetMap when enabled.
- View explicitly associated local images and videos alongside records. Unresolved or ambiguous media references are not guessed.
- Ask Codex questions using retrieved evidence, or request a dashboard change. Changes build in an isolated workspace, pass checks and a browser smoke test, then activate atomically. Undo restores the previous UI. UI undo does not delete imported data.

Model processing sends selected imported content to the signed-in cloud model. Imported text is treated as untrusted data. The model cannot execute arbitrary ingestion scripts or shell commands through the fallback. Shell and agent-log collectors create redacted, visible-message projections; they omit hidden reasoning and tool results.

## Keep personal state separate

The public repository contains application code, synthetic tests, and generic examples only. Set `BIOGRAPHY_PROFILE` to a JSON file in your private workspace. `BIOGRAPHY_STATE_DIR`, `BIOGRAPHY_DATA_DIR`, and `BIOGRAPHY_EXPORT_ROOT` can override profile paths.

Keep raw objects, live databases, agent logs, screenshots, build artifacts and credentials out of ordinary Git. A private repository can hold configuration, normalized snapshots, analysis, manifests, and dashboard customizations. A records snapshot is not a backup of original media; preserve or separately back up the content-addressed vault.

## Validate

```sh
uv run pytest
cd web
npm test
npm run build
cd ..
uv run playwright install chromium
uv run python scripts/check_dashboard.py --dist web/dist --fixture --output .local/checks
```

Tests cover source cleanup safety, archive traversal and hardlinks, content readback, identity and timestamp behavior, parsing, localhost API boundaries, and isolated model-driven revisions. Fixture browser checks use synthetic data and block external network requests.

## Scope and roadmap

The first release emphasizes a source-backed timeline, map, retained media, and reversible dashboard edits. Remaining product work includes richer entity resolution, semantic retrieval, OCR/transcription, broader format coverage, background scheduling, per-user isolation and authenticated deployment. Inferences must preserve uncertainty and link back to evidence.

MIT licensed. Do not contribute personal exports or real credentials in examples, test fixtures, issues, or screenshots.
