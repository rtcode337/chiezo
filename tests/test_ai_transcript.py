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
    """画面から読めること。**依頼の表と同じ行に出す**(中身は畳んで、全文は別の口)。

    別の表に分けていた頃は、同じ 1 回が 2 か所に並び、突き合わせるには時刻と相手を
    目で照らすしかなかった —— 同じ秒に 2 本走っていると、どちらの中身なのか決められない。
    いまは呼び出しの行が控えの id を持つ(`transcript_id`)。
    """

    def _shown(self, store, monkeypatch, **record) -> str:
        """1 件控えて、その紐を持つ依頼の行を描く。"""
        from app import usage_store
        from app.views import ai_history

        ident = store.record(backend="codex", **record)
        monkeypatch.setattr(
            ai_history, "entries",
            lambda failed_only=False: [{
                "ok": True, "at": "2026-09-15T00:00:00+00:00", "backend": "codex",
                "model": "", "effort": "", "kind": record.get("kind", "chat"),
                "caller": "arrow-puzzle", "prompt_bytes": 10, "reply_bytes": 20,
                "ms": 1000, "input_tokens": None, "output_tokens": None,
                "transcript_id": ident,
            }],
        )
        monkeypatch.setattr(ai_history, "running_rows", list)
        monkeypatch.setattr(usage_store, "KEEP_DAYS", 30)
        return ai_history.section_html()

    def test_the_request_row_carries_the_head_and_a_way_to_the_whole_thing(
        self, store, monkeypatch
    ):
        html = self._shown(
            store, monkeypatch, kind="image", caller="arrow-puzzle",
            prompt="絵を描いて", trace="$ curl http://chiezo-app:7010/v1/media/image",
            reply="出来た",
        )

        assert "絵を描いて" in html
        # **途中経過がいちばん効く。** シェルを渡している相手の手順はここにしか出ない
        assert "curl" in html
        assert "/admin/ai/transcripts/" in html

    def test_a_cut_head_says_that_it_is_cut(self, store, monkeypatch):
        """**切れているならそう書く。** 印が無いと、目の前のものを全部だと思って
        判断する —— 途中で終わっている応答を「途中で止まった」と読むことになる。"""
        html = self._shown(store, monkeypatch, prompt="あ" * 5000, reply="い" * 5000)

        assert f"先頭 {store.HEAD_MAX:,} 字" in html
        assert "全文を開く" in html

    def test_a_short_one_says_nothing(self, store, monkeypatch):
        """切れていない控えに断り書きを出さない(毎行に付くと意味が薄れる)。"""
        html = self._shown(store, monkeypatch, prompt="短い依頼", reply="短い応答")

        assert "ここまでで先頭" not in html

    def test_a_row_without_a_transcript_still_shows_its_weight(self, store, monkeypatch):
        """控えを止めている・期限で消えた行は、目方だけの行になる(消えない)。"""
        from app.views import ai_history

        monkeypatch.setattr(
            ai_history, "entries",
            lambda failed_only=False: [{
                "ok": True, "at": "2026-09-15T00:00:00+00:00", "backend": "codex",
                "model": "", "effort": "", "kind": "chat", "caller": "", "prompt_bytes": 10,
                "reply_bytes": 20, "ms": 1000, "input_tokens": None, "output_tokens": None,
                "transcript_id": "",
            }],
        )
        monkeypatch.setattr(ai_history, "running_rows", list)

        html = ai_history.section_html()

        assert "codex" in html
        assert "全文を開く" not in html
