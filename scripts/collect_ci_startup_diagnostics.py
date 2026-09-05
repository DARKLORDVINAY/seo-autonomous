"""Collect bounded, redacted diagnostics for the disposable CI Compose stack."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, quote_plus, unquote, urlsplit

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("db", "api", "migrate", "worker")
LOG_SERVICES = ("api", "migrate", "worker")
SECRET_KEY = re.compile(
    r"password|passwd|token|secret|credential|api[_-]?key|authorization|cookie|database_url|postgres.*url",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"\b[\w.-]*(?:password|passwd|token|secret|credential|api[_-]?key|authorization|cookie|database_url)"
    r"[\w.-]*[\"']?\s*[:=]",
    re.IGNORECASE,
)
# This template intentionally never requests Config.Env, health commands, mounts,
# network configuration, or the full inspection object.
INSPECT_FORMAT = (
    '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"restart_count":{{json .RestartCount}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"health_checks":{{if .State.Health}}{{json .State.Health.Log}}{{else}}[]{{end}}}'
)


def secret_values(environment: dict, dotenv_path: Path) -> tuple[str, ...]:
    """Use both sources: Compose may override generated .env credentials in CI."""
    values = set()
    for source in (dotenv_values(dotenv_path, interpolate=False), environment):
        for key, value in source.items():
            if not value or not SECRET_KEY.search(key):
                continue
            values.add(value)
            if "://" in value:
                try:
                    password = urlsplit(value).password
                    if password:
                        values.add(unquote(password))
                except ValueError:
                    pass
    return tuple(sorted(
        {variant for value in values for variant in (value, quote(value, safe=""), quote_plus(value))},
        key=len, reverse=True,
    ))


def redact(value: str, secrets: tuple[str, ...], limit: int = 32768) -> str:
    # Redact before truncation so a boundary cannot leave a partial known secret.
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)",
        "[REDACTED_PRIVATE_KEY]", value, flags=re.DOTALL,
    )
    value = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[REDACTED_URL]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:Bearer|Basic)\s+\S+", "[REDACTED_AUTH]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)",
                   "[REDACTED_TOKEN]", value)
    lines = []
    for line in value.splitlines():
        if SENSITIVE_ASSIGNMENT.search(line):
            lines.append("[REDACTED_CREDENTIAL_LINE]")
        else:
            # Pydantic may render an entire configuration dictionary here.
            lines.append(re.sub(r"input_value=.*", "input_value=[REDACTED]", line))
    value = "\n".join(lines)
    return value if len(value) <= limit else "[TRUNCATED]\n" + value[-limit:]


def collect(environment: dict | None = None, root: Path = ROOT, run=subprocess.run) -> dict:
    secrets = secret_values(dict(os.environ) if environment is None else environment, root / ".env")
    report = {"scope": "disposable-ci-stack", "containers": [], "logs": {}, "collection_errors": []}

    def command(args: list[str], operation: str) -> str | None:
        try:
            result = run(args, cwd=root, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            # Exception strings and command stderr can themselves contain credentials.
            report["collection_errors"].append({"operation": operation, "error": type(error).__name__})
            return None
        if result.returncode:
            report["collection_errors"].append({"operation": operation, "exit_code": result.returncode})
            return None
        return result.stdout

    ids = command(["docker", "compose", "ps", "--all", "--quiet", *SERVICES], "list-containers")
    for container_id in (ids or "").splitlines()[:8]:
        if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
            report["collection_errors"].append({"operation": "list-containers", "error": "invalid-id"})
            continue
        raw = command(["docker", "inspect", "--format", INSPECT_FORMAT, container_id], "inspect-state")
        if raw is None:
            continue
        try:
            state = json.loads(raw)
            service = state["service"]
            if service not in SERVICES:
                raise ValueError("unexpected service")
            report["containers"].append({
                "service": service,
                "status": state["status"] if state["status"] in {
                    "created", "running", "paused", "restarting", "removing", "exited", "dead",
                } else "unknown",
                "exit_code": int(state["exit_code"]),
                "restart_count": int(state["restart_count"]),
                "health": state["health"] if state["health"] in {"starting", "healthy", "unhealthy"} else None,
                "health_checks": [
                    {"exit_code": int(check["ExitCode"]), "output": redact(check["Output"], secrets, 2048)}
                    for check in (state["health_checks"] or [])[-3:]
                ],
            })
        except (KeyError, TypeError, ValueError):
            report["collection_errors"].append({"operation": "inspect-state", "error": "invalid-state"})
    for service in LOG_SERVICES:
        raw = command([
            "docker", "compose", "logs", "--no-color", "--no-log-prefix", "--tail", "120", service,
        ], f"logs-{service}")
        if raw is not None:
            report["logs"][service] = redact(raw, secrets)
    return report


def main() -> int:
    output = ROOT / "artifacts" / "compose-startup-diagnostics.json"
    report = collect()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Saved redacted disposable-stack diagnostics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
