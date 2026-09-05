"""Verify a private atomic backup bundle without restoring a database.

This is a pre-restore integrity gate, not evidence that recovery works. Expected
pins must come from an independent operator release record. Only run it on an
operator-trusted bundle; PostgreSQL archive parsing cannot establish provenance
for bytes from an unknown source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from scripts.backup_database import (
    DATABASE_NAME,
    IDENTITY,
    RELEASE_COMMIT,
    RELEASE_IMAGE,
    SCHEMA_HEAD,
    SERVER_IDENTITY,
    TLS_SERVER_NAME,
)


MAX_RECEIPT_BYTES = 65_536
MAX_DUMP_DURATION = timedelta(hours=2)
MAX_POST_DUMP_VALIDATION = timedelta(minutes=10)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_NAME_TIME_SKEW = timedelta(minutes=5)
EARLIEST_ALLOWED_BACKUP = datetime(2020, 1, 1, tzinfo=timezone.utc)
BASENAME = r"seo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
ARCHIVE_NAME = re.compile(rf"^{BASENAME}\.dump$")
BUNDLE_NAME = re.compile(rf"^{BASENAME}\.backup$")
RECEIPT_NAME = re.compile(rf"^{BASENAME}\.json$")
REQUIRED_FIELDS = {
    "receipt_schema_version", "bundle", "archive", "receipt", "sha256", "bytes", "format",
    "source_service", "source_database", "source_server_identity", "source_server_address",
    "source_server_port", "source_tls_server_name", "transport_policy", "source_identity_pre_dump_sha256",
    "source_identity_post_dump_sha256", "source_identity_stable", "postgresql_server_version",
    "postgresql_server_version_num",
    "pg_dump_version", "pg_restore_version", "psql_version", "schema_heads", "release_image",
    "release_commit", "runtime_identity", "checkpoint_identity", "dump_started_at", "dump_completed_at",
    "archive_list_verified_at", "receipt_created_at", "archive_list_verified", "restore_verified",
    "writers_stopped_attested", "services_restarted", "warnings_present", "pg_dump_stderr_empty",
    "archive_list_stderr_empty", "contains_sensitive_canonical_state",
}


def _private_mode(path: Path, *, directory: bool = False) -> os.stat_result:
    details = path.stat(follow_symlinks=False)
    expected = stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
    if not expected or path.is_symlink():
        raise ValueError("Backup inputs must be non-symlink regular files in regular directories")
    if details.st_mode & 0o077:
        raise ValueError("Backup inputs and their directories must not grant group/other access")
    return details


def _bounded_text(value, *, name: str, maximum: int = 255) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Backup receipt has an invalid {name}")
    return value


def _version(value, *, tool: str) -> str:
    value = _bounded_text(value, name=f"{tool} version", maximum=160)
    pattern = re.escape(f"{tool} (PostgreSQL) ") + r"[0-9]+(?:\.[0-9]+)+(?: [ -~]{1,120})?"
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Backup receipt has an invalid {tool} version")
    return value


def _timestamp(value, *, name: str) -> datetime:
    value = _bounded_text(value, name=name, maximum=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Backup receipt has an invalid {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"Backup receipt {name} must be explicitly UTC")
    return parsed.astimezone(timezone.utc)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Backup receipt contains a duplicate JSON field")
        result[key] = value
    return result


def _validate_receipt(receipt: dict, *, now: datetime) -> dict[str, datetime]:
    if not isinstance(receipt, dict) or REQUIRED_FIELDS - set(receipt):
        raise ValueError("Backup receipt is missing required integrity fields")
    if set(receipt) != REQUIRED_FIELDS:
        raise ValueError("Backup receipt contains unsupported integrity fields")
    if type(receipt["receipt_schema_version"]) is not int or receipt["receipt_schema_version"] != 3:
        raise ValueError("Backup receipt schema version is unsupported")
    if (not isinstance(receipt["bundle"], str) or not BUNDLE_NAME.fullmatch(receipt["bundle"])
            or not isinstance(receipt["archive"], str) or not ARCHIVE_NAME.fullmatch(receipt["archive"])
            or not isinstance(receipt["receipt"], str) or not RECEIPT_NAME.fullmatch(receipt["receipt"])):
        raise ValueError("Backup receipt contains invalid bundle member names")
    archive_base = receipt["archive"].removesuffix(".dump")
    if receipt["bundle"] != archive_base + ".backup" or receipt["receipt"] != archive_base + ".json":
        raise ValueError("Backup bundle member basenames do not match")
    if (receipt["format"] != "postgresql-custom" or receipt["archive_list_verified"] is not True
            or receipt["restore_verified"] is not False or receipt["writers_stopped_attested"] is not True
            or receipt["services_restarted"] is not False or receipt["warnings_present"] is not False
            or receipt["pg_dump_stderr_empty"] is not True
            or receipt["archive_list_stderr_empty"] is not True
            or receipt["contains_sensitive_canonical_state"] is not True):
        raise ValueError("Backup receipt lifecycle or warning claims are inconsistent with a new checkpoint")
    if not isinstance(receipt["source_service"], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", receipt["source_service"]):
        raise ValueError("Backup receipt has an invalid source service")
    if not isinstance(receipt["source_database"], str) or not DATABASE_NAME.fullmatch(receipt["source_database"]):
        raise ValueError("Backup receipt has an invalid source database")
    if (not isinstance(receipt["source_server_identity"], str)
            or not SERVER_IDENTITY.fullmatch(receipt["source_server_identity"])):
        raise ValueError("Backup receipt has an invalid source server identity")
    source_address = _bounded_text(receipt["source_server_address"], name="source server address")
    if type(receipt["source_server_port"]) is not int or not 0 <= receipt["source_server_port"] <= 65_535:
        raise ValueError("Backup receipt has an invalid source server port")
    tls_server_name = _bounded_text(receipt["source_tls_server_name"], name="TLS server name")
    transport_policy = receipt["transport_policy"]
    if transport_policy == "verified-tls":
        if (not TLS_SERVER_NAME.fullmatch(tls_server_name)
                or tls_server_name != tls_server_name.lower().rstrip(".")):
            raise ValueError("Backup receipt has an invalid canonical TLS server name")
        try:
            canonical_address = str(ipaddress.ip_address(source_address))
        except ValueError as exc:
            raise ValueError("Backup receipt has an invalid verified network address") from exc
        if source_address != canonical_address or not 1 <= receipt["source_server_port"] <= 65_535:
            raise ValueError("Backup receipt has a non-canonical verified network endpoint")
    elif transport_policy == "local-socket-test":
        if (source_address, receipt["source_server_port"], tls_server_name) != (
                "unix-socket", 0, "local-socket-test"):
            raise ValueError("Backup receipt has an invalid disposable local-socket identity")
    else:
        raise ValueError("Backup receipt has an unsupported transport policy")
    pre_hash = receipt["source_identity_pre_dump_sha256"]
    post_hash = receipt["source_identity_post_dump_sha256"]
    if (receipt["source_identity_stable"] is not True
            or not isinstance(pre_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", pre_hash)
            or not isinstance(post_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", post_hash)
            or not hmac.compare_digest(pre_hash, post_hash)):
        raise ValueError("Backup receipt does not prove stable pre/post source observations")
    server_version = _bounded_text(
        receipt["postgresql_server_version"], name="PostgreSQL server version", maximum=160
    )
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?: [ -~]{1,120})?", server_version):
        raise ValueError("Backup receipt has an invalid PostgreSQL server version")
    if (not isinstance(receipt["postgresql_server_version_num"], str)
            or not re.fullmatch(r"[0-9]{5,6}", receipt["postgresql_server_version_num"])):
        raise ValueError("Backup receipt has an invalid PostgreSQL server version number")
    if int(receipt["postgresql_server_version_num"]) // 10_000 != int(server_version.split(".", 1)[0]):
        raise ValueError("Backup receipt PostgreSQL server version fields disagree")
    _version(receipt["pg_dump_version"], tool="pg_dump")
    _version(receipt["pg_restore_version"], tool="pg_restore")
    _version(receipt["psql_version"], tool="psql")
    heads = receipt["schema_heads"]
    if (not isinstance(heads, list) or not heads
            or any(not isinstance(head, str) or not SCHEMA_HEAD.fullmatch(head) for head in heads)
            or heads != sorted(set(heads))):
        raise ValueError("Backup receipt has invalid schema heads")
    source_observation = {
        "database": receipt["source_database"],
        "server_identity": receipt["source_server_identity"],
        "server_address": receipt["source_server_address"],
        "server_port": receipt["source_server_port"],
        "server_version": receipt["postgresql_server_version"],
        "server_version_num": receipt["postgresql_server_version_num"],
        "schema_heads": heads,
    }
    computed_source_hash = hashlib.sha256(
        json.dumps(source_observation, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(pre_hash, computed_source_hash):
        raise ValueError("Backup receipt source observation hash is inconsistent")
    if not isinstance(receipt["release_image"], str) or not RELEASE_IMAGE.fullmatch(receipt["release_image"]):
        raise ValueError("Backup receipt has an invalid immutable release image")
    if not isinstance(receipt["release_commit"], str) or not RELEASE_COMMIT.fullmatch(receipt["release_commit"]):
        raise ValueError("Backup receipt has an invalid release commit")
    for field in ("runtime_identity", "checkpoint_identity"):
        if not isinstance(receipt[field], str) or not IDENTITY.fullmatch(receipt[field]):
            raise ValueError(f"Backup receipt has an invalid {field}")
    expected_bytes = receipt["bytes"]
    expected_sha256 = receipt["sha256"]
    if (type(expected_bytes) is not int or expected_bytes <= 0
            or not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError("Backup receipt has invalid size or checksum fields")

    timestamps = {
        field: _timestamp(receipt[field], name=field)
        for field in ("dump_started_at", "dump_completed_at", "archive_list_verified_at", "receipt_created_at")
    }
    started = timestamps["dump_started_at"]
    completed = timestamps["dump_completed_at"]
    listed = timestamps["archive_list_verified_at"]
    created = timestamps["receipt_created_at"]
    if not EARLIEST_ALLOWED_BACKUP <= started <= completed <= listed <= created <= now + MAX_FUTURE_SKEW:
        raise ValueError("Backup receipt timestamps are outside allowed chronological bounds")
    if completed - started > MAX_DUMP_DURATION or created - completed > MAX_POST_DUMP_VALIDATION:
        raise ValueError("Backup receipt timing exceeds bounded dump or validation windows")
    archive_base = receipt["archive"].removesuffix(".dump")
    bundle_timestamp = datetime.strptime(archive_base[4:20], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if abs(started - bundle_timestamp) > MAX_NAME_TIME_SKEW:
        raise ValueError("Backup receipt dump time does not match its bundle identity")
    return timestamps


def _expected_pins(
    *,
    expected_service: str,
    expected_database: str,
    expected_server_identity: str,
    expected_server_address: str,
    expected_server_port: int,
    expected_tls_server_name: str,
    expected_schema_heads: tuple[str, ...],
    expected_release_image: str,
    expected_release_commit: str,
    expected_runtime_identity: str,
    expected_checkpoint_identity: str,
    expected_archive_sha256: str,
    expected_transport_policy: str,
) -> dict:
    if not isinstance(expected_service, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", expected_service):
        raise ValueError("Expected source service is invalid")
    if not isinstance(expected_database, str) or not DATABASE_NAME.fullmatch(expected_database):
        raise ValueError("Expected source database is invalid")
    if (not isinstance(expected_server_identity, str)
            or not SERVER_IDENTITY.fullmatch(expected_server_identity)):
        raise ValueError("Expected source server identity is invalid")
    if (not isinstance(expected_schema_heads, tuple) or not expected_schema_heads
            or tuple(sorted(set(expected_schema_heads))) != expected_schema_heads
            or any(not isinstance(head, str) or not SCHEMA_HEAD.fullmatch(head) for head in expected_schema_heads)):
        raise ValueError("Expected schema heads are invalid")
    if not isinstance(expected_release_image, str) or not RELEASE_IMAGE.fullmatch(expected_release_image):
        raise ValueError("Expected release image is invalid")
    if not isinstance(expected_release_commit, str) or not RELEASE_COMMIT.fullmatch(expected_release_commit):
        raise ValueError("Expected release commit is invalid")
    for name, value in (
        ("runtime identity", expected_runtime_identity),
        ("checkpoint identity", expected_checkpoint_identity),
    ):
        if not isinstance(value, str) or not IDENTITY.fullmatch(value):
            raise ValueError(f"Expected {name} is invalid")
    if (not isinstance(expected_archive_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_archive_sha256)):
        raise ValueError("Expected archive SHA-256 is invalid")
    if expected_transport_policy == "verified-tls":
        if (not isinstance(expected_tls_server_name, str)
                or not TLS_SERVER_NAME.fullmatch(expected_tls_server_name)):
            raise ValueError("Expected TLS server name is invalid")
        expected_tls_server_name = expected_tls_server_name.lower().rstrip(".")
        try:
            expected_server_address = str(ipaddress.ip_address(expected_server_address))
        except (TypeError, ValueError) as exc:
            raise ValueError("Expected server address is invalid") from exc
        if type(expected_server_port) is not int or not 1 <= expected_server_port <= 65_535:
            raise ValueError("Expected server port is invalid")
    elif expected_transport_policy == "local-socket-test":
        if (expected_server_address, expected_server_port, expected_tls_server_name) != (
                "unix-socket", 0, "local-socket-test"):
            raise ValueError("Expected disposable local-socket identity is invalid")
    else:
        raise ValueError("Expected transport policy is invalid")
    return {
        "source_service": expected_service,
        "source_database": expected_database,
        "source_server_identity": expected_server_identity,
        "source_server_address": expected_server_address,
        "source_server_port": expected_server_port,
        "source_tls_server_name": expected_tls_server_name,
        "schema_heads": list(expected_schema_heads),
        "release_image": expected_release_image,
        "release_commit": expected_release_commit,
        "runtime_identity": expected_runtime_identity,
        "checkpoint_identity": expected_checkpoint_identity,
        "sha256": expected_archive_sha256,
        "transport_policy": expected_transport_policy,
    }


def verify_backup_receipt(
    receipt_path: Path,
    *,
    expected_service: str,
    expected_database: str,
    expected_server_identity: str,
    expected_server_address: str,
    expected_server_port: int,
    expected_tls_server_name: str,
    expected_schema_heads: tuple[str, ...],
    expected_release_image: str,
    expected_release_commit: str,
    expected_runtime_identity: str,
    expected_checkpoint_identity: str,
    expected_archive_sha256: str,
    expected_transport_policy: str = "verified-tls",
    run=subprocess.run,
    now: datetime | None = None,
) -> dict:
    expected = _expected_pins(
        expected_service=expected_service,
        expected_database=expected_database,
        expected_server_identity=expected_server_identity,
        expected_server_address=expected_server_address,
        expected_server_port=expected_server_port,
        expected_tls_server_name=expected_tls_server_name,
        expected_schema_heads=expected_schema_heads,
        expected_release_image=expected_release_image,
        expected_release_commit=expected_release_commit,
        expected_runtime_identity=expected_runtime_identity,
        expected_checkpoint_identity=expected_checkpoint_identity,
        expected_archive_sha256=expected_archive_sha256,
        expected_transport_policy=expected_transport_policy,
    )
    supplied = Path(receipt_path)
    if supplied.parent.is_symlink():
        raise ValueError("Backup bundle directory must not be a symlink")
    bundle_directory = supplied.parent.resolve(strict=True)
    root_directory = bundle_directory.parent.resolve(strict=True)
    _private_mode(root_directory, directory=True)
    _private_mode(bundle_directory, directory=True)
    receipt_path = bundle_directory / supplied.name
    receipt_stat = _private_mode(receipt_path)
    if receipt_stat.st_size > MAX_RECEIPT_BYTES:
        raise ValueError("Backup receipt exceeds the bounded JSON size")
    with receipt_path.open("rb") as handle:
        raw_receipt = handle.read(MAX_RECEIPT_BYTES + 1)
    if len(raw_receipt) > MAX_RECEIPT_BYTES:
        raise ValueError("Backup receipt exceeds the bounded JSON size")
    try:
        receipt = json.loads(raw_receipt, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup receipt is not valid JSON") from exc
    verification_time = now or datetime.now(timezone.utc)
    if verification_time.tzinfo is None:
        raise ValueError("Verification time must be timezone-aware")
    _validate_receipt(receipt, now=verification_time.astimezone(timezone.utc))
    if bundle_directory.name != receipt["bundle"] or receipt_path.name != receipt["receipt"]:
        raise ValueError("Backup receipt path does not match its atomic bundle identity")
    members = {member.name for member in bundle_directory.iterdir()}
    if members != {receipt["archive"], receipt["receipt"]}:
        raise ValueError("Backup bundle must contain exactly its declared archive and receipt")
    for field, expected_value in expected.items():
        observed_value = receipt[field]
        matches = (hmac.compare_digest(observed_value, expected_value)
                   if field == "sha256" else observed_value == expected_value)
        if not matches:
            raise ValueError("Backup receipt differs from an independently supplied expected pin")
    archive_path = bundle_directory / receipt["archive"]
    archive_stat = _private_mode(archive_path)
    if archive_stat.st_size != receipt["bytes"]:
        raise ValueError("Backup archive size differs from its receipt")
    checksum = hashlib.sha256()
    with archive_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    if not hmac.compare_digest(checksum.hexdigest(), receipt["sha256"]):
        raise ValueError("Backup archive checksum differs from its receipt")
    inspect_env = {
        **{key: value for key, value in os.environ.items() if key in {"HOME", "PATH"}},
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    inspected = run(["pg_restore", "--list", str(archive_path)], stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, env=inspect_env, timeout=60, check=False)
    if inspected.returncode:
        raise RuntimeError("Backup archive list validation failed")
    if not isinstance(inspected.stderr, bytes) or inspected.stderr:
        raise RuntimeError("Backup archive list validation emitted stderr")
    return {
        "status": "verified",
        "bundle": receipt["bundle"],
        "archive": receipt["archive"],
        "sha256": receipt["sha256"],
        "bytes": receipt["bytes"],
        "source_service": receipt["source_service"],
        "source_database": receipt["source_database"],
        "source_server_identity": receipt["source_server_identity"],
        "schema_heads": receipt["schema_heads"],
        "release_image": receipt["release_image"],
        "release_commit": receipt["release_commit"],
        "runtime_identity": receipt["runtime_identity"],
        "checkpoint_identity": receipt["checkpoint_identity"],
        "archive_list_verified": True,
        "restore_verified": False,
        "safe_to_restore_over_active_database": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-service", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-server-identity", required=True)
    parser.add_argument("--expected-server-address", required=True)
    parser.add_argument("--expected-server-port", required=True, type=int)
    parser.add_argument("--expected-tls-server-name", required=True)
    parser.add_argument("--schema-head", action="append", required=True)
    parser.add_argument("--expected-release-image", required=True)
    parser.add_argument("--expected-release-commit", required=True)
    parser.add_argument("--expected-runtime-identity", required=True)
    parser.add_argument("--expected-checkpoint-identity", required=True)
    parser.add_argument("--expected-archive-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_backup_receipt(
            args.receipt,
            expected_service=args.expected_service,
            expected_database=args.expected_database,
            expected_server_identity=args.expected_server_identity,
            expected_server_address=args.expected_server_address,
            expected_server_port=args.expected_server_port,
            expected_tls_server_name=args.expected_tls_server_name,
            expected_schema_heads=tuple(sorted(set(args.schema_head))),
            expected_release_image=args.expected_release_image,
            expected_release_commit=args.expected_release_commit,
            expected_runtime_identity=args.expected_runtime_identity,
            expected_checkpoint_identity=args.expected_checkpoint_identity,
            expected_archive_sha256=args.expected_archive_sha256,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "restore_attempted": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
