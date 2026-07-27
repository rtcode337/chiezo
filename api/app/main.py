"""chiezo-api ルーティング(設計書 §5)。"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from app import claude_config, db
from app.fts import build_match_query, escape_like
from app.mcp_server import build_mcp
from app.known_sources import CONTINENT_LABELS, KNOWN_SOURCES, WIKIPEDIA_TIERS
from app.pages import esc, page_shell
from app.registry import (
    FILTER_MIN_SCHEMA_VERSION,
    TAG_MIN_SCHEMA_VERSION,
    Source,
    scan_sources,
)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("CHIEZO_DATA_DIR", "/data"))
    app.state.sources = scan_sources(data_dir)
    if not app.state.sources:
        log.warning("no sources registered from %s", data_dir)
    # MCP(/mcp)はここで組み立てて起動する。理由が 2 つある:
    #  1. セッションマネージャは lifespan の中で run() しないとタスクグループが張られず、
    #     最初のリクエストで "Task group is not initialized" になる(python-sdk#1367)。
    #  2. その run() は 1 インスタンスにつき 1 回しか呼べない。モジュール読み込み時に
    #     作り置きすると、同一プロセスでアプリを二度起動したとき(テストや再入する
    #     ホスティング)に RuntimeError で落ちる。なので起動ごとに作り直す。
    # マウント先(下の _mcp_asgi)はここで置いた app.state.mcp_asgi を見に行く。
    mcp = build_mcp(app)
    app.state.mcp_asgi = mcp.streamable_http_app()
    async with mcp.session_manager.run():
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


# chiezo-trigger のソースカタログのプロセス内キャッシュ。中身は trigger のイメージに
# 焼かれた静的な表(osm_<国> だけで 195 件)なので、一度取れたら取り直す必要はない。
_catalog_cache: dict[str, dict] | None = None
# 取得に失敗した時刻(単調時計)。trigger が落ちている間、管理画面を開くたびに
# タイムアウト待ちを重ねない(ジョブ状況の取得と合わせて毎回 10 秒待たされるため)。
_catalog_failed_at: float | None = None
CATALOG_RETRY_SECONDS = 60.0


def _fetch_trigger_catalog() -> dict[str, dict] | None:
    """初期化できるソースの一覧を chiezo-trigger から取る。取れなければ None。"""
    global _catalog_cache, _catalog_failed_at
    if _catalog_cache is not None:
        return _catalog_cache
    if not TRIGGER_URL:
        return None
    if _catalog_failed_at and time.monotonic() - _catalog_failed_at < CATALOG_RETRY_SECONDS:
        return None
    try:
        res = httpx.get(f"{TRIGGER_URL}/sources", timeout=TRIGGER_TIMEOUT)
        res.raise_for_status()
        catalog = res.json()["sources"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("chiezo-trigger source catalog unreachable: %s", e)
        _catalog_failed_at = time.monotonic()
        return None
    _catalog_cache = catalog
    return catalog


def initializable_sources() -> dict[str, dict]:
    """初期化できるソース名 → 表示用メタ。

    正は ingest 側(`ADAPTERS`)で、それを chiezo-trigger の `GET /sources` 経由で受け取る。
    trigger が未設定・到達不能なときだけ、静的な `KNOWN_SOURCES` で代替する。
    """
    return _fetch_trigger_catalog() or KNOWN_SOURCES


def _format_bytes(size: int | None) -> str:
    if not size:
        return ""
    for unit, scale in (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def _memory_hint(meta: dict) -> str:
    """必要メモリの目安。ディスク索引が既定の国は 2GiB で焼ける代わりに遅い。"""
    memory_gb = meta.get("memory_gb") or 0
    if (meta.get("node_index") or "").endswith("file_array"):
        return f"2 GiB(ディスク索引・低速。RAM 索引なら {memory_gb:.0f} GiB)"
    return f"{memory_gb:.0f} GiB" if memory_gb else ""


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
        name: meta for name, meta in initializable_sources().items() if name not in sources
    }
    job = _fetch_trigger_status()
    job_running = bool(job and job.get("state") == "running")
    disabled = " disabled" if not TRIGGER_URL or job_running else ""
    # osm_<国> 195 件・<lang>wiki 348 件はここには 1 行ずつだけ出し、
    # 国・言語の選択は /admin/osm・/admin/wikipedia に分ける
    osm_pending = {n: m for n, m in uninitialized.items() if m.get("group") == "osm"}
    wikipedia_pending = {
        n: m for n, m in uninitialized.items() if m.get("group") == "wikipedia"
    }
    rows_source = {
        n: m for n, m in uninitialized.items()
        if m.get("group") not in ("osm", "wikipedia")
    }
    init_rows = "\n".join(
        f"<tr>"
        f"<td>{esc(name)}</td>"
        f"<td>{esc(meta.get('kind', ''))}</td>"
        f"<td>{esc(meta.get('lang', ''))}</td>"
        f"<td>"
        f'<form class="init-form" method="post" action="/admin/init/{esc(name)}">'
        f'<button type="submit"{disabled}>初期化</button>'
        f"</form>"
        f"</td>"
        f"</tr>"
        for name, meta in sorted(rows_source.items())
    )
    if wikipedia_pending:
        init_rows += (
            f"<tr>"
            f"<td>wikipedia</td>"
            f"<td>wikipedia</td>"
            f"<td>言語ごと</td>"
            f'<td><a href="/admin/wikipedia">言語を選ぶ({len(wikipedia_pending)} 件が未初期化)</a></td>'
            f"</tr>"
        )
    if osm_pending:
        init_rows += (
            f"<tr>"
            f"<td>osm</td>"
            f"<td>osm</td>"
            f"<td>国ごと</td>"
            f'<td><a href="/admin/osm">国を選ぶ({len(osm_pending)} 件が未初期化)</a></td>'
            f"</tr>"
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

{_job_status_html(job)}

<h2>未初期化データの初期化</h2>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th></th></tr>
</thead>
<tbody>
{init_rows}
</tbody>
</table>

<h2>Claude Code 連携設定</h2>
<p class="muted">
いま設定を吐き出したら(<code>scripts/gen_claude_config.sh</code>)どういう内容になるかのプレビュー。
現在の登録ソースから生成した CLAUDE.md ブロックを表示する(実ファイルは書き換えない)。
</p>
<p><a href="/admin/claude-config">→ 生成される設定を見る</a></p>
"""
    return HTMLResponse(content=page_shell("chiezo 管理画面", body, refresh=5 if job_running else None))


