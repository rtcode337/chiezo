"""chiezo-api ルーティング(設計書 §5)。"""
from __future__ import annotations

import html
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import db
from app.fts import build_match_query, escape_like
from app.registry import Source, scan_sources

log = logging.getLogger("chiezo.api")

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


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(s.name)}</td>"
        f"<td>{html.escape(s.kind)}</td>"
        f"<td>{html.escape(s.lang or '')}</td>"
        f"<td>{s.doc_count:,}</td>"
        f"<td>{html.escape(s.dump_date or '')}</td>"
        f"<td>{html.escape(s.built_at or '')}</td>"
        f"<td>{s.schema_version}</td>"
        f"</tr>"
        for s in sorted(sources.values(), key=lambda s: s.name)
    )
    if not rows:
        rows = '<tr><td colspan="7">登録済みのソースはありません</td></tr>'
    body = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>chiezo 管理画面</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
  h1 {{ font-size: 1.25rem; }}
  table {{ border-collapse: collapse; margin-top: 1rem; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
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
</body>
</html>"""
    return HTMLResponse(content=body)


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
