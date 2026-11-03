# Working on YouCanSeeEverything

This is the public application repository. Keep personal archives, account names, machine paths, private configuration, screenshots, generated model requests, and real records outside this checkout. Use synthetic test fixtures. `.local` is private runtime state and must never be staged, including when it is a symlink.

The deployment profile is selected with `BIOGRAPHY_PROFILE`. Keep application code generic; use that external profile for account identity, source roots and exclusions. Public code and private dashboard/data repositories have separate remotes.

Retained source bytes are immutable. Do not remove source exports without independent hash readback, a durable manifest, complete archive reconciliation and an explicit cleanup root. Active notes vaults, repositories and application histories are snapshot sources, not cleanup targets. Preserve uncertainty and source variants. A metadata snapshot is not a raw-media backup.

Use existing Codex sign-in for model work. Do not add separately billed API calls, credential provisioning, arbitrary execution of generated ingestion code, or external dashboard publishing as defaults.

Validate relevant changes with `uv run pytest`, `npm test` and `npm run build` in `web/`. Browser checks use `scripts/check_dashboard.py` with an isolated fixture/browser context. Do not attach repeatedly to an existing user browser. Public commits contain code and synthetic fixtures only; private snapshots require credential review before Git publication.
