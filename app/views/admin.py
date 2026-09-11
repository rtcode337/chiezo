"""管理画面(`/admin`)。人が見る HTML と、そこから叩く操作の口。

取り込みの起動は chiezo-trigger(内部サービス)へのプロキシで、この画面自体は
DB を触らない。Claude Code 連携の設定を配る口(`/admin/claude-config*`)もここ。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import (
    ai_log,
    answer,
    build_info,
    capabilities,
    claude_config,
    collect,
    jst,
    media,
    memory,
    notes,
    providers,
    settings_store,
)
from app.known_sources import CONTINENT_LABELS, KNOWN_SOURCES, WIKIPEDIA_TIERS
from app.pages import CHAT_PATH, browse_url, esc, page_shell
from app.registry import SUPPORTED_SCHEMA_VERSIONS, Source
from app.views import ai_history, ai_settings, ai_usage

log = logging.getLogger("chiezo.app")

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
        return {"state": "unreachable", "error": "chiezo-trigger に到達できません(詳細は app コンテナのログ)"}


# chiezo-trigger のソースカタログのプロセス内キャッシュ。中身の大半は trigger のイメージに
# 焼かれた静的な表(osm_<国> だけで 195 件)だが、それだけとは限らない:
# `CHIEZO_PLUGIN_SOURCES` のプラグインは実行時に足せるので、trigger を入れ替えた
# あとにカタログが増える。一度取ったら永久に持ち続けると、プラグインを足したのに管理画面へ
# 出ないまま app の再起動を待つことになる。そこで有効期限を持たせる。
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
        # 期限切れでも古いカタログは捨てない。 捨てると控えの KNOWN_SOURCES に落ちて、
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
    ときは、app が対応できる最大バージョンで代替する(通常は両者一致する)。
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


def run_buttons_disabled(job: dict | None) -> str:
    """取り込みを起こすボタンの `disabled` 属性。

    起こせるのは chiezo-trigger が居るときだけ。**未設定でも到達不能でも押せなくする**
    —— 押せると 502 が返るだけで、なぜ動かないのかが画面から読めない。長期記憶へ
    書き込むとき(初期化・再構築・固化)しか要らない相手なので、立てない使い方が普通にある。
    実行中に押せないのは、同時に 1 ジョブしか受け付けないため。
    """
    if not TRIGGER_URL:
        return " disabled"
    if job is None or job.get("state") in ("unreachable", "running"):
        return " disabled"
    return ""


def _job_status_html(job: dict | None) -> str:
    if job is None:
        return (
            '<div class="job-status" id="job">'
            "取り込みトリガー(chiezo-trigger)は設定されていません"
            " (CHIEZO_TRIGGER_URL 未設定)。長期記憶への書き込み(初期化・再構築・固化)は"
            "できませんが、読むだけならこのままで動きます。"
            "</div>"
        )
    if job.get("state") == "unreachable":
        return (
            '<div class="job-status error" id="job">'
            f"<p>{esc(job.get('error') or 'chiezo-trigger に到達できません')}</p>"
            "<p>長期記憶への書き込み(初期化・再構築・固化)はできません。"
            "読むだけならこのままで動きます。</p>"
            "</div>"
        )
    state = job.get("state", "idle")
    css = f"job-status {state}" if state in ("running", "error") else "job-status"
    lines = [f'<div class="{css}" id="job">', f"<p>状態: {esc(state)}"]
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
    if state == "running":
        # **自動では読み直さない。** 走っている間 5 秒ごとに読み直していた頃は、
        # 開いた `<details>` は閉じ、書きかけの入力は消え、押そうとしたボタンは
        # 読み直しに攫われた —— 取り込みは数時間かかるので、その間ずっと画面が
        # 使えないことになる。進み具合を見たい人がここから読み直す
        lines.append(
            '<p><a href="/admin/memory#job">進み具合を読み直す</a>'
            ' <span class="muted">(自動では読み直しません)</span></p>'
        )
    lines.append("</div>")
    return "\n".join(lines)


def _memory_html(sources: dict[str, Source], disabled: str) -> str:
    """固化(短期記憶 → 長期記憶)の節。

    焼くこと自体は普通の取り込みなので、ボタンの行き先は初期化・再構築と同じ
    (`chiezo-app` が素材を配り、ingest が焼く)。ここが持つのは**待ち行列の数**と、
    焼き上がりを確かめてから短期側に印を付ける「片付ける」だけ。

    **`固化対象` を付ける口は置かない** —— ただのタグなので、短期記憶の画面や MCP の
    `update` で付く。ここに足すと同じことをする経路が 2 つになる。
    """
    if not memory.is_enabled():
        return (
            '<p class="muted">固化は無効です。短期記憶'
            "(<code>CHIEZO_NOTES_DIR</code>)を設定すると使えます。</p>"
        )
    state = memory.status(sources)
    name = memory.SOURCE_NAME
    burn = f"/admin/rebuild/{name}" if state["consolidated"] else f"/admin/init/{name}"
    # 待ちが 1 件も無いときは押せなくする。素材が空だと配る側(`/v1/memory/fetch`)が
    # 409 で断るので、押せると取り込みが始まってすぐ失敗し、画面には HTTP の
    # ステータスしか残らない。焼くものが無いことは押す前から分かっている。
    empty = not state["pending"]
    burn_disabled = disabled or (" disabled" if empty else "")
    waiting = f'<strong>{state["pending"]:,} 件</strong>' if not empty else (
        '<strong>0 件</strong> <span class="muted">(焼くものが無いので「固化する」は'
        "押せません)</span>"
    )
    if state["consolidated"]:
        long_term = (
            f'<a href="{esc(browse_url(name))}">{state["docs"]:,} 件</a>'
            f' <span class="muted">(最後に焼いたのは {esc(state["built_at"] or "")})</span>'
        )
    else:
        long_term = '<span class="muted">まだ 1 度も焼いていない</span>'
    return f"""
