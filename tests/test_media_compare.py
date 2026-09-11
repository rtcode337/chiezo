"""手元で作ったものの持ち込み(`POST /v1/media/upload`)と、管理画面の見比べ(`/admin/media`)。

持ち込みが要るのは、見比べに載せる手段が「chiezo に作らせる」しか無かったため ——
手元で仕上げたものや別の道具で作ったものを、生成させたものと並べられなかった。
比べたいのは出どころではなく出来のほうなので、出どころで弾かない。

見比べを管理画面にも置くのは、見比べているのが生成物だから —— 相手・鍵・使用量・
依頼の履歴と同じ「AI に頼んだこと」の面にある。
"""
import io

import pytest


@pytest.fixture()
def media_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_MEDIA_DIR", str(tmp_path / "media"))
    return monkeypatch


def _upload(name="案.md", mime="text/markdown", data=b"# \xe6\xa1\x88", **kw):
    from app import media

    return media.save_upload(stream=io.BytesIO(data), filename=name, mime=mime, **kw)


class TestBringingYourOwn:
    def test_it_lands_as_a_finished_job(self, media_env):
        """**走らせずに done で作る。** 生成の job と同じ表に入れるので、
        見比べも採用の印も掃除も、kind を問わずそのまま効く。"""
        from app import media

        job = _upload(prompt="手元の案", group="タイトルの一枚絵")
        assert job["state"] == "done"
        assert job["backend"] == media.UPLOAD_BACKEND
        assert job["group_name"] == "タイトルの一枚絵"
        assert len(job["files"]) == 1

    def test_it_shows_up_in_the_same_group_as_generated_ones(self, media_env):
        """出どころで弾かないので、作らせたものと同じ組に並ぶ。"""
        from app import media

        media.create_job("作らせたほう", backend="comfyui", group="そろえる")
        _upload(prompt="持ち込んだほう", group="そろえる")
        group = media.job_group("そろえる")
        assert group["count"] == 2

    def test_the_kind_comes_from_what_the_sender_says(self, media_env):
        assert _upload(name="x", mime="image/png")["kind"] == "image"
        assert _upload(name="x", mime="audio/mpeg")["kind"] == "audio"
        assert _upload(name="x", mime="video/mp4")["kind"] == "video"
        assert _upload(name="x", mime="text/plain")["kind"] == "text"

    def test_the_suffix_is_the_fallback(self, media_env):
        """**相手の名乗りは当てにならない**(`application/octet-stream` で送ってくる)。"""
        assert _upload(name="案.png", mime="")["kind"] == "image"
        assert _upload(name="案.md", mime="application/octet-stream")["kind"] == "text"

    def test_something_we_cannot_place_is_refused(self, media_env):
        """**黙って「絵」として置かない** —— 見比べの画面が壊れた絵を並べることになる。
        ここは生成物を見比べる場所で、汎用のファイル置き場ではない。"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _upload(name="data.bin", mime="application/octet-stream")
        assert e.value.status_code == 400

    def test_an_empty_file_is_refused(self, media_env):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _upload(data=b"")
        assert e.value.status_code == 400

    def test_too_big_is_refused_and_nothing_is_left_behind(self, media_env, monkeypatch):
        """**書きながら数える**(全部読んでから測ると、上限を超える大きさを抱える)。
        断るときは書きかけを消す —— 残すと、置き場に誰の持ち物でもないファイルが溜まる。"""
        from fastapi import HTTPException

        from app import media

        monkeypatch.setattr(media, "UPLOAD_MAX_BYTES", 16)
        with pytest.raises(HTTPException) as e:
            _upload(name="大きいの.png", mime="image/png", data=b"0" * 1024)
        assert e.value.status_code == 413
        # 日付のディレクトリは残るが、それは次の持ち込みが使い回すもの。
        # 残ってはいけないのは中身のほう
        assert [p for p in media.require_dir().rglob("*") if p.is_file()] == []

    def test_the_heading_falls_back_to_the_file_name(self, media_env):
        """見出しが空だと一覧で区別が付かない(名前の無い依頼は依頼文の 1 行目を借りる作り)。"""
        assert _upload(name="タイトル案.png", mime="image/png")["prompt"] == "タイトル案.png"

    def test_text_keeps_its_length(self, media_env):
        """文字数を `seconds` の列に置く(`_run_text` と同じ置き方。一覧で長さが読める)。"""
        job = _upload(name="長いの.md", mime="text/markdown", data=("あ" * 500).encode())
        assert job["seconds"] == 500.0


class TestTheCompareScreen:
    @staticmethod
    def _client(media_env, tmp_path):
        from test_agent import make_client

        media_env.setenv("CHIEZO_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        return make_client(media_env, None)

    def test_it_is_one_of_the_admin_pages(self, media_env, tmp_path):
        """**玄関にも帯にも出る**(面が増えたときに片方だけ出ないことが無いよう、
        `PAGES` の 1 か所で持つ)。"""
        from app.views.admin import PAGES, nav_html

        assert "/admin/media" in [path for path, _label, _note in PAGES]
        assert "/admin/media" in nav_html("/admin/ai")

    def test_a_group_lists_its_options(self, media_env, tmp_path):
        with self._client(media_env, tmp_path) as client:
            _upload(prompt="案 1", group="そろえる")
            _upload(prompt="案 2", group="そろえる")
            res = client.get("/admin/media")
            assert res.status_code == 200
            assert "そろえる" in res.text
            page = client.get("/admin/media/そろえる")
            assert page.status_code == 200
            assert page.text.count('class="media-card') == 2

    def test_picking_marks_one_and_comes_back(self, media_env, tmp_path):
        """**押した場所へ戻す**(303)。どれに付けたのかを確かめるのに探し直さない。"""
        with self._client(media_env, tmp_path) as client:
            job = _upload(prompt="案 1", group="そろえる")
            res = client.post(
                "/admin/media/そろえる/pick",
                data={"job_id": job["id"], "note": "これにする"},
                follow_redirects=False,
            )
            assert res.status_code == 303
            page = client.get("/admin/media/そろえる")
            assert "これにする" in page.text
            assert 'class="media-card picked"' in page.text

    def test_text_can_be_opened_full_screen_without_js(self, media_env, tmp_path):
        """**並べるための小ささと、読むための全画面は要求が逆。**
        管理画面は JS を持たない流儀なので、CSS の `:target` で開く。"""
        with self._client(media_env, tmp_path) as client:
            job = _upload(name="長いの.md", mime="text/markdown",
                          data=("第1章の目次\n" + "あ" * 2000).encode(),
                          prompt="長い案", group="そろえる")
            page = client.get("/admin/media/そろえる").text
            assert f'href="#full-{job["id"]}"' in page      # 押すと開く
            assert f'id="full-{job["id"]}"' in page         # 開く先がある
            assert 'class="media-full-text"' in page        # 全文が入っている
            assert "あ" * 2000 in page

    def test_an_image_can_be_opened_full_screen(self, media_env, tmp_path):
        with self._client(media_env, tmp_path) as client:
            job = _upload(name="案.png", mime="image/png",
                          data=b"\x89PNG\r\n\x1a\n" + b"0" * 40,
                          prompt="絵の案", group="そろえる")
            page = client.get("/admin/media/そろえる").text
            assert f'href="#full-{job["id"]}-0"' in page
            assert 'class="media-full-img"' in page

    def test_closing_goes_back_to_the_card_not_the_top(self, media_env, tmp_path):
        """`#` だけに戻すとページの先頭へ飛び、どれを見ていたのか分からなくなる。"""
        with self._client(media_env, tmp_path) as client:
            job = _upload(prompt="案", group="そろえる")
            page = client.get("/admin/media/そろえる").text
            assert f'id="card-{job["id"]}"' in page
            assert f'href="#card-{job["id"]}"' in page

    def test_a_group_that_is_gone_says_so(self, media_env, tmp_path):
        with self._client(media_env, tmp_path) as client:
            assert client.get("/admin/media/ないやつ").status_code == 404

    def test_a_name_with_url_characters_still_opens(self, media_env, tmp_path):
        """組の名前は人が付けるので `#` や `&` や `/` が入りうる。
        素で URL に埋めると別の組を指すか、途中で切れる。"""
        with self._client(media_env, tmp_path) as client:
            _upload(prompt="案", group="音 & 絵/両方")
            listing = client.get("/admin/media")
            assert "%20%26%20" in listing.text   # percent-encode されている
            assert client.get("/admin/media/音 & 絵/両方").status_code == 200

    def test_the_upload_endpoint_works_end_to_end(self, media_env, tmp_path):
        with self._client(media_env, tmp_path) as client:
            res = client.post(
                "/v1/media/upload",
                files={"file": ("案.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")},
                data={"prompt": "タイトル案 1", "group": "タイトルの一枚絵"},
            )
            assert res.status_code == 200
            assert res.json()["kind"] == "image"
            assert "タイトルの一枚絵" in client.get("/admin/media").text
