"""chiezo-api ルーティング(設計書 §5)。"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db
from app.fts import build_match_query, escape_like
from app.known_sources import KNOWN_SOURCES
from app.pages import esc, page_shell
from app.registry import Source, scan_sources

log = logging.getLogger("chiezo.api")

# 管理画面の初期化ボタンから叩く chiezo-trigger の内部 URL。未設定ならその機能を無効化する。
TRIGGER_URL = os.environ.get("CHIEZO_TRIGGER_URL")
TRIGGER_TIMEOUT = 5.0

DEFAULT_DOC_FIELDS = ["title", "opening", "body", "tags", "updated_at"]
ALLOWED_DOC_FIELDS = [
    "doc_id", "title", "opening", "body", "tags", "links",
    "updated_at", "rank_score", "extra",
]
JSON_FIELDS = {"tags", "links", "extra"}

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("CHIEZO_DATA_DIR", "/data"))
    app.state.sources = scan_sources(data_dir)
    if not app.state.sources:
        log.warning("no sources registered from %s", data_dir)
    yield


app = FastAPI(title="chiezo", version="0.2", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(db.QueryTimeout)
async def timeout_handler(request: Request, exc: db.QueryTimeout):
    return JSONResponse(status_code=504, content={"error": "query timeout"})


def get_source(request: Request, source: str) -> Source:
    sources: dict[str, Source] = request.app.state.sources
    src = sources.get(source)
    if src is None:
        raise HTTPException(
            404,
            {"error": f"unknown source: {source}", "sources": sorted(sources)},
        )
    return src


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


# ---- ヘルスチェック・ソース一覧 -------------------------------------------


@app.get("/healthz")
def healthz(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    return {
        "status": "ok",
        "sources": {
            s.name: {"docs": s.doc_count, "dump_date": s.dump_date} for s in sources.values()
        },
    }


@app.get("/v1/sources")
def list_sources(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    return {
        "sources": [
            {
                "name": s.name,
                "kind": s.kind,
                "lang": s.lang,
                "dump_date": s.dump_date,
                "docs": s.doc_count,
                "schema_version": s.schema_version,
                "built_at": s.built_at,
            }
            for s in sources.values()
        ]
    }


# ---- 管理画面 ---------------------------------------------------------------


def _fetch_trigger_status() -> dict | None:
    if not TRIGGER_URL:
        return None
    try:
        res = httpx.get(f"{TRIGGER_URL}/status", timeout=TRIGGER_TIMEOUT)
        res.raise_for_status()
        return res.json()
    except httpx.HTTPError as e:
        log.warning("chiezo-trigger status unreachable: %s", e)
        return {"state": "unreachable", "error": str(e)}


def _job_status_html(job: dict | None) -> str:
    if job is None:
        return (
            '<div class="job-status">'
            "初期化トリガー(chiezo-trigger)は設定されていません"
            " (CHIEZO_TRIGGER_URL 未設定)。"
            "</div>"
        )
    state = job.get("state", "idle")
    css = f"job-status {state}" if state in ("running", "error") else "job-status"
    lines = [f'<div class="{css}">', f"<p>状態: {esc(state)}"]
    if job.get("source"):
        lines.append(f" / ソース: {esc(job['source'])}")
    if job.get("started_at"):
        lines.append(f" / 開始: {esc(job['started_at'])}")
    if job.get("finished_at"):
        lines.append(f" / 終了: {esc(job['finished_at'])}")
    lines.append("</p>")
    if job.get("error"):
        lines.append(f"<p>エラー: {esc(job['error'])}</p>")
    log_tail = job.get("log_tail")
    if log_tail:
        lines.append('<div class="log-tail">' + esc("\n".join(log_tail)) + "</div>")
    lines.append("</div>")
    return "\n".join(lines)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    rows = "\n".join(
        f"<tr>"
        f"<td><a href=\"/{esc(s.name)}/\">{esc(s.name)}</a></td>"
        f"<td>{esc(s.kind)}</td>"
        f"<td>{esc(s.lang or '')}</td>"
        f"<td>{s.doc_count:,}</td>"
        f"<td>{esc(s.dump_date or '')}</td>"
        f"<td>{esc(s.built_at or '')}</td>"
        f"<td>{s.schema_version}</td>"
        f"</tr>"
        for s in sorted(sources.values(), key=lambda s: s.name)
    )
    if not rows:
        rows = '<tr><td colspan="7">登録済みのソースはありません</td></tr>'

    uninitialized = {
        name: meta for name, meta in KNOWN_SOURCES.items() if name not in sources
    }
    job = _fetch_trigger_status()
    job_running = bool(job and job.get("state") == "running")
    init_rows = "\n".join(
        f"<tr>"
        f"<td>{esc(name)}</td>"
        f"<td>{esc(meta.get('kind', ''))}</td>"
        f"<td>{esc(meta.get('lang', ''))}</td>"
        f"<td>"
        f'<form class="init-form" method="post" action="/admin/init/{esc(name)}">'
        f'<button type="submit"{" disabled" if not TRIGGER_URL or job_running else ""}>初期化</button>'
        f"</form>"
        f"</td>"
        f"</tr>"
        for name, meta in sorted(uninitialized.items())
    )
    if not init_rows:
        init_rows = '<tr><td colspan="4">未初期化のソースはありません</td></tr>'

    body = f"""
