"""ブラウズ画面(`/search/{source}/`)。中身を人が確かめるための最小の画面。

REST(`app/main.py`)と同じ DB を同じ並び順で引く。検索の本体は `app/deps.py` の
ORDER BY 断片を共有していて、ここで別の並びを作らない。唯一の例外は未検索の
全件一覧(doc_id 昇順)で、これは並び替えではなく「格納順に頭から確かめる」ため。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import db
from app.deps import exact_title_first, get_source, relevance_order, require_tag_schema
from app.fts import build_match_query, escape_like
from app.pages import browse_url, doc_url, esc, page_shell
from app.registry import TAG_MIN_SCHEMA_VERSION

log = logging.getLogger("chiezo.app")

router = APIRouter()

# ---- ブラウズ画面(人間向け HTML) -------------------------------------------
#
# `/search/` の下に置く。以前はソース名をそのまま `/{source}/` に置いていたが、
# それだとルート直下がキャッチオールになり、`ask` や `admin` という名前のソースを
# 足せない(既存の画面に食われる)。逆に画面を足すたびにソース名と衝突しないか
# 気にする必要もあった。前置きを 1 つ挟むだけで、その両方が消える。

# 1 ページの件数。未検索の全件一覧も、検索・タグ絞り込みの結果もこの単位でページングする
# (上限なしで出すと jawiki や geonames の百万件超がそのまま HTML になる)。
PAGE_SIZE = 100


def _browse_nav(source: str) -> str:
    return (
        '<nav><a href="/admin">管理画面</a>'
        f'<a href="{esc(browse_url(source))}">{esc(source)} トップ</a></nav>'
    )


def _tags_text(tags_json: str | None) -> str:
    tags = json.loads(tags_json) if tags_json else []
    return ", ".join(esc(t) for t in tags)


def _result_table(source: str, rows) -> str:
    """一覧の表。未検索・検索・タグ絞り込みの 3 経路で同じ列(doc_id / title / tags / snippet)。"""
    items = "\n".join(
        f"<tr><td>{r['doc_id']}</td>"
        f"<td><a href=\"{esc(doc_url(source, r['doc_id']))}\">{esc(r['title'])}</a></td>"
        f"<td class=\"tags\">{_tags_text(r['tags'])}</td>"
        f"<td class=\"snippet\">{esc(r['snippet'] or '')}</td></tr>"
        for r in rows
    )
    if not items:
        items = '<tr><td colspan="4">該当する文書がありません</td></tr>'
    return f"""
