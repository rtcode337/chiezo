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


class TestThumbnailsStayInTheirPlace:
    def test_a_link_pointing_outside_is_not_served(self, tmp_path, monkeypatch):
        """名前の形は通っても、置き場の外を指す 1 件は配らない(組んだ結果で確かめる)。"""
        from fastapi import HTTPException

        from app import thumbs

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        root = tmp_path / "state" / "thumbs"
        root.mkdir(parents=True)
        outside = tmp_path / "secret.webp"
        outside.write_bytes(b"x")
        name = "0" * 40 + ".webp"
        (root / name).symlink_to(outside)

        with pytest.raises(HTTPException):
            thumbs.resolve(name)

        real = "1" * 40 + ".webp"
        (root / real).write_bytes(b"y")
        assert thumbs.resolve(real).read_bytes() == b"y"


class TestNamesDoNotEndInANewline:
    """名前の検査は全体一致で行う(`fullmatch`)。

    `re.match(r"^…$")` の `$` は**末尾の改行 1 つの手前でも一致する**ので、
    `"news\\n"` が通っていた。名前はファイル名・ソース名・URL にそのまま使われるので、
    見た目が同じで中身の違う名前を作らせない。
    """

    def test_collect(self):
        from fastapi import HTTPException

        from app import collect

        with pytest.raises(HTTPException) as e:
            collect.create("news\n", "{cursor}", 60)
        assert e.value.status_code == 400

    def test_store(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        from app import notes

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path))
        with pytest.raises(HTTPException) as e:
            notes.create_store("mystore\n", set())
        assert e.value.status_code == 400

    def test_api_key(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        from app import settings_store

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path))
        with pytest.raises(HTTPException) as e:
            settings_store.set_api_key("jquants\n", "secret")
        assert e.value.status_code == 400

    def test_worker(self):
        from app import workers

        # 呼ぶ側は前後の空白を落としてから見るが、検査そのものも末尾の改行を通さない
        assert workers.NAME_RE.fullmatch("夜\n") is None
        assert workers._worker_from({"name": "夜"}) is not None

    def test_trigger_source(self):
        from fastapi import HTTPException

        import server

        with pytest.raises(HTTPException) as e:
            server.delete_source("news\n")
        assert e.value.status_code == 400

    def test_plugin_catalog(self):
        from sources import remote

        with pytest.raises(remote.PluginError):
            remote._parse_catalog("http://plugin.test", {"sources": [{"name": "abc\n", "kind": "x"}]})