<h1>chiezo 管理画面</h1>
<p>登録ソース数: {len(sources)}</p>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th>docs</th><th>dump_date</th><th>built_at</th><th>schema_version</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<h2>未初期化データの初期化</h2>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th></th></tr>
</thead>
<tbody>
{init_rows}
</tbody>
</table>
{_job_status_html(job)}
"""
    return HTMLResponse(content=page_shell("chiezo 管理画面", body, refresh=5 if job_running else None))


@app.post("/admin/init/{source}")
def admin_init(source: str, request: Request):
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    if source not in KNOWN_SOURCES:
        raise HTTPException(404, {"error": f"unknown source: {source}"})
    sources: dict[str, Source] = request.app.state.sources
    if source in sources:
        raise HTTPException(409, {"error": f"source already initialized: {source}"})
    try:
        res = httpx.post(f"{TRIGGER_URL}/run/{source}", timeout=TRIGGER_TIMEOUT)
    except httpx.HTTPError as e:
        raise HTTPException(502, {"error": f"chiezo-trigger unreachable: {e}"}) from e
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json())
    return RedirectResponse(url="/admin", status_code=303)


# ---- 検索 -------------------------------------------------------------------


@app.get("/v1/{source}/search")
def search(
    request: Request,
    source: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    offset: int = Query(0, ge=0),
):
    src = get_source(request, source)
    match = build_match_query(q)
    if match is None:
        # trigram で扱えない短い検索語 → タイトル前方一致へフォールバック
        prefix = escape_like(q.strip())
        rows = db.query(
            src.path,
            "SELECT doc_id, title, substr(coalesce(opening, body), 1, 120) AS snippet,"
            " updated_at"
            " FROM docs WHERE title LIKE ? ESCAPE '\\'"
            " ORDER BY rank_score DESC, title LIMIT ? OFFSET ?",
            (prefix + "%", limit, offset),
        )
        mode = "title_prefix"
    else:
        rows = db.query(
            src.path,
            "SELECT d.doc_id AS doc_id, d.title AS title,"
            " snippet(docs_fts, 1, '', '', '…', 40) AS snippet, d.updated_at AS updated_at"
            " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
            " WHERE docs_fts MATCH ?"
            " ORDER BY bm25(docs_fts, 5.0, 1.0) ASC, d.rank_score DESC"
            " LIMIT ? OFFSET ?",
            (match, limit, offset),
        )
        mode = "fts"
    return {
        "source": source,
        "query": q,
        "mode": mode,
        "results": [dict(r) for r in rows],
    }


# ---- 文書取得 ---------------------------------------------------------------


def parse_fields(fields: str | None) -> list[str]:
    if not fields:
        return DEFAULT_DOC_FIELDS
    requested = [f.strip() for f in fields.split(",") if f.strip()]
    unknown = [f for f in requested if f not in ALLOWED_DOC_FIELDS]
    if unknown:
        raise HTTPException(
            400,
            {
                "error": f"unknown fields: {', '.join(unknown)}",
                "allowed_fields": ALLOWED_DOC_FIELDS,
            },
        )
    return requested


def doc_response(row, fields: list[str], max_chars: int) -> dict:
    out: dict = {}
    for f in fields:
        value = row[f]
        if f in JSON_FIELDS and value is not None:
            value = json.loads(value)
        if f == "body" and value is not None and max_chars > 0:
            value = value[:max_chars]
        out[f] = value
    return out


def title_candidates(src: Source, title: str, limit: int = 5) -> list[str]:
    rows = db.query(
        src.path,
        "SELECT title FROM docs WHERE title LIKE ? ESCAPE '\\'"
        " ORDER BY rank_score DESC, title LIMIT ?",
        (escape_like(title) + "%", limit),
    )
    return [r["title"] for r in rows]


def fetch_doc_by_title(src: Source, title: str):
    """完全一致 → aliases 解決の順で文書行を返す。見つからなければ None。"""
    rows = db.query(src.path, "SELECT * FROM docs WHERE title = ?", (title,))
    if rows:
        return rows[0]
    rows = db.query(
        src.path,
        "SELECT d.* FROM aliases a JOIN docs d ON d.doc_id = a.doc_id WHERE a.alias = ? LIMIT 1",
        (title,),
    )
    return rows[0] if rows else None


def not_found_with_candidates(src: Source, title: str) -> HTTPException:
    return HTTPException(
        404,
        {"error": f"document not found: {title}", "candidates": title_candidates(src, title)},
    )


@app.get("/v1/{source}/doc")
def get_doc_by_title(
    request: Request,
    source: str,
    title: str = Query(..., min_length=1),
    fields: str | None = None,
    max_chars: int = Query(0, ge=0),
):
    src = get_source(request, source)
    field_list = parse_fields(fields)
    row = fetch_doc_by_title(src, title)
    if row is None:
        raise not_found_with_candidates(src, title)
    return doc_response(row, field_list, max_chars)


@app.get("/v1/{source}/doc/{doc_id}")
def get_doc_by_id(
    request: Request,
    source: str,
    doc_id: int,
    fields: str | None = None,
    max_chars: int = Query(0, ge=0),
):
    src = get_source(request, source)
    field_list = parse_fields(fields)
    rows = db.query(src.path, "SELECT * FROM docs WHERE doc_id = ?", (doc_id,))
    if not rows:
        raise HTTPException(404, {"error": f"document not found: doc_id={doc_id}"})
    return doc_response(rows[0], field_list, max_chars)


# ---- タイトル前方一致 / リンク / ランダム -----------------------------------


@app.get("/v1/{source}/titles")
def titles(
    request: Request,
    source: str,
    prefix: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    src = get_source(request, source)
    rows = db.query(
        src.path,
        "SELECT doc_id, title FROM docs WHERE title LIKE ? ESCAPE '\\'"
        " ORDER BY rank_score DESC, title LIMIT ?",
        (escape_like(prefix) + "%", limit),
    )
    return {"source": source, "prefix": prefix, "titles": [dict(r) for r in rows]}


@app.get("/v1/{source}/links")
def links(
    request: Request,
    source: str,
    title: str = Query(..., min_length=1),
    direction: str = Query("out"),
):
    if direction != "out":
        raise HTTPException(
            400, {"error": "only direction=out is supported in v0.2"}
        )
    src = get_source(request, source)
    row = fetch_doc_by_title(src, title)
    if row is None:
        raise not_found_with_candidates(src, title)
    link_list = json.loads(row["links"]) if row["links"] else []
    return {"source": source, "title": row["title"], "direction": "out", "links": link_list}


@app.get("/v1/{source}/random")
def random_docs(
    request: Request,
    source: str,
    limit: int = Query(5, ge=1, le=50),
):
    src = get_source(request, source)
    rows = db.query(
        src.path,
        "SELECT doc_id, title FROM docs ORDER BY RANDOM() LIMIT ?",
        (limit,),
    )
    return {"source": source, "results": [dict(r) for r in rows]}


# ---- ブラウズ画面(人間向け HTML) -------------------------------------------

BROWSE_LIMIT = 50


def _browse_nav(source: str) -> str:
    return (
        '<nav><a href="/admin">管理画面</a>'
        f'<a href="/{esc(source)}/">{esc(source)} トップ</a></nav>'
    )


@app.get("/{source}/", response_class=HTMLResponse)
def browse_source(request: Request, source: str, q: str | None = Query(None)):
    src = get_source(request, source)
    if q:
        match = build_match_query(q)
        if match is None:
            prefix = escape_like(q.strip())
            rows = db.query(
                src.path,
                "SELECT doc_id, title, substr(coalesce(opening, body), 1, 160) AS snippet"
                " FROM docs WHERE title LIKE ? ESCAPE '\\'"
                " ORDER BY rank_score DESC, title LIMIT ?",
                (prefix + "%", BROWSE_LIMIT),
            )
        else:
            rows = db.query(
                src.path,
                "SELECT d.doc_id AS doc_id, d.title AS title,"
                " snippet(docs_fts, 1, '', '', '…', 40) AS snippet"
                " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
                " WHERE docs_fts MATCH ?"
                " ORDER BY bm25(docs_fts, 5.0, 1.0) ASC, d.rank_score DESC LIMIT ?",
                (match, BROWSE_LIMIT),
            )
        items = "\n".join(
            f"<tr><td><a href=\"/{esc(source)}/doc/{r['doc_id']}\">{esc(r['title'])}</a></td>"
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
<form method="get" action="/{esc(source)}/">
<input type="text" name="q" value="{esc(q or '')}" placeholder="キーワード検索">
<button type="submit">検索</button>
</form>
{results_html}
"""
    return HTMLResponse(content=page_shell(f"chiezo: {source}", body))


