"""chiezo-api ルーティング(設計書 §5)。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from pydantic import Field as PydField
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from app import agent, answer, claude_config, db, notes, websearch
from app.fts import build_match_query, escape_like
from app.mcp_server import build_mcp
from app.known_sources import CONTINENT_LABELS, KNOWN_SOURCES, WIKIPEDIA_TIERS
from app.pages import APPLE_TOUCH_ICON_PNG, esc, page_shell
from app.registry import (
    COORDS_MIN_SCHEMA_VERSION,
    FILTER_MIN_SCHEMA_VERSION,
    RANK_INDEX_MIN_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    TAG_COUNTS_MIN_SCHEMA_VERSION,
    TAG_MIN_SCHEMA_VERSION,
    Source,
    data_dir_fingerprint,
    scan_sources,
)

log = logging.getLogger("chiezo.api")

# 管理画面の初期化ボタンから叩く chiezo-trigger の内部 URL。未設定ならその機能を無効化する。
TRIGGER_URL = os.environ.get("CHIEZO_TRIGGER_URL")
TRIGGER_TIMEOUT = 5.0

# /data の変化(ブルーグリーン切り替え・DB コピー)を検知する定期再走査の間隔(秒)。
# 0 以下で無効(= 従来どおり再起動でのみ反映)。
RESCAN_INTERVAL_SECONDS = float(os.environ.get("CHIEZO_RESCAN_INTERVAL", "5"))

DEFAULT_DOC_FIELDS = ["title", "opening", "body", "tags", "updated_at"]
ALLOWED_DOC_FIELDS = [
    "doc_id", "title", "opening", "body", "tags", "links",
    "updated_at", "rank_score", "extra",
]
JSON_FIELDS = {"tags", "links", "extra"}

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50

# 関連度(bm25)に人気度(rank_score)を混ぜる重み。0 にすると従来どおり bm25 のみ。
# 実測(scripts/fts_lab.py で本番 jawiki 3 万件・重みを 0〜2 で振った)から 0.4 を採った。
# 0.3〜0.5 で「ラーメン」に対する有名店、「浅草寺」に対する浅草のような順当な記事が
# 上がり、2.0 まで上げると語の関連が薄い人気記事(織田信長など)を拾い始める。
POPULARITY_WEIGHT = 0.4

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
        except Exception:  # noqa: BLE001 - 常駐タスクを例外で殺さない
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
    app.state.mcp_asgi = mcp.streamable_http_app()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


app = FastAPI(title="chiezo", version="0.2", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(db.QueryTimeout)
async def timeout_handler(request: Request, exc: db.QueryTimeout):
    return JSONResponse(status_code=504, content={"error": "query timeout"})


def exact_title_first(prefix: str = "") -> str:
    """タイトルが検索語と完全一致する文書を最上位へ寄せる、ORDER BY の第 1 キー。

    bm25 は「その語をよく含む文書」を上に置くが、「その語そのものを説明している文書」を
    特別扱いはしない。実測でも `京都` の検索で京都市・近鉄京都線が上に来て、記事「京都」は
    5 位以内に入らなかった(本文が長いぶん bm25 の長さ正規化で不利になるため)。
    百科事典的な引き方では同名の記事があればそれが答えなので、人気度や関連度と混ぜず、
    独立した段として先に置く(完全一致が無いクエリでは何も起きない)。

    `lower()` は英語版 wiki 向け(SQLite の lower は ASCII のみなので日本語では実質無効)。
    **呼び出し側は WHERE 句のパラメータの後・LIMIT の前に検索語を渡すこと。**
    """
    return f"CASE WHEN lower({prefix}title) = lower(?) THEN 0 ELSE 1 END"


def relevance_order(prefix: str = "") -> str:
    """関連度に人気度を混ぜた ORDER BY 句を返す(FTS 検索用)。

    bm25() は「良い一致ほど小さい負値」なので、人気度で係数を大きくするほど上位へ動く。
    rank_score を 0〜1 に丸めてから使うのは、`schema_version` 3 以前の geonames が
    rank_score に人口の生値(最大 3000 万)を入れているため。丸めないとその 1 列だけで
    並びが決まってしまう。丸めれば古い DB は全件 1.0 に張り付き、実質 bm25 のみ
    (= 従来と同じ並び)に戻るので、取り込み直していない DB でも壊れない。
    """
    score = f"MIN(1.0, MAX(0.0, COALESCE({prefix}rank_score, 0.0)))"
    return (
        f"{exact_title_first(prefix)},"
        f" bm25(docs_fts, 5.0, 1.0) * (1.0 + {POPULARITY_WEIGHT} * {score}) ASC"
    )


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
        # 例外の文字列は管理画面にそのまま埋め込まれる。接続エラーの文言は内部 URL
        # (CHIEZO_TRIGGER_URL)等を含みうるので画面には出さず、詳細はログに残すだけにする
        # (CodeQL: Information exposure through an exception)。
        return {"state": "unreachable", "error": "chiezo-trigger に到達できません(詳細は api コンテナのログ)"}


# chiezo-trigger のソースカタログのプロセス内キャッシュ。中身は trigger のイメージに
# 焼かれた静的な表(osm_<国> だけで 195 件)なので、一度取れたら取り直す必要はない。
_catalog_cache: dict[str, dict] | None = None
# trigger(= ingest イメージ)が焼くスキーマバージョン。カタログと一緒に受け取る
_catalog_schema_version: int | None = None
# 取得に失敗した時刻(単調時計)。trigger が落ちている間、管理画面を開くたびに
# タイムアウト待ちを重ねない(ジョブ状況の取得と合わせて毎回 10 秒待たされるため)。
_catalog_failed_at: float | None = None
CATALOG_RETRY_SECONDS = 60.0


def _fetch_trigger_catalog() -> dict[str, dict] | None:
    """初期化できるソースの一覧を chiezo-trigger から取る。取れなければ None。"""
    global _catalog_cache, _catalog_failed_at, _catalog_schema_version
    if _catalog_cache is not None:
        return _catalog_cache
    if not TRIGGER_URL:
        return None
    if _catalog_failed_at and time.monotonic() - _catalog_failed_at < CATALOG_RETRY_SECONDS:
        return None
    try:
        res = httpx.get(f"{TRIGGER_URL}/sources", timeout=TRIGGER_TIMEOUT)
        res.raise_for_status()
        payload = res.json()
        catalog = payload["sources"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("chiezo-trigger source catalog unreachable: %s", e)
        _catalog_failed_at = time.monotonic()
        return None
    _catalog_cache = catalog
    _catalog_schema_version = payload.get("schema_version")
    return catalog


def latest_schema_version() -> int:
    """いま取り込み(再構築)を実行すると焼かれるスキーマバージョン(= 最新)。

    正は ingest 側(`core.SCHEMA_VERSION`)で、chiezo-trigger の `GET /sources` が
    カタログと一緒に返す。trigger が未設定・到達不能・古い(schema_version を返さない)
    ときは、api が対応できる最大バージョンで代替する(通常は両者一致する)。
    """
    _fetch_trigger_catalog()
    return _catalog_schema_version or max(SUPPORTED_SCHEMA_VERSIONS)


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


def _answer_status_html() -> str:
    """管理画面に出す「答える」層の状態(既定では無効なので、その旨と有効化方法を出す)。"""
    if not answer.is_enabled():
        return (
            '<p class="muted">「答える」層は無効です。推論サーバの OpenAI 互換 URL を'
            " <code>CHIEZO_LLM_URL</code> に設定すると有効になります"
            "(compose なら <code>docker compose --profile answer up -d</code>)。</p>"
        )
    return '<p><a href="/ask">→ AI と話す(chiezo の知識を引きます)</a></p>'


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    job = _fetch_trigger_status()
    job_running = bool(job and job.get("state") == "running")
    disabled = " disabled" if not TRIGGER_URL or job_running else ""
    latest_schema = latest_schema_version()

    def schema_cell(version: int) -> str:
        if version >= latest_schema:
            return str(version)
        return f'{version} <span class="stale">(最新: {latest_schema})</span>'

    rows = "\n".join(
        f"<tr>"
        f"<td><a href=\"/{esc(s.name)}/\">{esc(s.name)}</a></td>"
        f"<td>{esc(s.kind)}</td>"
        f"<td>{esc(s.lang or '')}</td>"
        f"<td>{s.doc_count:,}</td>"
        f"<td>{esc(s.dump_date or '')}</td>"
        f"<td>{esc(s.built_at or '')}</td>"
        f"<td>{schema_cell(s.schema_version)}</td>"
        f"<td>"
        f'<form class="init-form" method="post" action="/admin/rebuild/{esc(s.name)}" '
        f"onsubmit=\"return confirm('{esc(s.name)} を再構築します。ダンプの取得からやり直すため"
        f"時間がかかります(構築中も現行 DB での配信は続きます)。よろしいですか?')\">"
        f'<button type="submit"{disabled}>再構築</button>'
        f"</form>"
        f"</td>"
        f"</tr>"
        for s in sorted(sources.values(), key=lambda s: s.name)
    )
    if not rows:
        rows = '<tr><td colspan="8">登録済みのソースはありません</td></tr>'

    uninitialized = {
        name: meta for name, meta in initializable_sources().items() if name not in sources
    }
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
<p>登録ソース数: {len(sources)} / 最新のスキーマバージョン: {latest_schema}</p>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th>docs</th><th>dump_date</th><th>built_at</th><th>schema_version</th><th></th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<p class="muted">
スキーマバージョンが最新より古いソースは再構築で最新になる(タグ・座標まわりの一部は
<code>scripts/add_tag_index.py</code> でのその場移行でも可)。再構築はブルーグリーンで、
構築中も現行 DB での配信は続く。完了後は数秒以内に自動で新しい DB へ切り替わる(再起動不要)。
</p>

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

<h2>ためた知識で答える</h2>
{_answer_status_html()}

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


def _proxy_trigger_run(source: str) -> RedirectResponse:
    """chiezo-trigger の POST /run/{source} へプロキシし、管理画面へ戻す(init / rebuild 共通)。"""
    try:
        res = httpx.post(f"{TRIGGER_URL}/run/{source}", timeout=TRIGGER_TIMEOUT)
    except httpx.HTTPError as e:
        # 例外の文字列は内部 URL 等を含みうるのでレスポンスに載せない(上の
        # _fetch_trigger_status と同じ理由。詳細はログへ)。
        log.warning("chiezo-trigger run request failed: %s", e)
        raise HTTPException(502, {"error": "chiezo-trigger unreachable (details in api logs)"}) from e
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json())
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/init/{source}")
def admin_init(source: str, request: Request):
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    if source not in initializable_sources():
        raise HTTPException(404, {"error": f"unknown source: {source}"})
    sources: dict[str, Source] = request.app.state.sources
    if source in sources:
        raise HTTPException(409, {"error": f"source already initialized: {source}"})
    return _proxy_trigger_run(source)


@app.post("/admin/rebuild/{source}")
def admin_rebuild(source: str, request: Request):
    """登録済みソースの再構築(管理画面の「再構築」ボタン)。

    init と違い登録済みであることを要求する(未登録は init 側の担当)。ジョブの実体は
    init と同じ ingest の一括取り込みで、ブルーグリーン(別ファイル構築 → シンボリック
    リンク差し替え)なので構築中も現行 DB での配信は続く。ソースの正は trigger 側の
    ADAPTERS なので、カタログに無い登録済みソースでも trigger に判断を委ねる。
    """
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    sources: dict[str, Source] = request.app.state.sources
    if source not in sources:
        raise HTTPException(404, {"error": f"source not initialized: {source}"})
    return _proxy_trigger_run(source)


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
    mcp: bool = Query(False, description="MCP サーバーを登録した前提の使い分けの指示を含める"),
):
    """生成される CLAUDE.md ブロックを text/plain で返す(gen_claude_config.sh の取得元)。

    ベース URL は「この画面へのアクセス元」(request_origin)から導出するので、
    そのままクライアントに貼れば curl の例が到達可能な URL になる。

    `?hook=1` は gen_claude_config.sh が `--with-hook` で実際にフックを設置する
    ときだけ付けてくる。フックの無い環境に「自動許可される」と書くと嘘になるため、
    その一文は既定では出さない。
    """
    sources: dict[str, Source] = request.app.state.sources
    return claude_config.build_block(sources, request_origin(request), hook=hook, mcp=mcp)


@app.get("/admin/claude-config.mcp.json", response_class=PlainTextResponse)
def admin_claude_config_mcp(request: Request):
    """MCP サーバー登録の断片(`.mcp.json` の中身)を返す。

    URL はアクセス元から導出した `<base>/mcp`。プロジェクト用 `.mcp.json` への
    書き込み・ユーザースコープでの `claude mcp add` は gen_claude_config.sh
    (`--with-mcp`)が行う。
    """
    return PlainTextResponse(
        claude_config.mcp_servers_json(request_origin(request)),
        media_type="application/json",
    )


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
    # MCP 登録はスクリプトの既定なので、プレビューも既定(mcp=True)側で見せる。
    # フックは --with-hook のときだけなので、こちらは既定のまま出さない。
    block = claude_config.build_block(sources, base, mcp=True)
    perms = claude_config.permission_json(base)
    hook = claude_config.hook_settings_json()
    mcp = claude_config.mcp_servers_json(base)
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

<h2>MCP サーバー登録(既定で入る)</h2>
<p class="muted">
chiezo は MCP サーバーでもある(<code>{esc(base.rstrip("/"))}/mcp</code>)。
<code>gen_claude_config.sh</code> は既定でこれも Claude Code に登録する:
<code>--user</code> ならユーザースコープ(<code>claude mcp add --scope user</code>。
claude CLI が無ければ jq で <code>~/.claude.json</code> へ直接マージ)、
<code>--project</code>/<code>--target</code> なら
<code>.mcp.json</code> へ下記断片をマージする。あわせて上の CLAUDE.md ブロックに
「単発の参照は MCP・大量取得は curl」の使い分けの指示が入る。
登録が不要なら <code>--no-mcp</code>。
生: <a href="/admin/claude-config.mcp.json">/admin/claude-config.mcp.json</a>
</p>
<p><button type="button" id="copy-mcp">クリップボードにコピー</button>
<span id="msg-mcp" class="muted"></span></p>
<pre class="doc-body" id="config-mcp">{esc(mcp)}</pre>

<h2>権限ファイル(既定で入る)</h2>
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
wireCopy('copy-mcp', 'config-mcp', 'msg-mcp');
</script>
"""
    return HTMLResponse(content=page_shell("chiezo: Claude Code 連携設定", body))


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


