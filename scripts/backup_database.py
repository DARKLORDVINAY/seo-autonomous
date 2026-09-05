"""Create one atomic, private PostgreSQL backup bundle; never restore or restart.

Uses a preconfigured libpq service for credentials plus a bounded, non-secret
conninfo override for the exact verified-TLS target. The operator must pin the
source and release identities and explicitly quiesce writers. A hidden staging
directory is renamed only after both the custom archive and its receipt have
been synced and validated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid


IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}")
DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SCHEMA_HEAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
SERVER_IDENTITY = re.compile(r"[0-9]{10,24}")
TLS_SERVER_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?"
)
RELEASE_IMAGE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}")
RELEASE_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MAX_COMMAND_OUTPUT_BYTES = 16_384
PASSTHROUGH_ENVIRONMENT = {
    "HOME", "PATH", "PGPASSFILE", "PGSERVICEFILE", "PGSYSCONFDIR",
    "SSL_CERT_DIR", "SSL_CERT_FILE",
}

SOURCE_METADATA_SQL = """\
SELECT pg_catalog.json_build_object(
  'database', pg_catalog.current_database(),
  'server_identity', control.system_identifier::text,
  'server_address', COALESCE(pg_catalog.inet_server_addr()::text, 'unix-socket'),
  'server_port', COALESCE(pg_catalog.inet_server_port(), 0),
  'server_version', pg_catalog.current_setting('server_version'),
  'server_version_num', pg_catalog.current_setting('server_version_num'),
  'schema_heads', COALESCE(
    (SELECT pg_catalog.json_agg(version_num ORDER BY version_num) FROM public.alembic_version),
    '[]'::json
  )
)::text
FROM pg_catalog.pg_control_system() AS control
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_identity(value: str, *, name: str, pattern: re.Pattern[str] = IDENTITY) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} must be an explicit bounded identity")
    return value


def _validate_network_target(*, tls_server_name: str, server_address: str, server_port: int) -> tuple[str, str, int]:
    if not isinstance(tls_server_name, str) or not TLS_SERVER_NAME.fullmatch(tls_server_name):
        raise ValueError("TLS server name must be one exact bounded DNS name")
    try:
        canonical_address = str(ipaddress.ip_address(server_address))
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected server address must be one exact numeric IP address") from exc
    if type(server_port) is not int or not 1 <= server_port <= 65_535:
        raise ValueError("Expected server port must be between 1 and 65535")
    return tls_server_name.lower().rstrip("."), canonical_address, server_port


def _connection_argument(
    *, service: str, database: str, tls_server_name: str, server_address: str, server_port: int
) -> str:
    # Values are validated to exclude conninfo quoting characters. Parameters
    # after `service` override weaker or multi-host values in the service file.
    return (
        f"service={service} dbname={database} host={tls_server_name} "
        f"hostaddr={server_address} port={server_port} "
        "sslmode=verify-full gssencmode=disable"
    )


