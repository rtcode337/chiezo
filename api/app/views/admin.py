"""管理画面(`/admin`)。人が見る HTML と、そこから叩く操作の口。

取り込みの起動は chiezo-trigger(内部サービス)へのプロキシで、この画面自体は
DB を触らない。Claude Code 連携の設定を配る口(`/admin/claude-config*`)もここ。
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import answer, claude_config
from app.known_sources import CONTINENT_LABELS, KNOWN_SOURCES, WIKIPEDIA_TIERS
from app.pages import CHAT_PATH, browse_url, esc, page_shell
from app.registry import SUPPORTED_SCHEMA_VERSIONS, Source

log = logging.getLogger("chiezo.api")

router = APIRouter()

# 初期化ボタンから叩く chiezo-trigger の内部 URL。未設定ならその機能を無効化する。
TRIGGER_URL = os.environ.get("CHIEZO_TRIGGER_URL")
TRIGGER_TIMEOUT = 5.0

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


# chiezo-trigger のソースカタログのプロセス内キャッシュ。中身の大半は trigger のイメージに
# 焼かれた静的な表(osm_<国> だけで 195 件)だが、**それだけとは限らない**:
# `CHIEZO_SOURCE_PLUGINS` の差し込みはボリュームで実行時に足せるので、trigger を入れ替えた
# あとにカタログが増える。一度取ったら永久に持ち続けると、プラグインを足したのに管理画面へ
# 出ないまま api の再起動を待つことになる。そこで有効期限を持たせる。
_catalog_cache: dict[str, dict] | None = None
# trigger(= ingest イメージ)が焼くスキーマバージョン。カタログと一緒に受け取る
_catalog_schema_version: int | None = None
# 最後に取れた時刻(単調時計)。この値から CATALOG_TTL_SECONDS 経過したら取り直す。
_catalog_fetched_at: float | None = None
# 取得に失敗した時刻(単調時計)。trigger が落ちている間、管理画面を開くたびに
# タイムアウト待ちを重ねない(ジョブ状況の取得と合わせて毎回 10 秒待たされるため)。
_catalog_failed_at: float | None = None
CATALOG_RETRY_SECONDS = 60.0
# カタログの有効期限(秒)。0 以下で無期限(取り直さない)。既定の 5 分は「プラグインを
# 足して管理画面を開き直す」のに待たされない長さと、内部 HTTP を叩く頻度の折り合い。
CATALOG_TTL_SECONDS = answer._env_num("CHIEZO_CATALOG_TTL", 300.0, float)


def _catalog_is_fresh() -> bool:
    if _catalog_cache is None or _catalog_fetched_at is None:
        return False
    if CATALOG_TTL_SECONDS <= 0:
        return True
    return time.monotonic() - _catalog_fetched_at < CATALOG_TTL_SECONDS


def _fetch_trigger_catalog() -> dict[str, dict] | None:
    """初期化できるソースの一覧を chiezo-trigger から取る。取れなければ None。"""
    global _catalog_cache, _catalog_failed_at, _catalog_fetched_at, _catalog_schema_version
    if _catalog_is_fresh():
        return _catalog_cache
    if not TRIGGER_URL:
        return _catalog_cache
    if _catalog_failed_at and time.monotonic() - _catalog_failed_at < CATALOG_RETRY_SECONDS:
        return _catalog_cache
    try:
        res = httpx.get(f"{TRIGGER_URL}/sources", timeout=TRIGGER_TIMEOUT)
        res.raise_for_status()
        payload = res.json()
        catalog = payload["sources"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("chiezo-trigger source catalog unreachable: %s", e)
        _catalog_failed_at = time.monotonic()
        # **期限切れでも古いカタログは捨てない。** 捨てると控えの KNOWN_SOURCES に落ちて、
        # 管理画面から 545 件が消える(trigger が一時的に落ちただけなのに)。
        return _catalog_cache
    _catalog_cache = catalog
    _catalog_fetched_at = time.monotonic()
    _catalog_failed_at = None
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
    """管理画面に出す「使う」層の状態(既定では無効なので、その旨と有効化方法を出す)。"""
    if not answer.is_enabled():
        return (
            '<p class="muted">「使う」層は無効です。推論サーバの OpenAI 互換 URL を'
            " <code>CHIEZO_LLM_URL</code> に設定すると有効になります"
            "(compose なら <code>docker compose --profile answer up -d</code>)。</p>"
        )
    return f'<p><a href="{CHAT_PATH}">→ AI と話す(Chiezo の知識を引きます)</a></p>'


@router.get("/admin", response_class=HTMLResponse)
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
        f"<td><a href=\"{esc(browse_url(s.name))}\">{esc(s.name)}</a></td>"
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
<h1>Chiezo 管理画面</h1>
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

<h2>ためた知識を使う AI</h2>
{_answer_status_html()}

<h2>Claude Code 連携設定</h2>
<p class="muted">
いま設定を吐き出したら(<code>scripts/gen_claude_config.sh</code>)どういう内容になるかのプレビュー。
現在の登録ソースから生成した CLAUDE.md ブロックを表示する(実ファイルは書き換えない)。
</p>
<p><a href="/admin/claude-config">→ 生成される設定を見る</a></p>
"""
    return HTMLResponse(content=page_shell("Chiezo 管理画面", body, refresh=5 if job_running else None))


