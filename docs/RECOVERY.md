# Resume without restarting

Read `MISSION_STATE.json`, `VERIFICATION.json` and `RUNBOOK.md` before changing code. The current stopping point is a tested Level 1 shadow build awaiting real site/infrastructure choices, not an unfinished foundation.

The saved archive contains the source tree plus a `recovery` directory with a Git bundle, pytest report and fixture-only SQLite snapshot. No runtime environment file, private token, provider credential or production database is included.

Use the source directly, or clone `recovery/repository.bundle` into a new working directory to recover the branch/history. Follow README setup to recreate dependencies and generate new local capabilities. To resume the exact fixture database, copy `recovery/demo-state.sqlite3` to `seo-autonomous.db` before bootstrap. Keep it separate from any production database. Running bootstrap again preserves the same site/cycle.

The database's MissionState and immutable checkpoint action record the build status. Future live state belongs in PostgreSQL on the selected durable host, with backups. The fixture snapshot is a recovery aid, not a production backup or evidence of SEO results.

Next critical decisions are the real domain/CMS, the qualified business outcome, scoped access, repository destination and host. Optional SERP/AI credentials do not block core shadow ingestion. Do not activate publishing, destructive operations or automatic autonomy graduation.