<p>
長期記憶(<code>{esc(name)}</code>): {long_term}<br>
固化を待っているメモ: {waiting}
</p>
<form class="init-form" method="post" action="{esc(burn)}">
<button type="submit"{burn_disabled}>固化する</button></form>
<form class="init-form" method="post" action="/admin/memory/sweep"
 onsubmit="return confirm('長期側へ移せたメモの印を{esc(notes.CONSOLIDATE_TAG)}から
{esc(notes.CONSOLIDATED_TAG)}に付け替えます。よろしいですか?')">
<button type="submit">片付ける</button></form>
<p class="muted">
短期記憶のメモに <code>{esc(notes.CONSOLIDATE_TAG)}</code> を付けると、次の固化で
長期記憶へ移る(見出しが同じものは上書き、<code>{esc(notes.TOMBSTONE_TAG)}</code> も
付いていれば長期側から落とす)。焼き上がったら「片付ける」で
<code>{esc(notes.CONSOLIDATED_TAG)}</code> に変わり、<code>recall</code> の既定から外れる。<br>
付けるのは人でも AI でもよい —— MCP の <code>update</code> でタグを足すだけなので、
「短期記憶を順に見て、残す価値があるものに印を付けて」と頼めば回る。
</p>
"""


def _history_args(request: Request) -> tuple[int, bool]:
    """「AI への依頼」節のページと絞り込みをクエリから読む。

    **おかしな値は 1 ページ目に寄せる**(手で URL をいじられても落とさない)。
    """
    raw = request.query_params.get("ai_page", "1")
    page = int(raw) if raw.isdigit() and int(raw) > 0 else 1
    return page, request.query_params.get("ai_failed") == "1"


def _disk_html(data_dir: Path) -> str:
    """データの置き場があるディスクの空き。

    **コンテナの中から見える**。`/data` は bind マウントなのでホストの実体の統計が返り、
    ホストで `df` を打った値と一致する(実測で一致を確認済み)。

    出す理由は、**この画面から始まる操作がディスクを一番食う**から ——
    取り込み 1 回で数十 GB 増えることがあり(jawiki の DB は 42GB)、
    足りないまま走らせると数時間かけて最後に失敗する。押す前に見えるところに置く。

    単位は GiB(`df -h` と同じ数え方。GB で書くと df の表示と食い違って見える)。
    """
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as e:
        # 例外の文字列は置き場のパスを含む。画面には種別だけ出し、詳細はログに残す
        log.warning("could not read disk usage for %s: %s", data_dir, e)
        return f'<span class="muted">ディスクの空きを取れません({type(e).__name__})</span>'
    gib = 1024**3
    free_gib = usage.free / gib
    used_pct = 100 * usage.used / usage.total if usage.total else 0
    text = (
        f"ディスクの空き: {free_gib:,.0f} GiB "
        f'<span class="muted">/ 全体 {usage.total / gib:,.0f} GiB({used_pct:.0f}% 使用)</span>'
    )
    # 取り込み 1 回で数十 GB 増えることがあるので、そのくらいを切ったら目立たせる
    if free_gib < 20 or used_pct >= 95:
        return f'<span class="stale">⚠️ {text}(取り込みには足りないかもしれません)</span>'
    return text


def _consult_page_html(name: str | None, want: str, draft: str, error: str) -> str:
    """AI が書いた指示文の案を見せて、直して保存できる 1 枚。

    **案は保存しない**。押されるまで何も変わらない —— 相談は何度でもやり直すもので、
    途中の案が勝手に入れ替わると、気に入っていた前の案に戻れない。

    **もう一度相談する側にも案を持ち回る**ので、「ここをこう直して」を重ねられる
    (Chiezo が状態を持たない作りに合わせて、毎回まるごと渡す)。
    """
    heading = f"「{esc(name)}」のプロンプトを相談する" if name else "新しい収集のプロンプトを相談する"
    # 既存を直すなら保存先はその収集、新規ならフォームごと作る
    if name:
        save = (
            f'<form method="post" action="/admin/collect/{esc(name)}/edit" class="collect-form">'
            f'<input type="hidden" name="description" value="{esc(collect.get(name).description)}">'
            f'<input type="hidden" name="interval_minutes" value="{collect.get(name).interval_minutes}">'
            f'<input type="hidden" name="cursor" value="{esc(collect.get(name).cursor)}">'
            # 載せないと既定へ戻る（集め方は「足す」に、抽出の指定は空に）。
            # 相談で直したいのはプロンプトだけなので、他はそのまま持ち回る
            f'<input type="hidden" name="mode" value="{esc(collect.get(name).mode)}">'
            f'<textarea name="extract" hidden>{esc(_extract_json(collect.get(name)))}</textarea>'
            f'<p><label>この案(直してから保存できる)<br>'
            f'<textarea name="prompt" rows="12">{esc(draft)}</textarea></label></p>'
            f'<button type="submit">この内容で保存する</button></form>'
        )
    else:
        save = (
            '<form method="post" action="/admin/collect/create" class="collect-form">'
            '<p><label>name(ソース名になる)<br>'
            '<input name="name" required pattern="[a-z][a-z0-9_]{1,30}"></label></p>'
            f'<p><label>説明<br><input name="description" value="{esc(want)}"></label></p>'
            f'<p><label>間隔(分)<br><input name="interval_minutes" type="number"'
            f' min="{collect.MIN_INTERVAL_MINUTES}" value="360"></label></p>'
            f'<p><label>この案(直してから保存できる)<br>'
            f'<textarea name="prompt" rows="12">{esc(draft)}</textarea></label></p>'
            '<p><label><input type="checkbox" name="web" value="1" checked> web 検索を開ける</label></p>'
            '<button type="submit">この内容で追加する(止めた状態で作る)</button></form>'
        )
    again = (
        '<form method="post" action="/admin/collect/consult" class="collect-form">'
        f'<input type="hidden" name="name" value="{esc(name or "")}">'
        f'<input type="hidden" name="want" value="{esc(want)}">'
        f'<textarea name="current" hidden>{esc(draft)}</textarea>'
        '<p><label>もう一度相談する(どう直したいか)<br>'
        '<input name="feedback" placeholder="例: 件数を減らして、出典を必ず付けさせて"></label></p>'
        "<button type=\"submit\">この案を直してもらう</button></form>"
    )
    body = f"""
