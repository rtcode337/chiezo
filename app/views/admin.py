"""管理画面(`/admin`)。人が見る HTML と、そこから叩く操作の口。

取り込みの起動は chiezo-trigger(内部サービス)へのプロキシで、この画面自体は
DB を触らない。Claude Code 連携の設定を配る口(`/admin/claude-config*`)もここ。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import (
    ai_inflight,
    ai_log,
    ai_transcript,
    answer,
    build_info,
    capabilities,
    claude_config,
    collect,
    collect_log,
    db,
    jst,
    machine_store,
    media,
    notes,
    providers,
    registry,
    repartition_job,
    settings_store,
    tasks,
    usage,
    usage_store,
    workers,
)
from app import extract as extraction
from app import partition as partitioning
from app.known_sources import CONTINENT_LABELS, KNOWN_SOURCES, WIKIPEDIA_TIERS
from app.pages import CHAT_PATH, browse_url, esc, page_shell
from app.registry import SUPPORTED_SCHEMA_VERSIONS, TAG_MIN_SCHEMA_VERSION, Source
from app.views import ai_history, ai_settings, ai_usage, ai_workers

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


def _baking_now(job: dict | None, name: str) -> bool:
    """いま**その収集を**焼いている最中か。

    **相手が誰かまで見る。** 取り込みは同時に 1 本しか受けないが、別のソースを
    焼いている最中に台帳を組み直すのは構わない —— ぶつかるのは同じ収集の
    ときだけ(あちらも終わりに台帳を書き戻す)。
    """
    return bool(job and job.get("state") == "running" and job.get("source") == name)


def run_buttons_disabled(job: dict | None) -> str:
    """取り込みを起こすボタンの `disabled` 属性。

    起こせるのは chiezo-trigger が居るときだけ。**未設定でも到達不能でも押せなくする**
    —— 押せると 502 が返るだけで、なぜ動かないのかが画面から読めない。長期記憶へ
    書き込むとき(初期化・再構築・削除)しか要らない相手なので、立てない使い方が普通にある。
    実行中に押せないのは、同時に 1 ジョブしか受け付けないため。
    """
    if not TRIGGER_URL:
        return " disabled"
    if job is None or job.get("state") in ("unreachable", "running"):
        return " disabled"
    return ""


# 取り込みの塊に添える見出し(状況の面で付ける)。**「取り込み」とだけ書かない** ——
# 何をどこへ書く処理なのかが読めないと、初期化・再構築・削除のどれと繋がる表示なのかが
# 分からない。**「ingest」とも書かない** —— コードの中の言葉で、画面の言葉ではない。
# **いま動いているものを見る面**(取り込み・AI への依頼・使用量・ディスクの空き)。
# 取り込みの塊はここにしか出さない —— 記憶や初期化の面にも出していた頃は、
# 同じ塊がいくつもの面に散らばり、どこで見ればよいのかが決まらなかった。
# **走らせるボタンを押したら、ここへ連れてくる**(押した人が次に見たいのは進み具合)
STATUS_PAGE = "/admin/status"
STATUS_JOB = f"{STATUS_PAGE}#job"

JOB_HEADING = '<h2 id="job-head">取り込み(素材を長期記憶へ焼く)</h2>'


def _running_sweep(source: str) -> str:
    """いま走っている取り込みが、どの巡回のぶんか。収集でなければ空。

    **控えを読むだけ**(`pending_sweep`)。起こす側が起こす前に書いているもので、
    素材を作る側もここを読む —— 画面だけ別の引き方をすると、食い違ったときに
    どちらが本当か分からなくなる。

    **読めなくても画面を落とさない。** 巡回の名前は添え物で、本体(取り込みが
    走っていること)はそれ無しでも出せる。
    """
    with suppress(Exception):
        return collect.get(source).pending_sweep
    return ""


def _job_status_html(job: dict | None, heading: bool = False, back: str = STATUS_PAGE) -> str:
    """取り込みの塊。`heading` は**状況の面で付ける**。

    状況の面はいくつもの塊が縦に並ぶだけなので、見出しが無いと
    **何の表示なのかが読めない**。
    """
    return (JOB_HEADING if heading else "") + _job_body_html(job, back)


def _trigger_missing_html(job: dict | None) -> str:
    """取り込み側に繋がらないときだけ出す断り書き。**進み具合は出さない**(状況の面の役目)。

    記憶や初期化の面から取り込みの塊を外すと、**押せないボタンの理由まで消える** ——
    初期化・再構築・削除が灰色のまま並ぶだけで、壊れているのか設定が足りないのかが
    読めない。理由の文は塊と同じものを使う(書き分けると食い違う)。
    """
    if job is None or job.get("state") == "unreachable":
        return _job_body_html(job)
    return ""


def _job_body_html(job: dict | None, back: str = STATUS_PAGE) -> str:
    if job is None:
        return (
            '<div class="job-status" id="job">'
            "取り込みトリガー(chiezo-trigger)は設定されていません"
            " (CHIEZO_TRIGGER_URL 未設定)。長期記憶への書き込み(初期化・再構築・削除)は"
            "できませんが、読むだけならこのままで動きます。"
            "</div>"
        )
    if job.get("state") == "unreachable":
        return (
            '<div class="job-status error" id="job">'
            f"<p>{esc(job.get('error') or 'chiezo-trigger に到達できません')}</p>"
            "<p>長期記憶への書き込み(初期化・再構築・削除)はできません。"
            "読むだけならこのままで動きます。</p>"
            "</div>"
        )
    state = job.get("state", "idle")
    css = f"job-status {state}" if state in ("running", "error") else "job-status"
    lines = [f'<div class="{css}" id="job">', f"<p>状態: {esc(state)}"]
    if source := str(job.get("source") or ""):
        lines.append(f" / ソース: {esc(source)}")
        # **収集なら、どの巡回のぶんかも出す。** 取り込みは収集の名前しか運べない
        # ので、ここだけ見ても「ざっと見るなのか整理なのか」が読めない ——
        # 待たされているときに知りたいのは、たいていそちら(相手も 1 回に見る量も
        # 巡回ごとに違う)。**走っている間だけ**(終わったあとの控えは前の回のもの)
        if sweep := _running_sweep(source) if state == "running" else "":
            lines.append(f" / 巡回: {esc(sweep)}")
    # **日時は日本時間で出す。** 取り込みは UTC で名乗ってくるが、読むのは
    # 画面の前の人 —— 実行ログの時刻と揃わないと、9 時間ずれたまま突き合わせることになる
    for label, key in (("開始", "started_at"), ("終了", "finished_at")):
        if when := jst.parse(str(job.get(key) or "")):
            lines.append(f" / {label}: {esc(jst.format(when))}")
    lines.append("</p>")
    if job.get("error"):
        lines.append(f"<p>エラー: {esc(job['error'])}</p>")
    log_tail = job.get("log_tail")
    if log_tail:
        # **走っている間だけ開いておく。** 終わったログは「見に行けば読める」で足り、
        # 出しっぱなしにすると、何も起きていない画面の大半をログが占める。
        # `open` を状態で決めるので、走り始めれば読み直したときに自然と開く
        opened = " open" if state == "running" else ""
        lines.append(
            f'<details class="job-log"{opened}><summary>実行ログ</summary>'
            '<div class="log-tail">' + esc("\n".join(log_tail)) + "</div></details>"
        )
    # **自動では読み直さない。** 走っている間 5 秒ごとに読み直していた頃は、
    # 開いた `<details>` は閉じ、書きかけの入力は消え、押そうとしたボタンは
    # 読み直しに攫われた —— 取り込みは数時間かかるので、その間ずっと画面が
    # 使えないことになる。**読み直す入口も置かない** —— ブラウザの再読み込みと
    # 同じことしかできず、状態欄の 1 行を食うだけだった
    if state == "running":
        lines.append(_stop_job_html(job, back))
    lines.append("</div>")
    return "\n".join(lines) + _last_failure_html(job, state)


def _last_failure_html(job: dict, state: str) -> str:
    """**最後に落ちた回**。いまの 1 本がそれ自身なら出さない(同じものが二度並ぶ)。

    状態もログも「いまの 1 本」ぶんしか無いので、**次の取り込みが始まった瞬間に
    落ちた回の理由が読めなくなっていた** —— 収集は 1 時間おきに回るうえ、別の
    収集が続けて走ることもある(実際、落ちた 56 秒後に次が始まって何も残らなかった)。
    無人で回る層は、その場に居合わせない人が後から原因を追う。

    **畳んでおく。** ふだん見たいのはいまの 1 本で、これは追いに来た人のためのもの。
    """
    last = job.get("last_failure")
    if not last or state == "error":
        return ""
    when = jst.parse(str(last.get("finished_at") or ""))
    head = f"前に落ちた回: {esc(last.get('source') or '')}"
    if when:
        head += f"({esc(jst.format(when))})"
    tail = last.get("log_tail") or []
    log_html = (
        '<div class="log-tail">' + esc("\n".join(tail)) + "</div>" if tail else ""
    )
    return (
        f'<details class="job-log"><summary>{head}</summary>'
        f'<p class="stale">{esc(last.get("error") or "")}</p>{log_html}</details>'
    )


def _stop_job_html(job: dict, back: str) -> str:
    """走っている取り込みを降ろす口。**押してから降りるまでに間がある**。

    **止める手段が再起動しかなかった。** 取り込みは数分〜数時間かかるので、
    設定を直したい・押し間違えた・いま動かしたくない、はどれも普通に起きる。

    **殺すのではなく、安全なところで降りる**(`core.check_stop`)—— 降りるのは
    切り替えより前なので、**いま配信している世代はそのまま**。集めた素材も
    捨てないので、押し直せば AI を呼び直さずに続きから焼ける。

    **すぐ止まるとは書かない。** 外から素材が届くのを待っている最中は、動いて
    いるのは向こうでこちらは待っているだけなので、印を見る手がない ——
    「押しても何も起きない」と読まれるより、待ちがあると先に書くほうがよい。
    """
    if job.get("stopping"):
        return (
            '<p class="stale">⚠️ 止める印を立てました。'
            "区切りのいいところまで来たら降ります"
            "(外から素材が届くのを待っている最中は、届き始めてからになります)。</p>"
        )
    ask = (
        "取り込みを止めます。切り替えの前で降りるので、いま配信している世代は"
        "そのまま残ります。集めた素材も捨てないので、押し直せば続きから焼けます。"
    )
    # **1 つの塊に包んで上を空ける**(`div.job-stop`)。form は行内に流れる作りで
    # 自分では余白を持てず、真上の実行ログの見出しにボタンが貼り付いていた
    return (
        '<div class="job-stop">'
        '<form class="init-form" method="post" action="/admin/ingest/stop"'
        f" onsubmit=\"return confirm('{esc(ask)}')\">"
        '<button type="submit">止める</button></form>'
        # **短く言い切る。** 括弧で「すぐには止まりません」を添えていた頃は、
        # スマホで 2 行に割れていた —— 「区切りのいいところで」で同じことが伝わる
        ' <span class="muted">区切りのいいところで止まります</span>'
        "</div>"
    )


def _history_args(request: Request) -> tuple[int, bool]:
    """「AI への依頼」節のページと絞り込みをクエリから読む。

    **おかしな値は 1 ページ目に寄せる**(手で URL をいじられても落とさない)。
    """
    raw = request.query_params.get("ai_page", "1")
    page = int(raw) if raw.isdigit() and int(raw) > 0 else 1
    return page, request.query_params.get("ai_failed") == "1"


def db_size_text(path: Path) -> str:
    """いま配っている世代の DB の大きさ。**読めなければ空**(表を落とさない)。

    **リンクの先を測る**(`stat` はたどる)—— `<ソース名>.db` は世代への
    シンボリックリンクで、リンクそのものは数十バイトしかない。
    単位は GiB 系(`_disk_html` と同じ数え方。ディスクの空きと並べて読むため)。
    """
    try:
        nbytes = path.stat().st_size
    except OSError:
        return ""
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if nbytes >= scale:
            return f"{nbytes / scale:,.1f} {unit}"
    return f"{nbytes} B"


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
<p class="muted"><a href="/admin/collect">管理画面へ戻る</a>(保存しなければ何も変わりません)</p>
"""
    return page_shell("プロンプトの相談", body)


# 収集の**種類**の言い方。その収集が何を集めているのか
# (返ってきた 1 件で何ができるかは、巡回ごとに決まる)
# 一覧の行に出す短い印。**説明はここでは書かない**(面のほうに出る)
KIND_MARKS = {collect.KIND_FLOW: "流れ", collect.KIND_STOCK: "網羅"}

KIND_LABELS = {
    collect.KIND_FLOW: "流れ(時とともに増えるものを追う。古いものは順に落とす)",
    collect.KIND_STOCK: "網羅(ある括りの全部を集めて、端から端まで精査し続ける)",
}


def _backend_select(
    current: str | None,
    field: str = "backend",
    with_workers: bool = False,
    empty_label: str = "Chiezo の既定にまかせる",
) -> str:
    """相手を選ぶセレクト。**空が「Chiezo の既定にまかせる」**。

    `empty_label` は空欄の見せ方。**「既定にまかせる」が意味を持たない場所がある**
    —— ワーカーの段は書いた順に試す並びなので、空欄は「ここで終わり」であって
    「Chiezo が選ぶ」ではない(そう出ていると、生きているのか未設定なのかが読めない)。

    候補は**有効にしてある相手だけ**(`answer.backend_names()`)—— 無効な相手を選べても
    走らせた瞬間に断られる。**描画のときに相手へ問い合わせない**ので、モデルの一覧は
    `app/providers.py` が持つ控えを使う(管理画面の他の表と同じ流儀)。

    **いま選ばれている相手が無効になっていても選択肢に残す** —— 落とすと、保存し直した
    瞬間に既定へ倒れて、誰に頼んでいたのかが画面から消える。

    **ワーカーも同じ欄に並べる**(`with_workers`)。欄を分けていた頃は、相手と
    ワーカーの両方を選べて、**どちらが効くのかが画面から読めなかった**(効くのは
    ワーカー)。1 つの欄にすれば、選べるのは片方だけになる。
    **ワーカーの段に出す欄では並べない** —— ワーカーがワーカーを指せてしまう。
    """
    enabled = answer.backend_names()
    names = list(enabled)
    if current and current not in names and not workers.ref_in(current or ""):
        names.append(current)
    options = [f'<option value="">{esc(empty_label)}</option>']
    for name in names:
        spec = providers.get(name)
        label = spec.label if spec else name
        if name not in enabled:
            label += "(いまは無効)"
        selected = " selected" if name == current else ""
        options.append(f'<option value="{esc(name)}"{selected}>{esc(label)}</option>')
    if with_workers:
        options.append(_worker_options(current or ""))
    return f'<select name="{field}">{"".join(options)}</select>'


def _worker_options(current: str) -> str:
    """相手のセレクトに並べるワーカーのぶん。**1 本も無ければ何も出さない**
    (空の見出しだけが並ぶと、設定し忘れているように見える)。

    **いま名指しされているものが定義に無くても残す**(相手と同じ理由)——
    落とすと、保存し直した瞬間に既定へ倒れて、何を指していたのかが画面から消える。

    **値は id、札は名前。** 名前を値にしていた頃は、ワーカーを改名した瞬間に
    保存済みの巡回が指し先を失っていた(落ちるのではなく、巡回自身に書いてある
    相手で黙って走り出す)。
    """
    try:
        found = [(w.key, w.name) for w in workers.load()]
    except ValueError:
        found = []
    if (picked := workers.ref_in(current)) and not any(key == picked for key, _ in found):
        # **消えたワーカーは鍵のまま出す** —— 名前は引けないが、
        # 何かを指していたことは読める
        found.append((picked, picked))
    if not found:
        return ""
    out = []
    for key, name in found:
        value = workers.option_for(key)
        selected = " selected" if value == current else ""
        out.append(f'<option value="{esc(value)}"{selected}>{esc(name)}</option>')
    return f'<optgroup label="ワーカー(枠を見て振り替える)">{"".join(out)}</optgroup>'


