"""Bounded, deterministic scheduler chaos; all state and providers are isolated.

SQLite exercises transactions, SQL fencing and real competing threads locally.
The cross-backend cases additionally exercise PostgreSQL when TEST_POSTGRES_URL
names an explicitly configured *test* database. No lab/production DB is opened.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
import random
import threading
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError

from backend.app.config.settings import Settings
from backend.app.contracts import ProviderUnavailable
from backend.app.db import models as m
from backend.app.db.repositories import leases
from backend.app.db.session import make_engine, make_session_factory
from backend.app.integrations import common
from backend.app.integrations.crawler.network import PublicNetworkBackend
from backend.app.scheduler import jobs, locking, worker
from backend.app.scheduler.locking import CommitFence, LeaseLost, fenced_site_work, site_lease_key
from backend.app.services import agent_audit, control


SOAK_SEED = 20260903
SOAK_DAYS = 28
SOAK_DUPLICATES = 4
CONCURRENT_WORKERS = 8
MAX_ATTEMPTS = 3


@dataclass
class Clock:
    instant: datetime = datetime(2026, 9, 1, 19, tzinfo=timezone.utc)

    def now(self):
        return self.instant

    def advance(self, seconds):
        self.instant += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(leases, "utcnow", value.now)
    monkeypatch.setattr(m, "utcnow", value.now)
    monkeypatch.setattr(control, "utcnow", value.now)
    return value


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Scheduler chaos may use only explicit local MockTransport providers")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    # The SSRF-safe crawler has its own httpcore transport, not HTTPTransport.
    # Block that egress route too without disabling configured PostgreSQL sockets.
    monkeypatch.setattr(PublicNetworkBackend, "connect_tcp", forbidden)


@contextmanager
def isolated_database(tmp_path, backend):
    owner_engine = None
    schema = None
    if backend == "postgresql":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Actual PostgreSQL chaos requires TEST_POSTGRES_URL; SQLite is not a substitute")
        owner_engine = make_engine(url)
        schema = "chaos_v2_" + uuid4().hex
        with owner_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = make_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        url = f"sqlite:///{tmp_path / 'scheduler-chaos.sqlite'}"
        engine = make_engine(url)
    try:
        m.Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        settings = Settings(_env_file=None, environment="test", database_url=url,
            agent_mode="fixture", provider_mode="fixture", autonomy_level=1,
            production_enabled=False, shadow_mode=True, max_daily_actions=0,
            max_daily_cost_usd=0, scheduler_lease_seconds=30)
        with factory() as session:
            site = control.create_site(session, name="Isolated scheduler chaos", base_url="https://example.test", fixture=True)
            site.config_json = {**site.config_json, "max_daily_actions": 0, "max_daily_model_calls": 0}
            mission = session.scalar(select(m.MissionState).where(m.MissionState.site_id == site.id))
            mission.resource_budget_json = {**mission.resource_budget_json, "max_daily_actions": 0,
                                           "max_daily_model_calls": 0, "max_daily_cost_usd": 0}
            session.commit()
            site_id = site.id
        yield settings, factory, site_id, engine
    finally:
        engine.dispose()
        if owner_engine is not None:
            # Only the unique schema this fixture created is removable.
            with owner_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            owner_engine.dispose()


@pytest.fixture
def chaos_db(tmp_path):
    with isolated_database(tmp_path, "sqlite") as database:
        yield database


@pytest.fixture(params=["sqlite", "postgresql"])
def cross_backend_db(tmp_path, request):
    with isolated_database(tmp_path, request.param) as database:
        yield database


def count(session, model, *conditions):
    return session.scalar(select(func.count()).select_from(model).where(*conditions))


def assert_authority_unchanged(factory, site_id):
    with factory() as session:
        site = session.get(m.Site, site_id)
        assert site.autonomy_level == 1 and site.production_enabled is False
        assert site.config_json["earned_categories"] == []
        assert site.config_json["max_daily_actions"] == 0
        assert site.config_json["max_daily_model_calls"] == 0
        assert count(session, m.ExecutionLease) == 0
        for run in session.scalars(select(m.AgentRun)):
            assert not run.result_json.get("llm_executed", False)
            assert run.result_json.get("reserved_model_calls", 0) == 0
            assert run.result_json.get("reserved_cost_upper_bound_usd", 0) == 0


@pytest.mark.parametrize("fault", ["timeout", "rate_limit"])
def test_failed_duplicate_ticks_have_a_durable_period_retry_ceiling(chaos_db, clock, monkeypatch, fault):
    settings, factory, site_id, _ = chaos_db
    attempts, delays = [], []

    def transport(request):
        attempts.append(str(request.url))
        if fault == "timeout":
            raise httpx.ReadTimeout("untrusted-provider-secret-must-not-be-recorded", request=request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    def failing_operation(session, site, config):
        # Exercise the real bounded HTTP retry helper at the provider boundary;
        # scheduler admission/leases/audit/transactions are not replaced.
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            common.request(client, "GET", "https://provider.example.test/read", sleep=delays.append)

    monkeypatch.setitem(jobs.OPERATIONS, jobs.INTEGRITY_CRAWL, failing_operation)
    results = [jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL,
               settings, scheduled_for=clock.now(), owner=f"restart-{i}") for i in range(12)]
    assert len(attempts) == MAX_ATTEMPTS * 3
    assert len(delays) == MAX_ATTEMPTS * 2
    assert {row["status"] for row in results[MAX_ATTEMPTS:]} == {"retry_exhausted"}
    assert len({row["job_id"] for row in results}) == 1
    with factory() as session:
        assert count(session, m.JobRun) == 1
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == MAX_ATTEMPTS
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_failed") == MAX_ATTEMPTS
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_retry_exhausted") == 1
        failures = list(session.scalars(select(m.FailureCase)))
        assert len(failures) >= MAX_ATTEMPTS
        assert "untrusted-provider-secret" not in json.dumps([control.serialise(row) for row in failures], default=str)
    assert_authority_unchanged(factory, site_id)


def test_crashed_daily_cycle_is_explicitly_unreconciled_not_reexecuted(chaos_db, clock, monkeypatch):
    settings, factory, site_id, _ = chaos_db
    key = jobs.period_key(jobs.DAILY_CYCLE, clock.now(), "UTC")
    with factory() as session:
        abandoned = m.JobRun(site_id=site_id, job_name="seo-control-loop", idempotency_key=key,
            owner="terminated-worker", status="running", result_json={"last_known_stage": "model_result_unknown"})
        session.add(abandoned)
        old = leases.acquire_lease(session, site_lease_key(site_id), "terminated-worker", site_id=site_id, ttl_seconds=30)
        session.commit()
        abandoned_id = abandoned.id
    clock.advance(31)

    def forbidden(*args):
        raise AssertionError("An interrupted paid-capable cycle cannot be blindly replayed")

    monkeypatch.setattr(control, "ingest_site", forbidden)
    first = jobs.run_scheduled_job(factory, settings, jobs.DAILY_CYCLE, site_id=site_id, scheduled_for=clock.now())[0]
    second = jobs.run_scheduled_job(factory, settings, jobs.DAILY_CYCLE, site_id=site_id, scheduled_for=clock.now())[0]
    assert first["status"] == second["status"] == "reconciliation_required"
    assert first["job_id"] == second["job_id"] == abandoned_id
    with factory() as session:
        assert count(session, m.JobRun) == 1
        assert count(session, m.FailureCase, m.FailureCase.category == "control_loop_interrupted") == 1
        assert count(session, m.Action, m.Action.kind == "control_loop_recovery") == 1
        assert not leases.owns_lease(session, old, now=clock.now())
        assert session.get(m.JobRun, abandoned_id).result_json["last_known_stage"] == "model_result_unknown"
    assert_authority_unchanged(factory, site_id)


def test_seeded_28_day_duplicate_tick_soak_uses_real_observation_operations(chaos_db, clock, record_property):
    settings, factory, site_id, _ = chaos_db
    generator = random.Random(SOAK_SEED)
    logical_keys, results = set(), []
    for day in range(SOAK_DAYS):
        clock.instant = datetime(2026, 9, 1, 19, tzinfo=timezone.utc) + timedelta(days=day)
        dispatches = [name for name in (jobs.INTEGRITY_CRAWL, jobs.EVENING_MEASUREMENT, jobs.WEEKLY_REVIEW)
                      for _ in range(SOAK_DUPLICATES)]
        generator.shuffle(dispatches)
        for tick, name in enumerate(dispatches):
            logical_keys.add((name, jobs.period_key(name, clock.now(), settings.scheduler_timezone)))
            results.append(jobs.run_observation_job(factory, site_id, name, settings,
                scheduled_for=clock.now(), owner=f"logical-worker-{tick % SOAK_DUPLICATES}"))
    assert len(results) == SOAK_DAYS * 3 * SOAK_DUPLICATES == 336
    assert len(logical_keys) == SOAK_DAYS * 2 + 5 == 61
    assert {row["status"] for row in results} == {"completed"}
    assert sum(not row.get("idempotent_replay", False) for row in results) == len(logical_keys)
    assert all(row["result"]["model_calls"] == 0 and row["result"]["production_mutations"] == 0 for row in results)
    with factory() as session:
        assert count(session, m.JobRun) == len(logical_keys)
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == len(logical_keys)
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_started") == len(logical_keys)
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == len(logical_keys)
        assert count(session, m.FailureCase) == 0
        assert count(session, m.DecisionLog) == 5
        assert count(session, m.AgentRun) == 0
        assert session.get(m.JobLease, site_lease_key(site_id)).fencing_token == len(logical_keys)
        assert count(session, m.StrategyVersion) == 1
    for key, value in {"seed": SOAK_SEED, "virtual_days": SOAK_DAYS, "ticks": len(results),
                       "logical_period_jobs": len(logical_keys), "duplicate_noops": len(results) - len(logical_keys),
                       "production_writes": 0, "paid_api_calls": 0}.items():
        record_property(key, value)
    assert_authority_unchanged(factory, site_id)


def test_eight_actual_competing_workers_execute_one_observation(cross_backend_db, clock, monkeypatch):
    settings, factory, site_id, _ = cross_backend_db
    barrier = threading.Barrier(CONCURRENT_WORKERS)
    release_winner = threading.Event()
    mutex = threading.Lock()
    losers = []
    original = jobs.OPERATIONS[jobs.WEEKLY_REVIEW]

    def observed_operation(session, site, config):
        # Hold the actual durable lease while all other SQL acquirers contend.
        # The real operation runs after their lease_busy outcomes are observed.
        assert release_winner.wait(timeout=8), "Contending workers did not return under a held lease"
        return original(session, site, config)

    monkeypatch.setitem(jobs.OPERATIONS, jobs.WEEKLY_REVIEW, observed_operation)

    def contender(number):
        barrier.wait(timeout=8)
        result = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings,
            scheduled_for=clock.now(), owner=f"real-thread-{number}")
        if result["status"] == "lease_busy":
            with mutex:
                losers.append(number)
                if len(losers) == CONCURRENT_WORKERS - 1:
                    release_winner.set()
        return result

    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as pool:
        results = list(pool.map(contender, range(CONCURRENT_WORKERS)))
    assert len(losers) == 7
    assert sum(row["status"] == "completed" for row in results) == 1
    with factory() as session:
        assert count(session, m.JobRun) == 1
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == 1
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 1
        assert session.get(m.JobLease, site_lease_key(site_id)).fencing_token == 1
    assert_authority_unchanged(factory, site_id)


def test_two_hundred_expiry_release_reclaims_keep_monotonic_fences(chaos_db, clock, record_property):
    _, factory, site_id, _ = chaos_db
    prior_handles = []
    generator = random.Random(SOAK_SEED + 1)
    for index in range(200):
        with factory() as session:
            handle = leases.acquire_lease(session, site_lease_key(site_id), f"owner-{index % 12}",
                site_id=site_id, ttl_seconds=5, now=clock.now())
            assert handle and handle.fencing_token == index + 1
            session.commit()
        with factory() as session:
            stale = generator.sample(prior_handles, min(3, len(prior_handles)))
            for old in stale:
                assert not leases.owns_lease(session, old, now=clock.now())
                assert leases.renew_lease(session, old, now=clock.now()) is None
                assert not leases.release_lease(session, old, now=clock.now())
            assert leases.owns_lease(session, handle, now=clock.now())
            if index % 2 == 0:
                assert leases.release_lease(session, handle, now=clock.now())
            session.commit()
        prior_handles.append(handle)
        clock.advance(6)  # Every second owner disappears instead of releasing.
    with factory() as session:
        assert count(session, m.JobLease) == 1
        assert session.get(m.JobLease, site_lease_key(site_id)).fencing_token == 200
        assert count(session, m.JobRun) == 0
    record_property("lease_reclaims", 200)
    record_property("virtual_owners", 12)
    record_property("seed", SOAK_SEED + 1)
    assert_authority_unchanged(factory, site_id)


def test_expired_worker_cannot_commit_after_real_sql_takeover(cross_backend_db, clock):
    _, factory, site_id, _ = cross_backend_db
    with factory() as session, fenced_site_work(session, site_id, owner="stale", ttl_seconds=30,
                                               session_factory=factory) as original:
        site = session.get(m.Site, site_id)
        site.name = "A stale worker must not persist this change"
        clock.advance(31)
        with factory() as replacement_session:
            replacement = leases.acquire_lease(replacement_session, site_lease_key(site_id), "replacement",
                site_id=site_id, ttl_seconds=30, now=clock.now())
            replacement_session.commit()
        assert replacement.fencing_token == original.fencing_token + 1
        with pytest.raises(LeaseLost):
            session.commit()
        session.rollback()
    with factory() as session:
        assert session.get(m.Site, site_id).name == "Isolated scheduler chaos"
        assert leases.owns_lease(session, replacement, now=clock.now())
        assert not leases.release_lease(session, original, now=clock.now())
    assert_authority_unchanged(factory, site_id)


@pytest.mark.skipif(os.name != "posix", reason="The real worker termination test uses POSIX fork")
def test_real_process_exit_preserves_started_attempt_and_recovers_safely(chaos_db, clock):
    settings, factory, site_id, engine = chaos_db
    database_url = engine.url.render_as_string(hide_password=False)
    assert database_url.startswith("sqlite:") and database_url.endswith("scheduler-chaos.sqlite")

    def child_worker():
        # New child-local engine: do not inherit pooled DB connections across fork.
        child_engine = make_engine(database_url)
        child_factory = make_session_factory(child_engine)

        def terminated_operation(session, site, config):
            session.add(m.DecisionLog(site_id=site.id, decision="UNCOMMITTED", rationale="crash injection", owner="test"))
            session.flush()
            os._exit(23)  # No context-manager cleanup: a real abandoned durable lease.

        jobs.OPERATIONS[jobs.WEEKLY_REVIEW] = terminated_operation
        jobs.run_observation_job(child_factory, site_id, jobs.WEEKLY_REVIEW, settings,
                                 scheduled_for=clock.now(), owner="killed-test-process")

    process = multiprocessing.get_context("fork").Process(target=child_worker)
    process.start()
    process.join(timeout=8)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        pytest.fail("Bounded crash test child did not exit")
    assert process.exitcode == 23
    with factory() as session:
        job = session.scalar(select(m.JobRun))
        assert job.status == "running"
        job_id = job.id
        assert count(session, m.DecisionLog) == 0
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_started") == 1
    busy = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert busy["status"] == "lease_busy"
    clock.advance(31)
    recovered = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert recovered["status"] == "completed" and recovered["job_id"] == job_id
    assert recovered["result"]["fencing_token"] == 2
    with factory() as session:
        assert count(session, m.JobRun) == 1
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_interrupted") == 1
        assert count(session, m.FailureCase, m.FailureCase.category == "scheduled_observation_interrupted") == 1
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 1
    assert_authority_unchanged(factory, site_id)


def test_completion_sql_failure_rolls_back_partial_results_before_retry(cross_backend_db, clock):
    settings, factory, site_id, engine = cross_backend_db
    failures = []

    def fail_completion(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO action_events" in statement and "scheduler_completed" in repr(parameters) and not failures:
            failures.append("completion_statement")
            raise OperationalError("injected completion boundary", {}, RuntimeError("do-not-log-provider-secret"))

    event.listen(engine, "before_cursor_execute", fail_completion)
    try:
        failed = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
        assert failed["status"] == "failed" and failed["error_type"] == "OperationalError"
        with factory() as session:
            assert count(session, m.DecisionLog) == 0
            assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 0
            assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_failed") == 1
        retried = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    finally:
        event.remove(engine, "before_cursor_execute", fail_completion)
    assert retried["status"] == "completed" and retried["job_id"] == failed["job_id"]
    with factory() as session:
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 1
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == 2
        failure = session.scalar(select(m.FailureCase))
        assert "do-not-log-provider-secret" not in str(control.serialise(failure))
    assert_authority_unchanged(factory, site_id)


def test_failed_failure_audit_leaves_unknown_not_false_success_then_reconciles(chaos_db, clock):
    settings, factory, site_id, engine = chaos_db

    def unavailable_terminal_storage(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO action_events" in statement and any(
                name in repr(parameters) for name in ("scheduler_completed", "scheduler_failed")):
            raise OperationalError("injected terminal storage outage", {}, RuntimeError("sensitive driver details"))

    event.listen(engine, "before_cursor_execute", unavailable_terminal_storage)
    try:
        outcome = jobs.run_scheduled_job(factory, settings, jobs.WEEKLY_REVIEW,
                                         site_id=site_id, scheduled_for=clock.now())[0]
    finally:
        event.remove(engine, "before_cursor_execute", unavailable_terminal_storage)
    assert outcome["status"] == "failed" and outcome["error_type"] == "OperationalError"
    with factory() as session:
        assert session.scalar(select(m.JobRun)).status == "running"
        assert count(session, m.DecisionLog) == 0
        assert count(session, m.ActionEvent) == 2  # Registration + committed attempt start.
    recovered = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert recovered["status"] == "completed"
    with factory() as session:
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.FailureCase, m.FailureCase.category == "scheduled_observation_interrupted") == 1
        interruption = session.scalar(select(m.ActionEvent).where(m.ActionEvent.event_type == "scheduler_interrupted"))
        assert interruption.details_json["state"] == "unknown"
    assert_authority_unchanged(factory, site_id)


def test_heartbeat_storage_failure_prevents_next_commit(chaos_db, clock):
    _, factory, site_id, engine = chaos_db
    with factory() as session:
        handle = leases.acquire_lease(session, site_lease_key(site_id), "heartbeat-owner", site_id=site_id, ttl_seconds=30)
        session.commit()
        fence = CommitFence(session, factory, handle, 30)

        class ImmediateBeat:
            calls = 0

            def wait(self, timeout):
                self.calls += 1
                return self.calls > 1  # A regressed heartbeat cannot make this test spin forever.

        fence.stopped = ImmediateBeat()

        def failed_renewal(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE job_leases"):
                raise OperationalError("injected renewal outage", {}, RuntimeError("private driver message"))

        event.listen(engine, "before_cursor_execute", failed_renewal)
        try:
            # Drive the original heartbeat synchronously; the SQL renewal is
            # real, while its wakeup timing is deterministic and sleep-free.
            fence._heartbeat()
        finally:
            event.remove(engine, "before_cursor_execute", failed_renewal)
        assert fence.lost.is_set()
        event.listen(session, "before_commit", fence._before_commit)
        try:
            session.get(m.Site, site_id).name = "Must not commit after heartbeat failure"
            with pytest.raises(LeaseLost):
                session.commit()
            session.rollback()
        finally:
            event.remove(session, "before_commit", fence._before_commit)
    with factory() as session:
        assert session.get(m.Site, site_id).name == "Isolated scheduler chaos"
    assert_authority_unchanged(factory, site_id)


def test_release_failure_cannot_mask_completion_or_allow_early_takeover(chaos_db, clock, caplog, monkeypatch):
    _, factory, site_id, engine = chaos_db
    # Prior Alembic fixtures may configure logging with disable_existing_loggers.
    # Restore only this logger for the assertion; test ordering is not a lease
    # failure, and the production lease/release operations remain unmocked.
    monkeypatch.setattr(locking.log, "disabled", False)
    caplog.set_level("WARNING", logger=locking.log.name)

    def fail_release(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE job_leases"):
            raise OperationalError("injected release outage", {}, RuntimeError("private release details"))

    with factory() as session:
        try:
            with fenced_site_work(session, site_id, owner="completed-owner", ttl_seconds=30, session_factory=factory):
                session.get(m.Site, site_id).name = "Completed before release failure"
                session.commit()
                event.listen(engine, "before_cursor_execute", fail_release)
        finally:
            event.remove(engine, "before_cursor_execute", fail_release)
    assert "site_lease_release_failed" in caplog.text
    assert "private release details" not in caplog.text
    with factory() as session:
        assert session.get(m.Site, site_id).name == "Completed before release failure"
        assert leases.acquire_lease(session, site_lease_key(site_id), "too-early", site_id=site_id, now=clock.now()) is None
        session.commit()
    clock.advance(31)
    with factory() as session:
        recovered = leases.acquire_lease(session, site_lease_key(site_id), "after-expiry", site_id=site_id, now=clock.now())
        assert recovered and recovered.fencing_token == 2
        session.commit()
    assert_authority_unchanged(factory, site_id)


def test_replayed_and_new_paid_reservations_remain_blocked_at_zero_budget(chaos_db):
    settings, factory, site_id, _ = chaos_db
    with factory() as session:
        site = session.get(m.Site, site_id)
        site.config_json = {**site.config_json, "model_price_bound": {
            "model": "never-called-chaos-model", "verified": True, "usd_per_million_tokens": 1,
            "source": "explicit-local-fixture-only"}}
        session.commit()
    identifiers = [str(uuid4()) for _ in range(32)]
    for identifier in identifiers:
        packet = {"id": identifier, "cycle_id": "local-chaos", "role": "technical",
                  "mode": "live", "model": "never-called-chaos-model", "status": "started",
                  "started_at": m.utcnow().isoformat(), "reserved_tokens": 1000,
                  "llm_attempted": False, "llm_executed": False, "contract": {}}
        for _ in range(3):
            with factory() as session:
                record = agent_audit.run_recorder(session, site_id, settings)
                with pytest.raises(ProviderUnavailable):
                    record(packet)
    with factory() as session:
        rows = list(session.scalars(select(m.AgentRun)))
        assert len(rows) == 32 and {row.status for row in rows} == {"budget_blocked"}
        assert count(session, m.Action, m.Action.kind == "agent_run") == 32
        assert {row.result_json["reserved_model_calls"] for row in rows} == {0}
        assert {row.result_json["reserved_cost_upper_bound_usd"] for row in rows} == {0}
    assert_authority_unchanged(factory, site_id)


@pytest.mark.parametrize("status", ["retry_exhausted", "reconciliation_required"])
def test_worker_exit_status_does_not_report_reconciliation_as_success(chaos_db, monkeypatch, status):
    settings, _, site_id, _ = chaos_db
    monkeypatch.setattr(worker, "load_worker_settings", lambda: settings)
    monkeypatch.setattr(worker, "verify_database_readiness", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(worker, "run_scheduled_job", lambda *args, **kwargs: [{"site_id": site_id, "status": status}])
    assert worker.main(["--once", jobs.WEEKLY_REVIEW, "--site-id", site_id]) == 1


def test_daily_idempotency_is_scoped_to_its_actual_job_name(chaos_db, monkeypatch):
    settings, factory, site_id, _ = chaos_db
    key = "same-user-supplied-key"
    with factory() as session:
        session.add(m.JobRun(site_id=site_id, job_name=jobs.WEEKLY_REVIEW, idempotency_key=key,
                            owner="observation", status="completed", result_json={"unrelated_job": True}))
        session.commit()
        # The real offline control loop must not replay an unrelated job just
        # because its externally chosen idempotency key happens to match.
        result = control.run_cycle(session, site_id, settings, idempotency_key=key)
    assert result["status"] == "completed" and not result.get("idempotent_replay", False)
    assert not result["result"].get("unrelated_job")
    with factory() as session:
        assert count(session, m.JobRun) == 2
    assert_authority_unchanged(factory, site_id)


def test_retry_recheck_refreshes_a_cached_failed_job_completed_by_another_worker(cross_backend_db, clock, monkeypatch):
    settings, factory, site_id, _ = cross_backend_db
    operation = jobs.OPERATIONS[jobs.WEEKLY_REVIEW]

    def fail_once(*args):
        raise TimeoutError("local fixture failure")

    monkeypatch.setitem(jobs.OPERATIONS, jobs.WEEKLY_REVIEW, fail_once)
    failed = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings,
                                      scheduled_for=clock.now(), owner="initial-failure")
    assert failed["status"] == "failed"
    monkeypatch.setitem(jobs.OPERATIONS, jobs.WEEKLY_REVIEW, operation)
    real_fenced_work = jobs.fenced_site_work
    winners = []

    @contextmanager
    def interleaved_acquisition(session, identifier, **options):
        # This pauses only the outer worker *between* its optimistic read and
        # actual SQL lease acquisition. Both workers still use the real fence.
        if options["owner"] == "stale-reader":
            winners.append(jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings,
                scheduled_for=clock.now(), owner="winning-retry"))
        with real_fenced_work(session, identifier, **options) as handle:
            yield handle

    monkeypatch.setattr(jobs, "fenced_site_work", interleaved_acquisition)
    late = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings,
                                    scheduled_for=clock.now(), owner="stale-reader")
    assert winners[0]["status"] == "completed"
    assert late.get("idempotent_replay") is True
    assert late["job_id"] == winners[0]["job_id"] == failed["job_id"]
    with factory() as session:
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == 2
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 1
    assert_authority_unchanged(factory, site_id)


def test_late_provider_result_is_discarded_after_lease_loss(chaos_db, clock, monkeypatch):
    settings, factory, site_id, _ = chaos_db
    original = jobs.OPERATIONS[jobs.WEEKLY_REVIEW]
    replacements = []

    def late_result(session, site, config):
        clock.advance(31)
        with factory() as contender:
            replacements.append(leases.acquire_lease(contender, site_lease_key(site_id),
                "new-owner-during-provider-call", site_id=site_id, ttl_seconds=30, now=clock.now()))
            contender.commit()
        return original(session, site, config)

    monkeypatch.setitem(jobs.OPERATIONS, jobs.WEEKLY_REVIEW, late_result)
    lost = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert lost["status"] == "lease_lost"
    with factory() as session:
        assert count(session, m.DecisionLog) == 0
        assert count(session, m.ActionEvent, m.ActionEvent.event_type == "scheduler_completed") == 0
        assert session.get(m.JobRun, lost["job_id"]).status == "running"
        assert leases.owns_lease(session, replacements[0], now=clock.now())
    monkeypatch.setitem(jobs.OPERATIONS, jobs.WEEKLY_REVIEW, original)
    clock.advance(31)
    recovered = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert recovered["status"] == "completed" and recovered["job_id"] == lost["job_id"]
    with factory() as session:
        assert count(session, m.DecisionLog) == 1
        assert count(session, m.FailureCase, m.FailureCase.category == "scheduled_observation_interrupted") == 1
    assert_authority_unchanged(factory, site_id)


def test_perfect_fixture_calibration_cannot_promote_autonomy(chaos_db, clock):
    settings, factory, site_id, _ = chaos_db
    with factory() as session:
        for _ in range(30):
            session.add(m.CalibrationRecord(site_id=site_id, agent_name="explicit-test-agent",
                action_category="update_title", predicted_confidence=1, succeeded=True, evaluable=True,
                outcome_json={"independent": True, "is_primary_outcome": True,
                              "adjudication_source": "synthetic-chaos-control-not-real-outcomes"}))
        session.commit()
    result = jobs.run_observation_job(factory, site_id, jobs.WEEKLY_REVIEW, settings, scheduled_for=clock.now())
    assert result["status"] == "completed"
    assert result["result"]["calibration"]["adjudicated_unique_primary_outcomes"] == 30
    assert result["result"]["calibration"]["groups"][0]["sample_sufficient_for_policy"]
    assert not result["result"]["automatic_graduation"]
    assert not result["result"]["calibration"]["automatic_graduation"]
    assert_authority_unchanged(factory, site_id)


def test_dst_fall_back_does_not_create_an_extra_daily_observation(chaos_db, clock):
    settings, factory, site_id, _ = chaos_db
    settings.scheduler_timezone = "America/New_York"
    # Both UTC instants map to the repeated 01:30 on the same local day.
    first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)
    clock.instant = first
    a = jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL, settings, scheduled_for=first)
    clock.instant = second
    b = jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL, settings, scheduled_for=second)
    assert a["status"] == b["status"] == "completed"
    assert a["job_id"] == b["job_id"] and b["idempotent_replay"]
    with factory() as session:
        assert count(session, m.JobRun) == 1
        assert count(session, m.Action, m.Action.kind == "scheduled_observation") == 1
    assert_authority_unchanged(factory, site_id)


def test_cached_running_cycle_cannot_overwrite_another_workers_committed_completion(cross_backend_db, clock, monkeypatch):
    settings, factory, site_id, _ = cross_backend_db
    key = "cached-running-cycle-recovery"
    terminal_result = {"execution": {"status": "shadow"}, "independent_completion": "must-survive"}

    def forbidden(*args):
        raise AssertionError("Completed work must be replayed, not re-executed")

    monkeypatch.setattr(control, "ingest_site", forbidden)
    with factory() as stale_session:
        cached = m.JobRun(site_id=site_id, job_name="seo-control-loop", idempotency_key=key,
                         owner="original-owner", status="running", result_json={"stage": "pending"})
        stale_session.add(cached)
        stale_session.commit()
        assert cached.status == "running"  # Strong reference keeps its ORM identity cached.
        with factory() as completing_session:
            finished = completing_session.get(m.JobRun, cached.id)
            finished.status, finished.completed_at = "completed", clock.now()
            finished.result_json = terminal_result
            completing_session.commit()
        # Real lease acquisition and a new SQL SELECT must refresh that identity.
        # Otherwise recovery could rewrite the other worker's committed outcome.
        result = control.run_cycle(stale_session, site_id, settings, idempotency_key=key)
    assert result["status"] == "completed" and result["idempotent_replay"]
    assert result["result"] == terminal_result
    with factory() as session:
        stored = session.get(m.JobRun, result["job_id"])
        assert stored.status == "completed" and stored.result_json == terminal_result
        assert count(session, m.FailureCase, m.FailureCase.category == "control_loop_interrupted") == 0
        assert count(session, m.Action, m.Action.kind == "control_loop_recovery") == 0
    assert_authority_unchanged(factory, site_id)
