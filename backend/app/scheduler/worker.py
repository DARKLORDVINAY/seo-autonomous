"""One authoritative APScheduler 3 worker, restartable from canonical job records."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import signal
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.app.config.settings import Settings
from backend.app.db.readiness import verify_database_readiness
from backend.app.db.models import utcnow
from backend.app.db.session import make_engine, make_session_factory
from backend.app.scheduler.jobs import (
    DAILY_CYCLE, EVENING_MEASUREMENT, INTEGRITY_CRAWL, JOB_NAMES, WEEKLY_REVIEW, run_scheduled_job,
)

# This is process scheduling, never a model/tool/chat reminder.
CADENCE = (
    (DAILY_CYCLE, 5, 0, None),
    (INTEGRITY_CRAWL, 12, 0, None),
    (EVENING_MEASUREMENT, 19, 0, None),
    (WEEKLY_REVIEW, 6, 0, "mon"),
)
HEARTBEAT_PATH = Path(os.getenv("SCHEDULER_HEARTBEAT_PATH", "/tmp/seo-worker-heartbeat.json"))


def load_worker_settings() -> Settings:
    """Force the executable capability even when SERVICE_ROLE is forged."""
    bearer_names = {"API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN"}
    if any(key.upper() in bearer_names and value for key, value in os.environ.items()):
        raise ValueError("Worker executable must not receive human or API bearer capabilities")
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        # Compose supplies discrete POSTGRES_* fields so credentials need not be
        # interpolated into a URI. Health checks launch this module directly,
        # outside the long-running process whose entrypoint built DATABASE_URL.
        from docker.entrypoint import database_url_from_environment

        database_url = database_url_from_environment(environment=os.environ.get("ENVIRONMENT"))
    return Settings(
        service_role="worker",
        database_url=database_url,
        api_token=None,
        approval_token=None,
        admin_token=None,
    )


def describe_schedule(settings: Settings) -> list[dict]:
    ZoneInfo(settings.scheduler_timezone)
    return [{"job": name, "local_time": f"{hour:02}:{minute:02}", "days": day or "daily",
             "timezone": settings.scheduler_timezone, "may_call_models": name == DAILY_CYCLE}
            for name, hour, minute, day in CADENCE]


def due_slots(settings: Settings, now: datetime) -> list[tuple[str, datetime]]:
    """Catch up this day's observations/current week's review once, never a backlog."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Scheduler clock must be timezone-aware")
    zone = ZoneInfo(settings.scheduler_timezone)
    local_now = now.astimezone(zone)
    due = []
    for name, hour, minute, day in CADENCE:
        slot_date = local_now.date()
        if day == "mon":
            slot_date -= timedelta(days=slot_date.weekday())
        slot = datetime.combine(slot_date, time(hour, minute), tzinfo=zone)
        if slot <= local_now:
            due.append((name, slot))
    return sorted(due, key=lambda item: item[1])


def reconcile_due(factory, settings: Settings, *, now: datetime | None = None) -> None:
    for job_name, slot in due_slots(settings, now or utcnow()):
        run_scheduled_job(factory, settings, job_name, scheduled_for=slot)


def write_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"updated_at": utcnow().isoformat(), "pid": os.getpid()}), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def heartbeat_healthy(path: Path = HEARTBEAT_PATH, *, max_age_seconds: int = 90) -> bool:
    try:
        age = utcnow().timestamp() - path.stat().st_mtime
        return 0 <= age <= max_age_seconds
    except OSError:
        return False


def database_healthy(settings: Settings) -> bool:
    """Check that the scheduler can use this release's canonical database.

    This is deliberately read-only. It prevents a live heartbeat from masking a
    database outage or schema drift. Production additionally rechecks the
    restricted runtime role so a privilege regression marks the worker unhealthy.
    """
    engine = None
    try:
        engine = make_engine(settings.database_url, environment=settings.environment)
        with engine.connect() as connection:
            verify_database_readiness(connection, environment=settings.environment, profile="worker")
        return True
    except Exception:
        return False
    finally:
        if engine is not None:
            engine.dispose()


def worker_healthy(settings: Settings, path: Path = HEARTBEAT_PATH) -> bool:
    return heartbeat_healthy(path) and database_healthy(settings)


def build_scheduler(factory, settings: Settings, *, startup_catchup: bool = True) -> BlockingScheduler:
    timezone = ZoneInfo(settings.scheduler_timezone)
    scheduler = BlockingScheduler(timezone=timezone,
        executors={"default": ThreadPoolExecutor(max_workers=1), "heartbeat": ThreadPoolExecutor(max_workers=1)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
    for name, hour, minute, day in CADENCE:
        trigger = CronTrigger(hour=hour, minute=minute, day_of_week=day or "*", timezone=timezone)
        scheduler.add_job(run_scheduled_job, trigger, id=name, args=(factory, settings, name), replace_existing=True)
    scheduler.add_job(write_heartbeat, "interval", seconds=30, id="worker-heartbeat", executor="heartbeat",
                      next_run_time=utcnow(), misfire_grace_time=60, replace_existing=True)
    if startup_catchup:
        scheduler.add_job(reconcile_due, "date", run_date=utcnow(), id="startup-reconciliation",
                          args=(factory, settings), misfire_grace_time=3600, replace_existing=True)
    return scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--describe", action="store_true")
    mode.add_argument("--healthcheck", action="store_true")
    mode.add_argument("--once", choices=JOB_NAMES, help="Explicit one-off dispatch with normal lease/budget checks")
    parser.add_argument("--site-id", help="Limit --once to one registered site")
    args = parser.parse_args(argv)
    if args.healthcheck:
        try:
            return 0 if worker_healthy(load_worker_settings()) else 1
        except Exception:
            return 1
    if args.site_id and not args.once:
        parser.error("--site-id requires --once")
    engine = None
    try:
        settings = load_worker_settings()
        if args.describe:
            print(json.dumps(describe_schedule(settings)))
            return 0
        if not args.once and not settings.scheduler_enabled:
            print("Worker disabled; set SCHEDULER_ENABLED=true to run recurring observations.")
            return 2
        logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        from backend.app.observability.logging import configure_tracing
        configure_tracing()
        engine = make_engine(settings.database_url, environment=settings.environment)
        factory = make_session_factory(engine)
        with engine.connect() as connection:
            verify_database_readiness(connection, environment=settings.environment, profile="worker")
        if args.once:
            results = run_scheduled_job(factory, settings, args.once, site_id=args.site_id)
            print(json.dumps({"job": args.once, "sites": [{key: row[key] for key in ("site_id", "job_id", "status") if key in row}
                                                          for row in results]}))
            return 1 if any(row["status"] in {"failed", "lease_lost", "retry_exhausted", "reconciliation_required"}
                            for row in results) else 0
        scheduler = build_scheduler(factory, settings)

        def stop(signum, frame):
            if scheduler.running:
                scheduler.shutdown(wait=True)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        write_heartbeat()
        scheduler.start()
        return 0
    except Exception as error:
        print(f"Worker stopped ({type(error).__name__}); inspect configuration and canonical job records.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