def _candidate_select(field: str, current: str | None, candidates, empty_label: str) -> str:
    """候補から選ぶセレクト(いまはモデルだけ)。

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


def _model_select(backend: str | None, current: str | None, field: str = "model") -> str:
    """モデルのセレクト。**候補は起動時に控えたもの**(`answer.remembered_models`)。

    ここで相手に問い合わせない —— 管理画面の描画で外へ出ると、相手が落ちている
    ときにページ全体が待たされる(`_backend_select` と同じ約束)。選び直すときだけ
    `GET /ai/models` を引いて入れ替える(`pages.BACKEND_PICKER_SCRIPT`)。

    **`app/providers.py` の決め打ちを直に読まない。** あれは聞けないときの落ち先で、
    ブリッジ越しの相手では実物と食い違う —— codex は決め打ちを持たず(空のセレクトに
    なる)、claude は考える量を畳んだ名前を持たない。**開いた直後だけ候補が違う**、
    という状態になっていた。
    """
    return _candidate_select(field, current, answer.remembered_models(backend), "相手の既定")


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
        lines.append(f"{esc(spec.label)} — " + ("、".join(parts) if parts else "指定なしでよい"))
    if not lines:
        return ""
    return '<p class="muted">' + "<br>".join(lines) + "</p>"


def _backend_label(item) -> str:
    """行に出す相手の名前。未指定なら既定だと分かるように書く。

    **AI を呼ばない回には「既定にまかせる」と出さない** —— あれは「誰に頼むかは
    Chiezo が決める」の意味で、頼むこと自体は起きるように読める。機械で引く回は
    そもそも AI を呼ばない。
    """
    if (getattr(item, "use_extract", False) or getattr(item, "use_feed", False)
            or getattr(item, "by_hand", False)):
        return '<span class="muted">AI 利用無し</span>'
    # **ワーカーに頼む回は、そう出す。** 相手の名前を出すと、その 1 つに
    # 固定で頼んでいるように読める —— 実際に渡る先は毎回その場の枠で決まる
    if worker := getattr(item, "worker", ""):
        # **出すのは名前**(巡回が持っているのは id なので、そのままだと読めない)
        return (
            f'{esc(workers.label_for(worker))}<br>'
            '<span class="muted">ワーカー(枠を見て振り替える)</span>'
        )
    spec = providers.get(item.backend) if item.backend else None
    label = (
        esc(spec.label if spec else item.backend)
        if item.backend else '<span class="muted">既定にまかせる</span>'
    )
    # **相手を選んでいなくても、モデルは出す**(相手は既定でよいがモデルだけ
    # 決めている、が普通にある)。**考える量は画面から設定できない**が、外の
    # アプリや古い設定から入っていることがあるので、入っていれば出す
    detail = " / ".join(x for x in (item.model, item.effort) if x)
    return label + (f'<br><span class="muted">{esc(detail)}</span>' if detail else "")


# 相手を選び直したときに、モデルの候補を入れ替える。
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
PARTITION_EXAMPLE = json.dumps(
    {
        "by": "geo",
        "target": 200,
        "source": "osm_japan",
        "feature": "amenity=restaurant",
        "bbox": [20.0, 122.0, 46.0, 154.0],
    },
    ensure_ascii=False,
)

VERIFY_TAGS_EXAMPLE = json.dumps(
    [{"prefix": "代表作", "source": "jawiki"}], ensure_ascii=False
)

FEED_EXAMPLE = json.dumps(
    {"urls": ["https://example.com/feed", "https://example.org/atom"], "since": "last_run"},
    ensure_ascii=False,
)

def _sweep_fields(sweep, removable: bool, shared_prompt: str = "") -> str:
    """巡回 1 本ぶんの欄。`sweep` が None なら空の枠(足すため)。"""
    name = sweep.name if sweep else ""
    interval = sweep.interval_minutes if sweep else collect.MIN_INTERVAL_MINUTES * 12
    cover = f"{sweep.cover_days:g}" if sweep and sweep.cover_days else ""
    per_run = sweep.partitions_per_run if sweep and sweep.partitions_per_run else ""
    backend = sweep.backend if sweep else None
    enabled = sweep.enabled if sweep else True
    on_demand = bool(sweep and sweep.on_demand)
    only_new = bool(sweep and sweep.only_new)
    use_extract = bool(sweep and sweep.use_extract)
    use_feed = bool(sweep and sweep.use_feed)
    by_hand = bool(sweep and sweep.by_hand)
    # **巡回ごとの依頼文。** 空なら収集のものを使う ——
    # 頼むことが巡回ごとに違う(埋める / 見直して消す / 漏れを足す)のに、
    # 1 つの文で全部を頼むと、どの回も同じ薄さの仕事になる
    own_prompt = (sweep.prompt if sweep else "") or ""
    # **収集のものを引き継いでいるだけなら空で見せる**(`Sweep.prompt` は
    # 書いていなければ収集のものに落ちるので、そのまま出すと写しが並ぶ)
    if own_prompt == shared_prompt:
        own_prompt = ""
    hint = (
        '<span class="muted">名前を消すと、この巡回は無くなります</span>'
        if removable else '<span class="muted">名前を書くと増えます</span>'
    )
    return (
        '<div class="sweep-row">'
        f'<p><label>名前<br><input name="sweep_name" value="{esc(name)}"'
        f' placeholder="ざっと"></label> {hint}</p>'
        # **「持たない」は名指しでしか選べない値にする。** 空文字で表すと、この欄を
        # 持たないフォームから保存したときに、全部の巡回が黙って時計を失う
        '<p><label>時計<br><select name="sweep_clock">'
        f'<option value="interval"{"" if on_demand else " selected"}>間隔で回す</option>'
        f'<option value="on_demand"{" selected" if on_demand else ""}>'
        "持たない(割り込みで頼まれたときだけ)</option>"
        "</select></label></p>"
        f'<p><label>間隔(分)<br><input name="sweep_interval" type="number"'
        f' min="{collect.MIN_INTERVAL_MINUTES}" value="{interval}"></label></p>'
        '<p><label>一周の日数(区画を全部見終わるまで。空なら下の区画数を使う)<br>'
        f'<input name="sweep_cover_days" type="number" step="0.5" min="0.5"'
        f' value="{esc(str(cover))}"></label>'
        # **希望であって結果ではない、と書いておく。** 1 回に見る区画には天井が
        # あるので、区画が多いとここに書いた日数では回り切らない —— 書けることと
        # 起きることが違うのに、画面はどちらも同じ顔で出していた
        f' <span class="muted">1 回に見る区画は'
        f'{collect.MAX_PARTITIONS_PER_RUN} が上限なので、'
        '区画が多いとここに書いた日数より長くかかります'
        '(実際にかかる日数は上の表に出ます)</span></p>'
        '<p><label>1 回に見る区画(空なら上の日数から計算する)<br>'
        f'<input name="sweep_per_run" type="number" min="1"'
        f' max="{collect.MAX_PARTITIONS_PER_RUN}" value="{esc(str(per_run))}"></label></p>'
        f"{_sweep_backend_fields(sweep, backend, use_extract or use_feed or by_hand)}"
        # **足すだけの回は、既にある見出しに触らない。** 「漏れているものを足して」と
        # 頼む回に要る印で、AI の判断に頼らずにここで保証する —— 見せられるのはその
        # 区画のぶんだけなので、AI には「もう居るかどうか」が分からない
        '<p><label>この巡回の依頼文(空なら収集のものを使う)<br>'
        f'<textarea name="sweep_prompt" rows="6"'
        f' placeholder="この巡回にだけ頼みたいことがあれば">{esc(own_prompt)}</textarea>'
        "</label></p>"
        # **引き方**(AI に頼むか、抽出の指定で機械に引かせるか)。機械の回は
        # 名簿を最新に保つためのもので、AI を呼ばない ——「足すだけ」と組にして使う
        '<p><label>引き方<br><select name="sweep_source">'
        f'<option value="ai"'
        f'{"" if use_extract or use_feed or by_hand else " selected"}>'
        "AI に頼む</option>"
        f'<option value="extract"{" selected" if use_extract else ""}>'
        "機械で引く(抽出の指定をもう一度走らせる)</option>"
        f'<option value="feed"{" selected" if use_feed else ""}>'
        "外の道具で引く(フィードの見出しをそのまま溜める)</option>"
        # **手で回す回。** web の画面から使う AI に頼むための道で、
        # Chiezo は依頼文をファイルにして渡し、答えのファイルを読み込む
        f'<option value="hand"{" selected" if by_hand else ""}>'
        "手で回す(依頼文を書き出して、答えを読み込む)</option>"
        "</select></label></p>"
        '<p><label>集め方<br><select name="sweep_merge">'
        f'<option value="all"{"" if only_new else " selected"}>'
        "収集の設定にまかせる</option>"
        f'<option value="only_new"{" selected" if only_new else ""}>'
        "足すだけ(既にある見出しには触らない)</option>"
        "</select></label></p>"
        '<p><label>動かすか<br><select name="sweep_enabled">'
        f'<option value="1"{" selected" if enabled else ""}>動かす</option>'
        f'<option value=""{"" if enabled else " selected"}>止める</option>'
        "</select></label></p></div>"
    )


# 巡回の表の列数(巡回・相手・間隔・前回・一周のうち・次にいつ・実行)。
# 設定の行はこれを全部つないで 1 つのセルにする
SWEEP_COLUMNS = 7

# 「その回が動かしたもの」に出す件数。**全部は出さない** —— 1 回で数千件動く回が
# あるので、天井を置かないと画面が開かなくなる。読みたいのは新しいほうから。
CHANGED_HERE_LIMIT = 200

# 脇書きの変化に出す 1 つぶんの長さ。**ここは変化を読む欄**で、全文はいまの中身の
# 側にある —— 長い値をそのまま並べると、何が動いたのか目で探すことになる
EXTRA_VALUE_CHARS = 120


def _sweep_backend_fields(sweep, backend, mechanical: bool) -> str:
    """頼む相手の欄。**機械で引く巡回には出さない**。

    出していた頃は、選んでも何も起きなかった —— 機械の回は AI を呼ばずに返すので、
    相手もモデルも読まれない。**選べるのに効かない欄は、設定したつもりを作る**。

    **ワーカーは相手と同じ欄で選ぶ。** 別の欄にしていた頃は両方を選べて、
    どちらが効くのかが画面から読めなかった(効くのはワーカー)。

    **ワーカーを選んだ回にはモデルを出さない。** どちらに頼むかは
    そのときの枠で決まるので、ここで 1 つ選んでも**どの相手に渡る値なのか決まらない**
    —— モデルの名前は相手ごとに違う(`sonnet` は codex には無い)。
    段ごとの指定はワーカーの側が持っている。
    """
    if mechanical:
        return (
            '<p class="muted">この引き方では AI を呼ばないので、相手は選べません'
            "(「AI に頼む」にして保存すると出ます)。</p>"
        )
    worker = (sweep.worker if sweep else "") or ""
    chosen = workers.option_for(worker) if worker else (backend or "")
    head = (
        f'<p><label>頼む相手<br>'
        f'{_backend_select(chosen, "sweep_backend", with_workers=True)}</label></p>'
    )
    if worker:
        return head + (
            '<p class="muted"><strong>ワーカーに頼む回です。</strong>'
            "枠に余裕のある先頭の相手に渡し、どれも詰まっていればその回は走らせません。"
            "<br>モデルは<strong>ワーカーの段が持ちます</strong> —— "
            "渡る相手がそのときまで決まらないので、ここでは選べません"
            f'(<a href="/admin/collect#{ai_workers.SECTION_ANCHOR}">段を直す</a>)。</p>'
        )
    return head + (
        f'<p><label>モデル<br>'
        f'{_model_select(backend, sweep.model if sweep else None, "sweep_model")}</label></p>'
    )


def _sweep_edit_row(item, sweep, columns: int, removable: bool = True) -> str:
    """巡回 1 本ぶんの設定を、その行の下に畳んで置く。

    **その巡回だけを保存できる。** 収集ぜんたいの編集フォームに全部の巡回を並べて
    いた頃は、1 本の間隔を直すのに全部を送り直していた —— 別のセッションが同時に
    別の巡回を直していると、後から押したほうで上書きされる。

    **見ている行の真下に置く。** 設定が離れたところにあると、どの行のものかを
    名前で照合することになる。
    """
    key = sweep.name if sweep else ""
    # **「〜の設定」と名前を繰り返さない。** すぐ上の行にその名前が出ているし、
    # 名前入りだと下の巡回の見出しに見える(実際にそう読まれた)
    label = "設定" if sweep else "巡回を足す"
    return (
        f'<tr class="sweep-edit"><td colspan="{columns}">'
        f"<details><summary>{label}</summary>"
        f'<form method="post" action="/admin/collect/{esc(quote(item.name))}/sweep"'
        ' class="collect-form">'
        f'<input type="hidden" name="sweep_key" value="{esc(key)}">'
        f"{_sweep_fields(sweep, sweep is not None and removable, item.prompt)}"
        '<p><button type="submit">この巡回を保存</button></p>'
        "</form></details></td></tr>"
    )


def _sweep_table_body(item, disabled: str = "") -> str:
    """巡回の表の中身(行と、その下に畳んだ設定)。**一覧と詳細で同じものを出す**。"""
    sweeps = collect.sweeps_of(item)
    rows = []
    for cells, sweep in zip(_sweep_cells(item, disabled), sweeps, strict=False):
        rows.append(f"<tr>{cells}</tr>")
        rows.append(_sweep_edit_row(item, sweep, SWEEP_COLUMNS, len(sweeps) > 1))
    rows.append(_sweep_edit_row(item, None, SWEEP_COLUMNS))
    return "".join(rows)


def _cycle_label(days: float) -> str:
    """一周にかかる日数の書き方。**丸めるのは読む側の粒度まで**。

    小数を出さない —— 区画も相手の速さも回るたびに動くので、`68.2 日` の
    `.2` は精度のふりをするだけ。1 日に満たないものは時間で出す。
    """
    if days < 1:
        return f"{max(round(days * 24), 1)} 時間"
    return f"{round(days):,} 日"


def _sweep_cells(item, disabled: str = "", dry: bool = True) -> list[str]:
    """巡回 1 本ぶんのセル(巡回・相手・間隔・前回・一周のうち・次にいつ)。

    **「集める」の表にそのまま並べる。** 折り畳みの中へ入れていた頃は、動いているかを
    見るのにいちいち開くことになった —— この表はそれを読むための表なのに。

    **1 行に「一周のうちどこまで」を出す。** ざっとが一周した区画をじっくりは
    まだ見ていない、が普通に起きるので、巡回ごとに出さないと進み具合が読めない。

    巡回を書いていない収集にも 1 つ出る(定義そのものが 1 本の巡回として動くため)。
    """
    total = len(item.partitions)
    cells = []
    for sweep in collect.sweeps_of(item):
        visited, _ = partitioning.progress(item.partitions, sweep.name)
        # **相手は巡回ごとに変えられる。** ざっとは安い相手で数をこなし、じっくりは
        # よく考えるモデルに回す、という分け方をするためのもの
        who = _backend_label(sweep)
        due = jst.parse(sweep.next_run_at or "")
        # **一周したあとは「いちばん古い区画」を出す。** 「見終えた / 全区画」は
        # 一周すると総数に張り付いて動かなくなる —— 区画は消えないので、2 周目からは
        # 「どこまで来たか」ではなく「いちばん古いところがいつのものか」が読みたい値
        oldest = partitioning.oldest_visit(item.partitions, sweep.name) if total else None
        if not total:
            where = '<span class="muted">区画なし</span>'
        elif oldest:
            where = (
                "一周した"
                f'<br><span class="muted">いちばん古い区画: '
                f'{esc(jst.format(jst.parse(oldest)) or "")}</span>'
                f'<br><span class="muted">1 回に {sweep.per_run(total)} 区画</span>'
            )
        else:
            where = (
                f"{total:,} のうち {visited:,}"
                f'<br><span class="muted">1 回に {sweep.per_run(total)} 区画</span>'
            )
        if sweep.last_status == "error":
            result = f'<span class="stale">失敗: {esc(sweep.last_error or "")}</span>'
        else:
            last = jst.parse(sweep.last_run_at or "")
            result = esc(jst.format(last)) if last else '<span class="muted">まだ</span>'
        # **走らない巡回に「いますぐ」と出さない**(予定を持っていないだけで走らない)。
        # 理由を言うのは `collect.blocked_reason` だけ —— ここで場合分けを書き写すと、
        # 片方だけが古くなる
        if blocked := collect.blocked_reason(item, sweep):
            when = f'<span class="muted">{esc(blocked)}</span>'
        elif due:
            when = esc(jst.format(due))
        else:
            when = '<span class="muted">いますぐ</span>'
        name = esc(sweep.name) + ("" if sweep.enabled else ' <span class="muted">(止)</span>')
        # **足すだけの回はそう出す。** 同じ収集の中で、消す力を持つ回と持たない回が
        # 並ぶので、名前だけでは読み分けられない
        if sweep.only_new:
            name += '<br><span class="muted">足すだけ</span>'
        if sweep.use_extract:
            name += '<br><span class="muted">機械で引く</span>'
        if sweep.use_feed:
            name += '<br><span class="muted">外の道具で引く</span>'
        if sweep.by_hand:
            name += '<br><span class="muted">手で回す</span>'
        # **押す口は巡回ごとに 1 つずつ。** 相手も 1 回に見る量も巡回ごとに違うので、
        # 収集に 1 つだけ置くと「どの設定で走ったのか」が押した本人にも分からない。
        # **時計を持たない巡回には出さない** —— あれは割り込みで頼まれたときだけ
        # 動く 1 本で、自前の依頼文を持たない(口のほうでも断る)
        run = (
            '<span class="muted">—</span>'
            if sweep.on_demand
            else _sweep_run_forms(
                item.name, sweep.name, disabled, dry, bool(item.partitions),
            )
        )
        # **一周は書いた日数ではなく、実際にかかる日数を出す。** 1 回に見る区画には
        # 天井があるので(`collect.MAX_PARTITIONS_PER_RUN`)、区画が多いと指定から
        # 離れる —— 本番で「15 日で一周」と出ていた巡回が実際には 68 日だった。
        # **4 倍のずれがどこにも出ていなかった**ので、一周の見込みで枠を考えられない
        every = (
            '<span class="muted">時計なし</span>'
            if sweep.on_demand
            else f"{sweep.interval_minutes} 分ごと"
            + (f'<br><span class="muted">一周 {_cycle_label(cycle)}</span>'
               if (cycle := sweep.cycle_days(total)) else "")
        )
        # **前回を先、次にいつを後**。読む順が「いつ動いたか → 次はいつか」なので、
        # 逆に並べていると目が戻る(動いているかを確かめに来る表なので、
        # まず見たいのは実際に動いた側)
        cells.append(
            f"<td>{name}</td><td>{who}</td>"
            f"<td>{every}"
            + f"</td><td>{result}</td>"
            f"<td>{where}</td><td>{when}</td><td>{run}</td>"
        )
    return cells


def _doc_link(name: str, title: str) -> str:
    """動いた 1 件への入口。押すと、どう書き換わったかを別の画面で出す。

    **percent-encode してから HTML のエスケープを通す**(`quote` は HTML の
    エスケープではない)—— 見出しには `&` も `#` も入りうる。
    """
    return (
        f'<a href="/admin/collect/{esc(quote(name))}/doc?title={esc(quote(title))}">'
        f"{esc(title)}</a>"
    )


def _sweep_run_forms(
    name: str, sweep: str, disabled: str, dry: bool = True, partitioned: bool = False,
) -> str:
    """その巡回を 1 回だけ動かす口と、**一周をやり直す**口。

    **ドライランは焼かない**(差分を見るだけ)ので、取り込みが走っていても押せる。
    **今すぐ実行は焼く**ので、trigger が居ないときと取り込み中は押せない。

    **一覧にはドライランも一周のやり直しも出さない**(`dry=False`)。どちらも
    押した先で結果を読む口で、読みに来る場所は収集の面 —— 一覧に並べると、
    収集の数だけ場所を食う。

    **一周のやり直しは区画を持つ収集にだけ出す**(`partitioned`)。持たない収集は
    毎回ぜんたいを見るので、戻す「どこまで」が無い。
    """
    field = f'<input type="hidden" name="sweep" value="{esc(sweep)}">'
    preview = (
        f'<form class="init-form" method="post" action="/admin/collect/{esc(name)}/preview">'
        f'{field}<button type="submit"'
        f' title="この巡回で 1 回集めさせて、焼かずに差分だけ見ます">ドライラン</button></form>'
        if dry else ""
    )
    confirm = (
        f"「{sweep}」の一周をやり直します。区画の印を全部外すので、"
        "次の回は先頭から回り直します(集めた中身は動きません)。よろしいですか?"
    )
    restart = (
        f'<form class="init-form" method="post"'
        f' action="/admin/collect/{esc(name)}/restart"'
        f" onsubmit=\"return confirm('{esc(confirm)}')\">"
        f'{field}<button type="submit"'
        f' title="区画の印を全部外して、次の回を先頭から回し直します">'
        "一周をやり直す</button></form>"
        if dry and partitioned else ""
    )
    return (
        f'<div class="sweep-run">'
        f'<form class="init-form" method="post" action="/admin/collect/{esc(name)}/run"{disabled}>'
        f'{field}<button type="submit"{disabled}'
        f' title="この巡回で 1 回、集めて焼きます">今すぐ実行</button></form>'
        f"{preview}{restart}</div>"
    )


def _removed_html(item, sources: dict) -> str:
    """消えたもの(消えた印の付いた文書)。**読めるところに出す**。

    消す回と足す回が別々に走るので、これが無いと「なぜこの人が入ってこないのか」が
    画面から読めない —— 消し間違いに気づく手立てが、ここを見ることしか無い。

    **中身は収集そのものにある**(`notes.REMOVED_TAG` の付いた文書)。定義に控えを
    持っていた頃は 1 件のメモに収める都合で 2,000 件の上限が要り、溢れると古いものから
    静かに戻っていた。文書に印を付けて残せば、その上限が要らない。
    """
    src = sources.get(item.name)
    if src is None or src.schema_version < TAG_MIN_SCHEMA_VERSION:
        return ""
    # **出すのは消した理由**(本文ではない)。本文は戻すときのために残してあるが、
    # ここを見に来る人が確かめたいのは「なぜ消えたのか」のほう
    rows = db.query(
        src.path,
        "SELECT title, json_extract(extra, '$.removed_reason') AS why FROM docs WHERE doc_id IN"
        " (SELECT doc_id FROM doc_tags WHERE tag = ?) ORDER BY updated_at DESC LIMIT ?",
        (notes.REMOVED_TAG, REMOVED_HEAD + 1),
    )
    if not rows:
        return ""
    shown = rows[:REMOVED_HEAD]
    lines = [r["title"] + (f' —— {r["why"]}' if r["why"] else "") for r in shown]
    more = "、ほかにもあります" if len(rows) > REMOVED_HEAD else ""
    return (
        f'<p class="muted">消えたもの: 新しい {len(shown):,} 件{more}'
        "(<strong>読み口からは返りません</strong>。足す回も連れ戻しません。"
        "<strong>本文はそのまま残しています</strong>ので、"
        "その文書から印を外せば元の中身のまま戻ります)</p>"
        f'<pre class="prompt-view">{esc(chr(10).join(lines))}</pre>'
    )



# 区画の表に出す件数。**残りは畳む** —— 全部出すと、その下にある変更履歴まで
# 面の外へ押し出される
PARTITION_HEAD = 10

# 消えたものの一覧に出す件数。**残りは畳む** —— 溜まり続けるので、全部出すと
# その下にある変更履歴まで面の外へ押し出される
REMOVED_HEAD = 20


def _verify_tags_json(item) -> str:
    """タグの確かめ方を、欄に出せる形で。持っていなければ空。"""
    return json.dumps(item.verify_tags, ensure_ascii=False, indent=2) if item.verify_tags else ""


def _repartition_form(item, busy: bool) -> str:
    """**台帳の割り直しだけを走らせる口。**

    割り直しは母集団を丸ごと 1 周舐めるので、**焼くのと同じ回に乗ると山が二つ
    重なる** —— 本番で、台帳が空の状態から 686,602 件を割り直す回が、素材を
    280,270 件まで流したところで切れた。先に台帳だけ整えておければ、次の回は
    ふだんどおり「使い回すだけ」で済む。

    **押しても AI は動かないし、焼きもしない**ので、確認は出さない ——
    巡回の記録も引き継ぐ(一周は巻き戻らない)。取り消せない操作ではない。

    **焼いている最中は押せない。** あちらも終わりに台帳を書き戻すので、
    どちらが残るかが順番次第になる。
    """
    if not item.partition:
        return ""
    doing = repartition_job.running(item.name)
    off = " disabled" if busy or doing else ""
    note = (
        "この収集を焼いている最中は押せません" if busy
        else "いま割り直しています" if doing
        else "母集団を数え直して区画を割り直します(AI は動かず、焼きもしません)"
    )
    return (
        f'<form class="init-form" method="post"'
        f' action="/admin/collect/{esc(quote(item.name))}/repartition"{off}>'
        f'<button type="submit"{off} title="{esc(note)}">区画を割り直す</button></form>'
        f"{_repartition_state_html(item.name)}"
    )


def _repartition_state_html(name: str) -> str:
    """割り直しの様子。**押したあと、何が起きたかを画面に残す**。

    **数分かかる仕事なので、押した人はその場に居ないことがある**(本番の食事処は
    686,602 件)。押した瞬間に画面が戻るだけだと、終わったのか落ちたのか、
    そもそも走ったのかが読めない —— 実際、ブラウザが先に切れて「死んだ」ように
    見えていた(処理は裏で最後まで走っていた)。

    **区画の数も出す** —— 「押したら何区画になったか」を台帳を開かずに読めるように。
    **時間切れは落ちたものとして出る**(`repartition_job.state`)。
    """
    found = repartition_job.state(name)
    if not found:
        return ""
    when = jst.parse(str(found.get("finished_at") or found.get("started_at") or ""))
    at = f"({jst.compact(when)})" if when else ""
    if found.get("state") == "running":
        return f'<p class="muted">割り直しています{at}。終わると区画の数が変わります。</p>'
    if found.get("state") == "error":
        return f'<p class="stale">割り直せませんでした{at}: {esc(str(found.get("error") or ""))}</p>'
    return (
        f'<p class="muted">{int(found.get("partitions") or 0):,} 区画に割り直しました{at}。</p>'
    )


def _partition_html(item, src=None, busy: bool = False) -> str:
    """区画の進み具合。持っていない収集には何も出さない。

    **出すのは「どう割れたか」だけ。** どこまで回ったかは巡回ごとに違うので
    `_sweeps_html` の側にある。
    """
    if not item.partition:
        return ""
    total = len(item.partitions)
    if not total:
        # **設定は台帳が空でも出す。** どこから広げるかは収集に書いてある指定で、
        # 区画が割れているかとは別の話 —— 割る前こそ「効く設定になっているか」を
        # 確かめたい(台帳が消えた後や、入れたばかりのときがまさにそれ)
        return (
            '<p class="muted">区画: まだ割っていません'
            f"(次の実行で対象の空間を割ってから回り始めます){_spread_from_html(item)}。</p>"
            f"{_repartition_form(item, busy)}"
        )
    # **頭の 10 件だけ出して、残りは畳む。** 全部を出すと 325 行が面を埋めて、
    # その下にある変更履歴まで押し出される。**捨てはしない** —— ここを読みに来るのは
    # 「どこを見ていて、どこがまだか」を知りたいときなので、開けば全部ある
    # **選んで走らせられる**ので、行の頭にチェックを置く(`_run_here_html` の
    # フォームの中に表を入れてある)。**選べるのは区画を見る巡回があるときだけ**
    pickable = bool(_sweeps_for_trial(item))
    heads = (
        f'{"<th></th>" if pickable else ""}<th>区画</th><th>母集団</th>'
        "<th>見終えた巡回(日本時間)</th>"
    )

    def row(p):
        box = (
            f'<td><input type="checkbox" name="partition" value="{esc(p["key"])}"></td>'
            if pickable else ""
        )
        return (
            f"<tr>{box}<td>{_partition_link(item.name, p['key'])}</td>"
            f"<td>{p['count']:,}</td><td>{_visits_html(p)}</td></tr>"
        )

    head = "".join(row(p) for p in item.partitions[:PARTITION_HEAD])
    rest = item.partitions[PARTITION_HEAD:]
    more = (
        "<details><summary>"
        f"残りの {len(rest):,} 区画を見る</summary>"
        f"<table><thead><tr>{heads}</tr></thead>"
        f"<tbody>{''.join(row(p) for p in rest)}</tbody></table></details>"
        if rest else ""
    )
    table = (
        f"<table><thead><tr>{heads}</tr></thead>"
        f"<tbody>{head}</tbody></table>{more}"
    )
    return (
        f'<p class="muted">区画: {total:,}{_spread_from_html(item)}'
        f'{_uncovered_html(item, src)}</p>'
        f"{_repartition_form(item, busy)}"
        f"{_run_here_html(item, table, busy)}"
    )


def _uncovered_html(item, src) -> str:
    """**どの区画にも入っていない数**。合わないときだけ出す。

    区画の母集団を足しても長期記憶の数に届かない、が普通に起きる ——
    消えたものは数えないし、**座標を持たない 1 件はどの区画にも入らない**
    (地図で割る収集)。どちらも仕様だが、**画面に出ていないと「区画分けが
    壊れている」としか読めない**(実際にそう読まれた)。
    """
    if src is None:
        return ""
    counted = sum(int(p.get("count") or 0) for p in item.partitions)
    left = src.doc_count - counted
    if left <= 0:
        return ""
    return (
        f" / 長期記憶 {src.doc_count:,} 件との差 {left:,} 件"
        '<br><span class="muted">差は「消えたもの」と「区画に入れない 1 件」'
        "(地図で割る収集なら、座標を持たないもの)。"
        "区画に入っていないものは、どの巡回にも回ってきません。</span>"
    )


def collect_page(item) -> str:
    """その収集の面への行き先。**保存されている名前から組む**。

    要求に入っていた文字列をそのまま繋がない —— 名前はソース名にもファイル名にも
    URL にもなるので狭い字しか通らない(`collect.NAME_RE`)のに、行き先を組むところ
    だけその約束の外に居ると、読む側にも検査する側にも「任意の URL を作れる」と
    見える。保存できた側の名前を使えば、通った字しか入らない。
    """
    return f"/admin/collect/{quote(item.name, safe='')}"


def _spread_from_html(item) -> str:
    """どこから広げるか(`origin`)。書いていなければ何も出さない。

    **JSON の中にしか無かった。** 指定は編集のフォームの textarea に入っているが、
    開いて読まないと分からない —— 「東京から回るようにしたはずだが効いているのか」を
    確かめに来る人が見るのは、台帳のほうの行。
    """
    origin = (item.partition or {}).get("origin")
    if not origin:
        return ""
    return f"(緯度 {origin[0]}・経度 {origin[1]} から近い順に広げます)"


def _visits_html(p: dict) -> str:
    """その区画を、どの巡回がいつ見終えたか。

    **日付まで出す。** 記録は前から `visits[巡回名]` に日時で入っていたのに、
    画面は名前しか出していなかった —— 一周に何十日もかかる台帳では、
    **「見た」より「いつ見た」のほうが知りたい**(古い順に配られるので、
    次にどこが回ってくるかもそこから読める)。

    **まだのものは空欄にしない。** 空だと、見ていないのか記録が落ちたのかが
    読めない(`_delete_source_cell` と同じ考え方)。
    """
    visits = p.get("visits") or {}
    if not visits:
        return '<span class="muted">まだ</span>'
    return "<br>".join(
        f"{esc(sweep)} <span class=\"muted\">"
        f"{esc(jst.compact(at) if (at := jst.parse(str(raw))) else str(raw))}</span>"
        for sweep, raw in sorted(visits.items())
    )


def _partition_link(name: str, key: str) -> str:
    """区画の名前から、**その区画に入っているもの**へ。

    区画は「この範囲の全員」を並べて漏れを問う単位なので、台帳に鍵と数が出ていても
    中身が見えないと、割り方が合っているのかを人が確かめられない
    (合っているかどうかは、並んだ顔ぶれを見て初めて分かる)。
    """
    href = f"/admin/collect/{quote(name)}/partition?{urlencode({'key': key})}"
    return f'<a href="{esc(href)}">{esc(key)}</a>'


def _collect_running_html(name: str | None = None) -> str:
    """いま走っている収集の 1 行。**終わったものの控えとは別に出す**。

    控え(`app/collect_log.py`)は終わってから 1 行になるので、押した直後は何も出ない
    —— 走っているのかどうかを確かめるのに、別の画面まで見に行くことになっていた。
    """
    rows = [
        row for row in ai_inflight.running()
        if str(row.get("caller") or "").startswith("collect:")
        and (name is None or row.get("caller") == f"collect:{name}")
    ]
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td>{esc(jst.format(jst.parse(row.get('at') or '')) or '')}</td>"
        f"<td>{esc(ai_inflight.caller_label(str(row.get('caller') or '')))}</td>"
        f"<td>{esc(str(row.get('backend') or ''))}"
        + (f' <span class="muted">{esc(str(row.get("model") or ""))}</span>'
           if row.get("model") else "")
        + "</td>"
        + '<td><span class="job-status running">走っています</span></td></tr>'
        for row in rows
    )
    return (
        f'<p class="muted">いま {len(rows)} 件走っています'
        "(終わると下の「直近の変更」に 1 行増えます)。</p>"
        "<table><thead><tr><th>始めた時刻</th><th>収集</th><th>相手</th><th>状態</th>"
        f"</tr></thead><tbody>{cells}</tbody></table>"
    )


CHANGE_LABELS = {
    collect.CHANGE_ADDED: "足した",
    collect.CHANGE_UPDATED: "直した",
    collect.CHANGE_REMOVED: "消した",
}


def _changed_here_html(name: str, sources: dict, sweep: str | None) -> str:
    """その回が動かしたもの(`collect.changed_here`)。**回を選んだときだけ出す**。

    **「直近の変更」では届かないところを埋める。** あちらは回ごとに 1 行で、
    動いた見出しは頭の 20 件までしか残らない —— 1 回で数千件動く回では、
    何が動いたのかがほとんど読めない。ここは焼いたものを文書の側から引くので、
    その回が何回前に走っていようが、動かした全部が出る。

    **出るのは「最後に動かした回」だけ。** 後の回が同じ 1 件に触れば、その 1 件は
    そちらの一覧へ移る —— 印を 1 回分しか持たないため。書いておかないと、
    整理が直したはずのものが見当たらない理由が読めない。
    """
    if not sweep:
        return ""
    docs = collect.changed_here(name, sources, sweep, limit=CHANGED_HERE_LIMIT)
    head = f"<summary>「{esc(sweep)}」が動かしたもの</summary>"
    if not docs:
        return (
            f'<details id="changed">{head}'
            '<p class="muted">いま手元にあるもののうち、この回が最後に動かしたものは'
            "ありません。</p></details>"
        )
    rows = []
    for doc in docs:
        at = jst.parse(doc["updated_at"] or "")
        change = (doc.get("extra") or {}).get(collect.CHANGE_KEY) or ""
        mark = CHANGE_LABELS.get(str(change), "")
        rows.append(
            f"<tr><td>{esc(jst.format(at)) if at else ''}</td>"
            f'<td>{esc(mark) if mark else "<span class=\"muted\">—</span>"}</td>'
            f"<td>{_doc_link(name, doc['title'])}"
            f'<br><span class="muted">{esc((doc["opening"] or "")[:120])}</span></td></tr>'
        )
    more = (
        f'<p class="muted">新しい順に {CHANGED_HERE_LIMIT} 件まで。</p>'
        if len(docs) >= CHANGED_HERE_LIMIT else ""
    )
    return f"""
