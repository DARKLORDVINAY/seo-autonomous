# Resume without restarting

When recovering or resuming an interrupted build, read `MISSION_STATE.json` and `VERIFICATION.json` for the relevant checkpoint. Read the sections of `RUNBOOK.md` relevant to deployment, scheduling, credentials or recovery when the task affects those operations. Reread when relevant state changes; use the repository's `AGENTS.md` for other task prerequisites.

Checkpoint statements describe the state and authority recorded at that time. Reconcile historical blockers and pending actions with the latest applicable user instruction and recorded decisions before resuming; completed work must not be restarted solely because an older checkpoint lists it as pending. An existing freeze remains effective unless an applicable later instruction supersedes it. The saved Level 1 shadow build is a recovery foundation, not evidence that later changes have passed its checks.

The saved archive contains the source tree plus a `recovery` directory with a Git bundle, pytest report and fixture-only SQLite snapshot. No runtime environment file, private token, provider credential or production database is included.

Use the source directly, or clone `recovery/repository.bundle` into a new working directory to recover the branch/history. Follow README setup to recreate dependencies and generate new local capabilities. To resume the exact fixture database, copy `recovery/demo-state.sqlite3` to `seo-autonomous.db` before bootstrap. Keep it separate from any production database. Running bootstrap again preserves the same site/cycle.

The database's MissionState and immutable checkpoint action record the build status. Future live state belongs in PostgreSQL on the selected durable host, with backups. The fixture snapshot is a recovery aid, not a production backup or evidence of SEO results.

Use the reconciled mission state to identify unresolved decisions about the domain/CMS, qualified business outcome, scoped access, repository destination and host. Optional SERP/AI credentials do not block core shadow ingestion. Recovery alone does not authorize publishing or destructive operations; retain the applicable production approval and recovery gates. Do not activate automatic autonomy graduation.
