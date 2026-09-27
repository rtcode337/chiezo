"""量が多くて AI が手を付けなかったものに、目を通した印を付けない。

整理は触ったものしか返さないので、返ってこなかった 1 件が「見たうえで直す必要が
無かった」のか「手を付けなかった」のかは、返りからは分からない。本番で 164 冊を
渡した回に相手が先頭の 18 冊だけ返し、残り 146 冊が目を通した扱いのまま二度と
渡らなかった。見られなかったものは答えに申告させ(`not_reviewed`)、答えが途中で
切れたときは返ってこなかったもの全部を、印を付けずに次の回へ回す。
"""
from __future__ import annotations

import asyncio

import pytest

from app import collect, notes


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    collect.create("shelf", prompt="まだ見ていないもの:\n{unreviewed}\n調べて", interval_minutes=60)
    return collect.get("shelf")


def _previous(*titles):
    return {
        t: {"doc_id": i, "title": t, "body": "説明", "tags": ["技術書", notes.UNREVIEWED_TAG]}
        for i, t in enumerate(titles, 1)
    }


def _run(item, previous, answer, monkeypatch):
    from app import main

    async def ask(_item, _messages):
        return answer, "codex", "m"

    monkeypatch.setattr(main, "_ask_for_collection", ask)
    shown: set[str] = set()
    _items, _cursor, note = asyncio.run(
        main._collect_items(item, previous, {}, [], collect.sweep_named(item, None), seen=shown)
    )
    return shown, note


def test_the_ones_it_says_it_did_not_see_keep_the_mark(shelf, monkeypatch):
    answer = (
        '{"items":[{"title":"本:a","body":"直した","tags":["技術書"]}],'
        '"next_cursor":null,"not_reviewed":["本:c"]}'
    )
    shown, note = _run(shelf, _previous("本:a", "本:b", "本:c"), answer, monkeypatch)

    # 見て直さなかった b は印が外れ、手を付けなかった c は残る
    assert shown == {"本:a", "本:b"}
    assert "見られなかった 1 件" in note


def test_without_a_claim_everything_shown_counts_as_seen(shelf, monkeypatch):
    """申告が無ければ今までどおり(返らなかったものは見て直す必要が無かったもの)。"""
    answer = '{"items":[{"title":"本:a","body":"直した","tags":["技術書"]}],"next_cursor":null}'
    shown, note = _run(shelf, _previous("本:a", "本:b"), answer, monkeypatch)
    assert shown == {"本:a", "本:b"}
    assert note == ""


def test_a_cut_answer_leaves_the_rest_unseen(shelf, monkeypatch):
    """途中で切れた答えは、返ってこなかったものを誰も見ていない。"""
    answer = '{"items":[{"title":"本:a","body":"直した","tags":["技術書"]},{"title":"本:b","bo'
    shown, note = _run(shelf, _previous("本:a", "本:b", "本:c"), answer, monkeypatch)
    assert shown == {"本:a"}
    assert "見られなかった 2 件" in note


def test_the_claim_is_read_even_when_wrapped():
    assert collect.not_reviewed('```json\n{"items":[],"not_reviewed":["本:x", 3]}\n```') == {"本:x"}
    assert collect.not_reviewed("読めない") == set()