<details id="changed" open>{head}
<table>
<thead><tr><th>いつ</th><th>何を</th><th>見出し</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
{more}
<p class="muted">印は 1 件につき 1 回分だけなので、<strong>後の回が同じ見出しに触れば
そちらへ移る</strong>。消えたものもここに出る(読み口からは返らないが、手元には残っている)。</p>
</details>
"""


def _changes_filter_html(name: str | None, sweep: str | None) -> str:
    """「どの回を見るか」の切り替え。**回が 1 本しかなければ出さない**(選ぶ先が無い)。

    押した先はこの節へ戻す(`#changes`)—— 長い面の途中にある表なので、
    絞り込んだ結果が画面の外に出ていると、押しても何も起きていないように見える。
    """
    names = collect_log.sweeps(name)
    if len(names) < 2:
        return ""
    base = f"/admin/collect/{quote(name)}" if name else "/admin/memory"
    links = [("すべて", None), *((one, one) for one in names)]
    out = []
    for label, value in links:
        href = f"{base}?sweep={quote(value)}#changes" if value else f"{base}#changes"
        out.append(
            f"<strong>{esc(label)}</strong>" if value == sweep
            else f'<a href="{esc(href)}">{esc(label)}</a>'
        )
    return f'<p class="muted">どの回を見るか: {" / ".join(out)}</p>'


def _collect_changes_html(
    limit: int = 30, name: str | None = None, sweep: str | None = None,
) -> str:
    """直近どこに修正が入ったか(`app/collect_log.py`)。

    **表の「前回」列とは別に要る。** あちらは最新の 1 回で上書きされるので、
    6 時間ごとに回る収集なら朝には昨夜の 1 回しか残っていない。減り続けているのか、
    ある日だけ荒れたのかは、並べて初めて読める。

    **回で絞れるようにする。** 間隔は回ごとに桁違いなので、新しい順に並べるだけでは
    **短い回が長い回を押し流す** —— 1 時間ごとの回が 24 行を占めれば、4 時間ごとの
    回の差分は 1 日ぶんも残らない。読みたいのは「整理が何を直したか」のほうなのに。

    **控えの置き場が無ければ、何も出さずに理由だけ出す** —— 空の表を出すと
    「まだ動いていない」と読めてしまう(実際は記録していないだけ)。
    """
    if collect_log.db_path() is None:
        return (
            '<details id="changes"><summary>直近の変更</summary>'
            '<p class="muted">変更履歴は記録していません。'
            "<code>CHIEZO_STATE_DIR</code> を設定すると残ります。</p></details>"
        )
    picker = _changes_filter_html(name, sweep)
    changes = collect_log.recent(name, limit=limit, sweep=sweep)
    if not changes:
        return (
            f'<details id="changes" open><summary>直近の変更</summary>{picker}'
            '<p class="muted">'
            + ("その回はまだ走っていません。" if sweep else "まだ 1 回も走っていません。")
            + "</p></details>"
        )
    rows = []
    for row in changes:
        at = jst.parse(row["at"] or "")
        when = esc(jst.format(at)) if at else esc(row["at"])
        # **誰に頼んだ回かを出す。**「AI への依頼」の表と同じ書き方にそろえる ——
        # 同じ依頼が 2 つの画面で違って見えると、突き合わせるときに読み替えが要る
        who = (
            ai_history.who_html(row["backend"], row["model"], row["effort"])
            if row["backend"] else '<span class="muted">—</span>'
        )
        # **かかった時間を出す。** 件数からは遅くなったことが読めない —— 同じ件数を
        # 返していても、5 分が 20 分になっていれば一周の見込みが 4 倍ずれる。
        # 失敗の行にも出す(すぐ落ちたのか、待ち切って落ちたのかで打つ手が違う)。
        # 測っていない古い行は空欄(0 秒と混ぜない)
        spent = (
            f'<span class="muted">{esc(ai_history.took(row["ms"]))}</span>'
            if row.get("ms") is not None else ""
        )
        if row["status"] != collect_log.STATUS_OK:
            rows.append(
                f"<tr><td>{when}</td>{_changes_name_cell(row, name)}<td>{esc(row['sweep'])}</td>"
                f"<td>{who}</td><td>{spent}</td>"
                f'<td colspan="2"><span class="stale">失敗: {esc(row["error"])}</span></td></tr>'
            )
            continue
        # 動かなかった回も 1 行として出す。**空白にしない** —— 走ったが何も
        # 変わらなかったのと、走っていないのは別物
        marks = []
        if row["added"]:
            marks.append(f"+{row['added']}")
        if row["updated"]:
            marks.append(f"直し {row['updated']}")
        if row["removed"]:
            marks.append(f'<span class="stale">-{row["removed"]}</span>')
        summary = " / ".join(marks) or '<span class="muted">変化なし</span>'
        # **見出しは押せるようにする。** 名前だけ並べても「どう書き換わったか」は
        # 読めず、プロンプトを直す判断には中身の変化のほうが要る
        moved = []
        for label, key in (("足した", "added_titles"), ("直した", "updated_titles"),
                           ("消した", "removed_titles")):
            if row[key]:
                links = "、".join(_doc_link(row["name"], t) for t in row[key])
                moved.append(f"{label}: {links}")
        detail = (
            f'<details><summary class="muted">動いたもの</summary>'
            f'<div class="muted">{"<br>".join(moved)}</div></details>'
            if moved else ""
        )
        # **どこを見た回かを出す。** 「直近どこに修正が入ったか」は、件数だけでは
        # 答えにならない —— ざっとの回なのか割り込みなのかも、どの範囲かも読めない
        scope = (
            f'<br><span class="muted">{esc("、".join(row["scope"]))}</span>'
            if row["scope"] else ""
        )
        # 成功した回にも断り書きが付くことがある(答えが途中で切れた等)。
        # **件数だけ見て「少ない」と読まれないように**、そこへ並べて出す
        note = (
            f'<br><span class="stale">{esc(row["error"])}</span>' if row["error"] else ""
        )
        rows.append(
            f"<tr><td>{when}</td>{_changes_name_cell(row, name)}"
            f"<td>{esc(row['sweep'])}{scope}</td><td>{who}</td><td>{spent}</td>"
            f'<td>{summary}{note}</td><td>{row["total"]:,} 件{detail}</td></tr>'
        )
    return f"""
