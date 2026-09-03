"""Create a private, non-overwriting PostgreSQL archive; never restore or restart.

Uses a preconfigured libpq service, not a password/DSN command-line argument.
pg_dump and pg_restore must be installed. The owner explicitly quiesces writers
for the release checkpoint; this script does not control external processes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def backup(service: str, output_directory: Path, *, writers_stopped: bool, run=subprocess.run) -> dict:
    if not writers_stopped:
        raise ValueError("Explicit quiesced-writer confirmation is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", service):
        raise ValueError("Use a named libpq service, not a connection string")
    directory = output_directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (mode 0700)")
    basename = datetime.now(timezone.utc).strftime("seo-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    partial = directory / (basename + ".partial")
    archive = directory / (basename + ".dump")
    env = {**os.environ, "PGSERVICE": service, "PGCONNECT_TIMEOUT": "10"}
    # Do not let an ambient DSN override the explicitly selected service's target.
    for key in ("PGDATABASE", "PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGPASSWORD"):
        env.pop(key, None)
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            dumped = run(["pg_dump", "--format=custom", "--no-password"], env=env,
                         stdout=handle, stderr=subprocess.PIPE, timeout=1800, check=False)
            handle.flush()
            os.fsync(handle.fileno())
        if dumped.returncode:
            raise RuntimeError("pg_dump failed; incomplete private artifact retained for diagnosis")
        if partial.stat().st_size == 0:
            raise RuntimeError("An empty archive is not a backup")
        inspected = run(["pg_restore", "--list", str(partial)], env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, timeout=60, check=False)
        if inspected.returncode:
            raise RuntimeError("Archive inspection failed; artifact not promoted")
        checksum = hashlib.sha256()
        with partial.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
        # Hard-link promotion refuses an existing target and keeps every previous
        # backup. It happens only after successful process and archive validation.
        os.link(partial, archive)
        partial.unlink()
        receipt = {"archive": archive.name, "sha256": checksum.hexdigest(), "bytes": archive.stat().st_size,
                   "created_at": datetime.now(timezone.utc).isoformat(), "format": "postgresql-custom",
                   "archive_list_verified": True, "restore_verified": False,
                   "writers_stopped_attested": True, "services_restarted": False,
                   "warnings_present": bool(dumped.stderr), "contains_sensitive_canonical_state": True}
        receipt_path = directory / (basename + ".json")
        receipt_fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(receipt_fd, "w") as handle:
            json.dump(receipt, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return receipt
    except BaseException:
        # Preserve a partial file for private diagnosis. Never restart writers,
        # overwrite a prior backup, print raw stderr, or claim restore success.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, help="Existing private libpq service name")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        result = backup(args.service, args.output_directory, writers_stopped=args.writers_stopped)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "writers_remain_stopped": True}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