<h1>{heading}</h1>
{error}
<p class="muted">集めたいもの: {esc(want) or "(指定なし)"}</p>
{save}
{again}
<p class="muted"><a href="/admin/memory#collect">管理画面へ戻る</a>(保存しなければ何も変わりません)</p>
"""
    return page_shell("プロンプトの相談", body)


MODE_LABELS = {
    "append": "集める(外から取ってきて積む)",
    "refine": "整理する(いまの内容を読ませて、直すものと足すものを返させる)",
}


def _backend_select(current: str | None) -> str:
    """相手を選ぶセレクト。**空が「Chiezo の既定にまかせる」**。

    候補は**有効にしてある相手だけ**(`answer.backend_names()`)—— 無効な相手を選べても
    走らせた瞬間に断られる。**描画のときに相手へ問い合わせない**ので、モデルの一覧は
    `app/providers.py` が持つ控えを使う(管理画面の他の表と同じ流儀)。

    **いま選ばれている相手が無効になっていても選択肢に残す** —— 落とすと、保存し直した
    瞬間に既定へ倒れて、誰に頼んでいたのかが画面から消える。
    """
    enabled = answer.backend_names()
    names = list(enabled)
    if current and current not in names:
        names.append(current)
    options = ['<option value="">Chiezo の既定にまかせる</option>']
    for name in names:
        spec = providers.get(name)
        label = spec.label if spec else name
        if name not in enabled:
            label += "(いまは無効)"
        selected = " selected" if name == current else ""
        options.append(f'<option value="{esc(name)}"{selected}>{esc(label)}</option>')
    return f'<select name="backend">{"".join(options)}</select>'


def _candidate_select(field: str, current: str | None, candidates, empty_label: str) -> str:
    """候補から選ぶセレクト(モデル・考える量)。

    **いま入っている値が候補に無くても選択肢に残す**(`_backend_select` と同じ理由)
    —— 落とすと保存し直した瞬間に既定へ倒れて、何を指定していたのかが画面から消える。
    相手が控えを持たない・その項目を持たない場合は候補が空になるが、**セレクト自体は
    出す** —— 消すと、JS が候補を入れに来たときに入れる先が無い。
    """
    names = list(candidates)
    if current and current not in names:
        names.append(current)
    options = [f'<option value="">{esc(empty_label)}</option>']
    for name in names:
        selected = " selected" if name == current else ""
        options.append(f'<option value="{esc(name)}"{selected}>{esc(name)}</option>')
    return f'<select name="{field}">{"".join(options)}</select>'


def _model_select(backend: str | None, current: str | None) -> str:
    """モデルのセレクト。**候補は控え**(`app/providers.py`)から取る。

    ここで相手に問い合わせない —— 管理画面の描画で外へ出ると、相手が落ちている
    ときにページ全体が待たされる(`_backend_select` と同じ約束)。選び直すときだけ
    `GET /ai/models` を引いて入れ替える(下の `COLLECT_BACKEND_SCRIPT`)。
    """
    spec = providers.get(answer.normalize_backend(backend))
    return _candidate_select("model", current, spec.models if spec else (), "相手の既定")


def _effort_select(backend: str | None, current: str | None) -> str:
    """考える量のセレクト。持たない相手では候補が空(「相手の既定」だけ)になる。"""
    return _candidate_select(
        "effort", current, providers.efforts_of(answer.normalize_backend(backend)), "相手の既定"
    )


def _backend_hint() -> str:
    """相手ごとに渡せるモデルと深さ。**選ぶ前に読めるところに置く**。

    モデルと深さはセレクトで選べるが、あれは**いま選んでいる相手のぶんだけ**なので、
    「どの相手に替えれば何が使えるか」はここでしか読めない。相手を選ぶ前に効く。
    """
    lines = []
    for name in answer.backend_names():
        spec = providers.get(name)
        if spec is None:
            continue
        parts = []
        if spec.models:
            parts.append("モデル: " + " / ".join(spec.models))
        if spec.efforts:
            parts.append("深さ: " + " / ".join(spec.efforts))
        lines.append(f"{esc(spec.label)} — " + ("、".join(parts) if parts else "指定なしでよい"))
    if not lines:
        return ""
    return '<p class="muted">' + "<br>".join(lines) + "</p>"


def _backend_label(item) -> str:
    """行に出す相手の名前。未指定なら既定だと分かるように書く。"""
    if not item.backend:
        return '<span class="muted">既定にまかせる</span>'
    spec = providers.get(item.backend)
    label = esc(spec.label if spec else item.backend)
    detail = " / ".join(x for x in (item.model, item.effort) if x)
    return label + (f'<br><span class="muted">{esc(detail)}</span>' if detail else "")


def _mode_select(current: str) -> str:
    """集め方を選ぶセレクト。**既定は足すほう** —— 消える側を既定にしない。"""
    options = "".join(
        f'<option value="{esc(mode)}"{" selected" if mode == current else ""}>'
        f"{esc(MODE_LABELS[mode])}</option>"
        for mode in collect.MODES
    )
    return f'<select name="mode">{options}</select>'


# 相手を選び直したときに、モデルと考える量の候補を入れ替える。
#
# **フォームは 1 ページに何枚もある**(収集ごとに 1 枚 + 追加用)ので、id では捕まえない
# —— 同じ id が並ぶと最初の 1 枚しか動かない。`change` を document で受けて、
# **そのフォームの中だけ**を書き換える。
#
# **初期表示は JS 抜きで正しい**(サーバー側が、いま選ばれている相手の候補で組む)。
# ここが動かない環境で失われるのは「相手を替えた直後に候補が入れ替わること」だけで、
# 相手を保存してから選び直せば同じところへ行ける。
#
# 候補を控えではなく `GET /ai/models` から取るのは、**選び直すのは人が待っている
# 場面だから** —— そこでだけ相手に聞きに行けば、控えが古くても実物に追いつける
# (描画のときに聞かない理由は `_model_select` にある)。
COLLECT_BACKEND_SCRIPT = """<script>
document.addEventListener('change', function (ev) {
  var sel = ev.target;
  if (!sel.matches || !sel.matches('.collect-form select[name="backend"]')) { return; }
  var form = sel.closest('form');
  var model = form.querySelector('select[name="model"]');
  var effort = form.querySelector('select[name="effort"]');
  if (!model && !effort) { return; }
  // 入れ替わるまで触らせない(古い候補のまま保存されるのを防ぐ)
  [model, effort].forEach(function (el) { if (el) { el.disabled = true; } });
  fetch('/ai/models?backend=' + encodeURIComponent(sel.value))
    .then(function (r) { return r.ok ? r.json() : { models: [], efforts: [] }; })
    .then(function (d) {
      fill(model, d.models || []);
      fill(effort, d.efforts || []);
    })
    .finally(function () {
      [model, effort].forEach(function (el) { if (el) { el.disabled = false; } });
    });
  function fill(el, names) {
    if (!el) { return; }
    // 選んでいた値は候補に無くても残す(サーバー側の組み立てと同じ約束)
    var keep = el.value;
    el.innerHTML = '';
    var head = document.createElement('option');
    head.value = ''; head.textContent = '相手の既定';
    el.appendChild(head);
    if (keep && names.indexOf(keep) < 0) { names = names.concat([keep]); }
    names.forEach(function (id) {
      var o = document.createElement('option');
      o.value = id; o.textContent = id;
      if (id === keep) { o.selected = true; }
      el.appendChild(o);
    });
  }
});
</script>"""


def _collect_html(sources: dict[str, Source], disabled: str) -> str:
    """収集(AI に集めさせて溜めていく)の節。

    **出すのは「間隔・次にいつ走るか・いま何件」の 3 つ**。無人で回る層なので、
    これが揃って初めて「動いているか」を画面から判断できる —— 件数だけでは
    止まっているのか集まっていないのか分からず、予定だけでは溜まっているか分からない。

    **前回の結果も出す**。失敗しても時計は止めない作りなので、こけたまま静かに
    回り続ける状態がありうる。理由を出さないと気づけない。

    **追加も画面からできる**。REST だけにしていた頃は、この層を使い始めるのに
    curl を書く必要があった —— 設定を足すのは画面の仕事である。
    """
    if not collect.is_enabled():
        return (
            '<p class="muted">収集は無効です。'
            "<code>CHIEZO_NOTES_DIR</code>(収集の定義の置き場)と"
            "<code>CHIEZO_TRIGGER_URL</code>(取り込みを起こす相手)を設定すると使えます。"
            "<br>集めるのも焼くのも取り込みの中で起きるので、途中の置き場は要りません。</p>"
        )
    items = collect.load()
    rows = []
    for item in items:
        due = jst.parse(item.next_run_at or "")
        last = jst.parse(item.last_run_at or "")
        if item.last_status == "ok":
            # 作り直しは「消えた件数」まで出す —— 増えた数だけでは、整理の結果
            # 何が落ちたのか読めない(見出しは開いたところに出す)
            removed = (
                f'<br><span class="stale">-{item.last_removed} 件</span>'
                if item.last_removed else ""
            )
            dropped = (
                f'<details><summary class="muted">消えたもの</summary>'
                f'<div class="muted">{esc("、".join(item.last_removed_titles))}</div></details>'
                if item.last_removed_titles else ""
            )
            result = (
                f'<span class="muted">{jst.format(last) if last else ""}</span>'
                f"<br>+{item.last_added} 件"
                + (f'<span class="muted">(重複 {item.last_skipped})</span>'
                   if item.last_skipped else "")
                + removed + dropped
            )
        elif item.last_status == "error":
            result = f'<span class="stale">失敗: {esc(item.last_error or "")}</span>'
        else:
            result = '<span class="muted">まだ走っていない</span>'
        # 止めている収集は行ごと薄くする(「AI の相手」の表と同じ扱い)
        cls = "" if item.enabled else ' class="off"'
        when = (
            esc(jst.format(due)) if (item.enabled and due)
            else '<span class="muted">止めている</span>'
        )
        toggle_label = "止める" if item.enabled else "有効にする"
        # 焼き先(長期記憶)の様子。まだ 1 度も焼いていなければそう出す
        src = sources.get(item.name)
        baked_docs = (
            f'<a href="{esc(browse_url(item.name))}">{src.doc_count:,} 件</a>'
            if src is not None else "まだ焼いていない"
        )
        # 誰が置いたのかは、有効にするか決める手がかり(外のアプリも置けるため)
        requester = (
            f'<br><span class="muted">依頼元: {esc(item.requested_by)}</span>'
            if item.requested_by else ""
        )
        # 作り直しは「返らなかったものが消える」ので、行のいちばん目立つところに出す
        mode_mark = ' <span class="stale">整理</span>' if item.is_refine() else ""
        # 次の 1 回が機械で埋まるかどうかは、押す前に見えていないと分からない
        if item.extract:
            mode_mark += (
                ' <span class="muted">抽出</span>'
                if item.cursor
                else ' <span class="stale">次は抽出</span>'
            )
        # 消す前の確認。**行の組み立てとは別に作る** —— 隣り合った文字列は
        # 三項演算子より先につながるので、行の途中で分岐を書くと後ろの断片まで
        # else 側へ吸い込まれ、開始タグの無い <form> ができる(実際にそうなった)
        delete_confirm = (
            f"収集「{item.name}」の設定と、溜めたもの({src.doc_count:,} 件)を"
            "まとめて消します。元に戻せません。よろしいですか?"
            if src is not None
            else f"収集「{item.name}」の設定を消します(まだ何も溜まっていません)。よろしいですか?"
        )
        rows.append(
            f"<tr{cls}>"
            f'<td><a href="{esc(browse_url(item.name))}">{esc(item.name)}</a>'
            f"{mode_mark}"
            f'<br><span class="muted">{esc(item.description)}</span>{requester}'
            f"<details><summary>プロンプト</summary>"
            f'<pre class="prompt-view">{esc(item.prompt)}</pre>'
            f'<p class="muted">進み具合(次の実行で {{cursor}} に入る値): '
            f'<code>{esc(item.cursor) or "(まだ無し)"}</code></p>'
            f"<details><summary>編集する</summary>"
            f'<form method="post" action="/admin/collect/{esc(item.name)}/edit" class="collect-form">'
            f'<p><label>説明<br><input name="description" value="{esc(item.description)}"></label></p>'
            f'<p><label>間隔(分)<br><input name="interval_minutes" type="number"'
            f' min="{collect.MIN_INTERVAL_MINUTES}" value="{item.interval_minutes}"></label></p>'
            f'<p><label>プロンプト<br><textarea name="prompt" rows="10">{esc(item.prompt)}</textarea></label></p>'
            f'<p><label>進み具合(空にすると最初から)<br>'
            f'<input name="cursor" value="{esc(item.cursor)}"></label></p>'
            f"<p><label>頼む相手<br>{_backend_select(item.backend)}</label></p>"
            f"<p><label>モデル<br>{_model_select(item.backend, item.model)}</label></p>"
            f"<p><label>考える量<br>{_effort_select(item.backend, item.effort)}</label></p>"
            f"{_backend_hint()}"
            f"<p><label>集め方<br>{_mode_select(item.mode)}</label></p>"
            f'<p><label>消えすぎの歯止め(前の何割を下回ったら止めるか。0 で外す)<br>'
            f'<input name="keep_ratio" type="number" step="0.05" min="0" max="1"'
            f' value="{item.keep_ratio}"></label></p>'
            f'<p><label>抽出の指定(JSON。空なら毎回 AI に集めさせる)<br>'
            f'<textarea name="extract" rows="8" spellcheck="false">'
            f"{esc(_extract_json(item))}</textarea></label></p>"
            f'<p class="muted">指定を入れると、<strong>進み具合が空のあいだの 1 回だけ</strong>'
            f" AI を呼ばず、手元の長期記憶から機械的に組み立てる(名前・年代・出典のように"
            f" 既に書いてあることは、書かせると混ざるが引けば済む)。"
            f" 進み具合が入った次からは、いつもどおり AI が肉付けする。"
            f" 進み具合を空にすれば、また機械のほうから始まる。</p>"
            f'<p class="muted">整理にすると、AI にいまの内容を読ませたうえで、'
            f" 直すものと足すものだけを返させます(触れなかったものはそのまま残ります)。"
            f" プロンプトに <code>{{current}}</code> を入れてください"
            f"(そこへ今ある内容が差し込まれます)。消すのは AI が墓標を付けたときだけです。</p>"
            f'<button type="submit">保存する</button></form></details>'
            f"<details><summary>AI に抽出の指定を書かせる</summary>"
            f'<form method="post" action="/admin/collect/draft-extract" class="collect-form">'
            f'<input type="hidden" name="name" value="{esc(item.name)}">'
            f'<p><label>どういう条件で抽出してほしいか<br>'
            f'<textarea name="want" rows="3"'
            f' placeholder="例: 印象派の画家を有名な順に30人。年代と様式が分かるように"'
            f"></textarea></label></p>"
            f'<p class="muted">書かせるのは指定だけで、保存はしません。'
            f" 書けたらその場で引いてみて、何件あるか・最初の数件がどうなるかを出します。</p>"
            f'<button type="submit">指定を書かせる</button></form></details>'
            f"<details><summary>AI に相談して直す</summary>"
            f'<form method="post" action="/admin/collect/consult" class="collect-form">'
            f'<input type="hidden" name="name" value="{esc(item.name)}">'
            f'<p><label>どう直したいか<br>'
            f'<textarea name="feedback" rows="4"'
            f' placeholder="例: 件数を5件に減らし、海外のニュースも入れて。'
            f'出典は必ず付けさせて。"></textarea></label></p>'
            f'<p class="muted">AI に聞くので十数秒〜1分ほどかかります。案は保存されないので、見てから決められます。</p>'
            f'<button type="submit">相談する</button></form></details>'
            f"</details></td>"
            f"<td>{_backend_label(item)}</td>"
            f"<td>{item.interval_minutes} 分ごと</td>"
            f"<td>{when}</td>"
            f"<td>{baked_docs}</td>"
            f"<td>{result}</td>"
            f"<td>"
            f'<form class="init-form" method="post" action="/admin/collect/{esc(item.name)}/toggle">'
            f'<button type="submit">{toggle_label}</button></form>'
            f'<form class="init-form" method="post" action="/admin/collect/{esc(item.name)}/preview">'
            f'<button type="submit" title="AI に 1 回集めさせて、焼かずに差分だけ見ます">'
            f"試しに集めて差分を見る</button></form>"
            f'<form class="init-form" method="post" action="/admin/collect/{esc(item.name)}/run"'
            f"{disabled}>"
            f'<button type="submit"{disabled}>いま集めて焼く</button></form>'
            f'<form class="init-form" method="post" action="/admin/collect/{esc(item.name)}/delete"'
            f" onsubmit=\"return confirm('{esc(delete_confirm)}')\">"
            f'<button type="submit">削除</button></form>'
            f"</td></tr>"
        )
    table = f"""