<details id="changes" open><summary>直近の変更</summary>
{picker}
<table>
<thead><tr><th>いつ</th>{"" if name else "<th>収集</th>"}<th>どの回</th><th>頼んだ相手</th>
<th>かかった</th><th>変化</th><th>焼いた後</th></tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
<p class="muted">新しい順に最大 {limit} 件。「かかった」は<strong>集めるのにかかった時間</strong>で、
焼くぶんは入らない(控えを書いてから流すため)。
記録は <code>state/collect_runs.db</code> に残り、古いものから捨てられる。</p>
</details>
"""


def _changes_name_cell(row: dict, name: str | None) -> str:
    """収集の名前の欄。**1 つの収集の面では出さない**(全部同じ名前が並ぶだけ)。"""
    return "" if name else f"<td>{esc(row['name'])}</td>"


def _redo_form(item, disabled: str) -> str:
    """最後の 1 回をやり直す口。**戻す先が無ければ出さない**。

    押すと進み具合と区画の印を戻してから走らせる。**中身は戻さない**ので、
    そのことを押す前に書いておく(焼いた世代は 1 つ前までしか残らない)。
    """
    undo = item.last_undo or {}
    if not undo.get("sweep"):
        return ""
    when = jst.format(jst.parse(str(undo.get("at") or ""))) if undo.get("at") else ""
    confirm = (
        f"「{undo['sweep']}」の最後の 1 回をやり直します。"
        "進み具合と区画の印は戻しますが、集めた中身は戻りません"
        "(もう一度集めた結果で上書きされます)。よろしいですか?"
    )
    return (
        '<p class="muted">最後に走ったのは '
        f"<strong>{esc(str(undo['sweep']))}</strong>"
        + (f"({esc(when)})" if when else "")
        + "。設定を直したなら、同じところからやり直せる。</p>"
        f'<form class="init-form" method="post"'
        f' action="/admin/collect/{esc(quote(item.name))}/redo"{disabled}'
        f" onsubmit=\"return confirm('{esc(confirm)}')\">"
        f'<button type="submit"{disabled}'
        ' title="進み具合と区画の印を戻してから、もう一度走らせます">'
        "最後の 1 回をやり直す</button></form>"
    )


def _handoff_html(item, disabled: str = "") -> str:
    """手で回す回の受け渡し(`app/handoff.py`)。**巡回が無ければ何も出さない。**

    出すのは 3 つ —— 束(ファイル)・**貼り付ける一言**・答えを読み込む口。
    **一言が要る。** ファイルだけ渡すと、web の画面は要約や感想を返してくる
    (何をするかは本文で言わないと伝わらない)。

    **束を作る口は、区画を回らない収集にも出す。** あちらは押したときだけ作る
    (区画を回る収集は、答えが焼けたら次を自動で組む)。
    """
    from app import handoff

    sweep = collect.by_hand_sweep(item)
    if sweep is None:
        return ""
    if not handoff.is_enabled():
        return ('<h3>手で回す</h3><p class="muted">'
                "束の置き場がありません(<code>CHIEZO_STATE_DIR</code> が未設定)。</p>")
    held = handoff.get(item.name)
    make = (
        f'<form method="post" action="/admin/collect/{esc(item.name)}/handoff"'
        f' class="init-form"><button type="submit"{disabled}>束を作る</button></form>'
    )
    if held is None:
        return (
            f'<h3>手で回す(巡回「{esc(sweep.name)}」)</h3>'
            '<p class="muted">依頼文をファイルに書き出して、web の画面から使う AI に'
            "渡します。答えのファイルを読み込ませると、AI に頼んだ回と同じ道で焼かれます。</p>"
            f"{make}"
        )
    made = esc(jst.format(jst.parse(held.get("created_at") or "")) or "")
    body = [
        f'<h3>手で回す(巡回「{esc(str(held.get("sweep") or sweep.name))}」)</h3>',
        '<table><tbody>'
        f'<tr><th>作った時刻</th><td>{made}</td></tr>'
        f'<tr><th>渡す範囲</th><td>{len(held.get("keys") or []):,} 区画 / '
        f'{int(held.get("docs") or 0):,} 件</td></tr>'
        f'<tr><th>大きさ</th><td>{int(held.get("bytes") or 0):,} バイト</td></tr>'
        "</tbody></table>",
        f'<p><a href="/v1/collect/{esc(item.name)}/handoff/file">→ 束のファイルを取る</a>'
        "(これを相手に添付します)</p>",
        # **貼り付ける一言。** ファイルを添えるだけでは、読んで作業してもらえない
        f'<p class="muted">添えて送る一言:</p><pre>{esc(handoff.PASTE_NOTE)}</pre>',
    ]
    if held.get("answered_at"):
        body.append(
            '<p class="stale">答えを読み込みました('
            f'{int(held.get("answer_items") or 0):,} 件)。取り込みが焼くのを待っています。</p>'
        )
    else:
        body.append(
            f'<form method="post" action="/admin/collect/{esc(item.name)}/handoff/answer"'
            ' enctype="multipart/form-data" class="collect-form">'
            "<p><label>答えのファイル(JSON)<br>"
            '<input type="file" name="answer" accept=".json,.txt,.md,application/json">'
            "</label></p>"
            '<p><label>貼り付けで読み込む(ファイルが無いとき)<br>'
            '<textarea name="text" rows="4" spellcheck="false"></textarea></label></p>'
            f'<p><button type="submit"{disabled}>答えを読み込んで焼く</button></p></form>'
        )
    body.append(
        f'<form method="post" action="/admin/collect/{esc(item.name)}/handoff/drop"'
        ' class="init-form" onsubmit="return confirm(\'この束を捨てますか。'
        '答えを読み込んでいないぶんは失われます\')">'
        '<button type="submit" class="danger">この束を捨てる</button></form>'
    )
    return "".join(body)


def _collect_detail_html(
    item, disabled: str, sources: dict | None = None, busy: bool = False,
) -> str:
    """1 つの収集の中身(プロンプト・進み具合・区画・直す口)。

    **畳まない。** 一覧の中で開いていた頃は、開くたびに表が縦へ伸びて、
    他の収集の行が画面外へ押し出されていた —— 読みに来た人はその収集だけを
    見に来ているので、専用の面に置けば畳む理由が無い。
    """
    # **プロンプトは編集の中だけに置く。** 面の頭にも同じ長文を出していた頃は、
    # 2 回並ぶうえ、進み具合も区画も**そのぶん下へ押し出されていた** ——
    # この面を開く人が先に読みたいのは、どこまで進んだかのほう
    return (
        f'<p class="muted">進み具合(次の実行で {{cursor}} に入る値): '
        f'<code>{esc(item.cursor) or "(まだ無し)"}</code></p>'
        f"{_redo_form(item, disabled)}"
        f"{_handoff_html(item, disabled)}"
        f"{_partition_html(item, (sources or {}).get(item.name), busy)}"
        f"{_removed_html(item, sources or {})}"
        f"<details><summary>編集する</summary>"
        f'<form method="post" action="/admin/collect/{esc(item.name)}/edit" class="collect-form">'
        f'<p><label>説明<br><input name="description" value="{esc(item.description)}"></label></p>'
        f'<p><label>プロンプト<br><textarea name="prompt" rows="10">{esc(item.prompt)}</textarea></label></p>'
        f'<p><label>進み具合(空にすると最初から)<br>'
        f'<input name="cursor" value="{esc(item.cursor)}"></label></p>'
f"{_backend_hint()}"
        f'<p><label>消えすぎの歯止め(前の何割を下回ったら止めるか。0 で外す)<br>'
        f'<input name="keep_ratio" type="number" step="0.05" min="0" max="1"'
        f' value="{item.keep_ratio}"></label></p>'
        f'<p><label>抽出の指定(JSON。空なら毎回 AI に集めさせる)<br>'
        f'<textarea name="extract" rows="8" spellcheck="false">'
        f"{esc(_extract_json(item))}</textarea></label></p>"
        f'<p><label>種類<br><select name="kind">'
        + "".join(
            f'<option value="{esc(value)}"{" selected" if item.kind == value else ""}>'
            f"{esc(label)}</option>"
            for value, label in KIND_LABELS.items()
        )
        + "</select></label></p>"
        f'<p class="muted"><strong>流れ</strong>はニュースのように時とともに増えるもの。'
        f"直近だけが対象で、古いものは順に要らなくなる。"
        f"<strong>網羅</strong>は画家の名簿や全国の食事処のように、ある括りの全部が対象。"
        f"増減はしても<strong>古いものが要らなくなることはない</strong>ので、"
        f"期限では落とさない(区画で全部を回るのもこちらだけ)。</p>"
        f'<p><label>持つ日数(流れのときだけ。0 なら期限では落とさない)<br>'
        f'<input name="keep_days" type="number" min="0" value="{item.keep_days}">'
        f"</label></p>"
        f'<p><label>タグの確かめ方(JSON。空なら確かめない)<br>'
        f'<textarea name="verify_tags" rows="4" spellcheck="false">'
        f"{esc(_verify_tags_json(item))}</textarea></label></p>"
        f'<p class="muted"><code>{esc(VERIFY_TAGS_EXAMPLE)}</code> と書くと、'
        f"<code>代表作:&lt;見出し&gt;</code> の見出しがそのソースに無いタグを"
        f"<strong>焼く前に落とす</strong>。AI は代表作を挙げられても"
        f"<strong>それが記事として存在するかは知らない</strong> —— 読む側は"
        f"「タグがある = 押せば何か出る」と受け取るので、実在しない見出しが混ざると"
        f"押しても何も出ないものが並ぶ。<strong>既に入っているものにも掛かる</strong>"
        f"(1 回焼き直せば揃う)。</p>"
        f'<p><label>外向きの道具(JSON。空なら道具なし)<br>'
        f'<textarea name="feed" rows="5" spellcheck="false">'
        f"{esc(_feed_json(item))}</textarea></label></p>"
        f'<p class="muted">RSS / Atom を機械的に取ってきて、プロンプトの'
        f" <code>{{feed}}</code> へ<strong>参考として</strong>差し込む。"
        f"<strong>取ってきたものをそのまま溜めるわけではない</strong> ——"
        f" AI は自分でも調べ、渡されたぶんも含めて採否を判断する。"
        f" 取りに行くのは見出し・要約・URL・日付だけで、ページ本文は取らない。"
        f" 例: <code>{esc(FEED_EXAMPLE)}</code></p>"
        f'<p><label>区画の指定(JSON。空なら区画を持たない)<br>'
        f'<textarea name="partition" rows="6" spellcheck="false">'
        f"{esc(_partition_json(item))}</textarea></label></p>"

        f'<p class="muted">区画を入れると、<strong>対象としている空間を密度で割って</strong>'
        f" 1 回に 1 区画ずつ順に回る。プロンプトに <code>{{partition}}</code> を入れると"
        f" そこへ今回見る範囲が差し込まれる。<strong>まだ 1 件も集めていない範囲にも"
        f"区画ができる</strong>ので、「この範囲に足すべきものが無いか確かめて」が書ける。"
        f" 母集団(<code>source</code>)を書けば、そのソースが知っている密度で割る。"
        f" 例: <code>{esc(PARTITION_EXAMPLE)}</code></p>"
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
        f"<details><summary>この部分を集中的に直させる</summary>"
        f'<form method="post" action="/admin/collect/{esc(item.name)}/focus"'
        f' class="collect-form"{disabled}>'
        f'<p><label>どう直してほしいか<br>'
        f'<textarea name="note" rows="3" required'
        f' placeholder="例: この店は移転しているはず。住所を確かめて直して"'
        f"></textarea></label></p>"
        f'<p><label>直す見出し(1 行に 1 つ。空でもよい)<br>'
        f'<textarea name="titles" rows="3"></textarea></label></p>'
        f'<p><label>見てほしい区画(空なら上の見出しだけを見る)<br>'
        f'<input name="partition" value=""></label></p>'
        f'<p class="muted">定時の巡回には影響しません —— 進み具合も、次にいつ走るかも、'
        f"区画の巡回記録も動きません。<strong>必ず「直す」側で走ります</strong>"
        f"(集めるだけの収集でも、名指ししたものを直せます)。</p>"
        f'<button type="submit"{disabled}>いま直させる</button></form></details>'
        f"<details><summary>AI に相談して直す</summary>"
        f'<form method="post" action="/admin/collect/consult" class="collect-form">'
        f'<input type="hidden" name="name" value="{esc(item.name)}">'
        f'<p><label>どう直したいか<br>'
        f'<textarea name="feedback" rows="4"'
        f' placeholder="例: 件数を5件に減らし、海外のニュースも入れて。'
        f'出典は必ず付けさせて。"></textarea></label></p>'
        f'<p class="muted">AI に聞くので十数秒〜1分ほどかかります。案は保存されないので、見てから決められます。</p>'
        f'<button type="submit">相談する</button></form></details>'
    )


# 親の下に寄せる深さの上限。**輪になっていても止まる**ための数で、
# 実際に要るのは 1 段(溜めたものから 1 つ作る)。読める字下げの限界でもある
MAX_NEST = 3


def _nested(items) -> list[tuple]:
    """**親の下に子を寄せた並び**。`(収集, 深さ)` を返す。

    **溜めたものから作る収集は、元になるソース 1 つにつき 1 つ増えていく**
    (記事のタグから話題の索引、集めた店から系列、名簿から派閥)——
    一覧が平らなままだと、何と何が組なのかが名前の見当だけになる。

    **親子は `collect.derives_from` が決める**(新しい欄は持たない。どこから
    作られたかは指定に書いてあり、別に持つと 2 つがずれる)。

    **たどれなかったぶんは落とさずに後ろへ置く。** 指定が輪になっていると
    (A が B を読み、B が A を読む)どちらも根から届かない —— 落とすと、
    画面から消えた収集が裏で回り続けることになる。
    """
    known = {one.name for one in items}
    children: dict[str, list] = {}
    for one in items:
        children.setdefault(collect.derives_from(one, known), []).append(one)
    out: list[tuple] = []

    def walk(name: str, depth: int) -> None:
        for one in children.get(name, []):
            out.append((one, depth))
            if depth + 1 < MAX_NEST:
                walk(one.name, depth + 1)

    walk("", 0)
    seen = {one.name for one, _depth in out}
    return out + [(one, 0) for one in items if one.name not in seen]


def _derived_note(item, parent: str) -> str:
    """親を指す一言。**何から作っているのかまで書く** ——
    字下げだけだと「近い名前が並んでいる」ようにしか見えない。

    **タグから作る回はそう書く**(`of: "tags"`)。あれは溜めたものの索引を作る
    口で、文書を 1 件ずつ引く名簿とは読む人の期待が違う。
    """
    if not parent:
        return ""
    of_tags = any(
        str(one.get("of") or "") == "tags" for one in extraction.specs(item.extract)
    )
    what = "のタグから作る索引" if of_tags else "から作る"
    return f'<br><span class="muted">└ {esc(parent)}{what}</span>'


def _collect_html(
    sources: dict[str, Source], disabled: str, sweep: str | None = None,
) -> str:
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
    known = {one.name for one in items}
    rows = []
    for item, depth in _nested(items):
        parent = collect.derives_from(item, known)
        # 止めている収集は行ごと薄くする(「AI の相手」の表と同じ扱い)
        cls = "" if item.enabled else ' class="off"'
        toggle_label = "止める" if item.enabled else "有効化"
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
        # **一覧でまず見えるのは種類。** 流れか網羅かで、その収集が何をしているのかが
        # 決まる —— 「整理」は返ってきた 1 件で何ができるかの話でしかなく、
        # そちらを目立たせていたせいで、種類のことだと読まれた
        kind_mark = f' <span class="stale">{esc(KIND_MARKS[item.kind])}</span>'
        # 期限で落とすのは流れだけ。**消えることは押す前に見えている必要がある**
        if item.keep_days:
            kind_mark += f' <span class="muted">{item.keep_days} 日ぶん</span>'

        # 次の 1 回が機械で埋まるかどうかは、押す前に見えていないと分からない
        if item.extract:
            kind_mark += (
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
        # **消す口は、止めてある収集にだけ出す。** 動いている収集を消すと、
        # 走っている最中の 1 回が行き先を失う(焼く先の定義がもう無い)。
        # 止めるほうが先、という順番を画面の側で示す
        delete_form = (
            f'<form class="init-form" method="post"'
            f' action="/admin/collect/{esc(item.name)}/delete"'
            f" onsubmit=\"return confirm('{esc(delete_confirm)}')\">"
            f'<button type="submit">削除</button></form>'
            if not item.enabled
            else '<span class="muted">止めると消せます</span>'
        )
        # **巡回ごとに 1 行。** 間隔も次の予定も前回も相手も巡回ごとに違うので、
        # 収集に 1 行だけ与えると、そこに出る値はどちらか片方のものにしかならない。
        # 名前と溜まった件数と操作は収集のものなので、行をまたがせる
        # **一覧には巡回の設定を出さない。** 直しに来る場所は収集の面で、
        # 一覧は「動いているか」を読むための表 —— 畳んであっても、収集の数だけ
        # 行が増えて、見たいものが画面の外へ押し出される
        sweep_cells = _sweep_cells(item, disabled, dry=False)
        span = f' rowspan="{len(sweep_cells)}"' if len(sweep_cells) > 1 else ""
        rows.append(
            f"<tr{cls}>"
            f'<td{span}{" class=\"child\"" if depth else ""}>'
            f'{"└ " * depth}'
            f'<a href="/admin/collect/{esc(quote(item.name))}">{esc(item.name)}</a>'
            f"{kind_mark}"
            f'<br><span class="muted">{esc(item.description)}</span>{requester}'
            f"{_derived_note(item, parent)}</td>"
            + f"<td{span}>{baked_docs}</td>"
            + sweep_cells[0]
            + f"<td{span}>"
            f'<form class="init-form" method="post" action="/admin/collect/{esc(item.name)}/toggle">'
            f'<button type="submit">{toggle_label}</button></form>'
            f"{delete_form}"
            f"</td></tr>"
        )
        # 2 本目からは巡回のぶんだけ。左右のセルは 1 行目から伸びている
        rows += [f"<tr{cls}>{cells}</tr>" for cells in sweep_cells[1:]]
    table = f"""
<table>
<thead>
<tr><th>名前</th><th>件数</th><th>巡回</th><th>頼む相手</th><th>間隔</th>
<th>前回</th><th>一周のうち</th><th>次にいつ</th><th>実行</th><th></th></tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
""" if rows else '<p class="muted">まだ収集がありません。下のフォームから作れます。</p>'
    return f"""
{table}
{_collect_running_html()}
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
<p class="muted">整理を選ぶときは、プロンプトに <code>{{current}}</code> を入れる
(そこへ今ある内容が差し込まれる)。返さなかったものはそのまま残り、消えるのは AI が
墓標を付けたときだけ。</p>
<p><label>プロンプト<br>
<textarea name="prompt" rows="6" required
 placeholder="{esc(collect.PROMPT_EXAMPLE)}"></textarea></label></p>
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

<h2 id="collect-history">実行履歴</h2>
{_collect_changes_html(sweep=sweep)}
"""


def _short_term_section_html(sources: dict[str, Source]) -> str:
    """書き込める置き場の節。**長期側と同じ体裁の表で出す**。

    表にするのは、見に来る人が知りたいことが長期側と同じだから —— 何件あって、
    最後に動いたのはいつで、いまの形(スキーマ)で引けるのか。

    **列は長期側の写しにしない。** ダンプも取り込みも無いので `dump_date` と
    `built_at` は書きようがなく、空欄が並ぶ。**代わりに「最後に書かれた時刻」を出す**
    —— 書き込める置き場で「動いているか」を言えるのはそこ。`lang` も持たない。

    **操作の列は持たない**。長期側の右端は再構築と削除だが、こちらは取り込みで焼く
    ソースではないので押すものが無い(消せもしない —— `registry.SYSTEM_SOURCES`)。
    検索への入口は名前がそのままリンクなので、「検索する」を別の列に置くと
    **同じ行き先が 1 行に 2 つ**並んで幅を食うだけになる。

    **設定の置き場も同じ表に並べる。** 節を分けていた頃は、同じ「書き込める置き場」を
    2 か所で同じ体裁で出していた —— 置き場が別なのはファイルの話で、
    **人が消せないのは保存先が違うから**であって、表に並んでいるか
    どうかとは関係がない。分ける理由が無いのに分けると、どちらを見ればよいのかを
    読む人が覚えることになる。
    """
    if not notes.is_enabled():
        return (
            '<p class="muted">短期記憶は無効です。書き込み可能なディレクトリを'
            " <code>CHIEZO_NOTES_DIR</code> に設定すると有効になります。</p>"
        )
    latest = latest_schema_version()

    def schema_cell(src: Source | None) -> str:
        if src is None:
            return '<span class="muted">不明</span>'
        if src.schema_version >= latest:
            return str(src.schema_version)
        return f'{src.schema_version} <span class="stale">(最新: {latest})</span>'

    # **まだ 1 件も無くても行は出す**(件数 0 として)—— 表ごと消えると、
    # 置き場が無いのか空なのかが読めない。書き方は下の断りで伝える
    rows = [
        (notes.SOURCE_NAME, notes.SOURCE_KIND, notes.count() or 0, notes.last_updated(),
         "覚えたこと。人と AI が読み書きする"),
    ]
    if machine_store.is_enabled():
        kept = machine_store.records()
        rows.append((
            machine_store.SOURCE_NAME, machine_store.SOURCE_KIND, len(kept),
            max((r["updated_at"] for r in kept if r["updated_at"]), default=""),
            "機械が書き換える設定(収集の定義・ワーカー)。人は書かない",
        ))
    cells = []
    for name, kind, total, written in ((r[0], r[1], r[2], r[3]) for r in rows):
        at = jst.parse(written or "")
        cells.append(
            f'<tr><td><a href="{esc(browse_url(name))}">{esc(name)}</a></td>'
            f"<td>{esc(kind)}</td><td>{total:,}</td>"
            f'<td>{esc(jst.format(at)) if at else "<span class=\"muted\">—</span>"}</td>'
            f"<td>{schema_cell(sources.get(name))}</td></tr>"
        )
    what = "".join(
        f'<br><strong>{esc(name)}</strong>: {esc(note)}' for name, _k, _t, _w, note in rows
    )
    empty = (
        '<p class="muted">まだ何も覚えていません。MCP の <code>remember</code> か'
        " <code>POST /v1/chiezo_memory</code> で書き込めます。</p>"
        if not rows[0][2] else ""
    )
    tags = notes.tag_summary()
    tag_html = (
        f'<p class="muted">{esc(notes.SOURCE_NAME)} のタグ: '
        + " / ".join(f"{esc(tag)} {docs:,}" for tag, docs in tags)
        + "</p>"
        if tags
        else ""
    )
    return f"""
