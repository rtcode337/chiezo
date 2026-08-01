"""ブラウズ画面(`/search/{source}/`)。中身を人が確かめるための最小の画面。

REST(`app/main.py`)と同じ DB を同じ並び順で引く。検索の本体は `app/deps.py` の
ORDER BY 断片を共有していて、ここで別の並びを作らない。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import db
from app.deps import exact_title_first, get_source, relevance_order, require_tag_schema
from app.fts import build_match_query, escape_like
from app.pages import browse_url, doc_url, esc, page_shell
from app.registry import TAG_MIN_SCHEMA_VERSION

log = logging.getLogger("chiezo.api")

router = APIRouter()

# ---- ブラウズ画面(人間向け HTML) -------------------------------------------
#
# **`/search/` の下に置く**。以前はソース名をそのまま `/{source}/` に置いていたが、
# それだとルート直下がキャッチオールになり、`ask` や `admin` という名前のソースを
# 足せない(既存の画面に食われる)。逆に画面を足すたびにソース名と衝突しないか
# 気にする必要もあった。前置きを 1 つ挟むだけで、その両方が消える。

BROWSE_LIMIT = 50


def _browse_nav(source: str) -> str:
    return (
        '<nav><a href="/admin">管理画面</a>'
        f'<a href="{esc(browse_url(source))}">{esc(source)} トップ</a></nav>'
    )


@router.get("/search/{source}/", response_class=HTMLResponse)
def browse_source(
    request: Request,
    source: str,
    q: str | None = Query(None),
    tag: str | None = Query(None),
):
    src = get_source(request, source)
    if tag:
        # 文書詳細のタグから飛んでくる導線(= /v1/<source>/filter?tag= の人間向け)
        require_tag_schema(src)
        rows = db.query(
            src.path,
            "SELECT doc_id, title, substr(coalesce(opening, body), 1, 160) AS snippet"
            " FROM docs WHERE docs.doc_id IN (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag = ?)"
            " ORDER BY rank_score DESC, title LIMIT ?",
            (tag, BROWSE_LIMIT),
        )
        items = "\n".join(
            f"<tr><td><a href=\"{esc(doc_url(source, r['doc_id']))}\">{esc(r['title'])}</a></td>"
            f"<td class=\"snippet\">{esc(r['snippet'] or '')}</td></tr>"
            for r in rows
        )
        if not items:
            items = '<tr><td colspan="2">このタグの文書はありません</td></tr>'
        body = f"""
{_browse_nav(source)}
<h1>{esc(source)}: タグ「{esc(tag)}」</h1>
<p class="muted">先頭 {BROWSE_LIMIT} 件。全件は
<code>/v1/{esc(source)}/filter?tag=…</code> で取得できます。</p>
<table>
<thead><tr><th>title</th><th>snippet</th></tr></thead>
<tbody>
{items}
</tbody>
</table>
"""
        return HTMLResponse(content=page_shell(f"Chiezo: {source} / {tag}", body))
    if q:
        match = build_match_query(q)
        if match is None:
            prefix = escape_like(q.strip())
            rows = db.query(
                src.path,
                "SELECT doc_id, title, substr(coalesce(opening, body), 1, 160) AS snippet"
                " FROM docs WHERE title LIKE ? ESCAPE '\\'"
                f" ORDER BY {exact_title_first()}, rank_score DESC, title LIMIT ?",
                (prefix + "%", q.strip(), BROWSE_LIMIT),
            )
        else:
            rows = db.query(
                src.path,
                "SELECT d.doc_id AS doc_id, d.title AS title,"
                " snippet(docs_fts, 1, '', '', '…', 40) AS snippet"
                " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
                " WHERE docs_fts MATCH ?"
                f" ORDER BY {relevance_order('d.')} LIMIT ?",
                (match, q.strip(), BROWSE_LIMIT),
            )
        items = "\n".join(
            f"<tr><td><a href=\"{esc(doc_url(source, r['doc_id']))}\">{esc(r['title'])}</a></td>"
            f"<td class=\"snippet\">{esc(r['snippet'] or '')}</td></tr>"
            for r in rows
        )
        if not items:
            items = '<tr><td colspan="2">該当する文書がありません</td></tr>'
        results_html = f"""
<table>
<thead><tr><th>title</th><th>snippet</th></tr></thead>
<tbody>
{items}
</tbody>
</table>
"""
    else:
        # 大規模ソース(jawiki 等)では rank_score 順の全件一覧はフルスキャンになりタイムアウトしうるため、
        # 未検索時は一覧を出さず検索フォームのみ表示する。
        results_html = ""
    body = f"""
{_browse_nav(source)}
<h1>{esc(source)}</h1>
<form method="get" action="{esc(browse_url(source))}">
<input type="text" name="q" value="{esc(q or '')}" placeholder="キーワード検索">
<button type="submit">検索</button>
</form>
{results_html}
"""
    return HTMLResponse(content=page_shell(f"Chiezo: {source}", body))


@router.get("/search/{source}/doc/{doc_id}", response_class=HTMLResponse)
def browse_doc(request: Request, source: str, doc_id: int):
    """文書 1 件の詳細。

    **`opening` は出さない**。あれは `body` の冒頭を切り出したもの(検索結果の
    スニペットや「使う」層の抜粋に使う)で、人が読む画面で本文と並べると同じ文章が
    2 回出る。短いメモ(notes)では完全に同じ文字列が 2 度並んでいた。
    """
    src = get_source(request, source)
    rows = db.query(src.path, "SELECT * FROM docs WHERE doc_id = ?", (doc_id,))
    if not rows:
        body = f"""
{_browse_nav(source)}
<h1>見つかりません</h1>
<p>doc_id={esc(doc_id)} の文書は存在しません。</p>
"""
        return HTMLResponse(content=page_shell(f"Chiezo: {source}", body), status_code=404)
    row = rows[0]
    tags = json.loads(row["tags"]) if row["tags"] else []
    links = json.loads(row["links"]) if row["links"] else []
    extra = json.loads(row["extra"]) if row["extra"] else {}
    if src.schema_version >= TAG_MIN_SCHEMA_VERSION:
        # 同じタグの文書一覧へ飛べるようにする(タグ絞り込みの導線)
        tags_html = ", ".join(
            f'<a href="{esc(browse_url(source) + "?tag=" + quote(t))}">{esc(t)}</a>'
            for t in tags
        )
    else:
        tags_html = ", ".join(esc(t) for t in tags)
    tags_html = tags_html or "(なし)"
    links_html = "\n".join(f"<li>{esc(link)}</li>" for link in links) or "<li>(なし)</li>"
    extra_html = json.dumps(extra, ensure_ascii=False, indent=2) if extra else "(なし)"
    body = f"""
{_browse_nav(source)}
<h1>{esc(row['title'])}</h1>
<p>doc_id: {row['doc_id']} / updated_at: {esc(row['updated_at'] or '')}</p>
<p>tags: {tags_html}</p>
<h2>本文</h2>
<pre class="doc-body">{esc(row['body'] or row['opening'] or '(なし)')}</pre>
<h2>リンク</h2>
<ul>{links_html}</ul>
<h2>extra</h2>
<pre class="doc-body">{esc(extra_html)}</pre>
"""
    return HTMLResponse(content=page_shell(f"Chiezo: {row['title']}", body))