<table>
<thead>
<tr><th>name</th><th>相手</th><th>間隔</th><th>次にいつ</th><th>長期記憶</th><th>前回</th><th></th></tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
""" if rows else '<p class="muted">まだ収集がありません。下のフォームから作れます。</p>'
    return f"""
{table}
<details><summary>収集を追加する</summary>
<form method="post" action="/admin/collect/create" class="collect-form">
<p><label>name(ソース名になる。英小文字・数字・_)<br>
<input name="name" required pattern="[a-z][a-z0-9_]{{1,30}}" placeholder="tech_news"></label></p>
<p><label>説明(画面に出るだけ)<br>
<input name="description" placeholder="技術ニュース"></label></p>
<p><label>間隔(分。{collect.MIN_INTERVAL_MINUTES} 以上)<br>
<input name="interval_minutes" type="number" min="{collect.MIN_INTERVAL_MINUTES}" value="360"></label></p>
<p><label>頼む相手<br>{_backend_select(None)}</label></p>
{_backend_hint()}
<p><label>集め方<br>{_mode_select(collect.MODE_APPEND)}</label></p>
<p class="muted">整理を選ぶときは、プロンプトに <code>{{current}}</code> を入れる
(そこへ今ある内容が差し込まれる)。返さなかったものはそのまま残り、消えるのは AI が
墓標を付けたときだけ。</p>
<p><label>プロンプト<br>
<textarea name="prompt" rows="6" required
 placeholder="{esc(collect.SAMPLE["prompt"])}"></textarea></label></p>
<p><label><input type="checkbox" name="web" value="1" checked> web 検索を開ける</label></p>
<button type="submit">追加する(止めた状態で作る)</button>
</form>
<form method="post" action="/admin/collect/consult" class="collect-form">
<p class="muted">プロンプトの書き方が決まらないときは、AI に書いてもらってから直せる。</p>
<p><label>集めたいもの(ふつうの言葉で)<br>
<input name="want" required placeholder="近所の飲食店を、地域を変えながら少しずつ"></label></p>
<button type="submit">AI に相談する</button>
</form>
</details>
<p class="muted">
まとまったダンプの無いもの(直近のニュース、入れ替わりの速い店、人物の関係)を
AI に集めさせて溜めていく層。<strong>溜め先は収集ごとに別のソース</strong>なので、
<code>/v1/&lt;name&gt;/search</code> やブラウズ画面でそのまま引ける。<br>
<strong>集めるのも焼くのも取り込みの中で起きる</strong>ので、押すボタンは 1 つだけ
(途中の待ち行列は無い)。焼き直しは全件だが素材に前世代を混ぜるので、
中身は追記として積み上がる(同じ見出しは新しいほうで置き換わる)。<br>
プロンプトの <code>{{cursor}}</code> が実行ごとに進む印に置き換わり、AI が
<code>next_cursor</code> で次を返す。これで「前回以降のニュース」「次の地域」
「次に調べる人」が同じ仕組みに乗る。返させる形は
<code>{{"items":[{{"title","body","tags","url"}}],"next_cursor"}}</code> で、
<strong>title が重複の鍵</strong>。<br>
<strong>追加したものは止めた状態で作る</strong> —— プロンプトを見直してから
「有効にする」で動き出す(いきなり AI の枠を使わない)。
</p>
{COLLECT_BACKEND_SCRIPT}
"""


def _short_term_section_html(sources: dict[str, Source]) -> str:
    """短期記憶(notes)の節。**長期側と同じ体裁の表で出す**。

    表にするのは、見に来る人が知りたいことが長期側と同じだから —— 何件あって、
    最後に動いたのはいつで、いまの形(スキーマ)で引けるのか。かつては件数を 1 行の
    文で出し、その下に検索への入口を別に置いていたが、隣の節と見比べるときに
    目の動かし方が変わるだけだった。

    **列は長期側の写しにしない。** ダンプも取り込みも無いので `dump_date` と
    `built_at` は書きようがなく、空欄が並ぶ。**代わりに「最後に書かれた時刻」を出す**
    —— 短期記憶で「動いているか」を言えるのはそこ。`lang` も notes は持たない。

    **操作の列は持たない**。長期側の右端は再構築ボタンだが、短期記憶は取り込みで
    焼くソースではないので押すものが無い。検索への入口は名前がそのままリンクなので、
    「検索する」を別の列に置くと**同じ行き先が 1 行に 2 つ**並んで幅を食うだけになる。

    **表に混ぜないのは今までどおり**。再構築ボタンが出ていた頃は、押すと trigger が
    unknown source を返すだけなのに、確認ダイアログだけが「ダンプの取得からやり直します」
    と言っていた(唯一書き込めるソースで、消えたと読める文言がいちばん危ない行に出ていた)。
    件数の出どころも違う(走査ではなく描画時に数える。`notes.count()` 参照)。

    中身そのものは出さず、件数とタグの分布までに留める。1 件ずつ読むのはブラウズ画面の
    仕事で、そちらは見に行った人だけが見る。
    """
    if not notes.is_enabled():
        return (
            '<p class="muted">短期記憶は無効です。書き込み可能なディレクトリを'
            " <code>CHIEZO_NOTES_DIR</code> に設定すると有効になります。</p>"
        )
    # やること層への入口はメモが 0 件でも出す(タスクはこれから足すもの)
    tasks_link = (
        '<p><a href="/tasks/">→ やること(タスク・ルール)</a> '
        '<span class="muted">短期記憶の上にタグで載っている層。'
        "ここからは認証なしで開く</span></p>"
    )
    total = notes.count()
    if not total:
        return (
            '<p class="muted">まだ何も覚えていません。MCP の <code>remember</code> か'
            " <code>POST /v1/notes</code> で書き込めます。</p>" + tasks_link
        )
    browse = esc(browse_url(notes.SOURCE_NAME))
    src = sources.get(notes.SOURCE_NAME)
    # スキーマは長期側と同じ意味(古いと filter / tag が効かない)ので同じ出し方にする
    latest = latest_schema_version()
    version = src.schema_version if src is not None else None
    if version is None:
        schema_cell = '<span class="muted">不明</span>'
    elif version >= latest:
        schema_cell = str(version)
    else:
        schema_cell = f'{version} <span class="stale">(最新: {latest})</span>'
    written = jst.parse(notes.last_updated() or "")
    table = f"""