<table>
<thead>
<tr><th>name</th><th>kind</th><th>docs</th><th>最後に書かれた</th><th>schema_version</th></tr>
</thead>
<tbody>
{"".join(cells)}
</tbody>
</table>
{empty}
{tag_html}
<p class="muted">
長期記憶と同じ口で引ける(<code>/v1/&lt;name&gt;/search|doc|filter|tags</code>)。
取り込みで焼くソースではないので、ダンプの日付も焼いた時刻も持たない
(再構築も削除もできない。書き込みが直接届く場所){what}
</p>
<p class="muted">
設定の置き場に<strong>直す口は持たない</strong> —— ここを手で書き換えても、
次に機械が書いた拍子に消える(直すのはそれぞれの画面から)。
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
# トップに要約を置き、深いところは選んで入る。
PAGES = (
    # **先頭に置く。** 毎回まず見に来るのは「いま何が動いているか」で、
    # 走らせるボタンを押したあとに連れてこられるのもここ
    (STATUS_PAGE, "状況", "いま動いているもの。取り込み・AI への依頼・使用量・ディスクの空き"),
    ("/admin/memory", "記憶", "溜めて引く。短期記憶・長期記憶・初期化"),
    # **記憶の隣に置く。** タスクもルールも短期記憶のメモにタグで載っているだけで、
    # 別の置き場を持たない —— 記憶を見に来た流れでそのまま開ける位置にする
    # (かつては別プロセスの SPA で、帯からは外部リンクのように見えていた)
    ("/admin/todo", "ToDo", "タスクとルール。短期記憶の上にタグで載る層"),
    # **記憶から切り出した面。** 記憶の中に畳んでいた頃は、収集を 1 本見るのに
    # 長期記憶の一覧と初期化の表をまたいでいた —— 無人で回る層は毎日見に来る側で、
    # 一度入れたら開かない表と同じ高さに置く理由が無い。ワーカーも一緒に置く
    # (何を回すかと、誰に回すかは 1 つの話)
    ("/admin/collect", "収集", "無人で回る層。巡回・区画・変更履歴と、回す相手の並び"),
    ("/admin/ai", "AI と鍵", "貸し出すもの。話せる相手、使用量、依頼の履歴"),
    ("/admin/media", "見比べ", "作らせたものを並べて選ぶ。手元のものも持ち込める"),
    ("/admin/server", "その他", "このサーバー。Claude Code 連携といま動いているビルド"),
)


def nav_html(current: str) -> str:
    """どの面にも出す見出しの帯。**左に名前、右に面へのリンク**。

    **トップへ戻ってから選び直す、を毎回させない。** 別のモジュールの面
    （`views/media_compare.py`）からも呼ぶので公開している —— 写しを持つと、
    面が増えたときに片方の帯にだけ出ないことになる。

    **狭い画面ではプルダウンに畳む。** 面が増えるほど 1 行に入らなくなり、
    入らなければ折り返して見出しが 2 段 3 段になる。**JS は持たない**
    （管理画面の流儀）ので `<details>` で開閉する。

    横に並べる版と畳んだ版の**両方を出し、CSS がどちらかを消す** ——
    片方だけを出し分けるには画面の幅を知る必要があり、それは描く側には分からない。
    並びの素は `PAGES` の 1 か所なので、二度書いてもずれない。
    """
    def items():
        # **トップは並べない。** 見出しの名前(「Chiezo 管理画面」)がトップへの
        # リンクを兼ねる —— トップは各面への入口だけの面なので、毎回選ぶ行き先
        # として並べるほどの中身が無い
        for path, label, _note in PAGES:
            yield path, label, path == current

    # **`data-label` を添える。** 太字にすると字の幅が変わり、見ている面が
    # 移るたびに帯の中身が左右へずれる。CSS がこの文字列で「太字にしたときの幅」を
    # 先に取っておくので、太くしても動かない（下の `.admin-nav a::after`）
    wide = "".join(
        f'<span class="admin-here" data-label="{esc(label)}">{esc(label)}</span>' if here
        else f'<a href="{path}" data-label="{esc(label)}">{esc(label)}</a>'
        for path, label, here in items()
    )
    folded = "".join(
        f'<a href="{path}"{" class=\"admin-here\"" if here else ""}>{esc(label)}</a>'
        for path, label, here in items()
    )
    # いま見ている面の名前を summary に出す。**「メニュー」とだけ書かない** ——
    # 畳んでいるあいだ、どこに居るのかが画面から消える
    here_label = next((label for _p, label, h in items() if h), "メニュー")
    return f"""
<header class="admin-head">
  <a class="admin-brand" href="/admin">Chiezo 管理画面</a>
  <nav class="admin-nav">{wide}</nav>
  <details class="admin-menu">
    <summary>{esc(here_label)}</summary>
    <div class="admin-menu-panel">{folded}</div>
  </details>
</header>"""


def _usage_html(request: Request | None = None) -> str:
    """状況の面に出す「使用量」。**有効にしてある相手だけ**を、AI の面と同じ表で。

    かつては 1 行の帯に畳み、相手ごとに**いちばん詰まっている窓**を 1 つだけ
    出していた。あれでは**短い窓しか見えない相手が出る** —— 5 時間の窓が
    詰まっていても週の窓が空いていれば重い仕事は頼めるので、片方だけでは
    頼んでよいかを決められない。表なら窓が何本あっても段が増えるだけで済む。

    **使わない相手は出さない**(状況の面は概況で、設定を見に来る場所ではない)。
    全部の相手と説明が要るときは「AI と鍵」の面（`views/ai_usage.py`）——
    **そこへのリンクはここには置かない**。状況の面から辿れる面はメニューに並んでいて、
    節ごとに「詳しくはあちら」を足すと、同じ行き先が画面の中に何本も増える。

    **描くときに相手へ問い合わせない**（`usage.rows()` は控えを読むだけ）。
    状況の面は何度も開く画面なので、開くたびに外へ出ると相手のレート制限に当たる。
    """
    if not usage_store.is_enabled():
        return ""
    rows = [row for row in usage.rows() if row["enabled"]]
    # **取り直す口を状況の面にも置く。** ここは「重い仕事を頼んでよいか」を見に来る
    # 画面なので、数字が古いと判断できない —— 取り直すために AI の面まで開くのは、
    # 見に来た目的から遠い。**押した画面へ戻る**(`back`)
    button = (
        ai_usage.refresh_all_form("すべて取り直す", back=STATUS_PAGE, klass="usage-refresh")
        if usage.refreshable() else ""
    )
    if not rows:
        # 使う相手が 1 つも無いときは何も出さない（列だけの表は、枠が取れて
        # いないのか相手がいないのかが読めない）。枠がまだ取れていないだけなら
        # 行は出る —— その行に「まだ取っていない」と書くので、最初の 1 回も
        # 状況の面から始められる
        return ""
    return (
        # **「AI 使用量」と書く。** 状況の面はディスクや取り込みと並ぶので、
        # 「使用量」だけではディスクの使用量と区別が付かない
        f'<h2 id="{ai_usage.SECTION_ANCHOR}">AI 使用量</h2>\n'
        f'{ai_usage.banner_html(request)}'
        f'<p class="usage-strip">{button}</p>\n'
        f'{ai_usage.table_html(rows, back=STATUS_PAGE)}'
    )


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    """トップ。**各面への入口だけ**を受け持つ。

    いま動いているもの(取り込み・AI への依頼・使用量・ディスクの空き)は
    状況の面(`admin_status`)へ移した —— トップに並べていた頃は、面を選びに
    来ただけでも入口が画面の下へ押し出されていた。
    入口の札には概況を 1 行ずつ添える(どの面を開けばよいかを札で決められるように)。
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
        STATUS_PAGE: _status_summary(job, running),
        "/admin/memory": (
            f"長期 {len(long_term)} ソース / {docs:,} 文書、短期 {notes_docs:,} 件。"
            f"収集 {len(collections)} 件(有効 {len(enabled_collections)} 件)"
        ),
        "/admin/ai": f"話せる相手 {len(answer.backend_names())} 件",
        "/admin/todo": _todo_summary(),
        "/admin/server": esc(build_info.describe().splitlines()[0] if build_info.describe() else ""),
    }

    cards = "\n".join(
        f'<div class="admin-card"><h2><a href="{path}">{esc(label)}</a></h2>'
        f'<p class="muted">{esc(note)}</p>'
        f'<p>{summary.get(path, "")}</p></div>'
        for path, label, note in PAGES
    )

    # **トップにも同じ帯を出す。** ここだけ帯が無いと、面から戻ってきたときに
    # リンクの位置が変わる（見出しは帯が持つので `<h1>` は置かない）
    body = f"""
{nav_html("/admin")}
<div class="admin-cards">
{cards}
</div>
"""
    return HTMLResponse(content=page_shell("管理画面", body))


def _stopped_providers_html() -> str:
    """失敗を受けて Chiezo が自分で止めた相手の知らせ(状況の面の先頭)。

    **止めたことに気づけないと、相手が 1 つ減ったまま誰も直さない。** ワーカーは
    止まった相手を黙って飛ばすので(`workers._switched_off`)、回は別の相手で
    走り続け、止まったこと自体は表に出てこない。人が on に戻すまで出し続ける。
    **相手が言った理由も添える**(そのまま検索できるように、畳んで置く)。
    """
    try:
        stopped = settings_store.auto_disabled()
    except Exception:
        log.exception("could not read the providers Chiezo stopped")
        return ""
    lines = []
    for st in stopped:
        when = jst.parse(st.disabled_at)
        at = f"{jst.format(when)} に" if when else ""
        lines.append(
            '<div class="job-status error">'
            f"<p>⚠️ {esc(providers.label_of(st.provider))} は、{esc(at)}"
            "認証の失敗(401)が返ったので無効にしました。ワーカーの振り先からも外れています。"
            "「AI と鍵」の面で認証情報を登録し直し、「接続を試す」→「有効にする」を"
            "押すと戻ります。</p>"
            f'<details><summary class="muted">相手が言ったこと</summary>'
            f"<pre>{esc(st.disabled_reason)}</pre></details></div>"
        )
    return "\n".join(lines)


def _status_summary(job: dict | None, running: list[dict]) -> str:
    """トップの「状況」の札に添える 1 行。取り込みと AI への依頼が動いているか。

    **走っているときだけ強く書く** —— 札を見て開くかどうかを決めるための行なので、
    静かなときに目立たせても判断の足しにならない。
    """
    if job is None:
        ingest = "取り込み: 未設定"
    elif job.get("state") == "running":
        ingest = f"<strong>取り込み中({esc(str(job.get('source') or ''))})</strong>"
    elif job.get("state") == "unreachable":
        ingest = "取り込み: 繋がらない"
    else:
        ingest = "取り込み: 待機中"
    ai = (f"<strong>AI への依頼 {len(running)} 件走っている</strong>" if running
          else "AI への依頼は無い")
    # **止めた相手があれば札にも出す**(開かなくても気づけるように)
    try:
        stopped = len(settings_store.auto_disabled())
    except Exception:
        stopped = 0
    alert = f'。<span class="stale">⚠️ 認証の失敗で止めた相手 {stopped} つ</span>' if stopped else ""
    return f"{ingest}。{ai}{alert}"


@router.get(STATUS_PAGE, response_class=HTMLResponse)
def admin_status(request: Request):
    """状況。**いま何が起きているかが 1 画面で読めること**だけを受け持つ。

    設定は持たない —— 状態と数、それに「いま頼めるか」を読むための表だけを出して、
    直しに行くのは各面。**走らせるボタンを押した人もここへ連れてくる**
    (`STATUS_JOB`)—— 押した直後に見たいのは進み具合で、それが出るのはここだけ。
    """
    job = _fetch_trigger_status()
    running = ai_history.running_rows()
    body = f"""
{nav_html(STATUS_PAGE)}
{_stopped_providers_html()}
<p>{_disk_html(request.app.state.data_dir)}</p>
{_job_status_html(job, heading=True)}
{_running_html(running, _round_done(job))}
{_usage_html(request)}
"""
    return HTMLResponse(content=page_shell("状況", body))


def _todo_summary() -> str:
    """トップに出す ToDo の概況。**短期記憶が無効なら数えない**(置き場が無い)。"""
    if not notes.is_enabled():
        return '<span class="muted">短期記憶が無効なので置けません</span>'
    active = tasks.list_active_tasks()
    doing = sum(1 for t in active if t.status == tasks.STATUS_IN_PROGRESS)
    rules = tasks.list_rules()
    enabled = sum(1 for r in rules if r.enabled)
    return (
        f"未完了 {len(active)} 件(着手中 {doing} 件)。"
        f"ルール {len(rules)} 本(有効 {enabled} 本)"
    )


def _is_collection(name: str) -> bool:
    """その名前が収集か。**読めなければ「違う」に倒す**(状況の面を落とさない)。"""
    with suppress(Exception):
        return collect.get(name) is not None
    return False


def _round_done(job: dict | None) -> list[dict]:
    """いま走っている取り込みが収集なら、**その回でもう終わった依頼**。

    **1 回の取り込みで何本も走る。** 巡回は区画ごとに AI を呼ぶので、状況の面に出るのは
    そのうち**いま飛んでいる 1 本だけ**だった —— 終わったぶんは控えへ移るので、
    「この回で何本目か」「さっきのは通ったのか」が状況の面からは読めない。
    **回の始まりは取り込みの開始時刻**(依頼元が同じでも、前の回のぶんまで
    引っ張ってきては意味が変わる)。

    **走っているのが収集でなければ空**(回という括りが無い)。
    **落ちたぶんは出ない** —— 失敗の控えは依頼元を持たないので、回に結び付かない。
    """
    if not (job and job.get("state") == "running"):
        return []
    name = str(job.get("source") or "")
    # **収集かどうかは定義を引いて確かめる**(`_running_sweep` と同じ流儀)——
    # ダンプのソースを焼いている回には、回という括りが無い。
    # **読めなくても画面は落とさない**(状況の面の本体はここではない)
    if not name or not _is_collection(name):
        return []
    return [
        {**r, "state": "終わった", "prompt": ""}
        for r in usage_store.calls_by(f"collect:{name}", str(job.get("started_at") or ""))
    ]


def _running_html(running: list[dict], done: list[dict] | None = None) -> str:
    """いま走っている AI への依頼。

    ここが表なのは、**待たされているときに見に来る画面がここだから** ——
    数だけでは「何が遅いのか」が分からず、結局 AI の面まで開くことになる。

    **中身も出す**(`ai_history._prompt`)。同じ相手へ似た大きさの依頼を 2 本
    投げていると、相手と経過だけではどちらが遅いのか分からない ——
    畳んであるので、開いた人にだけ全文が出る。

    **走っていないときは何も出さない。** 空の表を置くと、いつも何かが動いていない
    ことのほうが目立つ。
    """
    # **終わったぶんは後ろに付ける**(新しい順のまま)。走っているものが先頭に
    # 来ていないと、いちばん知りたい「いま何が詰まっているか」が下へ流れる
    rows_in = list(running) + list(done or [])
    if not rows_in:
        return ""

    rows = "".join(
        f"<tr><td>{esc(ai_log.kind_label(r['kind']))}</td>"
        # 相手とモデルの書き方は `ai_history` と共有する —— 別々に書くと、
        # 同じ依頼が状況の面と表で違って見える(経過の `elapsed` と同じ理由)
        f"<td>{ai_history.who_html(r['backend'], r.get('model') or '', r.get('effort') or '')}</td>"
        f"<td>{esc(r['state'])}</td>"
        # **終わったぶんは「かかった時間」を出す** —— 経過(いまとの差)だと、
        # 終わっているのに時計が進み続けているように見える
        f'<td class="muted">'
        f'{esc(ai_history.took(r["ms"]) if r.get("ms") is not None else ai_history.elapsed(r["at"]))}'
        f"</td>"
        f'<td>{ai_history.caller_html(r.get("caller") or "")}</td>'
        f'<td>{ai_history.prompt_html(r.get("prompt") or "", r.get("prompt_bytes"))}</td></tr>'
        for r in rows_in
    )

    return f"""
<h2>いま走っている AI への依頼</h2>
<table>
<thead><tr><th>依頼</th><th>相手</th><th>状態</th><th>経過</th>
<th>依頼元</th><th>中身</th></tr></thead>
<tbody>{rows}</tbody>
</table>
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
    # 長期(大脳)= 取り込みで焼く読み取り専用のソース、
    # 短期(海馬)= 書き込める置き場(覚えたことも、機械が書く設定も、こちら側)。
    # **表を分けない** —— 置き場が別なのはファイルの話で、人が消せないのは
    # 保存先が違うからであって、同じ表に並んでいるかどうかとは関係が無い
    long_term = {n: s for n, s in sources.items() if not s.mutable}

    rows = "\n".join(
        f"<tr>"
        f"<td><a href=\"{esc(browse_url(s.name))}\">{esc(s.name)}</a></td>"
        f"<td>{esc(s.kind)}</td>"
        f"<td>{esc(s.lang or '')}</td>"
        f"<td>{s.doc_count:,}</td>"
        # **DB の大きさも出す。** ディスクを食っているのがどのソースかは、
        # 文書数からは読めない(1 件の重さがソースごとに桁で違う)
        f'<td class="nowrap">{esc(db_size_text(s.path))}</td>'
        f"<td>{esc(s.dump_date or '')}</td>"
        f"<td>{esc(s.built_at or '')}</td>"
        f"<td>{schema_cell(s.schema_version)}</td>"
        # **2 段に組む。** 上の段は世代を作り直す・消す口(再構築 → 削除)、
        # 下の段は世代を張り替える口と、張り替える先。1 列に縦積みしていた頃は、
        # 「1 つ前: 日時」がボタンの間に挟まって途中で折り返し、どれがどの口か読めなかった
        f"<td>"
        f'<div class="source-actions">'
        f'<form class="init-form" method="post" action="/admin/rebuild/{esc(s.name)}" '
        f"onsubmit=\"return confirm('{esc(s.name)} を再構築します。ダンプの取得からやり直すため"
        f"時間がかかります(構築中も現行 DB での配信は続きます)。よろしいですか?')\">"
        f'<button type="submit"{disabled}>再構築</button>'
        f"</form> "
        f"{_delete_source_cell(s, _collection_using(s.name), disabled)}"
        f"</div>"
        # **下の段は折り返させない。** 列が狭いと「1 つ前: 日時」がボタンの下へ落ち、
        # 横に並べた意味が消える(列のほうを広げる)
        f'<div class="source-actions nowrap">{_rollback_cell(s, "/admin/memory#long-term")}</div>'
        f"</td>"
        f"</tr>"
        for s in sorted(long_term.values(), key=lambda s: s.name)
    )
    if not rows:
        rows = '<tr><td colspan="9">登録済みのソースはありません</td></tr>'

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
{nav_html("/admin/memory")}
<h1>記憶(溜めて引く)</h1>
<p class="muted">
知識は 2 層。<strong>短期記憶</strong>は Chiezo で書き込める置き場で、覚えたことも、
機械が書く設定もこちら側。<strong>長期記憶</strong>は読み取り専用のソースで、ダンプから焼いたものと、
引くときの口はどちらも同じ。
</p>

<h2 id="short-term">短期記憶(書き込める置き場)</h2>
{_short_term_section_html(sources)}

<h2 id="long-term">長期記憶(ためた知識)</h2>
<p>登録ソース数: {len(long_term)} / 最新のスキーマバージョン: {latest_schema}</p>
<!-- **いつも出す。** 繋がらないときだけ大きな枠で断っていた頃は、表より先に
     警告が目に入り、読むだけの人にも「壊れている」ように見えた。押せない理由は
     この 1 行で足りる(ボタンは繋がらなければ灰色になる) -->
<p class="muted">
chiezo-trigger が立ち上がっていない場合、再構築と削除はできません。
</p>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th>docs</th><th>size</th><th>dump_date</th><th>built_at</th><th>schema_version</th><th></th></tr>
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

<!-- **長期記憶の中に畳んでおく。** ここを開くのは新しいソースを入れるときだけで、
     日々見に来るのは上の一覧と下の「集める」のほう —— 同じ高さで並べると、
     見たい節に着くまで使わない表を何度もまたぐことになる -->
