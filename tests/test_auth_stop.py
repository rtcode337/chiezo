"""認証の失敗(401)で相手を止める(`answer._stop_on_auth_failure`)。

401 は人が認証情報を登録し直すまで直らない。on のままだとワーカーは空いた相手として
そこへ振り続け、頼むたびに同じ失敗を積む。止めて、止めたことを状況の面に知らせる。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import answer, settings_store, workers

# 実測の言い方(codex がブリッジ越しに返した理由)
CODEX_401 = (
    'codex failed / exit 1 / {"type":"turn.failed","error":{"message":"unexpected status '
    '401 Unauthorized: Incorrect API key provided: sk-svcac****"}}'
)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _on(provider: str) -> None:
    settings_store.set_enabled(provider, True)


class TestTellingItApart:
    def test_the_bridge_wraps_a_401_in_a_502(self):
        """ブリッジは相手の 401 を 502 に包むので、理由の文を読む。"""
        assert answer.is_auth_failure(502, CODEX_401)

    def test_a_plain_401_counts(self):
        assert answer.is_auth_failure(401, "")

    def test_other_failures_do_not(self):
        """混んでいる・モデルが違う、は人が直すものではない(止めると戻らない)。"""
        assert not answer.is_auth_failure(502, "The 'x' model is not supported")
        assert not answer.is_auth_failure(503, "The model is overloaded")
        assert not answer.is_auth_failure(429, "rate limited")


class TestStopping:
    def test_an_auth_failure_switches_it_off(self, state):
        _on("codex")

        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 502, CODEX_401)

        stored = settings_store.load("codex")
        assert not stored.enabled
        assert "401 Unauthorized" in stored.disabled_reason
        # 登録し直したあと、確かめてからでないと戻せない
        assert not stored.verified
        assert [s.provider for s in settings_store.auto_disabled()] == ["codex"]

    def test_other_failures_leave_it_on(self, state):
        _on("codex")

        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 503, "overloaded")

        assert settings_store.load("codex").enabled

    def test_turning_it_back_on_clears_the_notice(self, state):
        """直して戻したので、知らせを出し続ける理由が無い。"""
        _on("codex")
        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 502, CODEX_401)

        settings_store.set_enabled("codex", True)

        assert settings_store.auto_disabled() == []
        assert settings_store.load("codex").disabled_reason == ""

    def test_one_that_a_person_switched_off_is_not_a_notice(self, state):
        """人が止めた相手は知らせない(止めた本人は知っている)。"""
        _on("codex")
        settings_store.set_enabled("codex", False)

        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 502, CODEX_401)

        assert settings_store.auto_disabled() == []


class TestWorkersSkipIt:
    def test_a_stopped_backend_is_not_picked(self, state):
        """枠が空いて見えても、止まっている相手には振らない。"""
        _on("codex")
        _on("claude")
        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 502, CODEX_401)
        worker = workers.Worker(name="w", steps=(workers.Step("codex"), workers.Step("claude")))

        assert workers.pick(worker) == workers.Step("claude")


class TestTheStatusPageSaysSo:
    def test_the_notice_is_on_the_status_page_and_the_top(
        self, state, built_data_dir, monkeypatch,
    ):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        _on("codex")
        answer._stop_on_auth_failure(SimpleNamespace(name="codex"), 502, CODEX_401)
        from app.main import app

        with TestClient(app) as client:
            status = client.get("/admin/status").text
            top = client.get("/admin").text

        assert "認証の失敗(401)が返ったので無効にしました" in status
        assert "Incorrect API key provided" in status
        assert "認証の失敗で止めた相手 1 つ" in top
