"""chiezo-api の REST(設計書 §5)。

**このモジュールが持つのは機械向けの口(`/v1/...`)とアプリの組み立て**
(lifespan・例外ハンドラ・画面 router の登録・`/mcp` のマウント)。
人間向けの HTML は `app/views/`、両者が共有する下ごしらえは `app/deps.py`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel
from pydantic import Field as PydField

from app import agent, answer, db, media, notes, providers
from app.deps import (
    exact_title_first,
    get_source,
    relevance_order,
    require_attributes,
    require_filter_schema,
    require_tag_schema,
)
from app.fts import build_match_query, escape_like
from app.mcp_server import build_mcp, build_mcp_app
from app.pages import APPLE_TOUCH_ICON_PNG
from app.registry import (
    COORDS_MIN_SCHEMA_VERSION,
    FILTER_MIN_SCHEMA_VERSION,
    RANK_INDEX_MIN_SCHEMA_VERSION,
    TAG_COUNTS_MIN_SCHEMA_VERSION,
    TAG_MIN_SCHEMA_VERSION,
    Source,
    data_dir_fingerprint,
    scan_sources,
)
from app.views import admin as views_admin
from app.views import ai_settings as views_ai_settings
from app.views import browse as views_browse
from app.views import chat as views_chat

log = logging.getLogger("chiezo.api")

# /data の変化(ブルーグリーン切り替え・DB コピー)を検知する定期再走査の間隔(秒)。
# 0 以下で無効(= 従来どおり再起動でのみ反映)。compose は未設定の変数を `VAR=`(空文字)
# で渡すので、素の float() だと「.env に書いていない」だけで起動時に落ちる。
RESCAN_INTERVAL_SECONDS = answer._env_num("CHIEZO_RESCAN_INTERVAL", 5.0, float)

DEFAULT_DOC_FIELDS = ["title", "opening", "body", "tags", "updated_at"]
ALLOWED_DOC_FIELDS = [
    "doc_id", "title", "opening", "body", "tags", "links",
    "updated_at", "rank_score", "extra",
]
JSON_FIELDS = {"tags", "links", "extra"}

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50

# doc で同名の別地物を併記する上限
DOC_CANDIDATE_LIMIT = 5

# /v1/<source>/filter: 一括抽出が用途なので search より上限を上げてある
FILTER_LIMIT_DEFAULT = 50
FILTER_LIMIT_MAX = 500
FILTER_DEFAULT_FIELDS = ["doc_id", "title", "feature", "area", "lat", "lon"]
FILTER_ALLOWED_FIELDS = [
    "doc_id", "title", "feature", "area", "lat", "lon", "wikidata",
    "opening", "body", "tags", "links", "updated_at", "rank_score", "extra",
]


def scan_all(data_dir: Path) -> dict[str, Source]:
    """/data と(有効なら)notes の両方を走査してソース表を作る。

    notes を別ディレクトリに置いているのは、`data_dir_fingerprint` が /data の変化を
    5 秒ごとに見て全ソースを再走査する(`COUNT(*)` 込み)ためで、同じ場所に置くと
    メモを 1 件書くたびに jawiki 150 万件の COUNT が走る(`app/notes.py` 参照)。
    """
    sources = scan_sources(data_dir)
    notes_dir = notes.notes_dir()
    if notes_dir is not None:
        sources.update(scan_sources(notes_dir, mutable=True))
    # 追記される DB は immutable で開けない、と db 側に伝える
    db.set_mutable_paths(s.path for s in sources.values() if s.mutable)
    return sources


def refresh_sources(app: FastAPI) -> bool:
    """/data の指紋が前回と変わっていればソースを再走査して差し替える(変わったら True)。

    ingest のブルーグリーン切り替え(世代ファイルへのリネーム + シンボリックリンク差し替え)や
    別マシンで焼いた DB のコピーを、api の再起動なしで反映するための入口。指紋を先に取って
    から走査するので、走査中にさらに変化があっても次回の呼び出しで拾い直せる。
    接続の開き直しはここではなく db.get_connection が実体の inode を見て行う。
    """
    fp = data_dir_fingerprint(app.state.data_dir)
    if fp == app.state.data_fingerprint:
        return False
    app.state.data_fingerprint = fp
    app.state.sources = scan_all(app.state.data_dir)
    return True


async def _watch_data_dir(app: FastAPI) -> None:
    """RESCAN_INTERVAL_SECONDS ごとに refresh_sources を呼ぶ常駐タスク。

    走査(各 DB の meta 読みと COUNT(*))はブロッキングなのでスレッドへ逃がす。
    失敗しても監視は止めない(次の周期でやり直す)。
    """
    while True:
        await asyncio.sleep(RESCAN_INTERVAL_SECONDS)
        try:
            if await asyncio.to_thread(refresh_sources, app):
                log.info(
                    "data dir changed; sources reloaded (%d registered)",
                    len(app.state.sources),
                )
        except Exception:
            log.exception("periodic source rescan failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("CHIEZO_DATA_DIR", "/data"))
    app.state.data_dir = data_dir
    # 指紋は走査の前に取る(走査中の変化を取りこぼさない側に倒す)
    app.state.data_fingerprint = data_dir_fingerprint(data_dir)
    # notes(唯一書き込めるソース)は ingest を回さずに使えるよう、無ければここで作る
    notes.ensure_db()
    app.state.sources = scan_all(data_dir)
    if not app.state.sources:
        log.warning("no sources registered from %s", data_dir)
    watcher = (
        asyncio.create_task(_watch_data_dir(app)) if RESCAN_INTERVAL_SECONDS > 0 else None
    )
    # MCP(/mcp)はここで組み立てて起動する。理由が 2 つある:
    #  1. セッションマネージャは lifespan の中で run() しないとタスクグループが張られず、
    #     最初のリクエストで "Task group is not initialized" になる(python-sdk#1367)。
    #  2. その run() は 1 インスタンスにつき 1 回しか呼べない。モジュール読み込み時に
    #     作り置きすると、同一プロセスでアプリを二度起動したとき(テストや再入する
    #     ホスティング)に RuntimeError で落ちる。なので起動ごとに作り直す。
    # マウント先(下の _mcp_asgi)はここで置いた app.state.mcp_asgi を見に行く。
    mcp = build_mcp(app)
    # agent モード(app/agent.py)は道具の定義も実行もここから借りるので、
    # ASGI アプリだけでなく MCP サーバー本体も置いておく。
    app.state.mcp = mcp
    # session_manager は streamable_http_app() を先に呼んでからでないと取れない。
    app.state.mcp_asgi = build_mcp_app(mcp)
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


app = FastAPI(title="Chiezo", version="0.2", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(db.QueryTimeout)
async def timeout_handler(request: Request, exc: db.QueryTimeout):
    return JSONResponse(status_code=504, content={"error": "query timeout"})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


# iOS の「ホーム画面に追加」はページの <link rel="apple-touch-icon">(page_shell が出す)を
# 見るが、リンクを解釈できない場面ではサイト直下の /apple-touch-icon.png も探しに来るため、
# 固定パスで配信する
@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon():
    return Response(content=APPLE_TOUCH_ICON_PNG, media_type="image/png")


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


# ---- 検索 -------------------------------------------------------------------


FTS_ROW_SQL = (
    "SELECT d.doc_id AS doc_id, d.title AS title,"
    " snippet(docs_fts, 1, '', '', '…', 40) AS snippet, d.updated_at AS updated_at"
    " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
)

# 並べ替えの前に docs を読む文書数の上限(下の fts_search 参照)。返す件数の
# SEARCH_POOL_FACTOR 倍を候補に取る。人気度の混ぜ込みは bm25 を最大 1.4 倍しか
# 動かせない(POPULARITY_WEIGHT)ので、この深さの候補があれば順位はまず変わらない。
SEARCH_POOL_MIN = 300
SEARCH_POOL_FACTOR = 5
SEARCH_POOL_MAX = 2000
# タイトルが検索語で始まる文書を候補に加えるときに見る索引の件数。
TITLE_ANCHOR_SCAN = 20


def search_pool_size(offset: int, limit: int) -> int:
    need = offset + limit
    return max(need, min(SEARCH_POOL_MAX, max(SEARCH_POOL_MIN, need * SEARCH_POOL_FACTOR)))


def fts_search(src: Source, match: str, exact: str, limit: int, offset: int) -> list:
    """全文検索の本体。**該当件数ではなく返す件数に比例した数の行しか読まない**。

    素直に書くと `docs_fts MATCH ... JOIN docs ORDER BY <bm25 と人気度>` になるが、
    並べ替えに docs の rank_score と title が要るせいで、該当した文書を**全部**
    docs から読むことになる。osm_japan の「東京都」は 17 万件が該当し(施設の本文に
    「所在: 東京都…」が入るため)、上位 10 件を返すのに 17 万行を読んで配信機で
    504 になっていた。都道府県名が軒並み引けなかったのはこれが理由。

    そこで 2 段にする:

    1. bm25 だけで上位 N 件の doc_id を取る。ここは FTS の索引の中で完結する
       (docs を 1 行も読まない)。
    2. その N 件だけ docs と突き合わせ、人気度を混ぜた本来の並びで limit 件返す。

    加えて「タイトルが検索語そのもの / 検索語で始まる」文書を索引から拾って候補に
    足す(idx_docs_title の被覆索引を数十件見るだけ)。bm25 の上位に入らなくても
    `東京都` で記事「東京都」が出るのは百科事典的な引き方の前提なので、
    そこは関連度の運任せにしない。

    候補の外側は順位付けから漏れるため、該当が N 件を超えるときの並びは厳密には
    近似になる(N 件以下なら従来と完全に一致する)。
    """
    pool = search_pool_size(offset, limit)
    ids = [
        r["doc_id"]
        for r in db.query(
            src.path,
            "SELECT rowid AS doc_id FROM docs_fts WHERE docs_fts MATCH ?"
            " ORDER BY bm25(docs_fts, 5.0, 1.0) LIMIT ?",
            (match, pool),
        )
    ]
    anchors = db.query(
        src.path,
        "SELECT doc_id, title FROM docs WHERE title LIKE ? ESCAPE '\\' LIMIT ?",
        (escape_like(exact) + "%", TITLE_ANCHOR_SCAN),
    )
    seen = set(ids)
    for row in anchors:
        # 完全一致と、OSM の同名回避で付く括弧付き(`東京都 (relation:1543125)`)まで。
        # 「東京都庁」のような別の文書まで拾わないよう、そこは前方一致で広げない。
        if row["doc_id"] not in seen and (
            row["title"] == exact or row["title"].startswith(f"{exact} (")
        ):
            ids.append(row["doc_id"])
            seen.add(row["doc_id"])
    if not ids:
        return []
    # 単項 `+` は「この条件を索引に使うな」の指示。付けないと SQLite は候補 1 件ごとに
    # FTS の rowid 検索を選び、そのたびに該当語の doclist(東京都なら 17 万件)を
    # たどり直す(候補 300 件で 0.87 秒)。付けると doclist は 1 回流すだけで済み、
    # docs を読むのも候補の分だけになる(0.013 秒)。
    return db.query(
        src.path,
        f"{FTS_ROW_SQL} WHERE docs_fts MATCH ?"
        f" AND +docs_fts.rowid IN ({','.join('?' * len(ids))})"
        f" ORDER BY {relevance_order('d.')} LIMIT ? OFFSET ?",
        (match, *ids, exact, limit, offset),
    )


@app.get("/v1/{source}/search")
def search(
    request: Request,
    source: str,
    q: str = Query(..., min_length=1),
    area: str | None = Query(None, description="所属行政区で絞る(同名の別地物の取り違え防止)"),
    feature: str | None = Query(None, description="地物種別で絞る。カンマ区切りで複数可"),
    bbox: str | None = Query(None, description="'min_lat,min_lon,max_lat,max_lon' で絞る"),
    tag: str | None = Query(None, description="タグ(Wikipedia のカテゴリ等)で絞る。カンマ区切りで複数可(OR)"),
    limit: int = Query(SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    offset: int = Query(0, ge=0),
):
    src = get_source(request, source)
    extra_where, extra_params = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag
    )
    # FTS 側は docs に別名 d を付けて JOIN するため、列名を修飾した版も用意する
    extra_where_d, _ = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, column_prefix="d."
    )
    match = build_match_query(q)
    # ORDER BY の「タイトル完全一致を最上位に」段へ渡す検索語
    exact = q.strip()
    if match is None:
        # trigram で扱えない短い検索語 → タイトル前方一致へフォールバック
        prefix = escape_like(exact)
        rows = db.query(
            src.path,
            "SELECT doc_id, title, substr(coalesce(opening, body), 1, 120) AS snippet,"
            " updated_at"
            f" FROM docs WHERE title LIKE ? ESCAPE '\\'{extra_where}"
            f" ORDER BY {exact_title_first()}, rank_score DESC, title LIMIT ? OFFSET ?",
            (prefix + "%", *extra_params, exact, limit, offset),
        )
        mode = "title_prefix"
    elif extra_where_d:
        # 絞り込みが付くときは候補を先に選べない(候補の中に条件を満たす文書が
        # 無いかもしれない)ので、従来どおり該当を全部見て並べる。
        rows = db.query(
            src.path,
            f"{FTS_ROW_SQL} WHERE docs_fts MATCH ?{extra_where_d}"
            f" ORDER BY {relevance_order('d.')}"
            " LIMIT ? OFFSET ?",
            (match, *extra_params, exact, limit, offset),
        )
        mode = "fts"
    else:
        rows = fts_search(src, match, exact, limit, offset)
        mode = "fts"
    return {
        "source": source,
        "query": q,
        "mode": mode,
        "results": [dict(r) for r in rows],
    }


# ---- 文書取得 ---------------------------------------------------------------


def parse_fields(
    fields: str | None,
    default: list[str] | None = None,
    allowed: list[str] | None = None,
) -> list[str]:
    default = default if default is not None else DEFAULT_DOC_FIELDS
    allowed = allowed if allowed is not None else ALLOWED_DOC_FIELDS
    if not fields:
        return default
    requested = [f.strip() for f in fields.split(",") if f.strip()]
    unknown = [f for f in requested if f not in allowed]
    if unknown:
        raise HTTPException(
            400,
            {"error": f"unknown fields: {', '.join(unknown)}", "allowed_fields": allowed},
        )
    # 返すのはユーザー入力の文字列そのものではなく、許可リスト側の文字列に引き直したもの
    # (挙動は同じ)。この戻り値は SELECT 句へ直接補間されるので、「SQL に届く文字列は
    # コード側の定数だけ」を検証ロジックの如何によらず構造で保証する
    # (CodeQL: SQL query built from user-controlled sources への手当てでもある)。
    canonical = {name: name for name in allowed}
    return [canonical[f] for f in requested]


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


def fetch_doc_candidates(
    src: Source,
    title: str,
    where: str = "",
    params: tuple = (),
    where_d: str | None = None,
) -> list:
    """同じ名前を持つ文書をすべて返す(完全一致を先頭に、残りは rank_score 降順)。

    OSM のように同名の別地物が併存するソースでは、タイトル完全一致で最初に当たった 1 件を
    返すだけだと「博多駅」で大阪のラーメン店を掴む、といった取り違えが起きる。呼び出し側で
    先頭を採用しつつ、残りを alternatives として提示できるよう候補を並べて返す。
    """
    rows = list(
        db.query(src.path, f"SELECT * FROM docs WHERE title = ?{where}", (title, *params))
    )
    seen = {r["doc_id"] for r in rows}
    alias_rows = db.query(
        src.path,
        "SELECT d.* FROM aliases a JOIN docs d ON d.doc_id = a.doc_id"
        f" WHERE a.alias = ?{where_d if where_d is not None else where}"
        " ORDER BY d.rank_score DESC LIMIT ?",
        (title, *params, DOC_CANDIDATE_LIMIT),
    )
    rows.extend(r for r in alias_rows if r["doc_id"] not in seen)
    return rows


def fetch_doc_by_title(src: Source, title: str):
    """完全一致 → aliases 解決の順で文書行を返す。見つからなければ None。"""
    rows = fetch_doc_candidates(src, title)
    return rows[0] if rows else None


def describe_candidate(src: Source, row) -> dict:
    """alternatives 用の短い説明(取り違えを見分けられる最小限の情報)。"""
    out = {"doc_id": row["doc_id"], "title": row["title"]}
    if src.schema_version >= FILTER_MIN_SCHEMA_VERSION:
        for key in ("feature", "area", "lat", "lon"):
            if row[key] is not None:
                out[key] = row[key]
    return out


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
    area: str | None = Query(None, description="所属行政区で絞る(同名の別地物の取り違え防止)"),
    feature: str | None = Query(None, description="地物種別で絞る。カンマ区切りで複数可"),
    bbox: str | None = Query(None, description="'min_lat,min_lon,max_lat,max_lon' で絞る"),
    tag: str | None = Query(None, description="タグ(Wikipedia のカテゴリ等)で絞る。カンマ区切りで複数可(OR)"),
    fields: str | None = None,
    max_chars: int = Query(0, ge=0),
):
    src = get_source(request, source)
    field_list = parse_fields(fields)
    where, params = build_attribute_filters(src, area=area, feature=feature, bbox=bbox, tag=tag)
    where_d, _ = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, column_prefix="d."
    )
    rows = fetch_doc_candidates(src, title, where, tuple(params), where_d)
    if not rows:
        raise not_found_with_candidates(src, title)
    body = doc_response(rows[0], field_list, max_chars)
    if len(rows) > 1:
        # 同名の別地物がある。黙って 1 件目を返すと取り違えに気づけないので併記する。
        body["alternatives"] = [describe_candidate(src, r) for r in rows[1:]]
    return body


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


# ---- 属性での絞り込み抽出 ---------------------------------------------------


def build_attribute_filters(
    src: Source,
    *,
    feature: str | None = None,
    area: str | None = None,
    bbox: str | None = None,
    wikidata: str | None = None,
    tag: str | None = None,
    column_prefix: str = "",
) -> tuple[str, list]:
    """属性条件を ` AND ...` の形の SQL 断片とパラメータに変換する。

    条件が 1 つも指定されなければ空文字を返すので、呼び出し側の SQL に無条件で
    連結してよい(その場合 schema_version の検査もしない = 既存 DB でも従来どおり動く)。
    """
    if not any((feature, area, bbox, wikidata, tag)):
        return "", []
    require_filter_schema(src)
    require_attributes(src, feature=feature, area=area)
    p = column_prefix
    where: list[str] = []
    params: list = []
    if tag:
        require_tag_schema(src)
        tags = split_tags(tag)
        # doc_tags を先に引いて doc_id で docs を叩く形(LIST SUBQUERY → rowid 検索)。
        # EXISTS(...) で書くと SQLite は docs 側を全走査して 1 行ずつ確認する計画を選び、
        # jawiki 規模では数百倍遅くなる(= タイムアウトする)。
        where.append(
            f"{p or 'docs.'}doc_id IN (SELECT dt.doc_id FROM doc_tags dt"
            f" WHERE dt.tag IN ({','.join('?' * len(tags))}))"
        )
        params.extend(tags)
    if feature:
        features = [f.strip() for f in feature.split(",") if f.strip()]
        where.append(f"{p}feature IN ({','.join('?' * len(features))})")
        params.extend(features)
    if area:
        where.append(f"{p}area = ?")
        params.append(area)
    if wikidata:
        where.append(f"{p}wikidata = ?")
        params.append(wikidata)
    if bbox:
        min_lat, min_lon, max_lat, max_lon = parse_bbox(bbox)
        if src.schema_version >= COORDS_MIN_SCHEMA_VERSION:
            # 実体の値を持つ doc_coords を引く。生成列(VIRTUAL)の索引だと経度の判定に
            # 行本体が要り、費用が該当件数ではなく緯度帯の文書数に比例する。
            # タグと同じ「doc_id の集合」の形。
            where.append(f"{p or 'docs.'}doc_id IN ({BBOX_DOC_IDS_SQL})")
        else:
            where.append(f"{p}lat BETWEEN ? AND ? AND {p}lon BETWEEN ? AND ?")
        params.extend([min_lat, max_lat, min_lon, max_lon])
    return "".join(f" AND {clause}" for clause in where), params


# 引数は (min_lat, max_lat, min_lon, max_lon) の順。上の params.extend と合わせてある。
# doc_coords は (lat, lon, doc_id) の被覆索引そのものなので、緯度帯の走査も経度の判定も
# 索引の中だけで終わる(docs 側で判定すると行を読み直す)。
BBOX_DOC_IDS_SQL = (
    "SELECT doc_id FROM doc_coords WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
)


def split_tags(tag: str) -> list[str]:
    """`tag=` の値(カンマ区切り、OR 条件)をタグ名のリストにする。"""
    return [t.strip() for t in tag.split(",") if t.strip()]


def build_doc_id_set(
    src: Source,
    *,
    feature: str | None = None,
    area: str | None = None,
    bbox: str | None = None,
    wikidata: str | None = None,
    tag: str | None = None,
) -> tuple[str, list] | None:
    """絞り込み条件を「doc_id を返す SELECT」に変換する(/filter 用)。行本体を読まない。

    どの条件も、値を実体で持つ表か索引だけで doc_id の集合に落とせる:

    - `tag` → doc_tags(idx_doc_tags_tag が (tag, doc_id) の被覆索引)
    - `bbox` → doc_coords(実体の lat/lon を持つ表。生成列の索引では経度の判定に
      行本体が要る。schema_version 4 から)
    - `feature` / `area` → idx_docs_feature_area / idx_docs_area_feature。
      生成列でも**索引には計算済みの値が入っている**ので、doc_id だけを取り出す
      分にはここも索引の中で完結する(2 つを 1 本の複合索引で捌けるので分けない)
    - `wikidata` → idx_docs_wikidata

    複数指定は INTERSECT で交差させる。ここが要点で、`doc_id IN (座標の集合) AND
    feature IN (...)` と書くと SQLite は片側の索引だけで駆動して**もう片方の判定に
    行本体を読む**(全国の amenity=restaurant 10 万行を読んでいた: 手元で 0.94 秒、
    配信機で 5 秒前後 = 504)。INTERSECT なら両側とも索引の中で終わり、行を読むのは
    交差した分だけになる(同じ条件で 0.05 秒)。

    索引だけで引けない組み合わせ(古い schema_version)では None を返す。呼び出し側は
    従来の WHERE 句(build_attribute_filters)へ落ちる。
    """
    if not any((feature, area, bbox, wikidata, tag)):
        return None
    if src.schema_version < FILTER_MIN_SCHEMA_VERSION:
        return None
    # 持っていない属性で絞ろうとしていないか(0 件ではなく理由を返す)。
    # ここは /filter の経路で、search / doc は build_attribute_filters 側で同じ検査をする。
    require_attributes(src, feature=feature, area=area)
    parts: list[str] = []
    params: list = []
    if tag:
        if src.schema_version < TAG_MIN_SCHEMA_VERSION:
            return None
        tags = split_tags(tag)
        # 1 文書が指定タグを 2 つ持てば 2 行出るので、複数指定のときだけ畳む
        # (総件数を数えるのに効く。単一タグなら重複しないので並べ替えを足さない)。
        distinct = "DISTINCT " if len(tags) > 1 else ""
        parts.append(
            f"SELECT {distinct}doc_id FROM doc_tags WHERE tag IN ({','.join('?' * len(tags))})"
        )
        params.extend(tags)
    if bbox:
        if src.schema_version < COORDS_MIN_SCHEMA_VERSION:
            return None
        min_lat, min_lon, max_lat, max_lon = parse_bbox(bbox)
        parts.append(BBOX_DOC_IDS_SQL)
        params.extend([min_lat, max_lat, min_lon, max_lon])
    if feature or area:
        where: list[str] = []
        if feature:
            features = [f.strip() for f in feature.split(",") if f.strip()]
            where.append(f"feature IN ({','.join('?' * len(features))})")
            params.extend(features)
        if area:
            where.append("area = ?")
            params.append(area)
        # 索引は先頭の列で絞れないと使えないので、feature の有無で名指しを変える
        index = "idx_docs_feature_area" if feature else "idx_docs_area_feature"
        parts.append(
            f"SELECT doc_id FROM docs INDEXED BY {index} WHERE {' AND '.join(where)}"
        )
    if wikidata:
        parts.append("SELECT doc_id FROM docs INDEXED BY idx_docs_wikidata WHERE wikidata = ?")
        params.append(wikidata)
    return " INTERSECT ".join(parts), params


# docs の行を 1 件読む費用は、idx_docs_rank を 1 件走る費用の何倍か(下の
# rank_index_hint の経路選択に使う)。配信機での実測から: 該当 25 万件を docs から
# 読むと 33 秒(= 1 行 132µs)、索引側は 336 件のタグの頁送りの伸び(offset=250 で
# 0.665 秒)から 1 件 3µs 前後なので 40 倍ほど。行の太さで変わる値(jawiki は
# 1 行 27KB、osm は 1.4KB)なので、境目付近はどちらの経路でも同程度の費用になる。
# 迷ったら行読み側に倒す: そちらは費用が total で頭打ちになり、頁の深さで破綻しない。
DOC_ROW_VS_INDEX_COST = 32


def rank_index_hint(src: Source, total: int, need: int) -> str:
    """`ORDER BY rank_score DESC, title` を索引で満たさせる INDEXED BY 句(安い方を選ぶ)。

    条件が「doc_id の集合」に落ちているとき(= build_doc_id_set が組めたとき)、
    並べ替えには 2 つの経路がある。費用の形が違うので、どちらが安いかは条件によって
    ひっくり返る:

    - 既定(名指し無し): 該当文書を**全部** docs から読んで並べ替える。
      費用 ≒ `total` 行の読み出し。**offset には依らない**。
    - `INDEXED BY idx_docs_rank`: 並び順そのものを持つ索引を上から走査し、
      該当を `need` 件(= offset + limit)拾った時点で打ち切る(doc_id の判定は
      ブルームフィルタで索引の中だけで済む)。該当が索引に一様に散らばっていれば
      費用 ≒ `doc_count * need / total` 件の走査。**total には依らず、深い頁ほど伸びる**。

    後者は「上位数件だけ見る」ときは桁違いに速い一方、頁が末尾に近づくと索引を端まで
    舐めることになる。実際、この判定に offset が入っていなかったために、336 件のタグの
    末尾(offset=300)や 131 件のタグの全件取得(limit=131)が 150 万件の全走査に落ちて
    配信機で 504 になっていた(offset=250 までは 0.665 秒で返っていた)。
    総件数だけで切り替えると、この「浅い頁は速いが末尾で破綻する」形を直せない。
    """
    if src.schema_version < RANK_INDEX_MIN_SCHEMA_VERSION:
        return ""  # 索引の無い DB に INDEXED BY を書くとエラーになる
    if total <= 0:
        return ""
    # 一様分布での期待走査件数(該当を need 件拾うまでに読む索引の件数)。
    # need が total 以上なら索引の端まで走ることが確定するので doc_count で頭打ち。
    scan = src.doc_count * min(1.0, need / total)
    if scan >= DOC_ROW_VS_INDEX_COST * total:
        return ""  # 素直に docs を読んで並べ替えた方が安い
    return " INDEXED BY idx_docs_rank"


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            400, {"error": "bbox must be 'min_lat,min_lon,max_lat,max_lon'"}
        )
    try:
        min_lat, min_lon, max_lat, max_lon = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(400, {"error": f"bbox is not numeric: {bbox}"}) from None
    if min_lat > max_lat or min_lon > max_lon:
        raise HTTPException(400, {"error": "bbox min must not exceed max"})
    return min_lat, min_lon, max_lat, max_lon


@app.get("/v1/{source}/filter")
def filter_docs(
    request: Request,
    source: str,
    feature: str | None = Query(
        None, description="地物種別。'amenity=place_of_worship' 形式。カンマ区切りで複数指定可"
    ),
    area: str | None = Query(None, description="所属する行政区名(OSM ソースでは都道府県相当)"),
    bbox: str | None = Query(None, description="'min_lat,min_lon,max_lat,max_lon'"),
    wikidata: str | None = Query(None, description="wikidata の Q 番号(逆引き)"),
    tag: str | None = Query(None, description="タグ(Wikipedia のカテゴリ等)。カンマ区切りで複数可(OR)"),
    fields: str | None = None,
    limit: int = Query(FILTER_LIMIT_DEFAULT, ge=1, le=FILTER_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    max_chars: int = Query(0, ge=0),
):
    """属性で文書を絞り込み一括で列挙する(全文検索ではなく等価・範囲条件)。

    用途は「京都府の寺社を全件」「カテゴリ:ラーメン店の記事を全件」のような機械的な
    抽出と、wikidata の Q 番号から記事を引く逆引き。総件数 total を返すので
    offset でページングできる。
    """
    src = get_source(request, source)
    require_filter_schema(src)
    if tag:
        require_tag_schema(src)
    field_list = parse_fields(fields, FILTER_DEFAULT_FIELDS, FILTER_ALLOWED_FIELDS)
    if not any((feature, area, bbox, wikidata, tag)):
        raise HTTPException(
            400,
            {"error": "at least one of feature, area, bbox, wikidata, tag is required"},
        )

    id_set = build_doc_id_set(
        src, feature=feature, area=area, bbox=bbox, wikidata=wikidata, tag=tag
    )
    if id_set is not None:
        # 索引だけで doc_id の集合に落ちた場合。総件数はその集合を数えるだけで済み
        # (docs を 1 行も読まない)、行を読むのは並べ替えの経路が決めた分だけになる。
        set_sql, params = id_set
        (total,) = db.query(src.path, f"SELECT COUNT(*) AS n FROM ({set_sql})", tuple(params))[0]
        clause = f"doc_id IN ({set_sql})"
        hint = rank_index_hint(src, total, offset + limit)
    else:
        # 古い schema_version の DB 向けの旧経路(索引が足りず docs 側で判定する)。
        where, params = build_attribute_filters(
            src, feature=feature, area=area, bbox=bbox, wikidata=wikidata, tag=tag
        )
        clause = where.removeprefix(" AND ")
        (total,) = db.query(
            src.path, f"SELECT COUNT(*) AS n FROM docs WHERE {clause}", tuple(params)
        )[0]
        hint = ""
    rows = db.query(
        src.path,
        f"SELECT {', '.join(field_list)} FROM docs{hint}"
        f" WHERE {clause} ORDER BY rank_score DESC, title LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "source": source,
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [doc_response(r, field_list, max_chars) for r in rows],
    }


# ---- タグ一覧 ---------------------------------------------------------------

TAGS_LIMIT_DEFAULT = 50
TAGS_LIMIT_MAX = 500


@app.get("/v1/{source}/tags")
def list_tags(
    request: Request,
    source: str,
    prefix: str | None = Query(None, description="タグ名の前方一致(索引が効く)"),
    contains: str | None = Query(None, description="タグ名の部分一致(索引が効かず遅い)"),
    limit: int = Query(TAGS_LIMIT_DEFAULT, ge=1, le=TAGS_LIMIT_MAX),
    offset: int = Query(0, ge=0),
):
    """タグ名を文書数つきで列挙する(`filter?tag=` に渡す正確な名前を探すため)。

    Wikipedia のカテゴリ名は表記の揺れが多く(「ラーメン店」「日本のラーメン店」…)、
    当てずっぽうで `filter?tag=` を叩くと 0 件が返るだけで、名前が違うのか本当に
    無いのかが分からない。ここで実在するタグ名を先に確かめられるようにしておく。
    """
    src = get_source(request, source)
    require_tag_schema(src)
    where = ""
    params: list = []
    if prefix:
        # 前方一致は索引の範囲検索に落ちる(db.py の case_sensitive_like=ON)
        where += " WHERE tag LIKE ? ESCAPE '\\'"
        params.append(escape_like(prefix) + "%")
    if contains:
        where += (" AND" if where else " WHERE") + " tag LIKE ? ESCAPE '\\'"
        params.append("%" + escape_like(contains) + "%")
    if src.schema_version >= TAG_COUNTS_MIN_SCHEMA_VERSION:
        # 集計済みのタグ名だけを読む(jawiki で 29 万行・12MB)。部分一致でも
        # idx_tag_counts_docs が docs 降順なので、上位 limit 件が埋まった時点で
        # 走査が止まる。
        sql = f"SELECT tag, docs FROM tag_counts{where} ORDER BY docs DESC, tag LIMIT ? OFFSET ?"
    else:
        # tag_counts が無い schema_version 3 の DB 向けの旧経路。転置表を丸ごと
        # 読むので、巨大ソースの部分一致はタイムアウトしうる(scripts/add_tag_index.py で移行する)。
        sql = (
            f"SELECT tag, COUNT(*) AS docs FROM doc_tags{where}"
            " GROUP BY tag ORDER BY docs DESC, tag LIMIT ? OFFSET ?"
        )
    rows = db.query(src.path, sql, (*params, limit, offset))
    return {
        "source": source,
        "prefix": prefix,
        "contains": contains,
        "tags": [dict(r) for r in rows],
    }


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
            400, {"error": "only direction=out is supported"}
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


# ---- notes(短期記憶。唯一書き込めるソース) ----------------------------------
#
# 実体は app/notes.py。ここは HTTP の口だけを持つ。`CHIEZO_NOTES_DIR` 未設定なら 503。
# 読み出しは専用の recall のほかに、コアスキーマなので /v1/notes/search・doc・filter・
# tags・/notes/ のブラウズ画面もそのまま効く(ソース種別を意識しない設計のおかげ)。


def _refresh_notes_count(request: Request) -> None:
    """ソース表の doc_count を追記のたびに直す。

    doc_count は走査時に数えた値で、走査は /data の変化でしか走らない(notes は別
    ディレクトリなので指紋に入らない)。放っておくと /v1/sources と管理画面の件数が
    増えないままになるので、書いた側で直す。
    """
    src = request.app.state.sources.get(notes.SOURCE_NAME)
    if src is None:
        return
    try:
        (count,) = db.query(src.path, "SELECT COUNT(*) FROM docs")[0]
        src.doc_count = count
    except Exception:
        log.debug("could not refresh notes doc_count", exc_info=True)


@app.post("/v1/notes")
def remember(
    request: Request,
    text: str = Body(..., embed=True, min_length=1, description="覚えておく内容"),
    title: str | None = Body(None, embed=True, description="省略時は本文の 1 行目から作る"),
    tags: str | None = Body(None, embed=True, description="カンマ区切り"),
):
    created = notes.add(text=text, title=title, tags=tags)
    # 作られたばかり(初回の追記)ならソースとして登録し直す
    if notes.SOURCE_NAME not in request.app.state.sources:
        request.app.state.sources = scan_all(request.app.state.data_dir)
    _refresh_notes_count(request)
    return created


@app.get("/v1/notes/recall")
def recall_notes(
    request: Request,
    q: str | None = Query(None, description="全文検索。省略すると時系列だけで引く"),
    since: str | None = Query(None, description="この日時以降(例 2026-07-31)"),
    until: str | None = Query(None, description="この日時以前"),
    tag: str | None = Query(None, description="タグで絞る。カンマ区切りで AND"),
    limit: int = Query(notes.RECALL_LIMIT_DEFAULT, ge=1, le=notes.RECALL_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    fields: str | None = Query(
        None, description=f"返す項目。カンマ区切り。省略時は全部({','.join(notes.RECALL_FIELDS)})"
    ),
    max_chars: int = Query(
        notes.RECALL_MAX_CHARS_DEFAULT,
        ge=0,
        description="本文の頭から返す文字数。切ったら truncated が立つ。0 で切らない",
    ),
):
    return notes.recall(
        q=q, since=since, until=until, tag=tag, limit=limit, offset=offset,
        fields=fields, max_chars=max_chars,
    )


@app.patch("/v1/notes/{doc_id}")
def update_note(
    request: Request,
    doc_id: int,
    text: str | None = Body(None, embed=True, description="本文を差し替える。省略は今のまま"),
    title: str | None = Body(None, embed=True, description="見出しを差し替える。省略は今のまま"),
    tags: str | None = Body(
        None, embed=True, description="カンマ区切りで丸ごと置き換え。空文字で全部外す。省略は今のまま"
    ),
):
    updated = notes.update(doc_id, text=text, title=title, tags=tags)
    if updated is None:
        raise HTTPException(404, {"error": f"note not found: doc_id={doc_id}"})
    return updated


@app.delete("/v1/notes/{doc_id}")
def forget(request: Request, doc_id: int):
    if not notes.delete(doc_id):
        raise HTTPException(404, {"error": f"note not found: doc_id={doc_id}"})
    _refresh_notes_count(request)
    return {"deleted": doc_id}


# ---- 使う(ローカル LLM。既定では無効) ---------------------------------------
#
# パイプラインの実体は app/answer.py。ここは HTTP の口(JSON / SSE / HTML)だけを持つ。
# `CHIEZO_LLM_URL` が未設定なら丸ごと無効で、503 と有効化の案内を返す。


# 会話画面のパス。画面の中のリンクと JS の両方が参照する。


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_response(events) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        # リバースプロキシに溜め込まれるとストリーミングの意味が無くなる
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/ask")
async def ask(
    request: Request,
    q: str = Query(..., min_length=1, description="質問文(自然文でよい)"),
    source: str | None = Query(None, description="引くソースを固定する(省略時は LLM が選ぶ)"),
    stream: bool = Query(False, description="1 なら SSE で回答を流す"),
    grounded: bool | None = Query(
        None,
        description="1 は Chiezo で取れたことだけを根拠にする。0 なら足りない分をモデルの知識で補う"
                    "(既定は CHIEZO_ASK_DEFAULT_GROUNDED、無指定なら 1)",
    ),
    mode: str | None = Query(
        None,
        pattern="^(rag|agent)$",
        description="rag は search を 1 回。agent は LLM 自身に道具を引かせる"
                    "(ツール呼び出しが安定するモデルが要る。既定は CHIEZO_ASK_DEFAULT_MODE)",
    ),
    web: bool | None = Query(
        None,
        description="agent モードで web 検索の道具を渡すか。既定はサーバー設定どおり"
                    "(CHIEZO_WEB_SEARCH_URL が未設定なら、頼まれても使えない)",
    ),
    notes_ok: bool | None = Query(
        None, alias="notes",
        description="agent モードで「覚える・思い出す」の道具を渡すか。既定はサーバー設定どおり"
                    "(CHIEZO_NOTES_DIR が未設定なら、頼まれても使えない)",
    ),
    backend: str | None = Query(
        None,
        description="どの AI に聞くか(CHIEZO_LLM_<名前>_URL で足した相手の名前)。"
                    "省略すると CHIEZO_LLM_URL の相手",
    ),
    model: str | None = Query(None, description="どのモデルを使うか(省略時はその相手の既定)"),
    effort: str | None = Query(None, description="どれだけ考えさせるか(相手が持っていれば)"),
):
    cfg = await answer.ensure_model(answer.require_settings(backend, model, effort))
    # 既定は環境変数で決める(GPU + 8B の環境と、CPU だけの環境で妥当な既定が違うため)。
    mode = answer.resolve_mode(backend, mode)
    grounded = answer.default_grounded() if grounded is None else grounded
    if mode == "agent":
        if not stream:
            return await agent.answer_question(
                cfg, request, q, source, grounded, None, web, notes_ok
            )
        # 流し始める前に済ませられる検査はここで(SSE はヘッダ送出後に
        # ステータスコードを変えられない)。残りの失敗は error イベントになる。
        agent.prepare_catalog(request, source)
        return _sse_response(
            _agent_events(cfg, request, q, source, grounded, None, web, notes_ok)
        )
    if not stream:
        return await answer.answer(cfg, request, q, source, grounded)

    # ストリーミングはヘッダを送った後でステータスを変えられないので、
    # 失敗しうる段(クエリ生成・検索)はここで済ませてから流し始める。
    queries, snippets, references = await answer.prepare(cfg, request, q, source)
    return _sse_response(_rag_events(cfg, q, queries, snippets, references, grounded))


async def _agent_events(
    cfg, request: Request, q: str, source: str | None, grounded: bool,
    history: list[dict] | None = None, web: bool | None = None,
    notes_ok: bool | None = None,
):
    """agent モードの SSE。

    rag と違い**流し始めた後にしかできない仕事**が本体(道具を引くこと自体が目的で、
    それが数十秒かかる)。ソースの検査だけは呼び出し側が先に済ませてあり、
    残りの失敗(推論サーバに繋がらない等)は error イベントとして流す。
    """
    events = agent.stream(cfg, request, q, source, grounded, history, web, notes_ok)
    yield _sse("meta", {
        "mode": "agent", "grounded": grounded, "model": cfg.model,
        "web": agent.web_allowed(web), "notes": agent.notes_allowed(notes_ok),
    })
    try:
        async for event, data in events:
            yield _sse(event, data)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
        yield _sse("error", detail)
    yield _sse("done", {})


# ---- 会話(/v1/chat) --------------------------------------------------------
#
# `/v1/ask` は 1 問 1 答で、curl から使うぶんにはそれでよい。会話として続けるには
# 直前のやり取りが要るので、こちらは **messages をまるごと受け取る**。
# **サーバーは会話の状態を持たない**(履歴はクライアントが持って毎回送る)。読み取り専用・
# LAN 内・複数ワーカーという前提を崩さないためで、MCP をステートレスにしたのと同じ判断。


class ChatMessage(BaseModel):
    role: str = PydField(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    # 末尾が今回の発言、それより前が履歴。空や assistant で終わる列は 400。
    messages: list[ChatMessage]
    source: str | None = None
    grounded: bool | None = None
    mode: str | None = PydField(default=None, pattern="^(rag|agent)$")
    # agent モードで web 検索の道具を渡すか(None = サーバー設定どおり)。
    # 画面のトグルはここを毎回送る = やり取りごとに切り替えられる。
    web: bool | None = None
    # 「覚える・思い出す」の道具を渡すか。**書き込みを伴う**ので、同じく切れるようにする。
    notes: bool | None = None
    # どの AI に聞くか。画面のセレクトはここを毎回送る = やり取りごとに相手を変えられる。
    backend: str | None = None
    # どのモデルを使うか。同じく毎回送るので、会話の途中でも切り替えられる。
    model: str | None = None
    # どれだけ考えさせるか。相手が持っていなければ無視される。
    effort: str | None = None


def _split_history(body: ChatRequest) -> tuple[str, list[dict]]:
    turns = [m.model_dump() for m in body.messages if (m.content or "").strip()]
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(400, {"error": "messages must end with a user message"})
    return turns[-1]["content"], turns[:-1]


@app.post("/v1/chat")
async def chat(request: Request, body: ChatRequest, stream: bool = Query(False)):
    cfg = await answer.ensure_model(answer.require_settings(body.backend, body.model, body.effort))
    question, history = _split_history(body)
    mode = answer.resolve_mode(body.backend, body.mode)
    grounded = answer.default_grounded() if body.grounded is None else body.grounded
    if mode == "agent":
        if not stream:
            return await agent.answer_question(
                cfg, request, question, body.source, grounded, history, body.web, body.notes
            )
        agent.prepare_catalog(request, body.source)
        return _sse_response(
            _agent_events(
                cfg, request, question, body.source, grounded, history, body.web, body.notes
            )
        )
    if not stream:
        return await answer.answer(cfg, request, question, body.source, grounded, history)
    queries, snippets, references = await answer.prepare(
        cfg, request, question, body.source, history
    )
    return _sse_response(
        _rag_events(cfg, question, queries, snippets, references, grounded, history)
    )


async def _rag_events(cfg, q, queries, snippets, references, grounded, history=None):
    """rag モードの SSE(/v1/ask と /v1/chat で共通)。"""
    yield _sse(
        "references",
        {
            "references": references, "queries": queries,
            "grounded": grounded, "model": cfg.model,
        },
    )
    try:
        async for delta in answer.stream_answer(cfg, q, snippets, grounded, history):
            yield _sse("delta", {"text": delta})
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
        yield _sse("error", detail)
    yield _sse("done", {})





# ---- 素の問い合わせ(知識ベースを介さない)------------------------------------
#
# **`/v1/chat` とは目的が違う。** あちらは知識ベースを引いて答えるための口で、必ず抽出が
# 混ざる。こちらは**渡したプロンプトをそのまま相手に投げる**だけ —— 呼び出す側が自分の
# 材料とプロンプトを持っていて、Chiezo に借りたいのは「話せる相手と鍵」だけ、という使い方
# (例: tech-antenna のサマリー生成)。認証情報は相手ごとに Chiezo が握っているので、
# 呼ぶ側は鍵を持たずに済み、管理画面で on にした相手をそのまま使える。


class AiMessage(BaseModel):
    # `/v1/chat` の ChatMessage と違い **system を許す**。プロンプトを組むのは呼ぶ側で、
    # 役割の付け方までこちらで決めない
    role: str = PydField(pattern="^(system|user|assistant)$")
    content: str


class AiCompleteRequest(BaseModel):
    messages: list[AiMessage]
    # どの相手に投げるか。空なら「先頭の相手」(`/v1/chat` と同じ規則)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None


@app.get("/v1/ai/backends")
async def ai_backends() -> dict:
    """いま話せる相手と、その相手で選べるモデル・エフォート。

    **呼ぶ側が画面を作れるだけの材料を返す。** 一覧は管理画面で on にしたものだけで、
    モデルは相手に聞けた場合はその答え(聞けなければコードの控え)。
    """
    names = answer.backend_names()
    models = await asyncio.gather(*(answer.available_models(name) for name in names))

    return {
        "backends": [
            {
                "id": name,
                "label": answer.backend_label(name),
                "models": list(available),
                "efforts": list(providers.efforts_of(name)),
                # モデルを必ず指定しないといけない相手か(false なら「既定」を選べる)
                "model_required": bool(spec.model_required) if spec else True,
            }
            for name, available in zip(names, models, strict=True)
            for spec in (providers.get(name),)
        ]
    }


@app.post("/v1/ai/complete")
async def ai_complete(body: AiCompleteRequest) -> dict:
    """渡されたメッセージをそのまま相手へ投げて、本文を返す(1 往復・道具なし)。"""
    messages = [m.model_dump() for m in body.messages if (m.content or "").strip()]
    if not messages:
        raise HTTPException(400, {"error": "messages must not be empty"})

    cfg = await answer.ensure_model(answer.require_settings(body.backend, body.model, body.effort))
    message = await answer.complete_message(cfg, messages)
    content = answer.content_of(message)
    if not content:
        raise HTTPException(502, {"error": "empty response from llm"})

    return {
        "backend": cfg.name,
        "label": answer.backend_label(cfg.name),
        # 実際に使われたモデル。呼ぶ側が「どれが書いたか」を残せるようにする
        "model": cfg.model,
        "effort": cfg.effort,
        "content": content,
    }


# ---- 画像の生成(ゲーム素材などを作る)---------------------------------------
#
# **知識を引くのとは別の仕事**だが、口は Chiezo にまとめてある —— クライアント(MCP)の
# 登録先を増やしたくないため。重い処理は例によって別コンテナ(ComfyUI)で、
# 外部サービス(Gemini)と選べる。実体は `app/media.py` / `app/media_backends.py`。


class ImageRequest(BaseModel):
    prompt: str
    # 相手。空なら既定(自前の GPU)
    backend: str | None = None
    model: str | None = None
    size: str = "1024x1024"
    # 0 なら毎回振り直す。**同じ絵を作り直したいときに指定する**
    seed: int = 0
    count: int = 1
    negative: str = ""
    steps: int = 25


@app.get("/v1/media/backends")
async def media_backends_list() -> dict:
    """絵を頼める相手と、その相手で選べるモデル・サイズ。"""
    return {"backends": await media.backends(), "enabled": media.is_enabled()}


@app.post("/v1/media/image")
async def media_image(body: ImageRequest) -> dict:
    """描き始めて job を返す(**待たない**)。進み具合は下の口で引く。"""
    return media.start_image_job(
        prompt=body.prompt,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        size=body.size,
        seed=body.seed,
        count=body.count,
        negative=body.negative,
        steps=body.steps,
    )


@app.get("/v1/media/jobs/{job_id}")
async def media_job(job_id: str) -> dict:
    job = media.get_job(job_id)
    if job is None:
        raise HTTPException(404, {"error": f"unknown job: {job_id}"})
    return job


@app.get("/v1/media/jobs")
async def media_jobs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"jobs": media.recent_jobs(limit)}


@app.get("/media/{path:path}", include_in_schema=False)
async def media_file(path: str):
    """出来た画像を配る。**置き場の外は返さない**(`../` を踏ませない)。"""
    return FileResponse(media.resolve(path))


# ---- 画面(人間向け HTML)-----------------------------------------------------
#
# 実体は app/views/ に分けてある。REST(この上)と画面は変更の理由が別で、
# 同じファイルに置くと管理画面の HTML だけで 700 行を占めるため。
# **import はここ(定義の後ろ)で行う** —— views は app/deps.py から共有の
# 下ごしらえを取るので main を import しない設計だが、登録の位置は末尾に揃える。

app.include_router(views_admin.router)
app.include_router(views_ai_settings.router)
app.include_router(views_browse.router)
app.include_router(views_chat.router)


# ---- MCP(Streamable HTTP) ---------------------------------------------------


async def _mcp_asgi(scope, receive, send):
    """/mcp を lifespan が用意した MCP アプリへ委譲する。

    実体を起動ごとに作り直す(上の lifespan 参照)ため、マウント時点では実体が無い。
    パスの前置き除去は Starlette の Mount 側が済ませてから呼ぶので、ここは素通しでよい。
    """
    inner = getattr(app.state, "mcp_asgi", None)
    if inner is None:  # lifespan を通らずに呼ばれた場合(通常は起こらない)
        raise RuntimeError("MCP app is not initialized (lifespan did not run)")
    await inner(scope, receive, send)


# ツールの実体は上のエンドポイント関数そのもの(app/mcp_server.py 参照)。
app.mount("/mcp", _mcp_asgi)