<details id="init"><summary><h3>未初期化データの初期化</h3></summary>
<table>
<thead>
<tr><th>name</th><th>kind</th><th>lang</th><th></th></tr>
</thead>
<tbody>
{init_rows}
</tbody>
</table>
</details>

"""
    return HTMLResponse(content=page_shell("記憶", body))


@router.get("/admin/collect", response_class=HTMLResponse)
async def admin_collect(
    request: Request,
    sweep: str | None = Query(None, description="直近の変更を、この回のぶんだけに絞る"),
):
    """無人で回る層の面。**記憶から切り出してある**。

    記憶の中の 1 節だった頃は、収集を 1 本見るのに長期記憶の一覧と初期化の表を
    またいでいた —— あちらは一度入れたら開かない表で、こちらは毎日動いているものを
    読みに来る場所。同じ高さに並べる理由が無い。

    **ワーカーも同じ面に置く。** 何を回すかと、それを誰に回すかは 1 つの話で、
    離すと「なぜこの相手に回ったのか」を別の面と突き合わせて読むことになる。
    """
    # **取り込みの状態は 1 度だけ引く。** ワーカーの節も収集の表も同じことを
    # 知りたがるので、別々に聞くと 1 回の描画で trigger を 2 度叩く
    job = _fetch_trigger_status()
    running = str((job or {}).get("source") or "?") if (job or {}).get("state") == "running" else ""
    body = f"""
{nav_html("/admin/collect")}
<h1>収集(AI に集めさせて溜める)</h1>
<p class="muted">
無人で回る層。<strong>頼んだ文で AI が集め、そのまま長期記憶へ焼かれる</strong>。
巡回ごとに時計と相手を分けられる。
</p>

{ai_workers.section_html((_backend_select, _model_select), running)}

<h2 id="collect-settings">収集の設定</h2>
{_collect_html(request.app.state.sources, run_buttons_disabled(job), sweep)}
"""
    return HTMLResponse(content=page_shell("収集", body))


def _int_arg(request: Request, name: str, fallback: int) -> int:
    """クエリの数。**読めない値は既定に落とす** —— 画面は JS を持たないので、
    人が URL を手で書き換えることがある(そこで 500 にしては困る)。"""
    with suppress(TypeError, ValueError):
        return max(1, int(request.query_params.get(name, fallback)))
    return fallback


@router.get("/admin/ai/transcripts/{ident}", response_class=PlainTextResponse)
def admin_ai_transcript(ident: str):
    """控えの全文。**そのまま出す** —— CLI の出力は整形すると意味が変わる
    (空白と改行で区切りを表す相手がいる)。"""
    text = ai_transcript.full_text(ident)
    if text is None:
        raise HTTPException(404, {"error": "その控えはありません(掃除で消えたか、無効)"})
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


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
async def admin_server(_request: Request):
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
ビルド日時(JST)とビルド元のコミット。手元の <code>git log -1</code> と見比べれば、
変更が反映済みかが分かる。<code>docker compose pull &amp;&amp; docker compose up -d</code>
のあと、ここが新しくなっていなければ古いイメージのままになっている。<br>
<strong>イメージは別々に焼かれる</strong>ので、<strong>片方だけ古いまま</strong>が
普通に起きる —— 直したはずの不具合を追いかけ続けないために、立っているものは
並べて出す(立っていないものは出ない)。
</p>
{await _builds_html()}
"""
    return HTMLResponse(content=page_shell("このサーバー", body))


# 相手に版を聞きに行くときの待ち時間。**短くする** —— 立っていない相手を
# 待つあいだ画面が出ないほうが困る(版は添え物で、面の本体ではない)。
BUILD_PROBE_TIMEOUT = 3.0


async def _builds_html() -> str:
    """立っているものの版を並べた表。**聞きに行くのはこの面だけ**。

    管理画面は描くときに相手へ問い合わせない流儀だが、ここは**版を確かめに来る
    ためだけの面**で、開く頻度も低い。並行に聞いて、答えない相手は出さない
    (「起動していたら出す」)。
    """
    rows = [("chiezo-app(この面)", build_info.describe())]
    found = await asyncio.gather(_trigger_build(), _bridge_builds())
    rows += found[0] + found[1]
    body = "".join(
        f"<tr><th>{esc(name)}</th><td>{esc(text)}</td></tr>" for name, text in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


async def _trigger_build() -> list[tuple[str, str]]:
    """取り込み(chiezo-trigger)の版。立っていなければ空。"""
    if not TRIGGER_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=BUILD_PROBE_TIMEOUT) as client:
            res = await client.get(f"{TRIGGER_URL}/status")
        found = res.json().get("build") or {}
    except (httpx.HTTPError, ValueError) as e:
        log.info("trigger build unknown: %s", e)
        return []
    return [("chiezo-ingest(取り込み)",
             build_info.describe_of(found.get("sha") or "", found.get("built_at") or ""))]


async def _bridge_builds() -> list[tuple[str, str]]:
    """立っている CLI ブリッジの版。**有効にしてある相手だけ**聞く。

    ブリッジは LAN に口を開けないので、**版を外から確かめる手段がここしかない**。
    """
    specs = [
        spec for name in answer.backend_names()
        if (spec := providers.get(name)) is not None and spec.bridge
    ]
    if not specs:
        return []

    async def ask(spec) -> tuple[str, str] | None:
        url = providers.url_of(spec).rstrip("/")
        base = url[: -len("/v1")] if url.endswith("/v1") else url
        try:
            async with httpx.AsyncClient(timeout=BUILD_PROBE_TIMEOUT) as client:
                res = await client.get(f"{base}/health")
            body = res.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("bridge build unknown (%s): %s", spec.id, e)
            return None
        return (
            f"chiezo-bridge({spec.label})",
            build_info.describe_of(str(body.get("build") or ""), str(body.get("built_at") or "")),
        )

    return [row for row in await asyncio.gather(*(ask(spec) for spec in specs)) if row]


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

{_trigger_missing_html(job)}

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

{_trigger_missing_html(job)}

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
                "hint": "長期記憶へ書き込むときだけ要るサービス。立てるまで初期化・再構築・削除はできない",
            },
        ) from e
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json())


@router.post("/admin/ingest/stop")
def admin_ingest_stop():
    """走っている取り込みを降ろす(`chiezo-trigger` の `POST /stop` へ取り次ぐ)。

    **状況の面へ戻す。** 取り込みの塊を出しているのはそこだけ。
    """
    if not TRIGGER_URL:
        raise HTTPException(
            503, {"error": "取り込み(chiezo-trigger)が設定されていないので止められません"}
        )
    try:
        res = httpx.post(f"{TRIGGER_URL}/stop", timeout=TRIGGER_TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("chiezo-trigger stop request failed: %s", e)
        raise HTTPException(
            502, {"error": "chiezo-trigger unreachable (details in app logs)"}
        ) from e
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json())
    return RedirectResponse(url=STATUS_JOB, status_code=303)


def _proxy_trigger_run(source: str) -> RedirectResponse:
    """上を叩いて状況の面へ連れていく(init / rebuild 共通)。進み具合はそこで見る。"""
    trigger_run(source)
    return RedirectResponse(url=STATUS_JOB, status_code=303)


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


@router.post("/admin/ai/inflight/{call_id}/stop")
def admin_ai_stop(call_id: int):
    """走っている 1 往復を止めるよう頼む(「AI への依頼」の表の「止める」)。

    **1 回の上限を 60 分まで伸ばしたので、ここが歯止めになる。** 打ち切っても
    枠は返らないので「走っているなら待つ」に倒したが、そのままでは暴走した
    1 本が取り込みを 1 時間占める。

    **手を離すのは押した人のワーカーではない**(`--workers 2`)—— 控えに印を
    書くだけで、往復を掴んでいるワーカーが数秒のうちに見つけて降りる。
    **無い依頼を押されても咎めない** —— 表は数秒古いので、終わった直後に
    押されるのは普通に起きる(押した人にできることは何も無い)。
    """
    from app import ai_inflight

    ai_inflight.ask_to_stop(call_id)
    return RedirectResponse("/admin/ai#ai-history", status_code=303)


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


def _delete_source_cell(src: Source, used_by: str, disabled: str) -> str:
    """長期記憶の行に出す削除の口。**消せないものは理由を出す**。

    **押せないボタンを出さない。** 押せてから断られるより、なぜ押せないのかが
    先に読めるほうがよい(収集なら、消す場所がどこかも書く)。

    **名前を打たせる。** 世代ごと消して取り消せないので、収集の削除と同じ重さにする
    —— 確認ダイアログだけだと、隣の行のつもりで押せてしまう。
    **焼き直しにかかる時間も書く** —— ダンプ由来のソースは数時間かかるので、
    「消してもまた入れればよい」で押せる相手ではない。
    """
    if reason := registry.blocked_from_deleting(src.name, used_by):
        where = (
            f' <a href="/admin/collect/{esc(quote(used_by))}">収集の面へ</a>'
            if used_by else ""
        )
        return f'<span class="muted">{esc(reason)}</span>{where}'
    if not TRIGGER_URL:
        return '<span class="muted">取り込みが設定されていないので消せません</span>'
    ask = (
        f"{src.name} を消します。世代も素材も消え、取り消せません"
        f"(入れ直すには取り込みからやり直しになります)。続けるなら名前を入力"
    )
    return (
        f'<form class="init-form" method="post" action="/admin/source/{esc(quote(src.name))}/delete"'
        f" onsubmit=\"return prompt('{esc(ask)}') === '{esc(src.name)}'\">"
        f'<button type="submit"{disabled}>削除</button></form>'
    )


def _rollback_cell(src: Source, back: str) -> str:
    """1 つ前の世代へ戻す口。**戻せる世代が無ければ、その旨を出す**。

    **押せないボタンを出さない** —— 削除の口と同じ考え方で、押してから断られるより
    「戻せる相手が無い」が先に読めるほうがよい。

    **名前は打たせない。** 消す操作ではなく張り替えるだけで、押し直せば元へ戻る
    (世代は 2 つとも残る)—— 取り消せない操作と同じ重さにすると、焼き直しが
    中身を壊したときに行き来して確かめられない。
    """
    before = registry.previous_generation(src.path)
    if before is None:
        return '<span class="muted">戻せる世代はありません</span>'
    if not TRIGGER_URL:
        return '<span class="muted">取り込みが設定されていないので戻せません</span>'
    label = _generation_label(registry.generation_stamp(before))
    ask = (
        f"{src.name} を 1 つ前の世代({label})へ戻します。"
        "いまの世代は消さないので、押し直せば戻ります。"
    )
    return (
        f'<form class="init-form" method="post"'
        f' action="/admin/source/{esc(quote(src.name))}/rollback"'
        f" onsubmit=\"return confirm('{esc(ask)}')\">"
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button type="submit">1 つ前へ戻す</button></form>'
        # **ボタンの横に置き、日時は途中で割らない**(`nowrap`)—— 下に回すと
        # どのボタンの話なのかが離れ、日付が「2026-07-」と「24」に泣き別れた
        f' <span class="muted nowrap">1 つ前: {label}</span>'
    )


@router.post("/admin/source/{source}/rollback")
def admin_source_rollback(source: str, request: Request, back: str = Form("")):
    """焼いたソースを **1 つ前の世代へ戻す**。**張り替えるのは取り込み側**。

    `chiezo-app` は `corpus/` を読み取り専用でマウントしているので、ここからは
    リンクに触れない —— 削除と同じ道(`POST /source/{name}/rollback`)を通す。

    **焼き直しが中身を壊したときの逃げ道**。ブルーグリーンは世代を 2 つ残すのに、
    画面からは新しいほうしか見えなかった —— 壊れたと分かっても、取り込みを
    やり直す以外に戻す手が無い(集めたものは、やり直しても同じものが返らない)。

    **消さないので押し直せば元へ戻る。** 外したほうも残るので、行き来して
    どちらが正しいかを確かめられる。
    """
    sources: dict[str, Source] = request.app.state.sources
    if sources.get(source) is None:
        raise HTTPException(404, {"error": f"そのソースはありません: {source}"})
    if not TRIGGER_URL:
        raise HTTPException(
            503, {"error": "取り込み(chiezo-trigger)が設定されていないので戻せません"}
        )
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(f"{TRIGGER_URL}/source/{source}/rollback")
    except httpx.HTTPError as e:
        raise HTTPException(502, {"error": f"取り込みにつながりません: {e}"}) from None
    if res.status_code != 200:
        raise HTTPException(res.status_code if res.status_code < 500 else 502, {
            "error": f"ソース「{source}」を戻せませんでした",
            "reason": res.text[:300],
        })
    # **戻したらすぐ読み直す。** 5 秒ごとの再走査を待つと、戻したはずの件数が
    # 前のまま出て、押せていないように見える
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url=back or "/admin/memory#long-term", status_code=303)


@router.post("/admin/source/{source}/delete")
def admin_source_delete(source: str, request: Request):
    """焼いたソースを消す(世代ごと)。**消すのは取り込み側**。

    `chiezo-app` は `corpus/` を読み取り専用でマウントしているので、ここからは
    ファイルに触れない —— 収集の削除と同じ道(`DELETE /source/{name}`)を通す。
    **種別を名乗って呼ぶ**(`expect`)。名乗らない呼び出しでは集めたものしか
    消えないので、それ以外を消すにはここが名乗る必要がある。

    **書き込める置き場と、収集が使っているソースは断る**
    (`registry.blocked_from_deleting`)。前者は消すと中身がどこにも無くなり、
    後者は消しても次の巡回でまた焼かれる。

    **定義の無いソースは消せる。** 収集の削除でソースを消し損ねると
    (取り込みが立っていない・走っている最中だった)、**定義だけ消えて DB が残る**
    —— そこを画面から片付けられないと、手で消しに行くことになる(実際にそうなった)。

    **消せなかったら理由を出す。** ここは消すことが目的の操作なので、
    収集の削除のように「消せなくても先へ進む」にはしない。
    """
    sources: dict[str, Source] = request.app.state.sources
    src = sources.get(source)
    if src is None:
        raise HTTPException(404, {"error": f"そのソースはありません: {source}"})
    if reason := registry.blocked_from_deleting(source, _collection_using(source)):
        raise HTTPException(400, {"error": reason})
    if not TRIGGER_URL:
        raise HTTPException(
            503, {"error": "取り込み(chiezo-trigger)が設定されていないので消せません"}
        )
    _drop_source(source, src.kind)
    # **消したらすぐ一覧から外す。** 5 秒ごとの再走査を待つと、消したはずの行が
    # 残ったまま戻ってきて、押せていないように見える
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url="/admin/memory#long-term", status_code=303)


def _collection_using(name: str) -> str:
    """そのソースへ焼いている収集の名前(無ければ空)。

    **収集の名前がそのままソース名**なので引き当ては 1 対 1。定義が読めないときは
    「使われていない」とは言えないので、名前をそのまま返して消させない。
    """
    if not collect.is_enabled():
        return ""
    try:
        return name if any(c.name == name for c in collect.load()) else ""
    except (ValueError, HTTPException):
        return name


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
        backend=_blank_to_none(form.get("backend")),
    )
    collect.update(item.name, enabled=False)
    # 作った時点で空の DB ができるので、ソースを取り直して検索に出るようにする。
    # main を関数の中で import するのは、views → main の循環参照を避けるため
    # (main が router を include する側。下の「いま走らせる」と同じ書き方)
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url="/admin/collect", status_code=303)


@router.post("/admin/collect/{name}/edit")
async def admin_collect_edit(name: str, request: Request):
    """プロンプト・説明・間隔・進み具合を書き換える。

    **進み具合(カーソル)もここで直せる**。集め直したい・別の地域から始めたい、が
    プロンプトを書き換えるのと同じ場面で起きるため(値を消せば最初から)。
    """
    form = await request.form()
    # **巡回には触らない。** 設定は巡回ごとの行から保存する —— ここで全部を
    # 送り直していた頃は、1 本の間隔を直すのに他の巡回まで上書きしていた
    collect.update(
        name,
        prompt=str(form.get("prompt") or ""),
        description=str(form.get("description") or ""),
        # 空にできるように、cursor だけは None ではなく空文字を通す
        cursor=str(form.get("cursor") or ""),
        # 空欄は「使わない」。指定を外せるのはここだけ
        extract=_parse_extract(form.get("extract")),
        partition=_parse_partition(form.get("partition")),
        feed=_parse_feed(form.get("feed")),
        # **空の配列で外せる**(消す手段がここしかない)
        verify_tags=_parse_verify_tags(form.get("verify_tags")),
        kind=collect.normalize_kind(form.get("kind")),
        keep_days=str(form.get("keep_days") or "").strip() or None,
        # 0 も意味のある値(守りを外す)なので、空のときだけ触らない
        keep_ratio=_ratio(form.get("keep_ratio")),
    )
    return RedirectResponse(url="/admin/collect", status_code=303)