<table>
<thead>
<tr><th>name</th><th>kind</th><th>docs</th><th>最後に書かれた</th><th>schema_version</th></tr>
</thead>
<tbody>
<tr>
<td><a href="{browse}">{esc(notes.SOURCE_NAME)}</a></td>
<td>{esc(notes.SOURCE_KIND)}</td>
<td>{total:,}</td>
<td>{esc(jst.format(written)) if written else '<span class="muted">—</span>'}</td>
<td>{schema_cell}</td>
</tr>
</tbody>
</table>
"""
    tags = notes.tag_summary()
    tag_html = (
        '<p class="muted">タグ: '
        + " / ".join(f"{esc(tag)} {docs:,}" for tag, docs in tags)
        + "</p>"
        if tags
        else ""
    )
    return f"""
{table}
{tag_html}
{tasks_link}
<p class="muted">
長期記憶と同じ口で引ける(<code>/v1/notes/search|doc|filter|tags</code>)。
書き込みは MCP の <code>remember</code> か <code>POST /v1/notes</code>、
新しい順に思い出すのは <code>/v1/notes/recall</code>。
取り込みで焼くソースではないので、ダンプの日付も焼いた時刻も持たない
(再構築もできない。書き込みが直接届く唯一の場所)。
</p>
"""


def _answer_status_html() -> str:
    """管理画面に出す「使う」層の状態(既定では無効なので、その旨を出す)。

    相手の増やし方そのものは下の「話す相手」節(app/views/ai_settings.py)が持つ。
    ここは「いま話せるか」と会話画面への入口だけ。
    """
    names = answer.backend_names()
    if not names:
        return (
            '<p class="muted">まだ話せる相手がいません。下の「話す相手」で有効にしてください'
            "(LAN の別マシンで動かしている推論サーバを指すなら、"
            " <code>CHIEZO_LLM_URL</code> に URL を設定します)。</p>"
        )
    links = " / ".join(
        f'<a href="{CHAT_PATH}?backend={quote(name)}">{esc(answer.backend_label(name))}</a>'
        for name in names
    )
    return (
        f'<p><a href="{CHAT_PATH}">→ AI と話す(Chiezo の知識を引きます)</a></p>'
        f'<p class="muted">話せる相手: {links}</p>'
    )


# 管理画面の面。**1 枚に積み上げない** —— 知識・AI・サーバーは見に来る目的が違い、
# 縦に並べると、いま見たい節に着くまで無関係な表を何度もスクロールすることになる。
# 玄関に要約を置き、深いところは選んで入る。
PAGES = (
    ("/admin/memory", "記憶", "溜めて引く。短期記憶・長期記憶・集める・固化・初期化"),
    ("/admin/ai", "AI と鍵", "貸し出すもの。話せる相手、使用量、依頼の履歴"),
    ("/admin/media", "見比べ", "作らせたものを並べて選ぶ。手元のものも持ち込める"),
    ("/admin/server", "その他", "このサーバー。Claude Code 連携といま動いているビルド"),
)


def nav_html(current: str) -> str:
    """面のあいだを行き来する帯。**どの面にも同じものを出す** ——
    玄関へ戻ってから選び直す、を毎回させない。

    別のモジュールの面（`views/media_compare.py`）からも呼ぶので公開している ——
    写しを持つと、面が増えたときに片方の帯にだけ出ないことになる。
    """
    links = []
    for path, label, _note in PAGES:
        if path == current:
            links.append(f'<strong>{esc(label)}</strong>')
        else:
            links.append(f'<a href="{path}">{esc(label)}</a>')
    return '<nav class="admin-nav"><a href="/admin">管理画面</a>' + "".join(
        f" · {link}" for link in links
    ) + "</nav>"


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    """玄関。**いま何が起きているかが 1 画面で読めること**だけを受け持つ。

    表は持たない —— 数と状態だけを出して、直しに行くのは各面。
    """
    sources: dict[str, Source] = request.app.state.sources
    job = _fetch_trigger_status()
    long_term = {n: s for n, s in sources.items() if not s.mutable}
    short_term = {n: s for n, s in sources.items() if s.mutable}
    running = ai_history.running_rows()

    docs = sum(s.doc_count for s in long_term.values())
    notes_docs = sum(s.doc_count for s in short_term.values())
    collections = collect.load() if collect.is_enabled() else []
    enabled_collections = [c for c in collections if c.enabled]

    summary = {
        "/admin/memory": (
            f"長期 {len(long_term)} ソース / {docs:,} 文書、短期 {notes_docs:,} 件。"
            f"収集 {len(collections)} 件(有効 {len(enabled_collections)} 件)"
        ),
        "/admin/ai": (
            f"話せる相手 {len(answer.backend_names())} 件。"
            + (f"<strong>いま {len(running)} 件走っている</strong>" if running else "いま走っているものは無い")
        ),
        "/admin/server": esc(build_info.describe().splitlines()[0] if build_info.describe() else ""),
    }

    cards = "\n".join(
        f'<div class="admin-card"><h2><a href="{path}">{esc(label)}</a></h2>'
        f'<p class="muted">{esc(note)}</p>'
        f'<p>{summary.get(path, "")}</p></div>'
        for path, label, note in PAGES
    )

    body = f"""
<h1>Chiezo 管理画面</h1>
<p>{_disk_html(request.app.state.data_dir)}</p>
{_job_status_html(job)}
{_running_html(running)}
<div class="admin-cards">
{cards}
</div>
"""
    return HTMLResponse(content=page_shell("管理画面", body))


def _running_html(running: list[dict]) -> str:
    """いま走っている AI への依頼。**玄関に出す唯一の表**。

    ここだけ表なのは、**待たされているときに見に来る画面がここだから** ——
    数だけでは「何が遅いのか」が分からず、結局 AI の面まで開くことになる。
    出すのは相手・種類・経過までで、詳しくは AI の面が受け持つ。

    **走っていないときは何も出さない。** 空の表を置くと、いつも何かが動いていない
    ことのほうが目立つ。
    """
    if not running:
        return ""

    rows = "".join(
        f"<tr><td>{esc(ai_log.kind_label(r['kind']))}</td>"
        # 相手とモデルの書き方は `ai_history` と共有する —— 別々に書くと、
        # 同じ依頼が玄関と表で違って見える(経過の `elapsed` と同じ理由)
        f"<td>{ai_history.who_html(r['backend'], r.get('model') or '')}</td>"
        f"<td>{esc(r['state'])}</td>"
        f'<td class="muted">{esc(ai_history.elapsed(r["at"]))}</td></tr>'
        for r in running
    )

    return f"""
