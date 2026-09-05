from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from backend.app.config.settings import Settings


BENCHMARK_PINS = {
    "benchmark_evaluator_key_id": "independent-test-key",
    "benchmark_expected_definition_sha256": "a" * 64,
    "benchmark_expected_source_fingerprint": "b" * 64,
    "benchmark_expected_evaluation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "benchmark_expected_challenge_sha256": "c" * 64,
    "benchmark_expected_observations_sha256": "d" * 64,
    "benchmark_expected_predictions_sha256": "e" * 64,
    "benchmark_expected_truth_commitment_sha256": "f" * 64,
    "benchmark_expected_execution_environment_sha256": "1" * 64,
}


def test_base_compose_optional_benchmark_defaults_allow_api_settings(monkeypatch):
    # Exercise the base stack's declared benchmark defaults through the real
    # Settings parser. Compose startup itself is checked against Docker in CI.
    for name in Settings.model_fields:
        if name.startswith("benchmark_"):
            monkeypatch.delenv(name.upper(), raising=False)
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    api_and_shared = compose.split("x-worker-environment:", 1)[0]
    for name, expression in re.findall(r"^  (BENCHMARK_\w+): (.+)$", api_and_shared, re.MULTILINE):
        default = re.fullmatch(r"\$\{\w+:-([^}]*)\}", expression)
        assert default is not None, f"Unexpected base-stack benchmark expression for {name}"
        monkeypatch.setenv(name, default.group(1))
    config = Settings(
        _env_file=None, environment="production", service_role="api",
        database_url="postgresql+psycopg://seo_api@db/seo",
        api_token="a" * 32, approval_token="b" * 32, admin_token="c" * 32,
    )
    assert all(getattr(config, name) is None for name in BENCHMARK_PINS)
    assert config.benchmark_evaluator_public_key_file is None
    assert not config.production_enabled


@pytest.mark.parametrize("field", BENCHMARK_PINS)
@pytest.mark.parametrize("value", ["", "malformed pin!"])
def test_explicit_benchmark_pins_reject_empty_or_malformed_values(field, value):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **{field: value})
    assert any(item["loc"] == (field,) and item["type"] == "string_pattern_mismatch"
               for item in error.value.errors())


def test_explicit_valid_benchmark_pins_remain_accepted():
    config = Settings(_env_file=None, **BENCHMARK_PINS)
    assert all(getattr(config, name) == value for name, value in BENCHMARK_PINS.items())


def test_defaults_have_no_production_mutation_authority():
    config = Settings(_env_file=None)
    assert config.autonomy_level == 1
    assert config.production_enabled is False
    assert config.shadow_mode is True
    assert config.scheduler_enabled is False
    assert config.agent_mode == "fixture"
    assert config.provider_mode == "fixture"
    assert config.openai_model is None


def test_human_and_agent_capabilities_cannot_share_token():
    with pytest.raises(ValidationError, match="distinct capabilities"):
        Settings(_env_file=None, api_token="same-token", approval_token="same-token")


@pytest.mark.parametrize("kwargs", [
    {"environment": "production", "database_url": "sqlite://", "api_token": "x" * 40},
    {"environment": "production", "database_url": "postgresql+psycopg://db/seo"},
    {"autonomy_level": 4},
    {"production_enabled": True, "shadow_mode": True},
    {"agent_mode": "openai", "openai_api_key": "secret"},
    {"agent_mode": "openai", "openai_model": "chosen-explicitly"},
])
def test_invalid_authority_fails_closed(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


def test_production_reads_can_boot_without_write_authority():
    config = Settings(_env_file=None, environment="production", database_url="postgresql+psycopg://seo@db/seo", api_token="x" * 40)
    assert not config.production_enabled


def test_production_worker_boots_without_human_bearer_capabilities():
    config = Settings(_env_file=None, environment="production", service_role="worker",
                      database_url="postgresql+psycopg://seo@db/seo")
    assert config.api_token is config.approval_token is config.admin_token is None


def test_remote_production_database_requires_hostname_verified_tls():
    with pytest.raises(ValidationError, match="sslmode=verify-full"):
        Settings(_env_file=None, environment="production", service_role="worker",
                 database_url="postgresql+psycopg://seo@database.example.test/seo?sslmode=require")
    config = Settings(_env_file=None, environment="production", service_role="worker",
                      database_url="postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full")
    assert config.database_url.endswith("sslmode=verify-full")


@pytest.mark.parametrize("field", ["api_token", "approval_token", "admin_token"])
def test_worker_rejects_human_bearer_capabilities(field):
    with pytest.raises(ValidationError, match="Worker processes must not receive"):
        Settings(_env_file=None, service_role="worker", **{field: "x" * 40})


def test_tokens_do_not_appear_in_repr():
    token = "token-with-private-secret"
    config = Settings(_env_file=None, api_token=token)
    assert token not in repr(config)


@pytest.mark.parametrize("field", ["api_token", "approval_token", "admin_token"])
@pytest.mark.parametrize("length", [0, 1, 31])
@pytest.mark.parametrize("production_enabled", [False, True])
def test_every_configured_production_authority_rejects_short_tokens(field, length, production_enabled):
    tokens = {"api_token": "a" * 32, "approval_token": "b" * 32, "admin_token": "c" * 32}
    tokens[field] = "x" * length
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, environment="production", database_url="postgresql+psycopg://seo@db/seo",
                 production_enabled=production_enabled, shadow_mode=not production_enabled, **tokens)


@pytest.mark.parametrize("production_enabled", [False, True])
def test_distinct_production_authority_tokens_accept_minimum_length(production_enabled):
    config = Settings(_env_file=None, environment="production", database_url="postgresql+psycopg://seo@db/seo",
                      api_token="a" * 32, approval_token="b" * 32, admin_token="c" * 32,
                      production_enabled=production_enabled, shadow_mode=not production_enabled)
    assert config.production_enabled is production_enabled
