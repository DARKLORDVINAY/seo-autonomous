"""Run the canonical Level 1 loop on owner-attested static lab bytes or a public lab."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config.settings import Settings
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.test_lab import register_lab, run_benchmark
from scripts.bootstrap import migrate

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("artifact", "public"), required=True)
    parser.add_argument("--base-url", required=True, help="Public HTTPS origin, or https://example.test for artifact mode")
    parser.add_argument("--build-dir", type=Path, help="Generated owner-controlled release directory")
    parser.add_argument("--manifest-sha256", help="Exact SHA-256 of the owner-controlled release inventory.json")
    parser.add_argument("--database-url", help="Dedicated lab state; use LAB_DATABASE_URL for a credential-bearing PostgreSQL URL")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/test-lab-report.json")
    parser.add_argument("--ground-truth", type=Path, default=ROOT / "test_lab/ground_truth.json")
    parser.add_argument("--idempotency-key", help="Reuse to replay one completed run; omit for a fresh observed benchmark")
    parser.add_argument("--gsc-property", help="Explicitly bind this Search Console property to the lab")
    parser.add_argument("--ga4-property-id", help="Explicitly bind this GA4 property to test-only analytics")
    args = parser.parse_args(argv)
    if args.mode == "artifact" and args.build_dir is None:
        parser.error("--mode artifact requires --build-dir")
    if not args.manifest_sha256 and args.build_dir is None:
        parser.error("An owner-attested --manifest-sha256 or local --build-dir is required")
    database_url = args.database_url or os.environ.get("LAB_DATABASE_URL")
    if database_url is None:
        state_path = ROOT / "artifacts" / f"test-lab-{args.mode}.sqlite3"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{state_path}"
    try:
        manifest_sha256 = args.manifest_sha256 or hashlib.sha256((args.build_dir / "inventory.json").read_bytes()).hexdigest()
        settings = Settings(_env_file=None, environment="test", database_url=database_url,
            agent_mode="deterministic", provider_mode="fixture" if args.mode == "artifact" else "live",
            autonomy_level=1, production_enabled=False, shadow_mode=True, max_daily_actions=0,
            openai_api_key=None, openai_model=None, gsc_property=args.gsc_property, ga4_property_id=args.ga4_property_id,
            max_pages_per_crawl=50, max_crawl_pages=50)
        migrate(database_url)
        engine = make_engine(database_url)
        try:
            with make_session_factory(engine)() as session:
                site = register_lab(session, mode=args.mode, base_url=args.base_url,
                    expected_manifest_sha256=manifest_sha256, build_dir=args.build_dir,
                    gsc_property=args.gsc_property, ga4_property_id=args.ga4_property_id)
                result = run_benchmark(session, site.id, settings, ground_truth_path=args.ground_truth,
                                       idempotency_key=args.idempotency_key)
        finally:
            engine.dispose()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        result["database_url"] = make_url(database_url).render_as_string(hide_password=True)
        result["build_dir"] = str(args.build_dir.resolve()) if args.build_dir else None
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        summary = {key: result[key] for key in ("site_id", "mode", "is_fixture", "autonomy_level", "production_enabled", "database_url", "build_dir")}
        summary.update(report=str(args.report.resolve()), job_id=result["job_id"],
            precision=result["assessment"]["precision"], recall=result["assessment"]["recall"],
            false_positives=result["assessment"]["false_positives"], false_negatives=result["assessment"]["false_negatives"],
            structural_benchmark_passed=result["assessment"]["structural_benchmark_passed"], level_2_eligible=False)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if result["assessment"]["structural_benchmark_passed"] else 2
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
            "detail": "Lab benchmark stopped; no provider credentials or connection strings were displayed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