<table>
<thead><tr><th>doc_id</th><th>title</th><th>tags</th><th>snippet</th></tr></thead>
<tbody>
{items}
</tbody>
</table>
"""


def _pager(source: str, page: int, has_next: bool, *, q: str | None = None,
           tag: str | None = None) -> str:
    """前後ページへのリンク。総件数は数えない(FTS の全件 COUNT は高くつく)ので、
    「次があるか」は 1 件多く取れたかどうかで判定した結果を受け取る。"""
    if page <= 1 and not has_next:
        return ""

    def url(p: int) -> str:
        params: dict[str, str | int] = {}
        if q:
            params["q"] = q
        if tag:
            params["tag"] = tag
        if p > 1:
            params["page"] = p
        query = urlencode(params)
        return browse_url(source) + (f"?{query}" if query else "")

    prev_html = f'<a href="{esc(url(page - 1))}">← 前の{PAGE_SIZE}件</a>' if page > 1 else ""
    next_html = f'<a href="{esc(url(page + 1))}">次の{PAGE_SIZE}件 →</a>' if has_next else ""
    # `page` は int で受けているので実害は無いが、HTML に出す値は例外なく esc を通す
    # (型注釈による絞り込みは呼び出し側の宣言に依存し、この関数を読むだけでは分からない)。
    return (f'<p class="pager">{prev_html}'
            f'<span class="muted">ページ {esc(page)}</span>{next_html}</p>')


def _paginate(rows: list) -> tuple[list, bool]:
    """PAGE_SIZE + 1 件で引いた結果を「表示する分」と「次ページの有無」に分ける。"""
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


@router.get("/search/{source}/", response_class=HTMLResponse)
def browse_source(
    request: Request,
    source: str,
    q: str | None = Query(None),
    tag: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    src = get_source(request, source)
    offset = (page - 1) * PAGE_SIZE
    fetch = PAGE_SIZE + 1  # 1 件多く引いて「次ページがあるか」を知る(COUNT を打たない)
    if tag:
        # 文書詳細のタグから飛んでくる導線(= /v1/<source>/filter?tag= の人間向け)
        require_tag_schema(src)
        rows = db.query(
            src.path,
            "SELECT doc_id, title, tags,"
            " substr(coalesce(opening, body), 1, 160) AS snippet"
            " FROM docs WHERE docs.doc_id IN (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag = ?)"
            " ORDER BY rank_score DESC, title LIMIT ? OFFSET ?",
            (tag, fetch, offset),
        )
        rows, has_next = _paginate(rows)
        body = f"""
{_browse_nav(source)}
<h1>{esc(source)}: タグ「{esc(tag)}」</h1>
<p class="muted">1 ページ {PAGE_SIZE} 件。API では
<code>/v1/{esc(source)}/filter?tag=…</code> で取得できます。</p>
{_result_table(source, rows)}
{_pager(source, page, has_next, tag=tag)}
"""
        return HTMLResponse(content=page_shell(f"{source} / {tag}", body))
    if q:
        match = build_match_query(q)
        if match is None:
            prefix = escape_like(q.strip())
            rows = db.query(
                src.path,
                "SELECT doc_id, title, tags,"
                " substr(coalesce(opening, body), 1, 160) AS snippet"
                " FROM docs WHERE title LIKE ? ESCAPE '\\'"
                f" ORDER BY {exact_title_first()}, rank_score DESC, title LIMIT ? OFFSET ?",
                (prefix + "%", q.strip(), fetch, offset),
            )
        else:
            rows = db.query(
                src.path,
                "SELECT d.doc_id AS doc_id, d.title AS title, d.tags AS tags,"
                " snippet(docs_fts, 1, '', '', '…', 40) AS snippet"
                " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
                " WHERE docs_fts MATCH ?"
                f" ORDER BY {relevance_order('d.')} LIMIT ? OFFSET ?",
                (match, q.strip(), fetch, offset),
            )
        rows, has_next = _paginate(rows)
        results_html = _result_table(source, rows) + _pager(source, page, has_next, q=q)
    else:
        # 未検索は全件一覧(doc_id 昇順)。doc_id は主キーなので ORDER BY は索引を歩くだけで、
        # rank_score 順のようなフルスキャンにはならない(かつては一覧を出していなかった理由)。
        # notes のような小さなソースを頭から確かめる用途と、大規模ソースの様子見の両方を
        # 同じページングでまかなう。
        rows = db.query(
            src.path,
            "SELECT doc_id, title, tags,"
            " substr(coalesce(opening, body), 1, 160) AS snippet"
            " FROM docs ORDER BY doc_id LIMIT ? OFFSET ?",
            (fetch, offset),
        )
        rows, has_next = _paginate(rows)
        results_html = (
            f'<p class="muted">全 {src.doc_count:,} 件を doc_id 順で表示'
            f'(1 ページ {PAGE_SIZE} 件)。</p>'
            + _result_table(source, rows)
            + _pager(source, page, has_next)
        )
    body = f"""
{_browse_nav(source)}
<h1>{esc(source)}</h1>
<form method="get" action="{esc(browse_url(source))}">
<input type="text" name="q" value="{esc(q or '')}" placeholder="キーワード検索">
<button type="submit">検索</button>
</form>
{results_html}
"""
    return HTMLResponse(content=page_shell(source, body))


@router.get("/search/{source}/doc/{doc_id}", response_class=HTMLResponse)
def browse_doc(request: Request, source: str, doc_id: int):
    """文書 1 件の詳細。

    `opening` は出さない。あれは `body` の冒頭を切り出したもの(検索結果の
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
        return HTMLResponse(content=page_shell(source, body), status_code=404)
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
    return HTMLResponse(content=page_shell(row["title"], body))