def has_feature_area(src: Source) -> bool:
    """このソースが `feature` / `area` を持っているか(索引だけで分かる)。

    持っているのは地物のソース(osm・geonames)だけで、wikipedia 系の文書はどれも
    NULL。1 件だけ探せばよいので `idx_docs_feature_area` の先頭を覗いて判定する
    (`app/claude_config.py` が索引付きの列を同じ形で探っているのと同じやり方)。
    結果は Source に覚える — 走査のたびに作り直されるので、DB を差し替えれば消える。
    """
    if src.schema_version < FILTER_MIN_SCHEMA_VERSION:
        return False
    cached = getattr(src, "_has_feature_area", None)
    if cached is None:
        rows = db.query(
            src.path,
            "SELECT 1 FROM docs INDEXED BY idx_docs_feature_area"
            " WHERE feature IS NOT NULL LIMIT 1",
            (),
        )
        cached = bool(rows)
        object.__setattr__(src, "_has_feature_area", cached)
    return cached


def require_attributes(src: Source, *, feature: str | None, area: str | None) -> None:
    """持っていない属性で絞ろうとしたら、0 件ではなく**理由**を返す。

    wikipedia 系のソースに `area=東京都` を付けると、条件としては正しいのに必ず 0 件になる。
    人にとっても分かりにくいが、**agent モードでは致命的**だった: モデルは 0 件を見ても
    理由が分からず、絞り込みを付けたまま検索語だけ変えて何度も空振りする(実測)。
    「そのソースにその属性は無い」と言えば、次の手に移れる。
    """
    if not (feature or area) or has_feature_area(src):
        return
    raise HTTPException(
        400,
        {
            "error": f"source {src.name} has no feature/area attributes",
            "hint": "地物の属性(feature / area)を持つのは osm・geonames などの地物ソースだけ。"
                    "wikipedia 系のソースは tag(カテゴリ名)で絞るか、絞り込み無しで引くこと",
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
    except Exception:  # noqa: BLE001 - 件数表示のためだけに書き込みを失敗させない
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
):
    return notes.recall(q=q, since=since, until=until, tag=tag, limit=limit, offset=offset)


@app.delete("/v1/notes/{doc_id}")
def forget(request: Request, doc_id: int):
    if not notes.delete(doc_id):
        raise HTTPException(404, {"error": f"note not found: doc_id={doc_id}"})
    _refresh_notes_count(request)
    return {"deleted": doc_id}


# ---- 答える(ローカル LLM。既定では無効) -------------------------------------
#
# パイプラインの実体は app/answer.py。ここは HTTP の口(JSON / SSE / HTML)だけを持つ。
# `CHIEZO_LLM_URL` が未設定なら丸ごと無効で、503 と有効化の案内を返す。


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
        description="1 は chiezo で取れたことだけを根拠にする。0 なら足りない分をモデルの知識で補う"
                    "(既定は CHIEZO_ASK_DEFAULT_GROUNDED、無指定なら 1)",
    ),
    mode: str | None = Query(
        None,
        pattern="^(rag|agent)$",
        description="rag は search を 1 回。agent は LLM 自身に道具を引かせる"
                    "(ツール呼び出しが安定するモデルが要る。既定は CHIEZO_ASK_DEFAULT_MODE)",
    ),
):
    cfg = answer.require_settings()
    # 既定は環境変数で決める(GPU + 8B の環境と、CPU だけの環境で妥当な既定が違うため)。
    mode = mode or answer.default_mode()
    grounded = answer.default_grounded() if grounded is None else grounded
    if mode == "agent":
        if not stream:
            return await agent.answer_question(cfg, request, q, source, grounded)
        # 流し始める前に済ませられる検査はここで(SSE はヘッダ送出後に
        # ステータスコードを変えられない)。残りの失敗は error イベントになる。
        agent.prepare_catalog(request, source)
        return _sse_response(_agent_events(cfg, request, q, source, grounded))
    if not stream:
        return await answer.answer(cfg, request, q, source, grounded)

    # ストリーミングはヘッダを送った後でステータスを変えられないので、
    # 失敗しうる段(クエリ生成・検索)はここで済ませてから流し始める。
    queries, snippets, references = await answer.prepare(cfg, request, q, source)
    return _sse_response(_rag_events(cfg, q, queries, snippets, references, grounded))


