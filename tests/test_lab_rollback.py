"""An isolated Git restore drill must not change the registered website authority."""
import json

import pytest
from sqlalchemy import select

from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.control import create_site
from scripts.lab_rollback_drill import run_drill
from test_lab.build import build_site


def test_artifact_drill_restores_all_bytes_with_immutable_canonical_events(tmp_path):
    release = tmp_path / "release"
    build_site("https://example.test", release, fixture=True)
    original = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*") if p.is_file()}
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = create_site(session, name="Explicit local drill fixture", base_url="https://example.test", fixture=True)
        result = run_drill(session, site.id, release, tmp_path / "drill")
        assert result["baseline_tree"] == result["restored_tree"] != result["changed_tree"]
        assert result["public_deployment_rollback"] == "not_verified" and result["qualifies_for_autonomy"] is False
        assert not site.production_enabled and site.autonomy_level == 1
        assert len(list(session.scalars(select(m.RollbackEvent)))) == 1
        actions = list(session.scalars(select(m.Action).where(m.Action.experiment_id == result["experiment_id"])))
        assert len(actions) == 2 and all(a.payload_json["production_write"] is False for a in actions)
        assert {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*") if p.is_file()} == original
        assert json.loads((tmp_path / "drill/inventory.json").read_text()) == json.loads((release / "inventory.json").read_text())
        with pytest.raises(ValueError, match="fresh drill directory"):
            run_drill(session, site.id, release, tmp_path / "drill")
    engine.dispose()


def test_artifact_drill_cannot_operate_on_live_site(tmp_path):
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = create_site(session, name="Read-only live target", base_url="https://example.com")
        with pytest.raises(ValueError, match="fixture site"):
            run_drill(session, site.id, tmp_path / "unused", tmp_path / "unused-copy")
        assert not (tmp_path / "unused-copy").exists()
        assert list(session.scalars(select(m.RollbackEvent))) == []
    engine.dispose()
