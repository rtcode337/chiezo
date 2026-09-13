"""AI へ渡したものと返ってきたものの控え。

**`ai_log` の「中身は残さない」とは目的が違う。** あちらは失敗の観測で、
こちらは「シェルを渡している相手が何をしたか」を後から読むためのもの。
"""
import pytest

from app import ai_transcript


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings_store.state_dir", lambda: tmp_path)
    monkeypatch.setattr(ai_transcript, "KEEP_DAYS", 14)
    return ai_transcript


class TestKeepingWhatTheAiWasToldAndSaid:
    def test_a_call_is_kept_with_all_three_parts(self, store):
        ident = store.record(
            backend="codex", model="gpt-image-2", kind="image", caller="arrow-puzzle",
            prompt="絵を描いて", trace="$ curl …", reply="out-1.png",
        )
        assert ident
        row = store.get(ident)
        assert (row["prompt"], row["trace"], row["reply"]) == ("絵を描いて", "$ curl …", "out-1.png")

    def test_the_list_carries_only_the_head(self, store):
        store.record(backend="codex", prompt="あ" * 5000, reply="い" * 5000)
        row = store.recent()[0]
        assert len(row["prompt"]) == store.HEAD_MAX
        # 元の大きさは切り詰めた先頭とは別に残す
        assert row["prompt_bytes"] == len(("あ" * 5000).encode())

    def test_the_whole_thing_is_readable_from_the_file(self, store):
        ident = store.record(backend="codex", prompt="あ" * 5000, trace="X", reply="Y")
        text = store.full_text(ident)
        assert text.count("あ") == 5000
        assert "## 相手の出力（途中経過）" in text and "X" in text and "Y" in text

    def test_turning_it_off_records_nothing(self, store, monkeypatch):
        """**丸ごと止められること。** 依頼文には呼んだ側の材料がそのまま入る。"""
        monkeypatch.setattr(ai_transcript, "KEEP_DAYS", 0)
        assert store.record(backend="codex", prompt="秘密") == ""
        assert store.recent() == []

    def test_recording_never_breaks_the_call(self, store, monkeypatch):
        monkeypatch.setattr("app.settings_store.state_dir", lambda: None)
        assert store.record(backend="codex", prompt="x") == ""

    def test_old_rows_and_files_go_together(self, store):
        """片方だけ残ると、一覧に出るのに開けない行ができる。"""
        ident = store.record(backend="codex", prompt="古い")
        store.prune(keep_days=0)
        assert store.get(ident) is None
        assert store.full_text(ident) is None


class TestReadingItBack:
    """画面から読めること。**一覧は先頭だけ、全文は別の口**。"""

    def test_the_section_shows_the_head_and_a_way_to_the_whole_thing(self, store):
        ident = store.record(
            backend="codex", kind="image", caller="arrow-puzzle",
            prompt="絵を描いて", trace="$ curl http://chiezo-app:7010/v1/media/image",
            reply="出来た",
        )
        from app.views import ai_history

        html = ai_history.transcripts_html()
        assert "絵を描いて" in html
        # **途中経過がいちばん効く。** シェルを渡している相手の手順はここにしか出ない
        assert "curl" in html
        assert f"/admin/ai/transcripts/{ident}" in html

    def test_turning_it_off_says_so_instead_of_showing_nothing(self, store, monkeypatch):
        monkeypatch.setattr(ai_transcript, "KEEP_DAYS", 0)
        from app.views import ai_history

        assert "CHIEZO_AI_TRANSCRIPT_DAYS" in ai_history.transcripts_html()