<h2>いま走っている AI への依頼</h2>
<table>
<thead><tr><th>依頼</th><th>相手</th><th>状態</th><th>経過</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="muted">詳しくは <a href="/admin/ai#ai-history">AI と鍵</a>。</p>
"""


@router.get("/admin/memory", response_class=HTMLResponse)
def admin_memory(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    job = _fetch_trigger_status()
    disabled = run_buttons_disabled(job)
    latest_schema = latest_schema_version()

    def schema_cell(version: int) -> str:
        if version >= latest_schema:
            return str(version)
        return f'{version} <span class="stale">(最新: {latest_schema})</span>'

    # 知識は 2 層あり、扱いが違う(下の _short_term_section_html):
    # 長期(大脳)= 取り込みで焼く読み取り専用のソース(素材はダンプか、固めた短期記憶)、
    # 短期(海馬)= 唯一書き込める notes。
    long_term = {n: s for n, s in sources.items() if not s.mutable}
    short_term = {n: s for n, s in sources.items() if s.mutable}

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
        for s in sorted(long_term.values(), key=lambda s: s.name)
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
        # 固化のテーマ(kind=memory)はここに出さない。長期記憶に足すという意味では
        # 同じだが、素材は外のダンプではなく短期記憶なので、操作は下の固化の節に集める
        # (両方に出すと、どちらを押せばいいのか読めない)
        if m.get("group") not in ("osm", "wikipedia") and m.get("kind") != "memory"
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
{nav_html("/admin/memory")}
<h1>記憶(溜めて引く)</h1>
<p class="muted">
知識は 2 層。<strong>短期記憶</strong>は Chiezo で唯一書き込める置き場で、覚えたことが
その場で積まれる。<strong>長期記憶</strong>は読み取り専用のソースで、ダンプから焼いたものと、
短期記憶から移した(固化した)ものが並ぶ。引くときの口はどちらも同じ。
</p>

<h2 id="short-term">短期記憶(覚えたこと)</h2>
{_short_term_section_html(short_term)}

<h2>長期記憶(ためた知識)</h2>
<p>登録ソース数: {len(long_term)} / 最新のスキーマバージョン: {latest_schema}<br>
{_disk_html(request.app.state.data_dir)}</p>
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

<h2 id="collect">集める(AI に集めさせて溜める)</h2>
{_collect_html(sources, disabled)}

<h2 id="consolidation">短期記憶から移す(固化)</h2>
{_memory_html(sources, disabled)}

<h2>未初期化データの初期化</h2>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th></th></tr>
</thead>
<tbody>
{init_rows}
</tbody>
</table>

"""
    return HTMLResponse(content=page_shell("記憶", body))


@router.get("/admin/ai", response_class=HTMLResponse)
async def admin_ai(request: Request):
    """AI と鍵の面。**呼ぶ側に認証情報を持たせないための面**をここにまとめる。"""
    body = f"""
{nav_html("/admin/ai")}
<h1>AI と鍵(貸し出すもの)</h1>
<p class="muted">
呼ぶ側に認証情報を持たせないための面。鍵はここで預かり、話せる相手と、
絵・音・動画・声を作る相手を同じ表で扱う。
</p>

<h2>ためた知識を使う AI</h2>
{_answer_status_html()}

{await ai_settings.section_html(request)}

{ai_usage.section_html(request)}

{ai_history.section_html(*_history_args(request))}
"""
    return HTMLResponse(content=page_shell("AI と鍵", body))


@router.get("/admin/server", response_class=HTMLResponse)
def admin_server(_request: Request):
    """このサーバー自身のこと。どちらも**読むだけ**で、押して変わるものは無い。"""
    body = f"""
{nav_html("/admin/server")}
<h1>このサーバー</h1>

<h2>Claude Code 連携設定</h2>
<p class="muted">
いま設定を吐き出したら(<code>scripts/gen_claude_config.sh</code>)どういう内容になるかのプレビュー。
現在の登録ソースから生成した CLAUDE.md ブロックを表示する(実ファイルは書き換えない)。
</p>
<p><a href="/admin/claude-config">→ 生成される設定を見る</a></p>

<h2>いま動いているビルド</h2>
<p class="muted">
{esc(build_info.describe())}<br>
ビルド日時(JST)とビルド元のコミット。手元の <code>git log -1</code> と見比べれば、
変更が反映済みかが分かる。<code>docker compose pull &amp;&amp; docker compose up -d</code>
のあと、ここが新しくなっていなければ古いイメージのままになっている。
</p>
"""
    return HTMLResponse(content=page_shell("このサーバー", body))


@router.get("/admin/osm", response_class=HTMLResponse)
def admin_osm(request: Request, q: str | None = Query(None, description="国名・region での絞り込み")):
    """OSM 国別ソースの初期化画面(管理画面の osm 行の「国を選ぶ」から開く)。

    Geofabrik の国別抽出は 195 件あり、管理画面の一覧に全部並べると他のソースが埋もれる。
    そこで一覧では osm 1 行にまとめ、国の選択だけをこの画面に切り出している。
    """
    sources: dict[str, Source] = request.app.state.sources
    catalog = {n: m for n, m in initializable_sources().items() if m.get("group") == "osm"}
    job = _fetch_trigger_status()
    disabled = run_buttons_disabled(job)

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
        content=page_shell("OSM 国別の初期化", body)
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
    disabled = run_buttons_disabled(job)

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
        content=page_shell("Wikipedia 言語版の初期化", body)
    )


def trigger_run(source: str) -> None:
    """chiezo-trigger の POST /run/{source} を叩く。失敗は HTTPException にする。

    **画面へ戻さない形も要る** —— 収集の時計(`app/main.py`)がここから取り込みを
    起こすので、リダイレクトを返されると使えない。
    """
    try:
        res = httpx.post(f"{TRIGGER_URL}/run/{source}", timeout=TRIGGER_TIMEOUT)
    except httpx.HTTPError as e:
        # 例外の文字列は内部 URL 等を含みうるのでレスポンスに載せない(上の
        # _fetch_trigger_status と同じ理由。詳細はログへ)。
        log.warning("chiezo-trigger run request failed: %s", e)
        raise HTTPException(
            502,
            {
                "error": "chiezo-trigger unreachable (details in app logs)",
                "hint": "長期記憶へ書き込むときだけ要るサービス。立てるまで初期化・再構築・固化はできない",
            },
        ) from e
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json())


def _proxy_trigger_run(source: str) -> RedirectResponse:
    """上を叩いて管理画面へ戻す(init / rebuild 共通)。"""
    trigger_run(source)
    return RedirectResponse(url="/admin/memory", status_code=303)


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


@router.post("/admin/media/{job_id}/cancel")
def admin_media_cancel(job_id: str):
    """走っている生成を止める(「AI への依頼」の表の「止める」)。

    **こちらの待ち枠を空けるのが主目的。** 向こう側の CLI までは止まらないので、
    相手のプロセスは自分の時間切れまで走り続ける —— それでも枠が空けば、
    後ろで順番待ちしていた依頼は先へ進める。
    """
    from app import media

    media.cancel_job(job_id)
    return RedirectResponse("/admin/ai#ai-history", status_code=303)


@router.post("/admin/rebuild/{source}")
def admin_rebuild(source: str, request: Request):
    """登録済みソースの再構築(管理画面の「再構築」ボタン)。

    init と違い登録済みであることを要求する(未登録は init 側の担当)。ジョブの実体は
    init と同じ ingest の一括取り込みで、ブルーグリーン(別ファイル構築 → シンボリック
    リンク差し替え)なので構築中も現行 DB での配信は続く。ソースの正は trigger 側の
    ADAPTERS なので、カタログに無い登録済みソースでも trigger に判断を委ねる。

    ただし短期記憶(`mutable` なソース)だけはここで断る。取り込みで焼くソースでは
    ないので trigger も unknown source を返すが、そこまで行かせると「実行しようとした
    が失敗した」に見える。書き込める唯一のソースについては、素通しにしない。
    """
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "chiezo-trigger is not configured (CHIEZO_TRIGGER_URL unset)"})
    sources: dict[str, Source] = request.app.state.sources
    src = sources.get(source)
    if src is None:
        raise HTTPException(404, {"error": f"source not initialized: {source}"})
    if src.mutable:
        raise HTTPException(
            409,
            {
                "error": f"source is not rebuildable: {source}",
                "hint": "短期記憶は取り込みで焼くソースではないので再構築の対象にならない",
            },
        )
    return _proxy_trigger_run(source)


