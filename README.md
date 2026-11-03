# YouCanSeeEverything

A local archive for personal data exports. Search records, browse days and trips, view attached media, and open the original files.

Python, SQLite and React. Runs on localhost for one user. Personal data stays outside the public repository.

## Run

Requires Python 3.11+, Node.js 20+, `uv`, and Linux or WSL.

```sh
uv sync --extra dev
cd web
npm ci
npm run build
cd ..
cp profile.example.json /path/to/private/profile.json
# Set the export and storage paths in that file.
export BIOGRAPHY_PROFILE=/path/to/private/profile.json
uv run biography init
uv run biography serve
```

Open <http://localhost:8000>.

## Import

Put exports in service folders under the profile's `export_root`.

```sh
uv run biography plan
uv run biography import --source "Example Service"
uv run biography daily-build
```

Original files are retained by content hash. Identical bytes share one copy. Records keep links to their sources. Unsupported files remain available for later extraction.

The day index resumes where it stopped. Run `daily-build` again after more imports or to continue a large index.

`--move` deletes export copies only after verified retention. Use it on export folders. Active notes, repositories and application histories should be snapshotted first.

## Browse

- **Records:** search and filter imported entries; view media and source files.
- **Days:** group entries by day, summarize measurement samples, and expand the underlying records.
- **Map and Trips:** browse recorded locations and inferred connections. Dashed lines connect known stops; the actual route may be unknown.
- **Imports:** view progress, extraction status, observed dates and gaps.
- **Ask:** ask questions or generate a cited summary for a day or month. Answers describe selected evidence, not the entire archive.

Google Maps is the default when a key is configured. Otherwise the map uses OpenStreetMap. Either can be selected manually. See [Google Maps setup](docs/google-maps-setup.md).

AI features use the Codex CLI's existing ChatGPT sign-in. Selected source excerpts are sent to that model. Dashboard edits are tested before activation and support undo. These features also require Bubblewrap and Playwright Chromium.

## Storage and backups

Keep personal data and configuration outside this repository. Metadata snapshots exclude original media. See [backup and restore](docs/backups.md) and [private Git LFS packages](docs/lfs-backups.md).

## Screenshots

Fictional records and generated artwork only. The map background is a schematic.

![Records and attached media](docs/screenshots/timeline.png)

<details>
<summary>Map and source details</summary>

![Map and inferred connection](docs/screenshots/map.png)
![Record and original source](docs/screenshots/evidence.png)

</details>

## Check changes

```sh
uv run pytest
cd web && npm test && npm run build && cd ..
uv run playwright install chromium
uv run python scripts/check_dashboard.py --dist web/dist --fixture --output .local/checks
```

Regenerate the fictional screenshots with `uv run python scripts/generate_demo_screenshots.py` after building the frontend.

MIT license. Use synthetic data in tests, examples and public screenshots.
