"""本文を返さなかった直し —— 見出しとタグしか見せない回(`{names}`)で起きる。

AI は今の本文を知らないので、タグだけ直して本文を空で返すのは筋の通った答え。
本文が無いとして捨てていた頃は、本棚で ISBN のタグを付けた 20 冊が全部捨てられ、
回の控えには「変化なし」だけが残った。
"""
from __future__ import annotations

import pytest

from app import collect


@pytest.fixture
def refine(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    collect.create("shelf", prompt="{names}\n直して", interval_minutes=60)
    return collect.get("shelf")


def test_an_edit_without_a_body_keeps_the_body(refine):
    previous = {"本:リーダブルコード": {
        "doc_id": 1, "title": "本:リーダブルコード", "body": "読みやすいコードの本",
        "tags": ["技術書", "分野:設計"], "extra": {"docs": 5},
    }}

    docs, diff = collect.material(
        refine, previous,
        [{"title": "本:リーダブルコード", "body": None,
          "tags": ["技術書", "分野:設計", "ISBN:9784873115658"], "extra": {"isbn": "9784873115658"}}],
        edits=True,
    )

    book = {d["title"]: d for d in docs}["本:リーダブルコード"]
    assert book["body"] == "読みやすいコードの本"
    assert "ISBN:9784873115658" in book["tags"]
    assert book["extra"]["isbn"] == "9784873115658"
    # 機械が入れた数はそのまま(脇書きは書いた鍵だけ変わる)
    assert book["extra"]["docs"] == 5
    assert diff["updated"] == 1


def test_a_new_item_without_a_body_is_still_skipped(refine):
    """足す 1 件は、本文が無ければ今までどおり捨てる(何の 1 件か読めない)。"""
    docs, diff = collect.material(
        refine, {}, [{"title": "本:新しい本", "body": None, "tags": ["技術書"]}], edits=True,
    )
    assert docs == []
    assert diff["added"] == 0