@router.post("/admin/collect/create")
async def admin_collect_create(request: Request):
    """画面のフォームから収集を作る。

    **止めた状態で作る**(REST の `POST /v1/collect` は有効で作る)。画面から足すのは
    たいてい書きながら考える場面で、送った瞬間に AI を呼び始めるのは驚きが大きい ——
    プロンプトを見直してから「有効にする」を押す流れにする。
    """
    form = await request.form()
    interval = str(form.get("interval_minutes") or "").strip()
    item = collect.create(
        name=str(form.get("name") or "").strip(),
        prompt=str(form.get("prompt") or ""),
        interval_minutes=int(interval) if interval.isdigit() else 360,
        description=str(form.get("description") or "").strip(),
        web=bool(form.get("web")),
        mode=collect.normalize_mode(form.get("mode")),
        backend=_blank_to_none(form.get("backend")),
    )
    collect.update(item.name, enabled=False)
    # 作った時点で空の DB ができるので、ソースを取り直して検索に出るようにする。
    # main を関数の中で import するのは、views → main の循環参照を避けるため
    # (main が router を include する側。下の「いま走らせる」と同じ書き方)
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url="/admin/memory#collect", status_code=303)


@router.post("/admin/collect/{name}/edit")
async def admin_collect_edit(name: str, request: Request):
    """プロンプト・説明・間隔・進み具合を書き換える。

    **進み具合(カーソル)もここで直せる**。集め直したい・別の地域から始めたい、が
    プロンプトを書き換えるのと同じ場面で起きるため(値を消せば最初から)。
    """
    form = await request.form()
    interval = str(form.get("interval_minutes") or "").strip()
    collect.update(
        name,
        prompt=str(form.get("prompt") or ""),
        description=str(form.get("description") or ""),
        interval_minutes=int(interval) if interval.isdigit() else None,
        # 空にできるように、cursor だけは None ではなく空文字を通す
        cursor=str(form.get("cursor") or ""),
        mode=collect.normalize_mode(form.get("mode")),
        # 空欄は「使わない」。指定を外せるのはここだけ
        extract=_parse_extract(form.get("extract")),
        # 0 も意味のある値(守りを外す)なので、空のときだけ触らない
        keep_ratio=_ratio(form.get("keep_ratio")),
        # 相手・モデル・深さは**空を「既定にまかせる」として通す** ——
        # `collect.update` は None を「触らない」と読むので、空文字で渡して消す
        backend=str(form.get("backend") or ""),
        model=str(form.get("model") or ""),
        effort=str(form.get("effort") or ""),
    )
    return RedirectResponse(url="/admin/memory#collect", status_code=303)


@router.post("/admin/collect/consult")
async def admin_collect_consult(request: Request):
    """AI にプロンプトの案を書いてもらい、そのまま直して保存できる画面を返す。

    **リダイレクトせずにその場で返す**。案は数百字あってクエリに載らないし、
    保存する前に手で直したいから —— 案を出す・直す・保存するを 1 枚に置く。

    **この画面だけ待たせる**(AI の応答ぶん、十数秒〜1 分)。管理画面は JS を持たない
    ので、待っている間の見せ方は作れない。押す前に、時間がかかることをボタンの
    近くに書いてある。
    """
    from app.main import draft_collection_prompt

    form = await request.form()
    name = str(form.get("name") or "").strip() or None
    want = str(form.get("want") or "").strip()
    feedback = str(form.get("feedback") or "").strip()
    current = str(form.get("current") or "").strip()
    if name and not current:
        current = collect.get(name).prompt
    if not want and name:
        want = collect.get(name).description or name
    try:
        draft = await draft_collection_prompt(want, current, feedback, name)
        error = ""
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
        draft = current
        error = f'<p class="stale">⚠️ 相談できませんでした: {esc(str(detail))}</p>'
    return HTMLResponse(_consult_page_html(name, want, draft, error))


@router.post("/admin/collect/draft-extract")
async def admin_collect_draft_extract(request: Request):
    """依頼文から抽出の指定を書かせて、**引いた結果と一緒に**見せる。

    **保存はしない**。指定は保存すると次の実行の中身が変わるので、
    何件取れて何が出るかを見てから決められるようにする。

    **この画面も待たせる**(AI の応答ぶん)。管理画面は JS を持たない。
    """
    from app.main import ExtractDraft, collect_draft_extract

    form = await request.form()
    name = str(form.get("name") or "").strip() or None
    want = str(form.get("want") or "").strip()
    try:
        drafted = await collect_draft_extract(
            request, ExtractDraft(want=want, name=name)
        )
        error = ""
    except HTTPException as e:
        log.warning("draft extract refused: name=%s status=%s detail=%r", name, e.status_code, e.detail)
        drafted = None
        error = f'<p class="stale">⚠️ 書けませんでした({_refusal_text(e.status_code)})</p>'
    except Exception as e:
        log.exception("draft extract failed: name=%s", name)
        drafted = None
        error = f'<p class="stale">⚠️ 書けませんでした({esc(type(e).__name__)})</p>'
    return HTMLResponse(_draft_extract_page_html(name, want, drafted, error))


def _draft_extract_page_html(name: str | None, want: str, drafted: dict | None, error: str) -> str:
    """書けた指定と、それで実際に引けたものを 1 枚に置く。

    **引いた結果を必ず添える** —— タグは完全一致でしか引けないので、それらしい
    名前を書かれると静かな 0 件になる。保存してから気づくと、次の実行まで分からない。
    """
    if drafted is None:
        result = ""
        save = ""
    else:
        spec_json = json.dumps(drafted["extract"], ensure_ascii=False, indent=2)
        samples = "".join(
            f"<li><strong>{esc(item['title'])}</strong>"
            f'<br><span class="muted">{esc(" / ".join(item["tags"])) or "(タグなし)"}</span>'
            f'<br><span class="muted">{esc(item.get("url", ""))}</span></li>'
            for item in drafted["sample"]
        )
        candidates = drafted.get("candidates") or []
        # 0 件だけが失敗ではない。それらしい一般名は「実在はするが数件しか
        # 付いていないタグ」に当たり、静かに痩せた結果になる
        listed = " / ".join(f"{c['tag']}({c['docs']} 件)" for c in candidates)
        hint = (
            '<p class="stale">頼んだ件数に届きませんでした。タグは完全一致でしか'
            f"引けません。実在するタグ: {esc(listed)}</p>"
            if candidates
            else ""
        )
        result = (
            f"<p>この指定で <strong>{drafted['total']} 件</strong>取れます。</p>"
            f"{hint}<ul>{samples}</ul>"
        )
        current = collect.get(name) if name else None
        save = (
            f'<form method="post" action="/admin/collect/{esc(name)}/edit" class="collect-form">'
            f'<input type="hidden" name="description" value="{esc(current.description)}">'
            f'<input type="hidden" name="interval_minutes" value="{current.interval_minutes}">'
            f'<input type="hidden" name="cursor" value="{esc(current.cursor)}">'
            f'<input type="hidden" name="mode" value="{esc(current.mode)}">'
            f'<textarea name="prompt" hidden>{esc(current.prompt)}</textarea>'
            f'<p><label>この指定(直してから保存できる)<br>'
            f'<textarea name="extract" rows="16" spellcheck="false">{esc(spec_json)}</textarea>'
            f"</label></p>"
            f'<button type="submit">この指定で保存する</button></form>'
        ) if current else ""

    body = f"""
<h1>{esc(name or "")}の抽出の指定を書かせる</h1>
{error}
<p class="muted">頼んだこと: {esc(want) or "(指定なし)"}</p>
{result}
{save}
<p class="muted"><a href="/admin/memory#collect">管理画面へ戻る</a>(保存しなければ何も変わりません)</p>
"""
    return page_shell("抽出の指定", body)


@router.post("/admin/collect/{name}/toggle")
def admin_collect_toggle(name: str):
    """有効・無効を切り替える(見本を動かし始める入口でもある)。"""
    current = collect.get(name)
    collect.update(name, enabled=not current.enabled)
    return RedirectResponse(url="/admin/memory#collect", status_code=303)