@app.get("/admin/osm", response_class=HTMLResponse)
def admin_osm(request: Request, q: str | None = Query(None, description="国名・region での絞り込み")):
    """OSM 国別ソースの初期化画面(管理画面の osm 行の「国を選ぶ」から開く)。

    Geofabrik の国別抽出は 195 件あり、管理画面の一覧に全部並べると他のソースが埋もれる。
    そこで一覧では osm 1 行にまとめ、国の選択だけをこの画面に切り出している。
    """
    sources: dict[str, Source] = request.app.state.sources
    catalog = {n: m for n, m in initializable_sources().items() if m.get("group") == "osm"}
    job = _fetch_trigger_status()
    job_running = bool(job and job.get("state") == "running")
    disabled = " disabled" if not TRIGGER_URL or job_running else ""

    total = len(catalog)
    needle = (q or "").strip().lower()
    if needle:
        catalog = {
            n: m for n, m in catalog.items()
            if needle in " ".join(
                str(m.get(k, "")) for k in ("label", "label_en", "slug", "region")
            ).lower()
            or needle in n.lower()
        }

    groups: dict[str, list[tuple[str, dict]]] = {}
    for name, meta in catalog.items():
        groups.setdefault(meta.get("continent", "standalone"), []).append((name, meta))

    order = [c for c in CONTINENT_LABELS if c in groups] + [
        c for c in sorted(groups) if c not in CONTINENT_LABELS
    ]
    blocks = []
    for continent in order:
        entries = sorted(groups[continent], key=lambda kv: kv[1].get("label") or kv[0])
        rows = []
        for name, meta in entries:
            src = sources.get(name)
            if src is not None:
                action = (
                    f'初期化済み(<a href="/{esc(name)}/">{src.doc_count:,} 件</a>)'
                )
            else:
                action = (
                    f'<form class="init-form" method="post" action="/admin/init/{esc(name)}">'
                    f'<button type="submit"{disabled}>初期化</button></form>'
                )
            rows.append(
                f"<tr>"
                f"<td>{esc(meta.get('label') or name)}"
                f'<div class="muted">{esc(name)}</div></td>'
                f"<td>{esc(meta.get('region', ''))}</td>"
                f"<td>{esc(_format_bytes(meta.get('pbf_bytes')))}</td>"
                f"<td>{esc(_memory_hint(meta))}</td>"
                f"<td>{action}</td>"
                f"</tr>"
            )
        blocks.append(
            f"<details{' open' if needle else ''}>"
            f"<summary>{esc(CONTINENT_LABELS.get(continent, continent))}"
            f"({len(entries)})</summary>"
            "<table><thead><tr><th>国・地域</th><th>region</th><th>pbf</th>"
            "<th>必要メモリの目安</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            "</details>"
        )
    if not blocks:
        blocks = ["<p>該当する国・地域がありません。</p>"]

    body = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>OSM(国別)の初期化</h1>