@app.get("/{source}/doc/{doc_id}", response_class=HTMLResponse)
def browse_doc(request: Request, source: str, doc_id: int):
    src = get_source(request, source)
    rows = db.query(src.path, "SELECT * FROM docs WHERE doc_id = ?", (doc_id,))
    if not rows:
        body = f"""
{_browse_nav(source)}
<h1>見つかりません</h1>
<p>doc_id={esc(doc_id)} の文書は存在しません。</p>
"""
        return HTMLResponse(content=page_shell(f"chiezo: {source}", body), status_code=404)
    row = rows[0]
    tags = json.loads(row["tags"]) if row["tags"] else []
    links = json.loads(row["links"]) if row["links"] else []
    extra = json.loads(row["extra"]) if row["extra"] else {}
    tags_html = ", ".join(esc(t) for t in tags) or "(なし)"
    links_html = "\n".join(f"<li>{esc(link)}</li>" for link in links) or "<li>(なし)</li>"
    extra_html = json.dumps(extra, ensure_ascii=False, indent=2) if extra else "(なし)"
    body = f"""
{_browse_nav(source)}
<h1>{esc(row['title'])}</h1>
<p>doc_id: {row['doc_id']} / updated_at: {esc(row['updated_at'] or '')}</p>
<p>tags: {tags_html}</p>
<h2>本文</h2>
<pre class="doc-body">{esc(row['opening'] or '')}

{esc(row['body'] or '')}</pre>
<h2>リンク</h2>
<ul>{links_html}</ul>
<h2>extra</h2>
<pre class="doc-body">{esc(extra_html)}</pre>
"""
    return HTMLResponse(content=page_shell(f"chiezo: {row['title']}", body))