async def _agent_events(
    cfg, request: Request, q: str, source: str | None, grounded: bool,
    history: list[dict] | None = None,
):
    """agent モードの SSE。

    rag と違い**流し始めた後にしかできない仕事**が本体(道具を引くこと自体が目的で、
    それが数十秒かかる)。ソースの検査だけは呼び出し側が先に済ませてあり、
    残りの失敗(推論サーバに繋がらない等)は error イベントとして流す。
    """
    events = agent.stream(cfg, request, q, source, grounded, history)
    yield _sse("meta", {"mode": "agent", "grounded": grounded, "model": cfg.model})
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


def _split_history(body: ChatRequest) -> tuple[str, list[dict]]:
    turns = [m.model_dump() for m in body.messages if (m.content or "").strip()]
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(400, {"error": "messages must end with a user message"})
    return turns[-1]["content"], turns[:-1]


@app.post("/v1/chat")
async def chat(request: Request, body: ChatRequest, stream: bool = Query(False)):
    cfg = answer.require_settings()
    question, history = _split_history(body)
    mode = body.mode or answer.default_mode()
    grounded = answer.default_grounded() if body.grounded is None else body.grounded
    if mode == "agent":
        if not stream:
            return await agent.answer_question(
                cfg, request, question, body.source, grounded, history
            )
        agent.prepare_catalog(request, body.source)
        return _sse_response(
            _agent_events(cfg, request, question, body.source, grounded, history)
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


# 会話画面の JS。**この画面だけ JS を使う**(他の画面は従来どおり JS なし)理由は 2 つ:
# 回答まで数十秒かかるので逐次表示しないと無反応に見えること、会話の履歴を持つ主体が
# クライアント側だからこと。EventSource ではなく fetch を使うのは、履歴を送るのに
# POST が要るため(EventSource は GET しか張れない)。
CHAT_JS = """
(function () {
  var log = document.getElementById('log');
  var form = document.getElementById('chat');
  var input = document.getElementById('q');
  if (!log || !form || !window.fetch) return;
  var history = [];   // 会話の主体はここ。サーバーは状態を持たない
  var busy = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function turn(who, label) {
    var t = el('div', 'turn ' + who);
    t.appendChild(el('div', 'who', label));
    var body = el('div', 'text', '');
    t.appendChild(body);
    log.appendChild(t);
    t.scrollIntoView({block: 'end'});
    return {node: t, text: body};
  }
  function addSteps(t, s) {
    if (!t.steps) { t.steps = el('ul', 'steps'); t.node.appendChild(t.steps); }
    t.steps.appendChild(el('li', null,
      s.tool + ' ' + JSON.stringify(s.arguments) + ' → ' + s.summary));
  }
  function addRefs(t, list) {
    if (!list.length) return;
    var ul = el('ul', 'refs');
    list.forEach(function (r) {
      // タイトルは < や " を含みうるので innerHTML では組み立てない
      var li = el('li', r.source === 'web' ? 'web' : null);
      var a = el('a', null, r.source === 'web' ? r.title : r.source + ' / ' + r.title);
      a.href = r.url;
      if (r.source === 'web') { a.target = '_blank'; a.rel = 'noreferrer'; }
      li.appendChild(a);
      ul.appendChild(li);
    });
    t.node.appendChild(ul);
  }

  function send(text) {
    if (busy || !text) return;
    busy = true;
    turn('you', 'あなた').text.textContent = text;
    history.push({role: 'user', content: text});
    var t = turn('bot', 'chiezo');
    t.text.textContent = '…';
    var first = true, answer = '';
    fetch('/v1/chat?stream=1', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        messages: history,
        source: document.getElementById('source').value || null,
        grounded: document.getElementById('grounded').value === '1',
        mode: document.getElementById('mode').value
      })
    }).then(function (res) {
      if (!res.ok) { throw new Error('HTTP ' + res.status); }
      var reader = res.body.getReader(), decoder = new TextDecoder(), buf = '';
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { return; }
          buf += decoder.decode(chunk.value, {stream: true});
          var frames = buf.split('\\n\\n');
          buf = frames.pop();
          frames.forEach(function (frame) {
            var ev = /^event: (.*)$/m.exec(frame), da = /^data: (.*)$/m.exec(frame);
            if (!ev || !da) return;
            var data = JSON.parse(da[1]);
            if (ev[1] === 'step') { addSteps(t, data); }
            else if (ev[1] === 'references') { addRefs(t, data.references || []); }
            else if (ev[1] === 'delta') {
              if (first) { t.text.textContent = ''; first = false; }
              answer += data.text;
              t.text.textContent = answer;
            } else if (ev[1] === 'error') {
              t.text.textContent += '\\n[エラー] ' + (data.error || '');
            }
          });
          t.node.scrollIntoView({block: 'end'});
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      t.text.textContent += '\\n[通信に失敗しました: ' + e.message + ']';
    }).then(function () {
      // 失敗しても履歴には残す(次の発言で文脈が飛ぶのを避ける)
      history.push({role: 'assistant', content: answer || '(応答なし)'});
      busy = false;
      input.focus();
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = input.value.trim();
    input.value = '';
    send(text);
  });
  if (form.dataset.first) { send(form.dataset.first); }
})();
"""


# 末尾スラッシュ付きはキャッチオールの /{source}/ に食われて「unknown source: ask」に
# なってしまうので、先に受けて /ask へ寄せる(ブラウザで手打ちしたときの迷子を防ぐ)。
@app.get("/ask/", include_in_schema=False)
def ask_page_slash(request: Request):
    query = request.url.query
    return RedirectResponse(url="/ask" + (f"?{query}" if query else ""))


@app.get("/ask", response_class=HTMLResponse)
async def ask_page(
    request: Request,
    q: str | None = Query(None),
    source: str | None = Query(None),
    nojs: bool = Query(False, description="JS を使わず、1 問 1 答で表示する"),
    grounded: bool | None = Query(None, description="chiezo で取れたことだけを根拠にする"),
    mode: str | None = Query(None, pattern="^(rag|agent)$", description="rag / agent"),
):
    mode = mode or answer.default_mode()
    grounded = answer.default_grounded() if grounded is None else grounded
    sources: dict[str, Source] = request.app.state.sources
    options = '<option value="">(自動)</option>' + "".join(
        f'<option value="{esc(name)}"{" selected" if name == source else ""}>{esc(name)}</option>'
        for name in sorted(sources)
    )
    # チェックボックスではなく select にしてある。チェックボックスは off のとき何も
    # 送らないので hidden との併用が要り、その場合 grounded=0&grounded=1 の 2 値が
    # 飛んで FastAPI は先頭(=0)を採る。select なら必ず 1 値だけ送られる。
    grounded_options = "".join(
        f'<option value="{value}"{" selected" if (value == "1") == grounded else ""}>{label}</option>'
        for value, label in (("1", "chiezo で取れたことだけ"), ("0", "モデルの知識で補ってよい"))
    )
    mode_options = "".join(
        f'<option value="{value}"{" selected" if value == mode else ""}>{label}</option>'
        for value, label in (("rag", "1 回検索して答える"), ("agent", "モデルに道具を引かせる"))
    )
    settings = f"""
<div class="settings">
ソース <select id="source" name="source">{options}</select>
根拠 <select id="grounded" name="grounded">{grounded_options}</select>
引き方 <select id="mode" name="mode">{mode_options}</select>
{'<span title="web 検索が有効です">🌐 web 検索: 有効</span>' if websearch.is_enabled() else ''}
</div>
"""
    if not answer.is_enabled():
        # 無効でも入力欄そのものは出す(何をする画面なのかが分からないと、
        # 「壊れている」のか「使っていない機能」なのか見分けが付かない)。
        body = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>AI と話す</h1>
<p class="stale">「答える」層は無効です。</p>
<p class="muted">推論サーバの OpenAI 互換 URL を <code>CHIEZO_LLM_URL</code> に設定すると有効になります
(compose なら <code>docker compose --profile answer up -d</code>)。</p>
<form class="chat">
<input type="text" name="q" placeholder="話しかける(自然文でよい)" disabled>
<button type="submit" disabled>送信</button>
</form>
"""
        return HTMLResponse(content=page_shell("AI と話す", body))

    cfg = answer.require_settings()
    # 話す相手は AI(モデル)で、chiezo はその AI が引く知識。見出しでその関係を出すため、
    # モデル名を名乗らせる(推論サーバに聞く。分からなければ名前なしの「AI」)。
    label = await answer.model_label(cfg)
    heading = f"AI({esc(label)})と話す" if label else "AI と話す"
    if not nojs:
        # 会話は JS(fetch + SSE)が主役。履歴を持つのはブラウザ側で、サーバーは
        # 毎回まるごと受け取る。JS が無い環境には下の 1 問 1 答へ誘導する。
        first = f' data-first="{esc(q)}"' if q else ""
        nojs_url = f"/ask?nojs=1&mode={mode}&grounded={'1' if grounded else '0'}" + (
            f"&q={quote(q)}" if q else ""
        )
        body = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>{heading}</h1>
<p class="muted">chiezo にためた知識(登録済みソース)を引ける AI です。
根拠にした文書は発言のあとに並びます。</p>
<div id="log"></div>
<form class="chat" id="chat"{first}>
<input type="text" id="q" name="q" placeholder="話しかける(自然文でよい)" autofocus>
<button type="submit">送信</button>
</form>
{settings}
<noscript><p class="stale">JavaScript が無効です。
<a href="{esc(nojs_url)}">1 問 1 答の画面</a>を使ってください(会話の継続はできません)。</p></noscript>
<script>{CHAT_JS}</script>
"""
        return HTMLResponse(content=page_shell(heading, body))

    # ---- JS なしの 1 問 1 答(会話は続かないが、これだけで用が足りることも多い)
    form = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>{heading}(JS なし・1 問 1 答)</h1>
<form method="get" action="/ask">
<input type="hidden" name="nojs" value="1">
<input type="text" name="q" value="{esc(q or '')}" placeholder="質問を書く(自然文でよい)">
<select name="source">{options}</select>
<select name="grounded">{grounded_options}</select>
<select name="mode">{mode_options}</select>
<button type="submit">質問する</button>
</form>
<p class="muted"><a href="/ask">会話できる画面へ戻る</a></p>
"""
    if not q:
        return HTMLResponse(content=page_shell(heading, form))

    steps_block = ""
    if mode == "agent":
        result = await agent.answer_question(cfg, request, q, source, grounded)
        trace = "\n".join(
            f'<li>{s["step"]}. {esc(s["tool"])} '
            f'{esc(json.dumps(s["arguments"], ensure_ascii=False))} → {esc(s["summary"])}</li>'
            for s in result["steps"]
        ) or "<li>(道具を使わずに答えた)</li>"
        steps_block = f"<h2>調べた手順</h2>\n<ul>\n{trace}\n</ul>\n"
        footer = f"モデル: {esc(result['model'])}(agent モード)"
    else:
        result = await answer.answer(cfg, request, q, source, grounded)
        footer = (
            f"検索に使ったクエリ: {esc(json.dumps(result['queries'], ensure_ascii=False))}"
            f" / モデル: {esc(result['model'])}"
        )
    refs = "\n".join(
        f'<li>[{r["n"]}] <a href="{esc(r["url"])}">{esc(r["source"])} / {esc(r["title"])}</a></li>'
        for r in result["references"]
    ) or "<li>(なし)</li>"
    body = form + f"""
{steps_block}<h2>回答</h2>
<pre class="answer">{esc(result['answer'])}</pre>
<h2>出典</h2>
<ul>
{refs}
</ul>
<p class="muted">{footer}</p>
"""
    return HTMLResponse(content=page_shell(heading, body))


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