<p class="muted">
Geofabrik の国別抽出 {total} 件{f"(絞り込み: {len(catalog)} 件)" if needle else ""}から 1 か国ずつ取り込みます。
店舗・営業時間まで要る国だけを個別に足す使い方を想定しています
(全世界のざっくりした地名は geonames が 1 ソースで賄います)。
取り込みは同時に 1 件のみ・数時間かかります。必要メモリが足りない場合は開始前に中止されます。
</p>
<form method="get" action="/admin/osm">
<input type="text" name="q" value="{esc(q or '')}" placeholder="国名・region で絞り込み(例: france)">
<button type="submit">絞り込み</button>
</form>

{_job_status_html(job)}

{''.join(blocks)}
"""
    return HTMLResponse(
        content=page_shell("chiezo: OSM 国別の初期化", body, refresh=5 if job_running else None)
    )


@app.get("/admin/wikipedia", response_class=HTMLResponse)
def admin_wikipedia(request: Request, q: str | None = Query(None, description="言語名での絞り込み")):
    """Wikipedia 言語版の初期化画面(管理画面の wikipedia 行の「言語を選ぶ」から開く)。

    言語版は 348 件あり、管理画面の一覧に全部並べると他のソースが埋もれる。
    そこで一覧では wikipedia 1 行にまとめ、言語の選択だけをこの画面に切り出している
    (/admin/osm の国選択と同じ構図。大陸の代わりに記事数の階層でグルーピングする)。
    """
    sources: dict[str, Source] = request.app.state.sources
    catalog = {
        n: m for n, m in initializable_sources().items() if m.get("group") == "wikipedia"
    }
    job = _fetch_trigger_status()
    job_running = bool(job and job.get("state") == "running")
    disabled = " disabled" if not TRIGGER_URL or job_running else ""

    total = len(catalog)
    needle = (q or "").strip().lower()
    if needle:
        catalog = {
            n: m for n, m in catalog.items()
            if needle in " ".join(
                str(m.get(k, "")) for k in ("label", "label_en", "autonym", "lang")
            ).lower()
            or needle in n.lower()
        }

    tiers: dict[str, list[tuple[str, dict]]] = {}
    for name, meta in catalog.items():
        articles = meta.get("articles") or 0
        for threshold, tier_label in WIKIPEDIA_TIERS:
            if articles >= threshold:
                tiers.setdefault(tier_label, []).append((name, meta))
                break

    blocks = []
    for _, tier_label in WIKIPEDIA_TIERS:
        entries = tiers.get(tier_label)
        if not entries:
            continue
        entries.sort(key=lambda kv: (-(kv[1].get("articles") or 0), kv[0]))
        rows = []
        for name, meta in entries:
            src = sources.get(name)
            if src is not None:
                action = (
                    f'初期化済み(<a href="/{esc(name)}/">{src.doc_count:,} 件</a>)'
                )
            else:
                action = (
                    f'<form class="init-form" method="post" action="/admin/init/{esc(name)}">'
                    f'<button type="submit"{disabled}>初期化</button></form>'
                )
            articles = meta.get("articles") or 0
            rows.append(
                f"<tr>"
                f"<td>{esc(meta.get('label') or name)}"
                f'<div class="muted">{esc(name)}</div></td>'
                f"<td>{esc(meta.get('lang', ''))}</td>"
                f"<td>{esc(meta.get('autonym', ''))}</td>"
                f"<td>{articles:,}</td>"
                f"<td>{action}</td>"
                f"</tr>"
            )
        blocks.append(
            f"<details{' open' if needle else ''}>"
            f"<summary>{esc(tier_label)}({len(entries)})</summary>"
            "<table><thead><tr><th>言語</th><th>コード</th><th>自称</th>"
            "<th>記事数</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            "</details>"
        )
    if not blocks:
        blocks = ["<p>該当する言語がありません。</p>"]

    body = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>Wikipedia(言語版)の初期化</h1>
<p class="muted">
Wikipedia の言語版 {total} 件{f"(絞り込み: {len(catalog)} 件)" if needle else ""}から 1 言語ずつ取り込みます。
記事数の多い言語ほどダンプが大きく構築に時間がかかります(jawiki で構築 2〜6 時間、
enwiki はその数倍)。ページビュー突合のため全プロジェクト合算ファイル(圧縮 5〜6GB)も
取得します。必要メモリは約 3 GiB です。取り込みは同時に 1 件のみ。
</p>
<form method="get" action="/admin/wikipedia">
<input type="text" name="q" value="{esc(q or '')}" placeholder="言語名・コードで絞り込み(例: french)">
<button type="submit">絞り込み</button>
</form>

{_job_status_html(job)}

{''.join(blocks)}
"""
    return HTMLResponse(
        content=page_shell("chiezo: Wikipedia 言語版の初期化", body, refresh=5 if job_running else None)
    )


