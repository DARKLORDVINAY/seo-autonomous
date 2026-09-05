import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts.collect_ci_startup_diagnostics import collect, redact, secret_values


def test_redacts_generated_and_overridden_credentials_before_truncating(tmp_path):
    (tmp_path / ".env").write_text('API_TOKEN="generated-token"\nPOSTGRES_PASSWORD="p a/ss"\n')
    secrets = secret_values({
        "API_TOKEN": "override-token",
        "DATABASE_URL": "postgresql+psycopg://ci:url-password@db/seo",
    }, tmp_path / ".env")
    raw = "generated-token override-token p a/ss p%20a%2Fss p+a%2Fss url-password"
    assert redact(raw, secrets) == " ".join(["[REDACTED]"] * 6)
    assert "generated" not in redact("x" * 100 + "generated-token", secrets, 10)


def test_redacts_unregistered_credentials_and_validation_values_but_preserves_traceback():
    raw = "\n".join([
        'Traceback (most recent call last):',
        'ValueError: invalid startup setting',
        '  input_value={"unexpected": "not-an-env-secret"}, input_type=dict',
        'Authorization: Bearer unknown-bearer',
        'payload={"api_key": "unknown-api-key"}',
        'connect postgresql+psycopg://user:unknown-password@db/seo',
        'token from provider: ghp_unknownprovidersecret',
        '-----BEGIN PRIVATE KEY-----\nunknown-private-key\n-----END PRIVATE KEY-----',
    ])
    result = redact(raw, ())
    assert "Traceback (most recent call last):" in result
    assert "ValueError: invalid startup setting" in result
    for forbidden in ("not-an-env-secret", "unknown-bearer", "unknown-api-key", "unknown-password",
                      "unknownprovidersecret", "unknown-private-key"):
        assert forbidden not in result


@pytest.mark.parametrize("health_checks", [
    None,
    [{"ExitCode": 1, "Output": "known-secret health failure"}] * 5,
])
def test_collects_only_allowed_states_and_bounded_sanitized_logs(tmp_path, health_checks):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1:3] == ["compose", "ps"]:
            return SimpleNamespace(returncode=0, stdout="a" * 64)
        if args[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "service": "api", "status": "restarting", "exit_code": 1,
                "restart_count": 3, "health": "unhealthy",
                "health_checks": health_checks,
                "Config": {"Env": ["unrequested-private-field"]},
            }))
        return SimpleNamespace(returncode=0, stdout="x" * 40000 + "\nknown-secret ValueError: startup failed")

    report = collect({"API_TOKEN": "known-secret"}, tmp_path, run)
    serialized = json.dumps(report)
    assert "known-secret" not in serialized
    assert "unrequested-private-field" not in serialized
    assert report["containers"][0]["restart_count"] == 3
    assert len(report["containers"][0]["health_checks"]) == (3 if health_checks else 0)
    assert set(report["logs"]) == {"api", "migrate", "worker"}
    assert all(len(value) <= 32768 + len("[TRUNCATED]\n") for value in report["logs"].values())
    assert "ValueError: startup failed" in report["logs"]["api"]
    assert all(kwargs["timeout"] == 10 and kwargs["capture_output"] for _, kwargs in calls)
    inspect = next(args for args, _ in calls if args[1] == "inspect")
    assert "--format" in inspect and ".Config.Env" not in inspect[3]
    assert not report["collection_errors"]


def test_command_failure_and_timeout_never_publish_raw_error_text(tmp_path):
    def run(args, **kwargs):
        if args[1:3] == ["compose", "ps"]:
            raise subprocess.TimeoutExpired(args, 10, output="private timeout output")
        return SimpleNamespace(returncode=1, stdout="private stdout", stderr="private stderr")

    report = collect({}, tmp_path, run)
    assert "private" not in json.dumps(report)
    assert len(report["collection_errors"]) == 4
    assert report["collection_errors"][0]["error"] == "TimeoutExpired"
    assert report["logs"] == {}


def test_ci_retains_failure_diagnostics_before_upload_and_cleanup():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    collect_at = workflow.index("- name: Collect sanitized startup failure diagnostics")
    upload_at = workflow.index("uses: actions/upload-artifact@v4")
    cleanup_at = workflow.index("- name: Stop isolated test stack")
    assert collect_at < upload_at < cleanup_at
    diagnostic_step = workflow[collect_at:workflow.index("- name: Retain dashboard screenshots")]
    assert "if: failure() && hashFiles('.env') != ''" in diagnostic_step
    assert "continue-on-error" not in diagnostic_step
    assert "artifacts/compose-startup-diagnostics.json" in workflow[upload_at:cleanup_at]
