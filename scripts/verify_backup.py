"""Verify a private backup receipt and archive without restoring a database.

This is a pre-restore integrity gate, not evidence that recovery works. Only run
it on an operator-trusted receipt/archive pair; PostgreSQL archive parsing is
not a safe way to establish provenance for bytes from an unknown source.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess


MAX_RECEIPT_BYTES = 65_536
ARCHIVE_NAME = re.compile(r"^seo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.dump$")
REQUIRED_FIELDS = {
    "archive", "sha256", "bytes", "format", "archive_list_verified",
    "restore_verified", "writers_stopped_attested", "services_restarted",
    "contains_sensitive_canonical_state",
}


def _private_mode(path: Path, *, directory: bool = False) -> os.stat_result:
    details = path.stat(follow_symlinks=False)
    expected = stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
    if not expected or path.is_symlink():
        raise ValueError("Backup inputs must be non-symlink regular files in a regular directory")
    if details.st_mode & 0o077:
        raise ValueError("Backup inputs and their directory must not grant group/other access")
    return details


def verify_backup_receipt(receipt_path: Path, *, run=subprocess.run) -> dict:
    supplied = Path(receipt_path)
    directory = supplied.parent.resolve(strict=True)
    _private_mode(directory, directory=True)
    receipt_path = directory / supplied.name
    receipt_stat = _private_mode(receipt_path)
    if receipt_stat.st_size > MAX_RECEIPT_BYTES:
        raise ValueError("Backup receipt exceeds the bounded JSON size")
    with receipt_path.open("rb") as handle:
        raw_receipt = handle.read(MAX_RECEIPT_BYTES + 1)
    if len(raw_receipt) > MAX_RECEIPT_BYTES:
        raise ValueError("Backup receipt exceeds the bounded JSON size")
    try:
        receipt = json.loads(raw_receipt)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or not REQUIRED_FIELDS.issubset(receipt):
        raise ValueError("Backup receipt is missing required integrity fields")
    archive_name = receipt["archive"]
    if not isinstance(archive_name, str) or not ARCHIVE_NAME.fullmatch(archive_name):
        raise ValueError("Backup receipt contains an invalid archive name")
    if receipt_path.name != archive_name.removesuffix(".dump") + ".json":
        raise ValueError("Backup receipt and archive basenames do not match")
    if (receipt["format"] != "postgresql-custom" or receipt["archive_list_verified"] is not True
            or receipt["restore_verified"] is not False or receipt["writers_stopped_attested"] is not True
            or receipt["services_restarted"] is not False
            or receipt["contains_sensitive_canonical_state"] is not True):
        raise ValueError("Backup receipt lifecycle claims are inconsistent with a new checkpoint")
    expected_bytes = receipt["bytes"]
    expected_sha256 = receipt["sha256"]
    if (type(expected_bytes) is not int or expected_bytes <= 0
            or not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError("Backup receipt has invalid size or checksum fields")
    archive_path = directory / archive_name
    archive_stat = _private_mode(archive_path)
    if archive_stat.st_size != expected_bytes:
        raise ValueError("Backup archive size differs from its receipt")
    checksum = hashlib.sha256()
    with archive_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    if not hmac.compare_digest(checksum.hexdigest(), expected_sha256):
        raise ValueError("Backup archive checksum differs from its receipt")
    inspected = run(["pg_restore", "--list", str(archive_path)], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=60, check=False)
    if inspected.returncode:
        raise RuntimeError("Backup archive list validation failed")
    return {"status": "verified", "archive": archive_name, "sha256": expected_sha256,
            "bytes": expected_bytes, "archive_list_verified": True,
            "restore_verified": False, "safe_to_restore_over_active_database": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_backup_receipt(args.receipt)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "restore_attempted": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
