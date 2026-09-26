"""外から届く値を、行き先・パス・画面にそのまま使わない(CodeQL の指摘への手当て)。"""

from __future__ import annotations

import pytest
from test_api import client, monkeypatch_module  # noqa: F401


class TestRollbackGoesBackOnlyToItsOwnPages:
    """戻り先は、このボタンを出している面から選ぶ(外のサイトへ飛ばさない)。"""

    @pytest.fixture
    def rolled(self, client, monkeypatch):  # noqa: F811
        from app.views import admin

        class Done:
            status_code = 200
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url):
                return Done()

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.test")
        monkeypatch.setattr(admin.httpx, "Client", FakeClient)

        def post(back: str) -> str:
            res = client.post(
                "/admin/source/jawiki/rollback", data={"back": back}, follow_redirects=False,
            )
            assert res.status_code == 303
            return res.headers["location"]

        return post

    def test_its_own_pages_are_kept(self, rolled):
        assert rolled("/admin/memory#long-term") == "/admin/memory#long-term"
        assert rolled("/admin/collect/jawiki") == "/admin/collect/jawiki"

    def test_anywhere_else_falls_back(self, rolled):
        for outside in ("https://example.test/", "//example.test", "/admin/collect/other", ""):
            assert rolled(outside) == "/admin/memory#long-term"


class TestHandoffStaysInItsPlace:
    def test_a_name_that_climbs_out_is_refused(self, tmp_path):
        from app import handoff

        with pytest.raises(ValueError):
            handoff._inside(tmp_path, "../outside.md")

    def test_an_ordinary_name_lands_inside(self, tmp_path):
        from app import handoff

        assert handoff._inside(tmp_path, "news.md").parent == tmp_path.resolve()


class TestScriptBlocksAreLeftAlone:
    def test_a_closing_tag_with_a_space_still_closes(self):
        """`</script >` も閉じとして読む(取りこぼすと、台本の外の表まで飛ばす)。"""
        from app import pages

        body = (
            "<script>var t = '<table>';</script >"
            "<table><thead><tr><th>名前</th></tr></thead><tbody><tr><td>x</td></tr></tbody></table>"
        )
        out = pages.as_cards(body)

        assert '<td data-label="名前">x</td>' in out
        assert "var t = '<table>';" in out


class TestBrokenWorkerDefinitions:
    def test_the_page_shows_the_fixed_sentence(self, monkeypatch):
        """例外の文そのものは画面に出さない(決まった一文を出す)。"""
        from app import workers
        from app.views import ai_workers

        def broken():
            raise ValueError("secret detail /srv/state/whatever")

        monkeypatch.setattr(workers, "load", broken)
        html = ai_workers.section_html((lambda *a, **k: "", lambda *a, **k: ""))

        assert workers.DEFS_BROKEN in html
        assert "secret detail" not in html