@app.post("/admin/init/{source}")
def admin_init(source: str, request: Request):
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    if source not in initializable_sources():
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


def request_origin(request: Request) -> str:
    """アクセス元 URL のプロトコル・ホスト名・ポートを組み立てる。

    生成する設定内の curl 例・許可ルールを「クライアントが chiezo に届いた URL」に
    そろえるための導出。リバースプロキシ越しでも到達可能な URL になるよう、
    スキームは X-Forwarded-Proto(あれば)、ホストは X-Forwarded-Host(あれば)
    → 無ければ Host ヘッダを使う。Host ヘッダはポートを保持しているので
    非標準ポート公開でもポートが落ちない。
    """
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",")[0].strip()
    return f"{proto}://{host}"


@app.get("/admin/claude-config.txt", response_class=PlainTextResponse)
def admin_claude_config_raw(
    request: Request,
    hook: bool = Query(False, description="自動許可フックを入れる前提の書き方の指示を含める"),
):
    """生成される CLAUDE.md ブロックを text/plain で返す(gen_claude_config.sh の取得元)。

    ベース URL は「この画面へのアクセス元」(request_origin)から導出するので、
    そのままクライアントに貼れば curl の例が到達可能な URL になる。

    `?hook=1` は gen_claude_config.sh が `--with-hook` で実際にフックを設置する
    ときだけ付けてくる。フックの無い環境に「自動許可される」と書くと嘘になるため、
    その一文は既定では出さない。
    """
    sources: dict[str, Source] = request.app.state.sources
    return claude_config.build_block(sources, request_origin(request), hook=hook)


