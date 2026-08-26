#!/usr/bin/env python3
"""短期記憶の `todo` タグを `task` に付け替える(1 回だけ流す移行)。

タスクを表すタグを `todo` から `task` へ分けたときの移行。`todo` は「あとでやる」
くらいの意味で普通のメモにも付くので、タスク画面に並べる基準にしていると、
メモがタスクに化ける。**この移行を流さないと、これまでのタスクが画面から消える**
(タスクとして並ぶ基準が `task` タグになるため)。

自動では流さない。起動のたびに走らせると、移行後に付けた `todo`(普通のメモの
つもりのもの)まで task に化けるため —— どこで線を引くかは人にしか決められない。

    python scripts/migrate_todo_to_task.py --notes-dir data/notes          # 下見
    python scripts/migrate_todo_to_task.py --notes-dir data/notes --apply  # 実行

`--apply` を付けるまで何も書き換えない。書き込むので **chiezo-app と chiezo-tasks は
止めてから実行する**(notes は WAL なので読みは並行できるが、付け替えの途中を
読ませる意味がない)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

OLD_TAG = "todo"
NEW_TAG = "task"


def _notes_db(notes_dir: Path) -> Path:
    path = notes_dir / "notes.db"
    if not path.is_file():
        sys.exit(f"notes の DB が見つからない: {path}")
    return path


def _targets(conn: sqlite3.Connection) -> list[tuple[int, str, list[str]]]:
    """`todo` を持ち、まだ `task` を持たないメモ。何度流しても同じ結果になる。"""
    rows = conn.execute(
        "SELECT doc_id, title, tags FROM docs"
        " WHERE doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        "   AND doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY doc_id",
        (OLD_TAG, NEW_TAG),
    ).fetchall()
    out = []
    for doc_id, title, raw in rows:
        try:
            tags = [str(t) for t in json.loads(raw or "[]")]
        except ValueError:
            tags = []
        out.append((doc_id, title, tags))
    return out


def _swap(conn: sqlite3.Connection, doc_id: int, tags: list[str]) -> None:
    """`docs.tags`・`doc_tags`・`tag_counts` を揃えて付け替える。

    3 つとも直すのは、どれか 1 つでもずれると絞り込みが静かに壊れるため
    (`app/notes.py` の update と同じ手順)。
    """
    new_tags = [NEW_TAG if t == OLD_TAG else t for t in tags]
    conn.execute(
        "UPDATE docs SET tags = ? WHERE doc_id = ?",
        (json.dumps(new_tags, ensure_ascii=False), doc_id),
    )
    conn.execute("UPDATE doc_tags SET tag = ? WHERE doc_id = ? AND tag = ?", (NEW_TAG, doc_id, OLD_TAG))
    conn.execute("UPDATE tag_counts SET docs = docs - 1 WHERE tag = ?", (OLD_TAG,))
    conn.execute(
        "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
        " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
        (NEW_TAG,),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-dir", type=Path, required=True, help="CHIEZO_NOTES_DIR と同じ場所")
    parser.add_argument("--apply", action="store_true", help="実際に書き換える(既定は下見だけ)")
    args = parser.parse_args()

    conn = sqlite3.connect(_notes_db(args.notes_dir))
    try:
        targets = _targets(conn)
        if not targets:
            print(f"付け替えるものはない({OLD_TAG} を持つ未移行のメモが 0 件)")
            return
        for doc_id, title, _ in targets:
            print(f"  {doc_id:>5}  {title[:60]}")
        print(f"{len(targets)} 件が対象。")
        if not args.apply:
            print("下見だけ。実際に書き換えるには --apply を付ける")
            return
        with conn:
            for doc_id, _, tags in targets:
                _swap(conn, doc_id, tags)
        conn.execute("DELETE FROM tag_counts WHERE docs <= 0")
        conn.commit()
        print(f"{len(targets)} 件を {OLD_TAG} → {NEW_TAG} に付け替えた")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
