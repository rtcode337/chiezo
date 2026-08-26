#!/usr/bin/env python3
"""1 プロジェクト 1 メモを、全プロジェクトを持つ 1 件の JSON メモにまとめる。

プロジェクトはタスクの入れ物の定義でしかないのに、短期記憶に 1 件ずつ並び、
並び替えのたびにメモを書き換えていた。1 件にまとめると、並びは配列の順そのもので
表せて、読み書きも 1 文書で済む。

**id は元のメモの doc_id を引き継ぐ**。画面が持っている `projectId` と、REST の
`/api/projects/{id}` がそのまま通るようにするため。

    python scripts/migrate_projects_to_one_note.py --notes-dir data/notes          # 下見
    python scripts/migrate_projects_to_one_note.py --notes-dir data/notes --apply  # 実行

`--apply` を付けるまで何も書き換えない。書き込むので **chiezo-app と chiezo-tasks は
止めてから実行する**。まとめた後は元のメモを消すので、**流す前に data/notes を
コピーしておくと安心**(タスクの所属はタグなので影響を受けないが、プロジェクトの
説明とリポジトリ URL はこのメモにしか無い)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

TAG_PROJECT = "project"
TAG_ARCHIVED = "アーカイブ"
PROJECTS_TITLE = "プロジェクト"


def _notes_db(notes_dir: Path) -> Path:
    path = notes_dir / "notes.db"
    if not path.is_file():
        sys.exit(f"notes の DB が見つからない: {path}")
    return path


def _collect(conn: sqlite3.Connection) -> list[dict]:
    """既存の 1 プロジェクト 1 メモを読む。集約済みのメモは対象にしない。"""
    rows = conn.execute(
        "SELECT doc_id, title, body, tags, extra, updated_at FROM docs"
        " WHERE doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY doc_id",
        (TAG_PROJECT,),
    ).fetchall()
    projects = []
    for doc_id, title, body, raw_tags, raw_extra, updated_at in rows:
        if title == PROJECTS_TITLE:
            continue  # 既にまとめたメモ
        tags = json.loads(raw_tags or "[]")
        extra = json.loads(raw_extra or "{}")
        body = (body or "").strip()
        # 本文はタイトル行から始まる(notes は空本文を許さないので名前が入っている)
        description = body[len(title):].strip() if body.startswith(title) else body
        projects.append({
            "doc_id": doc_id,
            "sort_order": int(extra.get("sort_order") or 0),
            "id": doc_id,
            "name": title,
            "description": description,
            "repo_urls": [str(u) for u in (extra.get("repo_urls") or [])],
            "archived": TAG_ARCHIVED in tags,
            "created_at": str(extra.get("created_at") or updated_at or ""),
            "updated_at": updated_at or "",
        })
    # 画面に出ていた順(sort_order → 名前)をそのまま配列の順に写す
    projects.sort(key=lambda p: (p["sort_order"], p["name"]))
    return projects


def _write(conn: sqlite3.Connection, projects: list[dict]) -> None:
    """集約したメモを 1 件足し、元のメモを消す。

    FTS と doc_tags / tag_counts も手で揃える(`app/notes.py` と同じ手順)。
    external content の FTS は自動で追従しないため。
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "next_id": max(p["id"] for p in projects) + 1,
        "projects": [
            {k: p[k] for k in ("id", "name", "description", "repo_urls", "archived",
                               "created_at", "updated_at")}
            for p in projects
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    (doc_id,) = conn.execute("SELECT COALESCE(MAX(doc_id), 0) + 1 FROM docs").fetchone()
    conn.execute(
        "INSERT INTO docs (doc_id, title, opening, body, tags, links, updated_at,"
        " rank_score, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, PROJECTS_TITLE, body[:400], body,
         json.dumps([TAG_PROJECT], ensure_ascii=False), None, now, 0.0, None),
    )
    conn.execute(
        "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)", (doc_id, PROJECTS_TITLE, body)
    )
    conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (TAG_PROJECT, doc_id))
    conn.execute(
        "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
        " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
        (TAG_PROJECT,),
    )

    for project in projects:
        old = project["doc_id"]
        row = conn.execute("SELECT title, body, tags FROM docs WHERE doc_id = ?", (old,)).fetchone()
        if row is None:
            continue
        title, old_body, raw_tags = row
        conn.execute(
            "INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
            (old, title, old_body),
        )
        for tag in json.loads(raw_tags or "[]"):
            conn.execute("UPDATE tag_counts SET docs = docs - 1 WHERE tag = ?", (tag,))
        conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (old,))
        conn.execute("DELETE FROM docs WHERE doc_id = ?", (old,))
    conn.execute("DELETE FROM tag_counts WHERE docs <= 0")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-dir", type=Path, required=True, help="CHIEZO_NOTES_DIR と同じ場所")
    parser.add_argument("--apply", action="store_true", help="実際に書き換える(既定は下見だけ)")
    args = parser.parse_args()

    conn = sqlite3.connect(_notes_db(args.notes_dir))
    try:
        projects = _collect(conn)
        if not projects:
            print("まとめるものはない(1 プロジェクト 1 メモの形は残っていない)")
            return
        for p in projects:
            urls = f" / {len(p['repo_urls'])} URL" if p["repo_urls"] else ""
            flag = " [アーカイブ]" if p["archived"] else ""
            print(f"  id={p['id']:<4} {p['name']}{flag}{urls}")
        print(f"{len(projects)} 件を 1 件のメモにまとめる(元のメモは消す)")
        if not args.apply:
            print("下見だけ。実際に書き換えるには --apply を付ける")
            return
        with conn:
            _write(conn, projects)
        print(f"{len(projects)} 件をまとめた")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