def _source_hash(source: dict) -> str:
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_output(result, *, command: str, prefix: str | None = None) -> str:
    if result.returncode:
        raise RuntimeError(f"{command} failed")
    stderr = result.stderr
    if not isinstance(stderr, bytes) or stderr:
        raise RuntimeError(f"{command} emitted stderr; promotion is blocked")
    stdout = result.stdout
    if not isinstance(stdout, bytes) or not stdout or len(stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise RuntimeError(f"{command} returned invalid or oversized output")
    try:
        value = stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{command} returned non-UTF-8 output") from exc
    if not value or any(ord(character) < 32 for character in value):
        raise RuntimeError(f"{command} returned malformed output")
    if prefix is not None:
        version_pattern = re.escape(prefix) + r"[0-9]+(?:\.[0-9]+)+(?: [ -~]{1,120})?"
        if len(value) > 160 or not re.fullmatch(version_pattern, value):
            raise RuntimeError(f"{command} returned an unexpected version string")
    return value


def _tool_version(tool: str, *, env: dict[str, str], run) -> str:
    result = run([tool, "--version"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 timeout=15, check=False)
    return _strict_output(result, command=f"{tool} --version", prefix=f"{tool} (PostgreSQL) ")


def _source_metadata(*, connection_argument: str, env: dict[str, str], run) -> dict:
    result = run(
        ["psql", "--no-password", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--tuples-only", "--no-align",
         "--dbname=" + connection_argument, "--command", SOURCE_METADATA_SQL],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    raw = _strict_output(result, command="source identity query")
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Source identity query returned invalid JSON") from exc
    required = {
        "database", "server_identity", "server_address", "server_port",
        "server_version", "server_version_num", "schema_heads",
    }
    if not isinstance(metadata, dict) or set(metadata) != required:
        raise RuntimeError("Source identity query returned an incomplete shape")
    return metadata


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_bundle(staging: Path, bundle: Path, directory: Path) -> None:
    """Publish the complete directory under a cooperative private-directory lock."""
    lock_path = directory / ".seo-backup-publish.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        lock_details = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_details.st_mode) or lock_details.st_mode & 0o077:
            raise ValueError("Backup publication lock must be a private regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if bundle.exists() or bundle.is_symlink():
            raise FileExistsError("Backup bundle target already exists; nothing was overwritten")
        os.rename(staging, bundle)
        _sync_directory(directory)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def backup(
    service: str,
    output_directory: Path,
    *,
    writers_stopped: bool,
    expected_database: str,
    expected_server_identity: str,
    expected_server_address: str,
    expected_server_port: int,
    expected_schema_heads: tuple[str, ...],
    tls_server_name: str,
    release_image: str,
    release_commit: str,
    runtime_identity: str,
    checkpoint_identity: str,
    run=subprocess.run,
    _allow_local_socket_test_adapter: bool = False,
) -> dict:
    if not writers_stopped:
        raise ValueError("Explicit quiesced-writer confirmation is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", service):
        raise ValueError("Use a named libpq service, not a connection string")
    _validate_identity(expected_database, name="Expected database", pattern=DATABASE_NAME)
    _validate_identity(expected_server_identity, name="Expected server identity", pattern=SERVER_IDENTITY)
    if _allow_local_socket_test_adapter:
        if (not os.environ.get("PYTEST_CURRENT_TEST")
                or service != "disposable_ci_source"
                or not re.fullmatch(r"v2_backup_[0-9a-f]{32}", expected_database)
                or (expected_server_address, expected_server_port, tls_server_name) != (
                    "unix-socket", 0, "local-socket-test")):
            raise ValueError("The local-socket adapter is restricted to the disposable restore test")
        connection_argument = f"service={service} dbname={expected_database} gssencmode=disable"
        transport_policy = "local-socket-test"
    else:
        tls_server_name, expected_server_address, expected_server_port = _validate_network_target(
            tls_server_name=tls_server_name,
            server_address=expected_server_address,
            server_port=expected_server_port,
        )
        connection_argument = _connection_argument(
            service=service,
            database=expected_database,
            tls_server_name=tls_server_name,
            server_address=expected_server_address,
            server_port=expected_server_port,
        )
        transport_policy = "verified-tls"
    if (not isinstance(expected_schema_heads, tuple) or not expected_schema_heads
            or tuple(sorted(set(expected_schema_heads))) != expected_schema_heads
            or any(not isinstance(head, str) or not SCHEMA_HEAD.fullmatch(head) for head in expected_schema_heads)):
        raise ValueError("Expected schema heads must be a non-empty sorted unique tuple")
    _validate_identity(release_image, name="Release image", pattern=RELEASE_IMAGE)
    _validate_identity(release_commit, name="Release commit", pattern=RELEASE_COMMIT)
    _validate_identity(runtime_identity, name="Runtime identity")
    _validate_identity(checkpoint_identity, name="Checkpoint identity")

    supplied_directory = Path(output_directory)
    if supplied_directory.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    directory = supplied_directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or directory.is_symlink() or details.st_mode & 0o077:
        raise ValueError("Backup directory must be a private non-symlink directory (mode 0700)")
    env = {
        **{key: value for key, value in os.environ.items() if key in PASSTHROUGH_ENVIRONMENT},
        "PGCONNECT_TIMEOUT": "10",
        "PGCLIENTENCODING": "UTF8",
        "PGSSLMODE": "verify-full",
        "PGGSSENCMODE": "disable",
        "LC_ALL": "C",
        "TZ": "UTC",
    }

    pg_dump_version = _tool_version("pg_dump", env=env, run=run)
    pg_restore_version = _tool_version("pg_restore", env=env, run=run)
    psql_version = _tool_version("psql", env=env, run=run)
    source = _source_metadata(connection_argument=connection_argument, env=env, run=run)
    if source["database"] != expected_database:
        raise RuntimeError("Connected database does not match the pinned source database")
    if source["server_identity"] != expected_server_identity:
        raise RuntimeError("Connected PostgreSQL cluster does not match the pinned server identity")
    if (source["server_address"] != expected_server_address
            or source["server_port"] != expected_server_port):
        raise RuntimeError("Connected PostgreSQL endpoint does not match the pinned network target")
    observed_heads = source["schema_heads"]
    if (not isinstance(observed_heads, list)
            or any(not isinstance(head, str) or not SCHEMA_HEAD.fullmatch(head) for head in observed_heads)
            or tuple(sorted(set(observed_heads))) != expected_schema_heads):
        raise RuntimeError("Connected database schema heads do not match the pinned schema identity")
    if (not isinstance(source["server_address"], str) or not source["server_address"]
            or len(source["server_address"]) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in source["server_address"])
            or type(source["server_port"]) is not int or not 0 <= source["server_port"] <= 65_535
            or not isinstance(source["server_version"], str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?: [ -~]{1,120})?", source["server_version"])
            or not isinstance(source["server_version_num"], str)
            or not re.fullmatch(r"[0-9]{5,6}", source["server_version_num"])):
        raise RuntimeError("Connected PostgreSQL server metadata is invalid")
    if int(source["server_version_num"]) // 10_000 != int(source["server_version"].split(".", 1)[0]):
        raise RuntimeError("Connected PostgreSQL server version fields disagree")

    basename = _utcnow().strftime("seo-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    staging = directory / ("." + basename + ".partial")
    bundle = directory / (basename + ".backup")
    archive_name = basename + ".dump"
    receipt_name = basename + ".json"
    if bundle.exists() or bundle.is_symlink():
        raise FileExistsError("Backup bundle target already exists; nothing was overwritten")
    staging.mkdir(mode=0o700)
    archive = staging / archive_name
    try:
        dump_started_at = _utcnow()
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            dumped = run(
                ["pg_dump", "--format=custom", "--no-password", "--dbname=" + connection_argument],
                env=env,
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=1800,
                check=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        dump_completed_at = _utcnow()
        if dumped.returncode:
            raise RuntimeError("pg_dump failed; hidden incomplete bundle retained for diagnosis")
        if not isinstance(dumped.stderr, bytes) or dumped.stderr:
            raise RuntimeError("pg_dump emitted stderr; hidden bundle was not promoted")
        if archive.stat().st_size == 0:
            raise RuntimeError("An empty archive is not a backup")
        source_after = _source_metadata(connection_argument=connection_argument, env=env, run=run)
        if source_after != source:
            raise RuntimeError("Post-dump source identity differs from the pinned pre-dump source")
        inspected = run(["pg_restore", "--list", str(archive)], env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, timeout=60, check=False)
        if inspected.returncode:
            raise RuntimeError("Archive inspection failed; hidden bundle was not promoted")
        if not isinstance(inspected.stderr, bytes) or inspected.stderr:
            raise RuntimeError("Archive inspection emitted stderr; hidden bundle was not promoted")
        archive_list_verified_at = _utcnow()
        checksum = hashlib.sha256()
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
        receipt_created_at = _utcnow()
        receipt = {
            "receipt_schema_version": 3,
            "bundle": bundle.name,
            "archive": archive_name,
            "receipt": receipt_name,
            "sha256": checksum.hexdigest(),
            "bytes": archive.stat().st_size,
            "format": "postgresql-custom",
            "source_service": service,
            "source_database": source["database"],
            "source_server_identity": source["server_identity"],
            "source_server_address": source["server_address"],
            "source_server_port": source["server_port"],
            "source_tls_server_name": tls_server_name,
            "transport_policy": transport_policy,
            "source_identity_pre_dump_sha256": _source_hash(source),
            "source_identity_post_dump_sha256": _source_hash(source_after),
            "source_identity_stable": True,
            "postgresql_server_version": source["server_version"],
            "postgresql_server_version_num": source["server_version_num"],
            "pg_dump_version": pg_dump_version,
            "pg_restore_version": pg_restore_version,
            "psql_version": psql_version,
            "schema_heads": list(expected_schema_heads),
            "release_image": release_image,
            "release_commit": release_commit,
            "runtime_identity": runtime_identity,
            "checkpoint_identity": checkpoint_identity,
            "dump_started_at": dump_started_at.isoformat(),
            "dump_completed_at": dump_completed_at.isoformat(),
            "archive_list_verified_at": archive_list_verified_at.isoformat(),
            "receipt_created_at": receipt_created_at.isoformat(),
            "archive_list_verified": True,
            "restore_verified": False,
            "writers_stopped_attested": True,
            "services_restarted": False,
            "warnings_present": False,
            "pg_dump_stderr_empty": True,
            "archive_list_stderr_empty": True,
            "contains_sensitive_canonical_state": True,
        }
        receipt_path = staging / receipt_name
        receipt_fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(receipt_fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(staging)
        _publish_bundle(staging, bundle, directory)
        return receipt
    except BaseException:
        # Preserve only the hidden staging directory for private diagnosis.
        # Never restart writers, overwrite a prior bundle, print raw stderr, or
        # claim restore success. No archive/receipt pair is published piecemeal.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, help="Existing private libpq service name")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-server-identity", required=True,
                        help="Pinned pg_control_system system_identifier")
    parser.add_argument("--expected-server-address", required=True,
                        help="Exact numeric address used with hostaddr")
    parser.add_argument("--expected-server-port", required=True, type=int)
    parser.add_argument("--tls-server-name", required=True,
                        help="Exact certificate hostname used with sslmode=verify-full")
    parser.add_argument("--schema-head", action="append", required=True,
                        help="Expected Alembic head; repeat for multiple heads")
    parser.add_argument("--release-image", required=True, help="Immutable OCI sha256 digest reference")
    parser.add_argument("--release-commit", required=True, help="Full 40- or 64-hex Git object ID")
    parser.add_argument("--runtime-identity", required=True)
    parser.add_argument("--checkpoint-identity", required=True)
    args = parser.parse_args()
    try:
        result = backup(
            args.service,
            args.output_directory,
            writers_stopped=args.writers_stopped,
            expected_database=args.expected_database,
            expected_server_identity=args.expected_server_identity,
            expected_server_address=args.expected_server_address,
            expected_server_port=args.expected_server_port,
            expected_schema_heads=tuple(sorted(set(args.schema_head))),
            tls_server_name=args.tls_server_name,
            release_image=args.release_image,
            release_commit=args.release_commit,
            runtime_identity=args.runtime_identity,
            checkpoint_identity=args.checkpoint_identity,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "writers_remain_stopped": True}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
