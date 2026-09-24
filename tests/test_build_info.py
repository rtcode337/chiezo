"""いま動いているビルドを、イメージごとに並べる(`/admin/server`)。"""
import httpx
import pytest


@pytest.fixture()
def env(monkeypatch, tmp_path, built_data_dir):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return monkeypatch


class TestTheBuildTable:
    """**イメージは別々に焼かれる。** 片方だけ古いままが普通に起きるので、
    立っているものを並べて見比べられるようにする。
    """

    def test_it_always_shows_this_one(self, env):
        import asyncio

        from app.views import admin

        env.setattr(admin, "TRIGGER_URL", "")
        html = asyncio.run(admin._builds_html())

        assert "chiezo-app" in html

    def test_a_trigger_that_answers_is_listed(self, env):
        import asyncio

        from app import build_info
        from app.views import admin

        def reply(_request):
            return httpx.Response(200, json={
                "state": "idle",
                "build": {"sha": "abc1234def", "built_at": "2026-09-24T01:02:03Z"},
            })

        real = httpx.AsyncClient
        env.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
        env.setattr(httpx, "AsyncClient",
                    lambda **kw: real(transport=httpx.MockTransport(reply)))
        html = asyncio.run(admin._builds_html())

        assert "chiezo-ingest" in html
        assert "abc1234" in html
        assert build_info.describe_of("abc1234def", "2026-09-24T01:02:03Z")[:4] in html

    def test_a_trigger_that_is_not_up_is_left_out(self, env):
        """「起動していたら出す」—— 立っていないものの行は出さない。"""
        import asyncio

        from app.views import admin

        def refuse(request):
            raise httpx.ConnectError("no", request=request)

        real = httpx.AsyncClient
        env.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
        env.setattr(httpx, "AsyncClient",
                    lambda **kw: real(transport=httpx.MockTransport(refuse)))
        html = asyncio.run(admin._builds_html())

        assert "chiezo-ingest" not in html
        assert "chiezo-app" in html


class TestTheFormat:
    def test_someone_elses_build_reads_the_same(self):
        """並べて見比べる面なので、書式が揃っていないと用をなさない。"""
        from app import build_info

        one = build_info.describe_of("1234567abc", "2026-09-24T00:00:00Z")

        assert "1234567" in one
        assert "JST" in one

    def test_an_unreadable_time_is_not_guessed(self):
        from app import build_info

        assert build_info.describe_of("1234567", "きのう") == "日時不明 (1234567)"
        assert build_info.describe_of("", "") == build_info.UNKNOWN