@router.post("/admin/collect/{name}/delete")
def admin_collect_delete(request: Request, name: str):
    """設定と、焼いたソースをまとめて消す。

    **画面からは 1 つの操作にする。** 設定だけ消して中身が残ると、一覧から
    消えたのに検索には出続けるものができ、後から辿る手段が無くなる
    (収集の定義が消えているので、どこから来たのかも読めない)。

    **消せるのは trigger だけ**(`chiezo-app` は `corpus/` を読み取り専用で
    マウントしている)。向こうは種別を確かめて、集めたものだけを消す。
    **ソースを消せなくても設定は消す** —— trigger が立っていない構成は普通に
    あるので、そこで操作ごと止めない。
    """
    dropped = _drop_collect_source(name)
    collect.remove(name)
    # 消した後のソース表を作り直す(消えたものが一覧に残らないように)
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url=f"/admin/memory#collect{'' if dropped else '&kept'}", status_code=303)


def _drop_collect_source(name: str) -> bool:
    """焼いたソースを trigger に消させる。消せたかどうかを返す。

    **失敗しても例外にしない。** ここは設定を消すついでの片付けで、
    trigger が居ない・まだ 1 度も焼いていない、はどちらも普通の状態。
    """
    if not TRIGGER_URL:
        return False
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.delete(f"{TRIGGER_URL}/source/{name}")
        if res.status_code == 200:
            return True
        log.warning("could not drop source %s: %s %s", name, res.status_code, res.text[:200])
    except httpx.HTTPError as e:
        log.warning("could not reach the trigger to drop %s: %s", name, e)
    return False


@router.post("/admin/collect/{name}/run")
def admin_collect_run(name: str):
    """予定を待たずに 1 回、集めて焼く(管理画面の「いま集めて焼く」)。

    **止めている収集もここからは走らせる** —— 有効にする前に一度試せないと、
    プロンプトが通るかを確かめる手段が無くなる。REST の `/v1/collect/{name}/run` は
    同じことを断る(外のアプリに勝手な実行を許さないため)。画面を開けるのは
    Chiezo を操作している人だけ、という前提の差。
    """
    from app.main import start_collection_bake

    start_collection_bake(name)
    return RedirectResponse(url="/admin/memory#collect", status_code=303)


def _blank_to_none(raw) -> str | None:
    """空欄は「指定しない」。作るときは None を渡す(既定にまかせる)。"""
    return str(raw or "").strip() or None


def _ratio(raw) -> float | None:
    """歯止めの入力。**空は「触らない」、0 は「守りを外す」**(混ぜない)。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_json(item) -> str:
    """抽出の指定を、編集できる文字列にする。持っていなければ空。"""
    if not item.extract:
        return ""
    return json.dumps(item.extract, ensure_ascii=False, indent=2)


def _parse_extract(raw):
    """フォームの文字列を指定に戻す。**空欄は「使わない」**。

    JSON として読めないものはその場で断る。保存してしまうと、次に走ったときに
    初めて分かる —— 無人で回る層なので、そのとき見ている人はいない。
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as e:
        raise HTTPException(400, {"error": f"抽出の指定が JSON として読めません: {e}"}) from None
    return value


def _refusal_text(status: int) -> str:
    """下見を断られた理由の一言。**この表にある文言しか画面へ出さない**。"""
    if status == 404:
        return "その収集はありません"
    if status == 403:
        return "止めている収集です"
    if status == 400:
        return "収集の設定が受け付けられません。詳細は app コンテナのログ"
    if status in (502, 504):
        return "頼んだ AI から結果を受け取れません。詳細は app コンテナのログ"
    return "詳細は app コンテナのログ"


@router.post("/admin/collect/{name}/preview")
async def admin_collect_preview(name: str, request: Request):
    """**焼かずに**1 回集めさせて、前世代との差分を見せる。

    整理(作り直し)を育てるための道具。何が増えて・何が残って・何が消えるかを
    見てから焼けないと、プロンプトの直しようがない(件数と勘で調整することになる)。

    **止めている収集もここからは見られる**(「いま集めて焼く」と同じ判断)。
    REST の `/v1/collect/{name}/preview` は止まっているものを断る。

    **この画面も待たせる**(AI の応答ぶん)。管理画面は JS を持たないので、
    待っている間の見せ方は作れない。
    """
    from app.main import collect_preview

    try:
        result = await collect_preview(name, request.app.state.sources)
        error = ""
    except HTTPException as e:
        # 断り方は配信側が組み立てた文言だが、そのまま画面へ流すと接続先や相手の
        # 応答本文まで一緒に出る。**どの種類で断られたか**だけを出し、中身はログに残す
        log.warning("collect preview refused: name=%s status=%s detail=%r", name, e.status_code, e.detail)
        result = None
        error = f'<p class="stale">⚠️ 試せませんでした({_refusal_text(e.status_code)})</p>'
    except Exception as e:
        # 相手の落ち方は読めない。種別だけ画面に出す(文言はログへ)
        log.exception("collect preview failed: name=%s", name)
        result = None
        error = f'<p class="stale">⚠️ 試せませんでした({esc(type(e).__name__)})</p>'
    return HTMLResponse(_preview_page_html(name, result, error))


def _preview_page_html(name: str, result: dict | None, error: str) -> str:
    """下見の結果 1 枚。**焼いていないことを最初に書く**。

    数字だけ見せると「もう入れ替わった」と読める —— この画面を見に来る人は
    まさにそれを心配して押している。
    """
    if result is None:
        body = error
    else:
        blocked = (
            f'<p class="stale">⚠️ このまま焼こうとすると止まります: {esc(result["blocked"])}</p>'
            if result.get("blocked") else ""
        )
        removed = (
            f'<p>消えるもの: {esc("、".join(result["removed_titles"]))}'
            + ("ほか" if result["removed"] > len(result["removed_titles"]) else "")
            + "</p>"
            if result["removed_titles"] else ""
        )
        body = (
            f"{error}{blocked}"
            f"<table><thead><tr><th>前</th><th>後</th><th>増える</th>"
            f"<th>残る</th><th>消える</th><th>捨てた答え</th></tr></thead><tbody>"
            f"<tr><td>{result['previous']}</td><td>{result['total']}</td>"
            f"<td>+{result['added']}</td><td>{result['kept']}</td>"
            f"<td>{result['removed']}</td><td>{result['skipped']}</td></tr>"
            f"</tbody></table>{removed}"
            f'<p class="muted">進み具合の次の値: <code>'
            f'{esc(str(result.get("next_cursor") or "(返らなかった)"))}</code></p>'
        )
    return page_shell(
        f"{name} を試しに集める",
        f"""
<h3>「{esc(name)}」を試しに集めた結果</h3>
<p class="muted">集め方: {esc(MODE_LABELS.get(str(result["mode"]), "") if result else "")}</p>
<p><strong>まだ焼いていません。</strong>長期記憶は変わっておらず、進み具合も次回の予定も
動いていません。この数字を見てから「いま集めて焼く」を押します。</p>
{body}
<p class="muted"><a href="/admin/memory#collect">管理画面へ戻る</a></p>
""",
    )


@router.post("/admin/memory/sweep")
def admin_sweep_memory(request: Request):
    """焼き上がりを確かめて、短期側の印を `固化対象` から `固化` に付け替える。"""
    memory.sweep(request.app.state.sources)
    return RedirectResponse(url="/admin/memory#consolidation", status_code=303)


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
async def admin_claude_config_raw(
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
    # いま頼める相手を渡す。 使えない相手を名指しで勧めると、呼んで断られるまで
    # 分からない(呼べない道具を勧めないのと同じ理由)。
    usable = await capabilities.usable_now()
    # ブロックの組み立ては DB を引く(例示に使うタグの抽出)ので、
    # 待たせるあいだ API 全体を止めないよう threadpool へ逃がす。
    return await run_in_threadpool(
        claude_config.build_block,
        sources, request_origin(request),
        hook=hook, mcp=mcp, media=media.tools_enabled(), usable=usable,
        # 依頼文の言語は管理画面で決める。 プロンプトを書くのは Chiezo ではなく
        # このブロックを読む AI なので、設定はここで文面になって届く
        prompt_language=settings_store.prompt_language_label(),
    )


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
async def admin_claude_config(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    base = request_origin(request)
    # MCP 登録はスクリプトの既定なので、プレビューも既定(mcp=True)側で見せる。
    # フックは --with-hook のときだけなので、こちらは既定のまま出さない。
    block = await run_in_threadpool(
        claude_config.build_block, sources, base,
        mcp=True, media=media.tools_enabled(), usable=await capabilities.usable_now(),
        prompt_language=settings_store.prompt_language_label(),
    )
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
    return HTMLResponse(content=page_shell("Claude Code 連携設定", body))