@router.post("/admin/collect/{name}/sweep")
async def admin_collect_sweep(name: str, request: Request):
    """巡回 1 本ぶんの設定を保存する。**その 1 本だけ**を書き換える。

    **名前を消すと、その巡回が消える**(足す口も消す口も名前 1 つで済ませている)。
    名前を書き換えれば改名になる —— そのときは予定と控えを引き継がない
    (`collect.update` は名前で突き合わせるため)。

    巡回を 1 本も持たない収集では、**収集そのものの設定**として書く。1 本しか
    持たない収集に一覧を持たせると、「既定」という名前だけが画面に増える。
    """
    form = await request.form()
    parsed = _parse_sweeps_form(form)
    key = str(form.get("sweep_key") or "").strip()
    item = collect.get(name)
    current = list(item.sweeps)
    sweep = parsed[0] if parsed else None

    # **名前を付けていない 1 本は、収集そのものの設定。** 1 本しか持たない収集に
    # 一覧を持たせると、「既定」という名前だけが画面に増える。
    # **名前を書いて保存したものは巡回になる**(まだ 1 本も持っていない収集でも)
    if sweep is not None and not current and sweep["name"] == collect.DEFAULT_SWEEP_NAME:
        collect.update(
            name,
            interval_minutes=sweep.get("interval_minutes"),
            # 空は「既定にまかせる」として通す(`collect.update` は None を「触らない」と読む)
            backend=str(sweep.get("backend") or ""),
            model=str(sweep.get("model") or ""),
            effort=str(sweep.get("effort") or ""),
        )
        return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)

    if sweep is None:
        # 名前を消した = この巡回を消す
        merged = [s for s in current if str(s.get("name") or "") != key]
    elif key and any(str(s.get("name") or "") == key for s in current):
        merged = [sweep if str(s.get("name") or "") == key else s for s in current]
    else:
        merged = [*current, sweep]
    item = collect.update(name, sweeps=merged)
    # **直したところへ戻す。** 一覧へ返していた頃は、直した結果を見るのに
    # もう一度その収集を探すことになった
    return RedirectResponse(url=collect_page(item), status_code=303)


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
        # **人が押して始めた依頼**。収集の時計が動かしているぶんと見分ける
        with ai_inflight.called_by("admin"):
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
        with ai_inflight.called_by("admin"):
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
<p class="muted"><a href="/admin/collect">管理画面へ戻る</a>(保存しなければ何も変わりません)</p>
"""
    return page_shell("抽出の指定", body)


@router.post("/admin/collect/{name}/toggle")
def admin_collect_toggle(name: str):
    """有効・無効を切り替える(見本を動かし始める入口でもある)。"""
    current = collect.get(name)
    collect.update(name, enabled=not current.enabled)
    return RedirectResponse(url="/admin/collect", status_code=303)


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
    # **動いている収集は消さない。** 口を隠すだけでは足りない —— URL は届くし、
    # 消した拍子に走っていた 1 回は焼く先の定義を失う。止めるほうが先
    # **無い名前でも 404 にしない**(消す操作は、既に無いなら done でよい)
    item = next((c for c in collect.load() if c.name == name), None)
    if item is not None and item.enabled:
        raise HTTPException(409, {
            "error": f"収集「{name}」は動いています",
            "hint": "先に「止める」を押してから消してください",
        })
    # **焼いたものを消せないなら、設定も消さない。** 「削除」は 1 つの操作として
    # 「設定と焼いたものが消える」ことを約束している —— 片方だけ消えると、
    # **次に集めたときに前の世代として戻ってくる**(消したはずの中身が件数にも
    # 区画にも入り続ける)。本番で 2 度踏んだ。
    #
    # **断っても行き止まりにならない。** 長期記憶の側の削除も同じ取り込みを使うので、
    # 取り込みが応えない状態では**どちらの道でも消せない** —— 片方だけ進めても、
    # 残ったものを片付ける手段は増えない。
    if (why := _drop_collect_source(name)) and name in request.app.state.sources:
        raise HTTPException(409, {
            "error": f"収集「{name}」の焼いたものを消せませんでした: {why}",
            "hint": "設定だけ消すと、次に集めたときに前の世代として戻ってきます。"
                    "取り込み(chiezo-trigger)が応える状態にしてから、もう一度押してください",
        })
    collect.remove(name)
    # 消した後のソース表を作り直す(消えたものが一覧に残らないように)
    from app.main import scan_all

    request.app.state.sources = scan_all(request.app.state.data_dir)
    return RedirectResponse(url="/admin/collect", status_code=303)


def _drop_collect_source(name: str) -> str:
    """焼いたソースを trigger に消させる(収集を消すついで)。**消せなかった理由**を返す
    (消せたなら空)。

    **ここでは例外にしない。** まだ 1 度も焼いていない収集では消す先が無く、
    それは普通の状態 —— 呼び出し側が「焼いたものがあるのに消せなかったのか」を
    見て断る(`admin_collect_delete`)。

    **理由は呼び出し側まで返す。** ログにしか出していなかった頃は、押した人に
    「消えたのか残ったのか」が分からなかった。

    **種別は名乗らない** —— 名乗らない呼び出しでは集めたものしか消えないので、
    収集の名前が他の種別のソースとぶつかっていても、そちらは消えない。
    """
    try:
        _drop_source(name)
    except HTTPException as e:
        log.warning("could not drop source %s: %s", name, e.detail)
        detail = e.detail
        why = detail.get("error") if isinstance(detail, dict) else str(detail)
        return str(why or "取り込みが応えませんでした")
    return ""


def _drop_source(name: str, expect: str = "") -> None:
    """取り込みにソースを消させる。**消せなければ理由を上げる**。

    `chiezo-app` は `corpus/` を読み取り専用でマウントしているので、消せるのは
    取り込み側だけ(長期記憶へ書けるのは ingest だけ、という線の裏返し)。

    `expect` を渡すと、その種別として消す。渡さなければ集めたものだけが対象
    (`ingest/server.py` の `delete_source`)。
    """
    if not TRIGGER_URL:
        raise HTTPException(503, {"error": "取り込み(chiezo-trigger)が設定されていません"})
    params = {"expect": expect} if expect else None
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.delete(f"{TRIGGER_URL}/source/{name}", params=params)
    except httpx.HTTPError as e:
        raise HTTPException(502, {"error": f"取り込みにつながりません: {e}"}) from None
    if res.status_code != 200:
        raise HTTPException(res.status_code if res.status_code < 500 else 502, {
            "error": f"ソース「{name}」を消せませんでした",
            "reason": res.text[:300],
        })


@router.post("/admin/collect/{name}/run")
async def admin_collect_run(name: str, request: Request):
    """予定を待たずに 1 回、集めて焼く(管理画面の「今すぐ実行」)。

    **止めている収集もここからは走らせる** —— 有効にする前に一度試せないと、
    プロンプトが通るかを確かめる手段が無くなる。REST の `/v1/collect/{name}/run` は
    同じことを断る(外のアプリに勝手な実行を許さないため)。画面を開けるのは
    Chiezo を操作している人だけ、という前提の差。

    **巡回ごとに押せる。** 相手も 1 回に見る量も巡回ごとに違うので、
    「じっくりのほうを今すぐ 1 回」が押せないと、分けて持った意味が半分になる。
    """
    from app.main import start_collection_bake

    form = await request.form()
    start_collection_bake(name, str(form.get("sweep") or "") or None)
    # **状況の面へ連れていく。** 走らせた本人が次に見たいのは、いま押した 1 回の
    # 進み具合 —— それが出るのは取り込みの塊で、塊は状況の面にしか無い
    return RedirectResponse(url=STATUS_JOB, status_code=303)


@router.post("/admin/collect/{name}/handoff")
async def admin_handoff_make(name: str, request: Request):
    """手で回す回の束を 1 つ作る(`app/handoff.py`)。AI も取り込みも動かさない。"""
    from app.main import build_handoff

    await build_handoff(name, request.app.state.sources)
    return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)


@router.post("/admin/collect/{name}/handoff/answer")
async def admin_handoff_answer(name: str, request: Request):
    """持ち帰った答えを読み込んで、焼く取り込みを 1 本起こす。

    **ファイルでも貼り付けでも受ける。** 相手がファイルで返すとは限らず
    (画面に JSON を出して終わることがある)、そのために一度ファイルへ
    保存させるのは手数が 1 つ増えるだけ。
    """
    from app import handoff
    from app.main import start_collection_bake

    form = await request.form()
    body = ""
    if (sent := form.get("answer")) is not None and hasattr(sent, "read"):
        body = (await sent.read()).decode("utf-8", "replace")
    if not body.strip():
        body = str(form.get("text") or "")
    held = handoff.get(name)
    if held is None:
        raise HTTPException(404, {"error": f"収集「{name}」に預かっている束がありません"})
    try:
        items, _cursor, note = collect.parse_response(body)
    except ValueError as e:
        raise HTTPException(400, {
            "error": "答えを読み取れませんでした",
            "reason": str(e)[:200],
            "hint": "束に書いてある形の JSON(items の配列)を、そのまま貼るか読み込ませてください",
        }) from None
    if not items:
        raise HTTPException(400, {
            "error": "答えから 1 件も読み取れませんでした",
            "hint": "束に書いてある形の JSON(items の配列)を、そのまま貼るか読み込ませてください",
        })
    handoff.answered(name, items, note)
    start_collection_bake(name, held.get("sweep") or None)
    return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)


@router.post("/admin/collect/{name}/handoff/drop")
async def admin_handoff_drop(name: str):
    """預かっている束を捨てる(答えごと)。次の束を作れるようになる。"""
    from app import handoff

    handoff.drop(name)
    return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)


@router.post("/admin/collect/{name}/redo")
async def admin_collect_redo(name: str, request: Request):
    """最後の 1 回を、やり直せる状態まで巻き戻してもう一度走らせる。

    **設定を直してからやり直したい**、が普通に起きる(依頼文を直した・相手を替えた)。
    そのまま「今すぐ実行」を押すと、進み具合が先へ進んでいるので**次のぶんを
    集めてしまう** —— 直したかった回は二度と来ない。

    **戻せるのは定義の側だけ。** 焼いた世代は 1 つ前までしか残らないので、対象の
    巡回が最後でなければ中身は戻せない。進み具合と区画の印を戻したうえで、
    もう一度集めさせて上書きする。
    """
    from app.main import start_collection_bake

    sweep = collect.rewind(name)
    start_collection_bake(name, sweep.name)
    return RedirectResponse(url=STATUS_JOB, status_code=303)


@router.post("/admin/collect/{name}/repartition")
def admin_collect_repartition(name: str, request: Request, tasks: BackgroundTasks):
    """**区画の割り直しだけを走らせる**(AI も焼きも動かさない)。

    **焼くのと同じ回に乗っていたのが重かった。** 割り直しは母集団を 1 周舐めて
    全点をメモリに載せるので、素材を流すのと重なると山が二つになる —— 本番で、
    台帳が空の状態から 686,602 件を割り直す回が、素材を 280,270 件まで流した
    ところで切れた。**台帳が消えると、いちばん重い回がいちばん条件の悪いときに
    来る**ので、先に台帳だけ整えておける口を分けてある。

    **焼いている最中は断る**(409)。あちらも終わりに台帳を書き戻すので、
    どちらが残るかが順番次第になる。**画面でボタンを消すだけにしない** ——
    口が受け付けるなら、いつか誰かが叩く。
    """
    collect.require_enabled()
    if _baking_now(_fetch_trigger_status(), name):
        raise HTTPException(
            status_code=409,
            detail="この収集を焼いている最中です(焼き終わってから押してください)",
        )
    # **二度押しを止める。** 2 本が同時に同じ母集団を読むので、メモリも時間も倍に
    # なる(本番の食事処は 686,602 件)—— しかも後に終わったほうが勝つだけで、
    # 早く終わるわけでもない
    if repartition_job.running(name):
        raise HTTPException(
            status_code=409,
            detail="いま割り直しています(終わるまで待ってください)",
        )
    # **走り始めた印は返す前に書く。** 書く前に走らせると、戻った画面が
    # 「押していない」ように見える(押した人はもう一度押す)
    repartition_job.start(name)
    tasks.add_task(repartition_job.run, name, request.app.state.sources)
    return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)


@router.post("/admin/collect/{name}/partition/run")
async def admin_collect_partition_run(name: str, request: Request):
    """**この区画だけを、ふつうの回として 1 回走らせる**(相手も選べる)。

    枠が細いときに「直したコードを、1 区画だけ、空いている相手で試したい」が
    普通に起きる。**ふつうの回として走る**ので、区画に印が付き、進み具合も
    次回の予定も進む —— 割り込み(`/focus`)と分かれるのはここ 1 点で、
    あちらは定時の巡回に影響を出さないのが約束。

    **上書きは 1 回きりで保存しない。** 巡回の設定を直して試すと、直したまま
    定時の回が走り出す(枠が細いときにいちばん避けたい壊れ方で、気づくのは
    枠が尽きてから)。

    **止めてある収集でも走らせる**(「今すぐ実行」と同じ理由。自動実行を止めた
    うえで直したものを試す、がまさにこの口の用途)。
    """
    from app.main import start_collection_bake

    collect.require_enabled()
    form = await request.form()
    keys = [str(k).strip() for k in form.getlist("partition") if str(k).strip()]
    if not keys:
        raise HTTPException(400, {"error": "区画が選ばれていません"})
    # **枠は区画の数だけ減る**(1 区画 = AI 1 回)。枠が細いときに使う口なので、
    # 選びすぎはここで止める —— 押してから気づいても、そのぶんはもう戻らない
    if len(keys) > collect.MAX_TRIAL_PARTITIONS:
        raise HTTPException(400, {
            "error": f"一度に選べるのは {collect.MAX_TRIAL_PARTITIONS} 区画までです"
                     f"(選ばれたのは {len(keys)} 区画)",
            "hint": "区画 1 つにつき AI を 1 回呼びます",
        })
    # **入口でも確かめる。** 開きっぱなしの画面から押されると、割り直しで消えた
    # 鍵を名指しすることがある —— 取り込みを起こしてから気づくより、ここで断る
    # ほうが 1 本ぶん安い(素材を作る側にも同じ確かめがある)。
    # **1 つでも欠けたら走らせない** —— 通ったぶんだけ走ると、押した人には
    # 全部を見たように見えて、抜けた区画だけが黙って飛ばされる
    item = collect.get(name)
    here = {p["key"] for p in item.partitions}
    if missing := [k for k in keys if k not in here]:
        raise HTTPException(409, {
            "error": f"区画「{missing[0]}」は台帳にありません",
            "hint": "割り直しで鍵が変わったかもしれません(区画の面から選び直してください)",
        })
    # **機械で引く回・外の道具で引く回では走らせない。** あれは区画を見ないので、
    # 名指ししても全件の回が走る —— 押した人からは「1 区画だけ」のつもりなのに、
    # 名簿を丸ごと作り直す回が動く(本番でそうなった)。画面から外すだけにしない
    named_sweep = str(form.get("sweep") or "")
    this = collect.sweep_named(item, named_sweep or None)
    if this.use_extract or this.use_feed:
        raise HTTPException(400, {
            "error": f"巡回「{this.name}」は区画を見ません(機械で引く回・"
                     f"外の道具で引く回)",
            "hint": "区画を見る巡回を選んでください",
        })
    if this.by_hand:
        # **手で回す回は、ここからは走らせない。** 走らせる中身(人が持ち帰った
        # 答え)がまだ無いので、押しても空振りする —— 束を作る口のほうへ案内する
        raise HTTPException(400, {
            "error": f"巡回「{this.name}」は手で回す回です",
            "hint": "「束を作る」で依頼文を書き出し、答えのファイルを読み込ませてください",
        })
    chosen = str(form.get("backend") or "")
    start_collection_bake(name, named_sweep or None, {
        "partitions": keys,
        # **相手とワーカーは同じ欄で選ぶ**(画面の流儀。両方選べると、
        # どちらが効くのか読めなくなる)
        "worker": workers.ref_in(chosen),
        "backend": "" if workers.ref_in(chosen) else chosen,
        "model": str(form.get("model") or ""),
    })
    # **状況の面へ連れていく**(「今すぐ実行」と同じ)。押した人が次に見たいのは
    # この回の進み具合で、区画の中身が動くのは焼き上がってから
    return RedirectResponse(url=STATUS_JOB, status_code=303)


@router.post("/admin/collect/{name}/restart")
async def admin_collect_restart(name: str, request: Request):
    """その巡回の**一周をやり直す**(区画の印を全部外す)。

    **最後の 1 回を戻す口とは別に要る。** 母集団が入れ替わったあとは、どの区画の
    「見た」も当てにならない —— 名簿を作り直した回は中身が動いた区画の印を自分で
    外すが(`partition.cleared_where_changed`)、**それ以外の理由で外したいとき**
    (依頼文を大きく直した・分類の付け方を変えた)に押す口がどこにも無かった。

    **走らせない。** 印を外すだけなので、次の予定でそのまま先頭から回り直す ——
    ここで走らせると、直したい設定を入れる前に 1 回消費してしまう。
    """
    form = await request.form()
    collect.restart_cycle(name, str(form.get("sweep") or ""))
    return RedirectResponse(url=collect_page(collect.get(name)), status_code=303)


@router.post("/admin/collect/{name}/focus")
async def admin_collect_focus(name: str, request: Request):
    """この部分を集中的に直させる(管理画面の「いま直させる」)。

    **定時の巡回には影響しない** —— 進み具合も、どの巡回の予定も、区画の巡回記録も
    動かさない。**止めている収集もここからは走らせる**(`run` と同じ理由)。
    """
    from app.main import start_focus_bake

    form = await request.form()
    titles = [t.strip() for t in str(form.get("titles") or "").splitlines() if t.strip()]
    start_focus_bake(name, {
        "note": str(form.get("note") or ""),
        "titles": titles,
        "partition": str(form.get("partition") or ""),
        "requested_by": "管理画面",
    })
    return RedirectResponse(url=STATUS_JOB, status_code=303)


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
    return _spec_json(item.extract)


def _feed_json(item) -> str:
    """外向きの道具の指定を、編集できる文字列にする。持っていなければ空。"""
    return _spec_json(item.feed)


def _partition_json(item) -> str:
    """区画の指定を、編集できる文字列にする。持っていなければ空。"""
    return _spec_json(item.partition)


def _spec_json(spec) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2) if spec else ""


def _parse_extract(raw):
    return _parse_spec(raw, "抽出")


def _parse_partition(raw):
    return _parse_spec(raw, "区画")


def _parse_feed(raw):
    return _parse_spec(raw, "フィード")


def _parse_verify_tags(raw):
    """タグの確かめ方の欄。**空なら空の配列**(「確かめない」を送れるようにする)。"""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except ValueError as e:
        raise HTTPException(400, {"error": f"タグの確かめ方が JSON として読めません: {e}"}) from None
    return collect.normalize_verify_tags(value)


def _parse_sweeps_form(form) -> list[dict]:
    """繰り返しの欄から巡回を組み立てる。**名前の無い枠は読まない**。

    足す口も消す口も名前 1 つで済ませている —— 書けば増え、消せば減る。
    行ごとにボタンを付けると、押した先で何が起きるかを別に説明することになる。
    """
    names = form.getlist("sweep_name")
    fields = {
        key: form.getlist(f"sweep_{key}")
        for key in (
            "interval", "cover_days", "per_run", "backend", "model", "effort",
            "enabled", "clock", "merge", "prompt", "source", "worker",
        )
    }
    out = []
    for index, raw_name in enumerate(names):
        name = str(raw_name or "").strip()
        if not name:
            continue

        def at(key, i=index):
            values = fields[key]
            return str(values[i]).strip() if i < len(values) else ""

        # **ワーカーは相手の欄から取り出す**(`workers.OPTION_PREFIX`)。
        # **空でも書く** —— 「使わない」に戻せないと、一度名指ししたら外せなくなる
        # (このフォームだけが持つ欄なので、他から消される心配は無い)
        sweep = {
            "name": name,
            "enabled": bool(at("enabled")),
            "worker": workers.ref_in(at("backend")),
        }
        # **時計を持たない巡回**(割り込み用)。定時には走らず、頼まれたときだけ動く。
        # 名指しされたときだけにする —— 欄を持たないフォームから保存されたときに、
        # 全部の巡回が黙って時計を失うのを避ける
        if at("clock") == "on_demand":
            sweep["on_demand"] = True
        # **足すだけ**も名指しのときだけ(欄を持たないフォームから集め方が変わらないように)
        if at("merge") == "only_new":
            sweep["only_new"] = True
        # **空欄は「収集のものを使う」**(巡回ごとに全部書かせない)
        if prompt := at("prompt"):
            sweep["prompt"] = prompt
        # **機械で引く**も名指しのときだけ(欄を持たないフォームから引き方が変わらないように)
        if at("source") == "extract":
            sweep["use_extract"] = True
        if at("source") == "feed":
            sweep["use_feed"] = True
        if at("source") == "hand":
            sweep["by_hand"] = True
        if at("interval").isdigit():
            sweep["interval_minutes"] = int(at("interval"))
        for key, field in (("cover_days", "cover_days"), ("partitions_per_run", "per_run")):
            if value := at(field):
                with suppress(ValueError):
                    sweep[key] = float(value) if key == "cover_days" else int(value)
        # **機械で引く回に相手は要らない。** 残すと、効かない設定が控えに残り、
        # 後から読む人には「この回は AI で走っている」と見える。
        # **ワーカーに頼む回も同じ** —— どの相手に渡るかはそのときの枠で決まるので、
        # ここに 1 つ書いても、どの相手に対する指定なのかが決まらない
        # (モデルの名前は相手ごとに違う)。段ごとの指定はワーカーの側が持つ
        for key in ("backend", "model", "effort"):
            if (sweep.get("use_extract") or sweep.get("use_feed")
                    or sweep.get("by_hand") or sweep["worker"]):
                break
            if value := at(key):
                sweep[key] = value
        out.append(sweep)
    return out


def _parse_spec(raw, label: str):
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
        raise HTTPException(400, {"error": f"{label}の指定が JSON として読めません: {e}"}) from None
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
    """**焼かずに**1 回集めさせて、前世代との差分を見せる(管理画面の「ドライラン」)。

    整理(作り直し)を育てるための道具。何が増えて・何が残って・何が消えるかを
    見てから焼けないと、プロンプトの直しようがない(件数と勘で調整することになる)。

    **巡回ごとに押せる** —— 相手も 1 回に見る量も巡回ごとに違うので、
    「じっくりで聞いたらどうなるか」は名指しできないと試せない。

    **止めている収集もここからは見られる**(「今すぐ実行」と同じ判断)。
    REST の `/v1/collect/{name}/preview` は止まっているものを断る。

    **この画面も待たせる**(AI の応答ぶん)。管理画面は JS を持たないので、
    待っている間の見せ方は作れない。
    """
    from app.main import collect_preview

    form = await request.form()
    sweep = str(form.get("sweep") or "") or None
    try:
        result = await collect_preview(name, request.app.state.sources, sweep)
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
<p class="muted">{'今あるものを直す回です' if result and result.get("edits") else '足すだけの回です'}</p>
<p><strong>まだ焼いていません。</strong>長期記憶は変わっておらず、進み具合も次回の予定も
動いていません。この数字を見てから「今すぐ実行」を押します。</p>
{body}
<p class="muted"><a href="/admin/collect/{esc(quote(name))}">「{esc(name)}」へ戻る</a></p>
""",
    )