@app.get("/admin/claude-config.permissions.json", response_class=PlainTextResponse)
def admin_claude_config_permissions(request: Request):
    """権限ファイル(settings.json / settings.local.json)へ書き出される内容を返す。"""
    return PlainTextResponse(
        claude_config.permission_json(request_origin(request)),
        media_type="application/json",
    )


@app.get("/admin/claude-config.hook.py", response_class=PlainTextResponse)
def admin_claude_config_hook_script(request: Request):
    """PreToolUse フック本体を返す(gen_claude_config.sh が実行可能ファイルとして置く)。

    `permissions.allow` は前方一致なので、ループやパイプに包まれた curl には効かない。
    フックはコマンドを構造で見て、chiezo だけを読む読み取り専用コマンドを自動許可する。
    """
    return PlainTextResponse(
        claude_config.hook_script(request_origin(request)),
        media_type="text/x-python",
    )


@app.get("/admin/claude-config.hook.json", response_class=PlainTextResponse)
def admin_claude_config_hook_settings(request: Request):
    """settings.json の `hooks` へマージされる断片を返す。

    フック本体の設置先はクライアント側で決まるので、コマンドは
    `{{HOOK_PATH}}` のまま返し、絶対パスへの差し替えはスクリプト側で行う。
    """
    return PlainTextResponse(
        claude_config.hook_settings_json(),
        media_type="application/json",
    )