@router.get("/admin/osm", response_class=HTMLResponse)
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
                    f'初期化済み(<a href="{esc(browse_url(name))}">{src.doc_count:,} 件</a>)'
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
        content=page_shell("Chiezo: OSM 国別の初期化", body, refresh=5 if job_running else None)
    )


@router.get("/admin/wikipedia", response_class=HTMLResponse)
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
                    f'初期化済み(<a href="{esc(browse_url(name))}">{src.doc_count:,} 件</a>)'
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
        content=page_shell("Chiezo: Wikipedia 言語版の初期化", body, refresh=5 if job_running else None)
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


@router.post("/admin/init/{source}")
def admin_init(source: str, request: Request):
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    if source not in initializable_sources():
        raise HTTPException(404, {"error": f"unknown source: {source}"})
    sources: dict[str, Source] = request.app.state.sources
    if source in sources:
        raise HTTPException(409, {"error": f"source already initialized: {source}"})
    return _proxy_trigger_run(source)


@router.post("/admin/rebuild/{source}")
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

    生成する設定内の curl 例・許可ルールを「クライアントが Chiezo に届いた URL」に
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


@router.get("/admin/claude-config.txt", response_class=PlainTextResponse)
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


@router.get("/admin/claude-config.mcp.json", response_class=PlainTextResponse)
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


@router.get("/admin/claude-config.permissions.json", response_class=PlainTextResponse)
def admin_claude_config_permissions(request: Request):
    """権限ファイル(settings.json / settings.local.json)へ書き出される内容を返す。"""
    return PlainTextResponse(
        claude_config.permission_json(request_origin(request)),
        media_type="application/json",
    )


@router.get("/admin/claude-config.hook.py", response_class=PlainTextResponse)
def admin_claude_config_hook_script(request: Request):
    """PreToolUse フック本体を返す(gen_claude_config.sh が実行可能ファイルとして置く)。

    `permissions.allow` は前方一致なので、ループやパイプに包まれた curl には効かない。
    フックはコマンドを構造で見て、Chiezo だけを読む読み取り専用コマンドを自動許可する。
    """
    return PlainTextResponse(
        claude_config.hook_script(request_origin(request)),
        media_type="text/x-python",
    )


@router.get("/admin/claude-config.hook.json", response_class=PlainTextResponse)
def admin_claude_config_hook_settings(request: Request):
    """settings.json の `hooks` へマージされる断片を返す。

    フック本体の設置先はクライアント側で決まるので、コマンドは
    `{{HOOK_PATH}}` のまま返し、絶対パスへの差し替えはスクリプト側で行う。
    """
    return PlainTextResponse(
        claude_config.hook_settings_json(),
        media_type="application/json",
    )


@router.get("/admin/claude-config", response_class=HTMLResponse)
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
Chiezo は MCP サーバーでもある(<code>{esc(base.rstrip("/"))}/mcp</code>)。
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
Chiezo への curl を許可プロンプトなしに実行できるよう、下記を
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
<strong>Chiezo だけを読む読み取り専用コマンド</strong>だけを自動許可する
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
    return HTMLResponse(content=page_shell("Chiezo: Claude Code 連携設定", body))