@router.get("/admin/collect/{name}", response_class=HTMLResponse)
def admin_collect_detail(
    request: Request,
    name: str,
    sweep: str | None = Query(None, description="直近の変更を、この回のぶんだけに絞る"),
):
    """収集 1 つぶんの面。**一覧から名前を押すとここへ来る**。

    **一覧の中で開かない。** 折り畳みの中に押し込んでいた頃は、開くたびに表が縦へ
    伸びて他の収集の行が画面外へ出ていた —— 読みに来た人はその収集だけを見に来て
    いるので、専用の面に置けば畳む理由が無い。

    **区画は省かずに全部出す。** 一覧では頭の数件で足りるが(どう割れたかが分かれば
    よい)、ここは「どこを見ていて、どこがまだか」を読みに来る面なので、
    途中で切ると読めない。
    """
    collect.require_enabled()
    item = collect.get(name)
    src = request.app.state.sources.get(name)
    job = _fetch_trigger_status()
    disabled = run_buttons_disabled(job)
    baked = (
        f'<a href="{esc(browse_url(name))}">{src.doc_count:,} 件</a>'
        if src is not None else '<span class="muted">まだ焼いていない</span>'
    )
    body = f"""
{nav_html("/admin/memory")}
<h1>{esc(name)}</h1>
<p class="muted">{esc(item.description)}
{'<br>依頼元: ' + esc(item.requested_by) if item.requested_by else ''}</p>
<p>種類: {esc(KIND_LABELS.get(item.kind, item.kind))}
{'／ ' + str(item.keep_days) + ' 日ぶんを持つ' if item.keep_days else ''}
<br>長期記憶: {baked}
/ 状態: {'有効' if item.enabled else '<span class="stale">止まっている</span>'}
{'<br>' + _rollback_cell(src, f'/admin/collect/{quote(name)}') if src is not None else ''}</p>
<table>
<thead>
<tr><th>巡回</th><th>頼む相手</th><th>間隔</th>
<th>前回</th><th>一周のうち</th><th>次にいつ</th><th>実行</th></tr>
</thead>
<tbody>
{_sweep_table_body(item, disabled)}
</tbody>
</table>
{_collect_detail_html(item, disabled, request.app.state.sources, _baking_now(job, name))}
{_collect_running_html(name)}
{_collect_changes_html(name=name, sweep=sweep)}
{_changed_here_html(name, request.app.state.sources, sweep)}
<p class="muted"><a href="/admin/collect">集める の一覧へ戻る</a></p>
"""
    return HTMLResponse(content=page_shell(name, body))


@router.get("/admin/collect/{name}/partition", response_class=HTMLResponse)
def admin_collect_partition(
    request: Request, name: str, key: str = Query(..., description="区画の鍵"),
):
    """**その区画に入っているものを全部**並べる。台帳の区画名から来る。

    区画は「この範囲の全員」を並べて漏れを問う単位なので、鍵と数だけでは
    割り方が合っているか人には確かめようがない —— 合っているかどうかは、
    並んだ顔ぶれを見て初めて分かる(年代の分からない人がどこへ落ちているか、
    分類の取り違えがどこに混ざっているか)。

    **切らずに全部出す。** 1 区画は `target` の数倍で頭打ちになるので短く、
    途中で切ると「この範囲の全員」を確かめるというこの面の用が足りない。

    **消えたものも出す**(`/v1/collect/{name}/partition` と同じ約束)。
    何を外したのかが見えないと、割り方の判断にも消し間違いの発見にも使えない。
    """
    collect.require_enabled()
    item = collect.get(name)
    sources = request.app.state.sources
    if not item.partition:
        body = '<p class="muted">この収集は区画を持っていません。</p>'
    else:
        members = collect.partition_docs(item, sources, key)
        body = (
            _seen_when_html(item, key)
            + _try_here_html(item, key, run_buttons_disabled(_fetch_trigger_status()))
            + _partition_members_html(name, item, key, members, sources)
        )
    return HTMLResponse(content=page_shell(
        f"{key} / {name}",
        f"""
{nav_html("/admin/memory")}
<h1>{esc(name)} の区画</h1>
<p class="muted">{esc(key)}</p>
{body}
<p class="muted"><a href="/admin/collect/{esc(quote(name))}">{esc(name)} へ戻る</a></p>
""",
    ))


def _sweeps_for_trial(item):
    """1 区画だけ走らせるのに選べる巡回。

    **機械で引く回・外の道具で引く回は出さない。** あれは指定を 1 本引いて全部を
    返す回で、**区画を見ない**(`_collect_material`)—— 並べると、区画を名指し
    したのに全件の回が走る。**しかも先頭が既定で選ばれる**ので、何も選ばずに
    押しただけでそれが起きた(本番で、名簿を作り直す回が丸ごと走った)。
    「選べるのに効かない欄は、設定したつもりを作る」の類い。
    """
    return [
        s for s in collect.sweeps_of(item)
        if not s.on_demand and not s.use_extract and not s.use_feed and not s.by_hand
    ]


def _trial_fields(item, disabled: str, button: str) -> str:
    """試し撃ちの欄(巡回と相手)とボタン。**表と 1 件ずつの面で同じものを使う。**"""
    choices = "".join(
        f'<option value="{esc(s.name)}">{esc(s.name)}</option>'
        for s in _sweeps_for_trial(item)
    )
    return (
        f'<p><label>どの巡回として<br><select name="sweep">{choices}'
        f"</select></label></p>"
        f'<p><label>頼む相手(空なら巡回の設定のまま)<br>'
        f'{_backend_select("", "backend", with_workers=True, empty_label="巡回の設定のまま")}'
        f"</label></p>"
        f'<p><label>モデル<br>{_model_select(None, None, "model")}</label></p>'
        f'<p class="muted"><strong>ふつうの回として走ります</strong> ——'
        f"選んだ区画に「見た」印が付き、進み具合も次回の予定も進みます。"
        f"<br><strong>AI の呼び出しは選んだ区画の数だけ</strong>です"
        f"(一度に選べるのは {collect.MAX_TRIAL_PARTITIONS} 区画まで)。"
        f"<br><strong>焼きます</strong>(ドライランではありません)。"
        f"<br><strong>相手の指定は保存しません</strong> —— "
        f"巡回の設定は書き換わらないので、次の定時の回は今までどおりの相手で走ります。"
        f"ワーカーを選べば、空いている相手はそちらが選びます。</p>"
        f'<button type="submit"{disabled}>{button}</button>'
    )


def _run_here_html(item, table: str, busy: bool) -> str:
    """**選んだ区画を、ふつうの回として 1 回走らせる**(台帳の表ごとフォームに包む)。

    **複数選べる。** 1 件ずつしか走らせられなかった頃は、直したところを何区画か
    まとめて確かめたいときに、区画の面を開き直して 1 回ずつ押すことになった。

    **表をフォームの中に入れる**ので、行の頭のチェックがそのまま送られる ——
    畳んである残りの区画(`<details>`)も同じフォームの中なので、開いて選べる。

    **区画を見る巡回が 1 つも無ければ出さない**(選んでも走らせる先が無い)。
    """
    if not _sweeps_for_trial(item):
        return table
    off = " disabled" if busy else ""
    return (
        f'<form method="post" action="/admin/collect/{esc(quote(item.name))}/partition/run"'
        f' class="collect-form"{off}>{table}'
        f"<details><summary>選んだ区画を 1 回走らせる</summary>"
        f'{_trial_fields(item, off, "選んだ区画で 1 回走らせる")}'
        f"</details></form>"
    )


def _try_here_html(item, key: str, disabled: str) -> str:
    """区画の面から、**その 1 区画だけ**を走らせる口。中身は台帳の表のものと同じ。

    **ここにも置く。** 中身を見て「この区画で試そう」と決める場所がここなので、
    台帳へ戻って選び直させない。
    """
    if not _sweeps_for_trial(item):
        return (
            '<p class="muted">この収集には、区画を見る巡回がありません'
            "(機械で引く回・外の道具で引く回は区画を見ないので、1 区画だけ"
            "走らせることができません)。</p>"
        )
    return (
        f"<details><summary>この区画だけ 1 回走らせる</summary>"
        f'<form method="post" action="/admin/collect/{esc(quote(item.name))}/partition/run"'
        f' class="collect-form"{disabled}>'
        f'<input type="hidden" name="partition" value="{esc(key)}">'
        f'{_trial_fields(item, disabled, "この区画で 1 回走らせる")}'
        f"</form></details>"
    )


def _seen_when_html(item, key: str) -> str:
    """この区画を、どの巡回がいつ見終えたか。**開いた人がまず知りたいのはここ**。

    台帳の表にも出ているが、あちらは何千行の中の 1 行 —— この面は 1 つの区画を
    確かめに来る場所なので、中身の上に置く。
    """
    found = next((p for p in item.partitions if p["key"] == key), None)
    if found is None:
        return '<p class="muted">この区画は、いまの台帳にありません(割り直しで消えた)。</p>'
    visits = found.get("visits") or {}
    if not visits:
        return '<p class="muted">まだどの巡回も見ていません。</p>'
    seen = "、".join(
        f"{esc(sweep)} {esc(jst.compact(at) if (at := jst.parse(str(raw))) else str(raw))}"
        for sweep, raw in sorted(visits.items())
    )
    return f'<p class="muted">見終えた巡回(日本時間): {seen}</p>'


def _partition_members_html(
    name: str, item, key: str, members: dict[str, dict], sources: dict,
) -> str:
    """区画に入っているものの表。**見出しからいまの中身へ飛べるようにする**。"""
    where = collect.describe_partition(item, key, sources)
    if not members:
        return (
            f'<p class="muted">{esc(where)}</p>'
            "<p>この区画には、いま 1 件も入っていません"
            "(まだ集めていないか、中身が別の区画へ移ったかのどちらか)。</p>"
        )
    rows = "".join(
        f"<tr><td>{_doc_title_html(name, doc)}</td>"
        f"<td>{esc('、'.join(str(t) for t in (doc.get('tags') or [])))}</td>"
        f"<td>{esc((doc.get('body') or '')[:120])}</td></tr>"
        for doc in sorted(members.values(), key=lambda d: str(d.get("title") or ""))
    )
    # **消えたものは数から外して、別に数える。** 台帳の件数は生きているものだけを
    # 数えているので(`collect.living`)、ここで合わせて出さないと表の行数と食い違う
    gone = sum(1 for doc in members.values() if collect.is_removed(doc))
    count = (
        f"{len(members) - gone:,} 件"
        + (f'<span class="muted">(ほかに消えたもの {gone:,} 件)</span>' if gone else "")
    )
    return (
        f'<p class="muted">{esc(where)}</p>'
        f"<p>{count}</p>"
        "<table><thead><tr><th>見出し</th><th>タグ</th><th>中身</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _doc_title_html(name: str, doc: dict) -> str:
    """見出し。焼けているなら、いまの中身への入口にする。

    **消えたものはそう見えるように書く** —— 並べておいて印が無いと、外したはずの
    ものを「まだ居る」と読んでしまう。
    """
    title = str(doc.get("title") or "")
    doc_id = doc.get("doc_id")
    shown = (
        f'<a href="/search/{esc(quote(name))}/doc/{doc_id}">{esc(title)}</a>'
        if doc_id is not None else esc(title)
    )
    if notes.REMOVED_TAG in (doc.get("tags") or []):
        why = collect.removed_reason(doc)
        shown += ' <span class="muted">(消えたもの' + (f": {esc(why)}" if why else "") + ")</span>"
    return shown


@router.get("/admin/collect/{name}/doc", response_class=HTMLResponse)
def admin_collect_doc(request: Request, name: str, title: str = Query(..., description="見出し")):
    """動いた 1 件が、**どう書き換わったか**を出す。

    「直近の変更」に並ぶのは見出しの名前までで、そこからは何が変わったのか読めない ——
    件数と名前が分かっても、プロンプトを直す判断に要るのは「どう書き換わったか」のほう。

    **比べられるのは 1 つ前の世代まで**(ブルーグリーンが残すのがそこまで)。
    古い回の行から来ても出せるのは最新の焼き直しのぶんなので、**どの世代どうしを
    比べたかを画面に書く** —— 書かないと、その回の変更として読まれる。
    """
    versions = collect.doc_versions(name, request.app.state.sources, title)
    return HTMLResponse(_doc_diff_page_html(name, title, versions))


def _doc_diff_page_html(name: str, title: str, versions: dict) -> str:
    now, before, kept = versions["now"], versions["before"], versions.get("kept") or {}
    if kept and now is not None:
        # **1 件が持っている控えと比べる。** 世代の比較は焼き直すたびに相手が
        # 入れ替わるので、直した回を見に来た頃には空になる。
        # 控えに入っているのは動いたところだけなので、残りはいまの中身で埋める
        before = {**now, **kept}
    if now is None and before is None:
        body = (
            '<p class="muted">この見出しは、いまの世代にも 1 つ前の世代にもありません。'
            "比べられるのは 1 つ前の世代までなので、それより古い回に動いたものは"
            "追えません。</p>"
        )
    else:
        body = (
            f"<p>{_what_happened(now, before, kept)}</p>"
            f"{_tag_diff_html(now, before)}"
            f"{_extra_diff_html(now, before)}"
            f"{_body_diff_html(now, before)}"
            f"{_no_record_html(kept, now)}"
        )
    return page_shell(
        f"{title} の変更",
        f"""
<h3>{esc(title)}</h3>
<p class="muted">収集「{esc(name)}」 / {_generations_html(versions)}</p>
{body}
<p class="muted">{_doc_now_link(name, now)}<a href="/admin/collect">管理画面へ戻る</a></p>
""",
    )


def _doc_now_link(name: str, now: dict | None) -> str:
    """**いま入っている中身への入口**。差分はタグと本文だけを切り出したものなので、
    出典も extra も、同じタグの他の文書への導線もここには出ない。

    **消えたものには出さない**(いまの世代に無いので、指す先が無い)。
    """
    doc_id = (now or {}).get("doc_id")
    if doc_id is None:
        return ""
    return (
        f'<a href="/search/{esc(quote(name))}/doc/{doc_id}">いまの中身を見る</a> / '
    )


def _generations_html(versions: dict) -> str:
    """**何と何を比べたかを必ず出す。** 出所が 2 つある —— 1 件が持っている控えと、
    世代どうし。書かないと、古い回の行から来た人が最新の焼き直しの差分を
    その回の変更として読む。
    """
    if versions.get("kept"):
        who = collect.changed_by(versions.get("now") or {})
        return (
            "この 1 件が控えている" + (f"「{esc(who)}」が" if who else "")
            + "直す前の中身との比較(控えるのは 1 回分だけなので、"
            "次に同じ 1 件が動くと入れ替わります)"
        )
    now = _generation_label(versions["now_stamp"])
    before = _generation_label(versions["before_stamp"])
    if not before:
        return f"いまの世代({now})だけ。1 つ前の世代はまだありません"
    return f"1 つ前({before}) → いま({now})の比較"


def _generation_label(stamp: str) -> str:
    """世代の日付(`YYYYMMDDHHMMSS`)を読める形に。読めなければそのまま。"""
    raw = (stamp or "").strip()
    if len(raw) == 14 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return esc(raw)


def _what_happened(now: dict | None, before: dict | None, kept: dict | None = None) -> str:
    if kept:
        # **世代どうしが同じでも、直した事実は 1 件の側に残っている**
        who = collect.changed_by(now or {})
        return f"<strong>{esc(who) if who else '直近の回'}が書き換えたもの</strong>"
    if before is None:
        return "<strong>足されたもの</strong>(1 つ前の世代にはありませんでした)"
    if now is None:
        return '<span class="stale"><strong>消えたもの</strong></span>(いまの世代にはありません)'
    if (now["body"], sorted(now["tags"])) == (before["body"], sorted(before["tags"])):
        return ("<strong>中身は変わっていません</strong>(直した回に名前が挙がっていても、"
                "本文もタグも同じなら書き換わっていない)")
    return "<strong>書き換わったもの</strong>"


def _tag_diff_html(now: dict | None, before: dict | None) -> str:
    """タグの出入り。**本文と分けて出す** —— この層のタグは図の線や分類そのもので、
    本文の差分に紛れると、何が増えて何が落ちたのか読み取れない。
    """
    after = set((now or {}).get("tags") or [])
    prior = set((before or {}).get("tags") or [])
    added = sorted(after - prior)
    gone = sorted(prior - after)
    if not added and not gone:
        return ""
    parts = []
    if added:
        parts.append("<strong>足したタグ</strong>: " + esc("、".join(added)))
    if gone:
        parts.append('<span class="stale"><strong>外したタグ</strong>: '
                     + esc("、".join(gone)) + "</span>")
    return "<p>" + "<br>".join(parts) + "</p>"


def _body_diff_html(now: dict | None, before: dict | None) -> str:
    """本文の差分。**行単位の差分にする** —— 全文を 2 つ並べると、長い本文では
    どこが動いたのか目で探すことになる(この層の本文は数百字ある)。

    **片側しか無いときは、そちらを丸ごと出す**(足された / 消えた、がそれ)。
    """
    after = (now or {}).get("body") or ""
    prior = (before or {}).get("body") or ""
    if not prior or not after:
        text = after or prior
        return f'<pre class="prompt-view">{esc(text)}</pre>' if text else ""
    lines = list(difflib.unified_diff(
        prior.splitlines(), after.splitlines(), lineterm="", n=2,
    ))
    # 先頭 2 行(`---` / `+++`)はファイル名の欄で、ここでは意味を持たない
    body = "\n".join(lines[2:])
    if not body.strip():
        return ""
    return f'<pre class="doc-diff">{_colour_diff(body)}</pre>'


def _extra_diff_html(now: dict | None, before: dict | None) -> str:
    """脇書きの出入り。**本文ともタグとも分けて出す** —— ここに入るのは出典・
    配信日・座標といった、本文を読んでも分からない事実。

    **こちらが押す印は外す**(`collect.facts_of`)。回ごとに必ず書き換わるので、
    混ぜると毎回「脇書きが変わった」になり、本当に動いた事実が埋もれる。
    """
    after = collect.facts_of(now or {})
    prior = collect.facts_of(before or {})
    rows = []
    for key in sorted(set(after) | set(prior)):
        was, is_now = prior.get(key), after.get(key)
        if was == is_now:
            continue
        if key not in prior:
            rows.append(f"<strong>{esc(key)}</strong>: " + esc(_short(is_now)))
        elif key not in after:
            rows.append(
                f'<span class="stale"><strong>{esc(key)}</strong>: '
                + esc(_short(was)) + " (落ちた)</span>"
            )
        else:
            rows.append(
                f"<strong>{esc(key)}</strong>: "
                + esc(_short(was)) + " → " + esc(_short(is_now))
            )
    if not rows:
        return ""
    return "<p>脇書きの変化<br>" + "<br>".join(rows) + "</p>"


def _short(value) -> str:
    """脇書きの値を 1 行に。**長いものは切る**(ここは変化を読む欄で、全文は
    いまの中身の側にある)。"""
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", " ")
    return text if len(text) <= EXTRA_VALUE_CHARS else text[:EXTRA_VALUE_CHARS] + "…"


def _no_record_html(kept: dict, now: dict | None) -> str:
    """控えを持たない 1 件に、**いま出ているのが世代どうしの比較だ**と書く。

    持っている 1 件には書かない —— 何と比べたかは見出しの下に出ている。
    """
    if kept or now is None or not collect.changed_by(now):
        return ""
    return (
        '<p class="muted">この 1 件は直す前の中身を控えていません'
        "(控えを持つ前に焼かれたもの)。上に出ているのは世代どうしの比較です。</p>"
    )


def _colour_diff(text: str) -> str:
    """差分に色を付ける。**先にエスケープしてから印を置く** —— 順番を逆にすると、
    本文にタグを書かれた時点で入り込む(会話画面の Markdown と同じ順番の話)。
    """
    out = []
    for line in esc(text).splitlines():
        if line.startswith("+"):
            out.append(f'<span class="added">{line}</span>')
        elif line.startswith("-"):
            out.append(f'<span class="removed">{line}</span>')
        elif line.startswith("@@"):
            out.append(f'<span class="muted">{line}</span>')
        else:
            out.append(line)
    return "\n".join(out)


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


