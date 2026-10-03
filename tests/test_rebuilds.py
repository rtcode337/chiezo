"""長期記憶の定期再構築(`app/rebuilds.py` と `main._tick_rebuilds`)。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import fastapi
import pytest

from app import rebuilds


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(UTC)


class TestWhenItRuns:
    def test_the_first_run_is_the_next_slot_after_setting(self):
        """決めてから 1 周期待たせない(効いているのか分からない)。"""
        before = rebuilds.Schedule("jawiki", 7, 3, set_at="2026-10-03T01:00:00+09:00")
        after = rebuilds.Schedule("jawiki", 7, 3, set_at="2026-10-03T05:00:00+09:00")

        assert before.next_run_at() == _utc("2026-10-03T03:00:00+09:00")
        assert after.next_run_at() == _utc("2026-10-04T03:00:00+09:00")

    def test_later_runs_count_days_not_hours(self):
        """前の回が空き待ちで遅れても、次は決めた時刻に戻る。"""
        s = rebuilds.Schedule("jawiki", 7, 3, last_started_at="2026-10-03T06:40:00+09:00")

        assert s.next_run_at() == _utc("2026-10-10T03:00:00+09:00")

    def test_setting_again_keeps_when_it_last_ran(self, state):
        """間隔を変えただけで、すぐ走り出したり止まったりしない。"""
        rebuilds.set_schedule("jawiki", 7, 3)
        rebuilds.mark_started("jawiki", "2026-10-03T03:00:00+00:00")
        s = rebuilds.set_schedule("jawiki", 14, 4)

        assert s.last_started_at == "2026-10-03T03:00:00+00:00"
        assert (s.interval_days, s.hour) == (14, 4)

    def test_only_the_offered_intervals(self, state):
        with pytest.raises(ValueError):
            rebuilds.set_schedule("jawiki", 2, 3)
        with pytest.raises(ValueError):
            rebuilds.set_schedule("jawiki", 7, 24)

    def test_due_lists_the_late_ones_first(self, state):
        rebuilds.set_schedule("geonames", 1, 3)
        rebuilds.mark_started("geonames", "2026-10-01T00:00:00+00:00")
        rebuilds.set_schedule("jawiki", 1, 3)
        rebuilds.mark_started("jawiki", "2026-09-20T00:00:00+00:00")

        got = rebuilds.due(_utc("2026-10-03T12:00:00+09:00"))

        assert [s.source for s in got] == ["jawiki", "geonames"]


class TestTheClock:
    @pytest.fixture
    def clock(self, state, monkeypatch):
        from app import main
        from app.views import admin

        status = {"state": "idle", "slots": 1, "dump_lane": True, "jobs": []}
        started: list[str] = []

        def run(name: str) -> None:
            if any(j.get("lane") == "dump" for j in status["jobs"]):
                raise fastapi.HTTPException(429, {"error": "another dump is being ingested"})
            started.append(name)

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: status)
        monkeypatch.setattr(admin, "trigger_run", run)
        monkeypatch.setattr(admin, "is_dump_source", lambda name: name != "news")
        app = SimpleNamespace(state=SimpleNamespace(sources={"jawiki": 1, "geonames": 1, "news": 1}))
        return main, app, status, started

    def _overdue(self, *names):
        for name in names:
            rebuilds.set_schedule(name, 1, 3)
            rebuilds.mark_started(name, "2026-01-01T00:00:00+00:00")

    def test_one_rebuild_per_tick(self, clock):
        main, app, _status, started = clock
        self._overdue("jawiki", "geonames")

        main._tick_rebuilds(app)

        assert len(started) == 1
        # 起こした回は控え、次の予定は先へ進む
        assert rebuilds.get(started[0]).last_started_at.startswith(str(datetime.now(UTC).year))

    def test_it_waits_while_a_dump_is_running(self, clock):
        """ダンプは 1 本ずつ。埋まっていれば何もせず、予定は残す(飛ばさない)。"""
        main, app, status, started = clock
        status["jobs"] = [{"source": "osm_japan", "state": "running", "lane": "dump"}]
        self._overdue("jawiki")

        main._tick_rebuilds(app)

        assert started == []
        assert rebuilds.get("jawiki").last_started_at == "2026-01-01T00:00:00+00:00"

    def test_a_schedule_for_a_gone_source_is_dropped(self, clock):
        main, app, _status, started = clock
        self._overdue("osm_france")

        main._tick_rebuilds(app)

        assert started == []
        assert rebuilds.get("osm_france") is None


class TestTheScreen:
    @pytest.fixture
    def client(self, state, monkeypatch, built_data_dir):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app
        from app.views import admin

        monkeypatch.setattr(admin, "is_dump_source", lambda name: True)
        with TestClient(app) as c:
            yield c

    def test_it_can_be_set_and_cleared(self, client):
        name = next(iter(client.app.state.sources))

        res = client.post("/admin/rebuild-schedule", follow_redirects=False,
                          data={"source": name, "interval_days": "7", "hour": "4"})
        assert res.status_code == 303
        s = rebuilds.get(name)
        assert (s.interval_days, s.hour) == (7, 4)
        assert "7 日おき" in client.get("/admin/memory").text

        client.post("/admin/rebuild-schedule", data={"source": name, "interval_days": ""})
        assert rebuilds.get(name) is None

    def test_an_unknown_source_is_refused(self, client):
        res = client.post("/admin/rebuild-schedule",
                          data={"source": "no_such", "interval_days": "7", "hour": "3"})
        assert res.status_code == 404