@app.get("/admin/claude-config", response_class=HTMLResponse)
def admin_claude_config(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    base = request_origin(request)
    block = claude_config.build_block(sources, base)
    perms = claude_config.permission_json(base)
    hook = claude_config.hook_settings_json()
    body = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>Claude Code 連携設定(プレビュー)</h1>
<p class="muted">
いま <code>scripts/gen_claude_config.sh</code> で設定を吐き出したら書き込まれる内容。
この画面は表示するだけで、実ファイルは書き換えない。
curl 例・許可ルールのベース URL は、この画面へのアクセス元
(<code>{esc(base.rstrip("/"))}</code>)から導出している。
</p>

<h2>CLAUDE.md ブロック</h2>
<p class="muted">
書き込み先: <code>--user</code> なら <code>~/.claude/CLAUDE.md</code>、
<code>--project</code> なら <code>./CLAUDE.md</code>(マーカー間を差し替え)。
生: <a href="/admin/claude-config.txt">/admin/claude-config.txt</a>
</p>
<p><button type="button" id="copy-block">クリップボードにコピー</button>
<span id="msg-block" class="muted"></span></p>
<pre class="doc-body" id="config-block">{esc(block)}</pre>

<h2>権限ファイル</h2>
<p class="muted">
書き込み先: <code>--user</code> なら <code>~/.claude/settings.json</code>、
<code>--project</code> なら <code>./.claude/settings.local.json</code>。
chiezo への curl を許可プロンプトなしに実行できるよう、下記を
<code>permissions.allow</code> へ<strong>追記マージ</strong>する(既存の許可は壊さない。
新規作成時の丸ごとの中身が下記)。<code>--no-permissions</code> で無効化できる。
生: <a href="/admin/claude-config.permissions.json">/admin/claude-config.permissions.json</a>
</p>
<p><button type="button" id="copy-perms">クリップボードにコピー</button>
<span id="msg-perms" class="muted"></span></p>
<pre class="doc-body" id="config-perms">{esc(perms)}</pre>

<h2>自動許可フック(任意 / 既定では入らない)</h2>
<p class="muted">
上の許可ルールは<strong>コマンド文字列の前方一致</strong>なので、
<code>for … do curl … done</code> やパイプに包まれた curl には 1 本も効かず、
大量取得のときだけ毎回プロンプトが出てしまう。これを解消したい場合は
<code>PreToolUse</code> フックを併せて入れる。フックはコマンドを構造で見て
<strong>chiezo だけを読む読み取り専用コマンド</strong>だけを自動許可する
(条件を外れたら黙るので、その場合は今までどおりプロンプトが出る)。
</p>
<p class="muted">
これは <strong>Claude が打つ Bash を毎回検査して自動承認しうる</strong>仕掛けで、
影響が権限ルールより広い。中身を読んで納得してから入れられるよう
<code>gen_claude_config.sh</code> は既定では設置せず、
<code>--with-hook</code> を明示したときだけ設置する。
設置先: <code>--user</code> なら <code>~/.claude/hooks/{esc(claude_config.HOOK_FILENAME)}</code>、
<code>--project</code> なら <code>./.claude/hooks/{esc(claude_config.HOOK_FILENAME)}</code>。
下記断片の <code>{esc(claude_config.HOOK_PATH_PLACEHOLDER)}</code> を実際の絶対パスに
差し替えて <code>hooks</code> へマージする。
判定ロジックの全文: <a href="/admin/claude-config.hook.py">/admin/claude-config.hook.py</a> ·
設定断片: <a href="/admin/claude-config.hook.json">/admin/claude-config.hook.json</a>
</p>
<p><button type="button" id="copy-hook">クリップボードにコピー</button>
<span id="msg-hook" class="muted"></span></p>
<pre class="doc-body" id="config-hook">{esc(hook)}</pre>

<script>
function wireCopy(btnId, srcId, msgId) {{
  document.getElementById(btnId).addEventListener('click', async () => {{
    const text = document.getElementById(srcId).textContent;
    const msg = document.getElementById(msgId);
    try {{
      await navigator.clipboard.writeText(text);
      msg.textContent = 'コピーしました';
    }} catch (e) {{
      msg.textContent = 'コピーできませんでした(手動で選択してください)';
    }}
  }});
}}
wireCopy('copy-block', 'config-block', 'msg-block');
wireCopy('copy-perms', 'config-perms', 'msg-perms');
wireCopy('copy-hook', 'config-hook', 'msg-hook');
</script>
"""
    return HTMLResponse(content=page_shell("chiezo: Claude Code 連携設定", body))


# ---- 検索 -------------------------------------------------------------------


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
    if match is None:
        # trigram で扱えない短い検索語 → タイトル前方一致へフォールバック
        prefix = escape_like(q.strip())
        rows = db.query(
            src.path,
            "SELECT doc_id, title, substr(coalesce(opening, body), 1, 120) AS snippet,"
            " updated_at"
            f" FROM docs WHERE title LIKE ? ESCAPE '\\'{extra_where}"
            " ORDER BY rank_score DESC, title LIMIT ? OFFSET ?",
            (prefix + "%", *extra_params, limit, offset),
        )
        mode = "title_prefix"
    else:
        rows = db.query(
            src.path,
            "SELECT d.doc_id AS doc_id, d.title AS title,"
            " snippet(docs_fts, 1, '', '', '…', 40) AS snippet, d.updated_at AS updated_at"
            " FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
            f" WHERE docs_fts MATCH ?{extra_where_d}"
            " ORDER BY bm25(docs_fts, 5.0, 1.0) ASC, d.rank_score DESC"
            " LIMIT ? OFFSET ?",
            (match, *extra_params, limit, offset),
        )
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


def require_filter_schema(src: Source) -> None:
    """生成列(feature/area/lat/lon/wikidata)が無い古い DB を明示的に断る。"""
    if src.schema_version < FILTER_MIN_SCHEMA_VERSION:
        raise HTTPException(
            409,
            {
                "error": (
                    f"source {src.name} was built with schema_version={src.schema_version};"
                    f" attribute filters require >= {FILTER_MIN_SCHEMA_VERSION} (re-run ingest)"
                )
            },
        )


def require_tag_schema(src: Source) -> None:
    """タグ転置表(doc_tags)が無い古い DB を明示的に断る。

    再取り込みは jawiki で数時間かかるので、既存 DB を作り直さずに移行できる
    scripts/add_tag_index.py の方も案内する(docs.tags は 2 以前の DB にも入っている
    ので、転置表と索引を足すだけで済む)。
    """
    if src.schema_version < TAG_MIN_SCHEMA_VERSION:
        raise HTTPException(
            409,
            {
                "error": (
                    f"source {src.name} was built with schema_version={src.schema_version};"
                    f" tag filters require >= {TAG_MIN_SCHEMA_VERSION}"
                    " (re-run ingest, or migrate in place with scripts/add_tag_index.py)"
                )
            },
        )


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
    p = column_prefix
    where: list[str] = []
    params: list = []
    if tag:
        require_tag_schema(src)
        tags = [t.strip() for t in tag.split(",") if t.strip()]
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
        where.append(f"{p}lat BETWEEN ? AND ? AND {p}lon BETWEEN ? AND ?")
        params.extend([min_lat, max_lat, min_lon, max_lon])
    return "".join(f" AND {clause}" for clause in where), params


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
    feature: str | None = Query(None, description="地物種別。'amenity=place_of_worship' 形式。カンマ区切りで複数指定可"),
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
    field_list = parse_fields(fields, FILTER_DEFAULT_FIELDS, FILTER_ALLOWED_FIELDS)

    where, params = build_attribute_filters(
        src, feature=feature, area=area, bbox=bbox, wikidata=wikidata, tag=tag
    )
    if not where:
        raise HTTPException(
            400,
            {"error": "at least one of feature, area, bbox, wikidata, tag is required"},
        )
    clause = where.removeprefix(" AND ")
    (total,) = db.query(src.path, f"SELECT COUNT(*) AS n FROM docs WHERE {clause}", tuple(params))[0]
    rows = db.query(
        src.path,
        f"SELECT {', '.join(field_list)} FROM docs WHERE {clause}"
        " ORDER BY rank_score DESC, title LIMIT ? OFFSET ?",
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
        # 前方一致は idx_doc_tags_tag の範囲検索に落ちる(db.py の case_sensitive_like=ON)
        where += " WHERE tag LIKE ? ESCAPE '\\'"
        params.append(escape_like(prefix) + "%")
    if contains:
        where += (" AND" if where else " WHERE") + " tag LIKE ? ESCAPE '\\'"
        params.append("%" + escape_like(contains) + "%")
    rows = db.query(
        src.path,
        f"SELECT tag, COUNT(*) AS docs FROM doc_tags{where}"
        " GROUP BY tag ORDER BY docs DESC, tag LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
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


# ---- ブラウズ画面(人間向け HTML) -------------------------------------------

BROWSE_LIMIT = 50


def _browse_nav(source: str) -> str:
    return (
        '<nav><a href="/admin">管理画面</a>'
        f'<a href="/{esc(source)}/">{esc(source)} トップ</a></nav>'
    )


@app.get("/{source}/", response_class=HTMLResponse)
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
            f"<tr><td><a href=\"/{esc(source)}/doc/{r['doc_id']}\">{esc(r['title'])}</a></td>"
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
        return HTMLResponse(content=page_shell(f"chiezo: {source} / {tag}", body))
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
    if src.schema_version >= TAG_MIN_SCHEMA_VERSION:
        # 同じタグの文書一覧へ飛べるようにする(タグ絞り込みの導線)
        tags_html = ", ".join(
            f'<a href="/{esc(source)}/?tag={quote(t)}">{esc(t)}</a>' for t in tags
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
<pre class="doc-body">{esc(row['opening'] or '')}

{esc(row['body'] or '')}</pre>
<h2>リンク</h2>
<ul>{links_html}</ul>
<h2>extra</h2>
<pre class="doc-body">{esc(extra_html)}</pre>
"""
    return HTMLResponse(content=page_shell(f"chiezo: {row['title']}", body))


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
