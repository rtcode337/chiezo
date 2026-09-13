"""画面から絵と音を頼む口。

**会話できない相手には、画面から頼む道が無かった。** ComfyUI と ElevenLabs は
話せる相手ではないので、会話の道具に混ぜても届かない。
"""
import pytest
from fastapi.testclient import TestClient

from app import media, media_providers
from app.views import media_ask


@pytest.fixture()
def client(built_data_dir, monkeypatch):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestAskingFromTheScreen:
    def test_only_usable_backends_are_offered(self, monkeypatch):
        """使えない相手を並べて、押してから断られるのは手間が増えるだけ。"""
        async def fake(kind):
            return [
                {"id": "comfyui", "label": "自前の GPU", "usable": True,
                 "sizes": ["1024x1024"]},
                {"id": "openai", "label": "OpenAI", "usable": False, "sizes": ["1024x1024"]},
            ]

        monkeypatch.setattr(media, "backends", fake)
        monkeypatch.setattr(media, "is_enabled", lambda: True)
        import anyio

        html = anyio.run(media_ask.section_html)
        assert "自前の GPU" in html
        assert "OpenAI" not in html

    def test_asking_starts_a_job_and_goes_back_to_the_list(self, client, monkeypatch):
        seen = {}

        def fake(prompt, **kw):
            seen.update({"prompt": prompt, **kw})
            return {"id": "x"}

        monkeypatch.setattr(media, "start_image_job", fake)
        res = client.post(
            "/admin/media/ask",
            data={"kind": media_providers.KIND_IMAGE, "prompt": "城",
                  "backend": "comfyui", "size": "1024x1024", "group": "城の案"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == "/admin/media"
        assert seen["prompt"] == "城" and seen["backend"] == "comfyui"
        # **押した人が分かるようにしておく**(依頼元の欄に出る)
        assert seen["requested_by"] == "管理画面"

    def test_the_reason_it_was_refused_is_shown(self, client, monkeypatch):
        """サイズや相手の選び違いは書き直せば通るので、何が悪かったのかが読めること。"""
        from fastapi import HTTPException

        def fake(prompt, **kw):
            raise HTTPException(400, {"error": "そのサイズは頼めません", "hint": "一覧から選ぶ"})

        monkeypatch.setattr(media, "start_image_job", fake)
        res = client.post(
            "/admin/media/ask",
            data={"kind": media_providers.KIND_IMAGE, "prompt": "城", "backend": "comfyui"},
        )
        assert res.status_code == 400
        assert "そのサイズは頼めません" in res.text and "一覧から選ぶ" in res.text

    def test_the_form_path_is_not_eaten_by_the_group_page(self, client):
        """`/admin/media/{key:path}` は総取りなので、頼む口を先に登録してある。"""
        res = client.post("/admin/media/ask", data={"kind": "image", "prompt": ""})
        assert res.status_code != 405, "組のページに吸われている"


class TestTheFormIsActuallyOnThePage:
    """**画面に出ていなければ、作った意味が無い。** 差し込みを忘れても
    テストが通ってしまったので、出ていることを直接見る。"""

    def test_the_compare_page_carries_the_form(self, client, monkeypatch, tmp_path):
        async def fake(kind):
            return [{"id": "comfyui", "label": "自前の GPU", "usable": True,
                     "sizes": ["1024x1024"]}]

        store = tmp_path / "media"
        store.mkdir()
        monkeypatch.setattr(media, "backends", fake)
        monkeypatch.setattr(media, "media_dir", lambda: store)
        html = client.get("/admin/media").text
        assert "作ってもらう" in html
        assert 'action="/admin/media/ask"' in html
