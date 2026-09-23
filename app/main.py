"""chiezo-app の REST(設計書 §5)。

このモジュールが持つのは機械向けの口(`/v1/...`)とアプリの組み立て
(lifespan・例外ハンドラ・画面 router の登録・`/mcp` のマウント)。
人間向けの HTML は `app/views/`、両者が共有する下ごしらえは `app/deps.py`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel
from pydantic import Field as PydField
from starlette.concurrency import run_in_threadpool

from app import (
    agent,
    ai_inflight,
    ai_log,
    answer,
    capabilities,
    claude_config,
    collect,
    collect_log,
    db,
    extract,
    feeds,
    logs,
    machine_store,
    media,
    media_backends,
    media_providers,
    notes,
    providers,
    usage,
    usage_store,
    websearch,
    workers,
)
from app import partition as partitioning
from app.deps import (
    exact_title_first,
    get_source,
    relevance_order,
    require_attributes,
    require_filter_schema,
    require_recency_index,
    require_tag_schema,
)
from app.fts import build_match_query, escape_like
from app.mcp_server import build_mcp, build_mcp_app
from app.pages import APP_ICON_SVG, APP_MANIFEST, APPLE_TOUCH_ICON_PNG
from app.registry import (
    COORDS_MIN_SCHEMA_VERSION,
    FILTER_MIN_SCHEMA_VERSION,
    RANK_INDEX_MIN_SCHEMA_VERSION,
    TAG_COUNTS_MIN_SCHEMA_VERSION,
    TAG_MIN_SCHEMA_VERSION,
    Source,
    data_dir_fingerprint,
    scan_source,
    scan_sources,
)
from app.views import admin as views_admin
from app.views import ai_settings as views_ai_settings
from app.views import ai_usage as views_ai_usage
from app.views import ai_workers as views_ai_workers
from app.views import browse as views_browse
from app.views import chat as views_chat
from app.views import media_ask as views_media_ask
from app.views import media_compare as views_media_compare
from app.views import todo as views_todo

# **控えを出せるようにしてから他を読む。** uvicorn は自分のロガーしか設定しないので、
# ここで手を入れないと `chiezo.*` の INFO は 1 行も出ない(`app/logs.py`)
logs.setup()

log = logging.getLogger("chiezo.app")

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

# 新しい順に引く口。溜まっていくソース(集めたもの)を「この 1 日で何が入ったか」で
# 読むためのもの。**既定に本文を入れない** —— 一覧として読むので、まず見出しと
# 冒頭が要る(全文は doc で取り直す)
RECENT_DEFAULT_FIELDS = ["doc_id", "title", "opening", "tags", "updated_at", "extra"]
RECENT_LIMIT_DEFAULT = 20
RECENT_LIMIT_MAX = 200


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
    # 設定の置き場(機械が書き換えるもの)も普通のソースとして出す。
    # **人が触らない置き場だが、見えないままでは確かめようがない** —— 収集が
    # 消えた・戻ってきたのような話を追うとき、まず知りたいのは「設定がどこに、
    # いつの姿で残っているか」。**ファイルを名指しで足す**(ディレクトリを舐めると、
    # ソースではない控えの DB のぶんだけ警告が出る)
    machine_path = machine_store.ensure_db()
    if machine_path is not None:
        machine_src = scan_source(machine_path, mutable=True)
        if machine_src is not None:
            sources[machine_src.name] = machine_src
    # 収集した中身は `corpus/` 側に焼かれるので、ここで足すものは無い
    # (焼く前の置き場も持たない。`app/collect.py` 参照)
    # 追記される DB は immutable で開けない、と db 側に伝える
    db.set_mutable_paths(s.path for s in sources.values() if s.mutable)
    return sources


def refresh_sources(app: FastAPI) -> bool:
    """/data の指紋が前回と変わっていればソースを再走査して差し替える(変わったら True)。

    ingest のブルーグリーン切り替え(世代ファイルへのリネーム + シンボリックリンク差し替え)や
    別マシンで焼いた DB のコピーを、app の再起動なしで反映するための入口。指紋を先に取って
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


# 収集の時計を回す間隔(秒)。**予定そのものは各収集が `next_run_at` で持つ**ので、
# ここは「見に来る頻度」でしかない。細かくしても AI の呼び出しは増えない
COLLECT_TICK_SECONDS = float(os.environ.get("CHIEZO_COLLECT_TICK_SECONDS", "60"))


# 枠を定時に聞きに行く間隔(分)。0 で止まる。**15 分は「跳ねた時刻を当てられる細かさ」
# と「CLI を起こす回数」の折り合い** —— 枠を聞くだけでもモデルを呼ばない CLI が 1 本立つ。
QUOTA_SAMPLE_MINUTES = answer._env_num("CHIEZO_QUOTA_SAMPLE_MINUTES", 15.0, float)

# 見に来る頻度。取り合いと間隔は下の `_sample_quotas` が見るので、ここを細かくしても
# 聞きに行く回数は増えない(`COLLECT_TICK_SECONDS` と同じ考え方)。
QUOTA_TICK_SECONDS = float(os.environ.get("CHIEZO_QUOTA_TICK_SECONDS", "60"))


def _worker_of(sweep) -> workers.Worker | None:
    """その巡回が使うワーカー。名指ししていなければ None。

    **名前が見つからないときも None にする**(= 巡回に書いてある相手で走る)。
    綴りを間違えただけで無人の層が止まるより、走って控えに相手が残るほうがよい ——
    どちらで走ったかは変更履歴の相手の欄に出る。**理由はログに残す**。
    """
    name = getattr(sweep, "worker", "")
    if not name:
        return None
    try:
        found = workers.get(name)
    except ValueError:
        log.warning("worker definitions unreadable; falling back to the sweep's own backend")
        return None
    if found is None or not found.steps:
        log.warning("worker %r not found (or empty); falling back to the sweep's own backend", name)
        return None
    return found


async def _sample_quotas() -> None:
    """枠の推移を控える常駐タスク(`app/usage_store.py` の `quota_samples`)。

    **押したときだけ聞きに行く作りでは、推移が残らない。** 控えは「いまどうか」で
    上書きされるので、無人で回る層が枠を食っても、跳ねた時刻も 1 ポイントぶんの
    重さも後から読めなかった —— 2 点無いと差が取れない。

    **呼んでいない相手には聞きに行かない**(`calls_since`)。前と同じ値が返るだけで、
    CLI を 1 本起こすぶんだけ損をする。**ただし 1 点目だけは呼ばれていなくても採る**
    —— 起点が無いと、次に呼んだぶんの差が取れない。

    **番は取り合う**(`claim_quota_poll`)。`--workers 2` なので同じ周期で両方が
    起きる —— 取れたほうだけが聞きに行く。

    **失敗しても止めない**(理由は控えに残る)。止めると、一度こけた相手の推移が
    二度と伸びない。
    """
    interval = timedelta(minutes=QUOTA_SAMPLE_MINUTES)
    while True:
        await asyncio.sleep(QUOTA_TICK_SECONDS)
        try:
            for provider_id in await asyncio.to_thread(usage.refreshable):
                last = await asyncio.to_thread(usage_store.last_quota_sample_at, provider_id)
                if last and not await asyncio.to_thread(
                    usage_store.calls_since, provider_id, last
                ):
                    continue
                if not await asyncio.to_thread(
                    usage_store.claim_quota_poll, provider_id, interval
                ):
                    continue
                await usage.refresh(provider_id)
        except Exception:
            log.exception("quota sampling tick failed")


async def _run_collections(app: FastAPI) -> None:
    """予定の来た収集を ingest に起こさせる常駐タスク(`app/collect.py`)。

    **叩くのは chiezo-trigger**。集めるのも焼くのも取り込みの中で起きる(素材を配るとき
    に AI へ聞く)ので、時計がすることは「取り込みを 1 本始める」だけになる。

    **1 周に 1 件ずつ**にしてある。trigger は同時に 1 ジョブしか受けないうえ、
    CLI ブリッジ越しの相手も同時に 1 本しか動かない。**起こすのは(収集, 巡回)の組**
    —— 同じ収集に「ざっと」と「じっくり」が別の時計で載っているため。

    **混んでいれば次の周期へ回す**(429/409)。予定は進めないので、空いたときに走る。
    **失敗しても止めない** —— 止めると、一度こけた収集が二度と走らなくなる。
    """
    while True:
        await asyncio.sleep(COLLECT_TICK_SECONDS)
        try:
            # **焼くところで落ちた回を、いちばん先に戻す。** 戻す前に次を起こすと、
            # 進んだままの印で次の区画が選ばれる
            await asyncio.to_thread(_rewind_failed_bakes)
            await asyncio.to_thread(_fill_worker_queues)
            # **ワーカーの順番が先。** 拾った塊を流し切るまでそのワーカーに優先権を
            # 持たせる —— 途中で他の回に割り込まれると「1 度の起動で N 本」が
            # 意味を失う。塊が無ければ、いつもの予定の回へ落ちる
            if await asyncio.to_thread(_run_one_from_a_worker):
                continue
            due = await asyncio.to_thread(collect.due_sweeps)
            # **ワーカーに任せた回はここでは走らせない**(行列から流す)
            due = [pair for pair in due if not getattr(pair[1], "worker", "")]
            if not due:
                continue
            item, sweep = due[0]
            await asyncio.to_thread(start_collection_bake, item.name, sweep.name)
        except HTTPException as e:
            # **混んでいるのは失敗ではない。** 取り込みは同時に 1 本しか受けず、
            # 時計はプロセスごとに立っている(`--workers 2`)ので、同じ周に 2 本が
            # 同じ組を起こしにいって片方が必ず断られる —— 控えも予定も進めていない
            # ので、空いた周でそのまま走る。**痕跡ごと出すと、追える失敗が埋まる**
            if e.status_code in (409, 429):
                log.info("collection tick: 取り込みが混んでいるので次の周期へ回します")
            else:
                log.exception("collection tick failed")
        except Exception:
            log.exception("collection tick failed")


def _rewind_failed_bakes() -> None:
    """焼くところで落ちた回を、走る前まで戻す(`collect.rewind_failed_bake`)。

    **集める層は「素材を組んだ」までしか知らない。** 控えを書いてから流し始める
    作りなので(流し始めたらステータスは変えられない)、焼くところで落ちても
    画面には成功しか出ない —— 区画の印もカーソルも進んだままで、その区画は
    一周するまで誰も見に来ない。**枠を 1 回ぶん使って、成果だけが無い。**
    本番で、素材が途中で切れた回がそのまま「見終わった」になった。

    **焼いた側の結果は取り込みの状態にある**ので、こちらから拾いに行く ——
    取り込みは収集の名前しか運べず、終わったことを教えに来る道も無い。

    **落ちた回は 2 つの形で出てくる。** いまの 1 本がそれなら `state` が error、
    次の取り込みが始まっていれば `last_failure` に退く。どちらも見る ——
    片方だけだと、1 分の周期の合間に次が始まった回を取りこぼす。

    **収集でないソースは素通りする**(地図辞典などの取り込み)。
    **ここで落ちても時計は止めない** —— 戻せないことと、次を起こせないことは別。
    """
    from app.views.admin import _fetch_trigger_status

    try:
        job = _fetch_trigger_status() or {}
    except Exception:
        log.exception("取り込みの状態を読めなかった(巻き戻しは次の周期へ)")
        return
    failures = [job] if job.get("state") == "error" else []
    if isinstance(last := job.get("last_failure"), dict):
        failures.append(last)
    # **同じ回は 1 度だけ見る。** 落ちた直後は `state` と `last_failure` が
    # **同じ回**を指す(次の取り込みが始まって初めて片方だけになる)—— 畳まないと、
    # 1 周のあいだに同じものを 2 度たどることになる
    seen_failures: set[tuple[str, str]] = set()
    for failed in failures:
        source = str(failed.get("source") or "")
        started = str(failed.get("started_at") or "")
        finished = str(failed.get("finished_at") or "")
        if not source or not started or not finished:
            continue
        if (source, finished) in seen_failures:
            continue
        seen_failures.add((source, finished))
        try:
            undone = collect.rewind_failed_bake(
                source, str(failed.get("error") or ""), started, finished,
            )
        except HTTPException:
            continue  # 収集ではないソース(あるいは消された収集)
        except Exception:
            log.exception("collect %s: 落ちた回を戻せなかった", source)
            continue
        if not undone:
            continue
        log.warning(
            "collect %s: 焼くところで落ちたので「%s」の 1 回を戻した"
            "(区画 %d、進み具合も戻した)",
            source, undone["sweep"], len(undone["visited"]),
        )
        with suppress(Exception):
            collect_log.record(
                source,
                status=collect_log.STATUS_ERROR,
                error=f"焼くところで落ちました: {failed.get('error') or ''}",
                sweep=undone["sweep"],
                scope=undone["visited"],
            )


def _fill_worker_queues() -> None:
    """ワーカーに任せた巡回を、待ち行列へ積む。

    **積む条件は「まだ居ないこと」と「前回の完了から間隔が空いたこと」の 2 つ。**
    予定(`next_run_at`)では見ない —— あれは起こした時点で進むので、行列で待って
    いるあいだに何度も予定が来る。完了の時刻(`last_run_at`)を起点にすれば、
    **待たされたぶんだけ次が後ろへずれる**(積み上がらない)。

    **失敗した回も完了として数える。** 落ち続ける回がすぐ積み直されると、
    その 1 本が行列を占め続ける —— 間隔を空けてから見直すほうがよい。
    """
    if not collect.is_enabled():
        return
    now = datetime.now(UTC)
    for item in collect.load():
        if not item.enabled:
            continue
        for sweep in collect.sweeps_of(item):
            name = getattr(sweep, "worker", "")
            if not name or not sweep.enabled or sweep.on_demand:
                continue
            if collect.blocked_reason(item, sweep):
                continue
            if not _due_for_queue(sweep, now):
                continue
            if workers.enqueue(name, item.name, sweep.name, _iso(now)):
                log.info("queued %s/%s for worker %r", item.name, sweep.name, name)
    _drop_stale_from_queues()


def _drop_stale_from_queues() -> None:
    """**もう走らせてはいけないものを行列から外す。**

    積む段は止まっている収集を飛ばすが(`_fill_worker_queues`)、**積んだあとに
    止めたぶんは行列に残ったまま流れていた** —— 押した「止める」が効かず、
    1 回ぶんの取り込みと AI の枠を食う。止めたことは行列にも効かないといけない。

    **外すのは流す段ではなく、ここ。** 流す段まで残すと、先頭が止まっている回の
    あいだ**そのワーカーが 1 本も進まない**(先頭しか見ないため)。ここで外して
    おけば、画面の待ち行列も 1 周期のうちに正しくなる。
    """
    try:
        defined = workers.load()
    except ValueError:
        return
    for worker in defined:
        for entry in workers.queued(worker.name):
            if why := _no_longer_due(entry):
                log.info(
                    "dropped %s/%s from worker %r: %s",
                    entry.get("collection"), entry.get("sweep"), worker.name, why,
                )
                workers.done(worker.name, entry["collection"], entry["sweep"])


def _no_longer_due(entry: dict) -> str:
    """行列に残っているが、もう流してはいけない理由。流してよければ空。

    **画面の「今すぐ実行」とは判断が違う。** あちらは人が押す試し撃ちなので、
    止めてある収集でも走らせる(有効にする前に試せる道)—— こちらは無人で回る側で、
    押した覚えのない回が動かないことのほうが大事。
    """
    try:
        item = collect.get(str(entry.get("collection") or ""))
    except HTTPException:
        return "収集がありません"
    if not item.enabled:
        return "収集が止まっています"
    sweep = next(
        (s for s in collect.sweeps_of(item) if s.name == entry.get("sweep")), None
    )
    if sweep is None:
        return "巡回がありません"
    if not sweep.enabled:
        return "巡回が止まっています"
    if not getattr(sweep, "worker", ""):
        return "この巡回はワーカーに任せていません"
    return collect.blocked_reason(item, sweep)


def _due_for_queue(sweep, now: datetime) -> bool:
    """その巡回を積んでよいか。**一度も走っていなければ積む**。"""
    last = _parse_iso(getattr(sweep, "last_run_at", None))
    if last is None:
        return True
    return now - last >= timedelta(minutes=max(sweep.interval_minutes, 1))


def _run_one_from_a_worker() -> bool:
    """行列から 1 本流す。流したら True。

    **流すのは 1 本ずつ。** 取り込みは同時に 1 ジョブしか受けないので、拾った塊は
    周回をまたいで順に消える —— 前の 1 本が終わるまで次は起こらない
    (`trigger_run` が混んでいれば例外になり、次の周でやり直す)。

    **塊が空になったワーカーだけが、次の起動で拾い直す。**
    """
    now = datetime.now(UTC)
    try:
        defined = workers.load()
    except ValueError:
        return False
    # **1 周で流すのは 1 本。** 取り込みは同時に 1 ジョブしか受けないので、
    # 先に流せたワーカーで打ち切る(残りは次の周)
    return any(_flush_one(worker, now) for worker in defined)


def _flush_one(worker, now: datetime, wake: bool = False) -> bool:
    """そのワーカーから 1 本流す。流したら True。

    `wake` は**時計を待たずに起こす**(画面の「今すぐ起こす」)。枠が明いている
    うちに回しておきたい、が普通に起きる —— 次の起動まで待つと、待っているあいだに
    誰かが枠を食う。**起こした時刻は普通に控える**ので、そこから間隔を数え直す。
    """
    batch = workers.queued(worker.name)
    if not batch:
        return False
    # **いま流している塊が先。** 無ければ、起動の時刻が来ていれば拾う
    if not workers.claim_ready(worker.name):
        if not wake and not _worker_due(worker, now):
            return False
        batch = workers.claim(worker.name, worker.per_run, _iso(now))
        if not batch:
            return False
    for entry in batch:
        # **止まったぶんはここでも外す。** ふだんは `_drop_stale_from_queues` が
        # 先に片付けるが、積んでから流すまでのあいだに止められることもある ——
        # **外さずに見送ると、先頭に居座ってそのワーカーが 1 本も進まない**
        if why := _no_longer_due(entry):
            log.info(
                "skipped %s/%s on worker %r: %s",
                entry["collection"], entry["sweep"], worker.name, why,
            )
            workers.done(worker.name, entry["collection"], entry["sweep"])
            continue
        if workers.pick(worker) is None:
            # **どれも詰まっていたら流さない。** 塊はそのまま残るので、
            # 窓が明けた周で続きから流れる
            return False
        try:
            start_collection_bake(entry["collection"], entry["sweep"])
        except HTTPException as e:
            # 取り込みが混んでいる・巡回が消えた。**塊からは外す** ——
            # 消えた巡回を抱えたままだと、そのワーカーが二度と進まない
            if e.status_code == 404:
                workers.done(worker.name, entry["collection"], entry["sweep"])
            return False
        workers.done(worker.name, entry["collection"], entry["sweep"])
        return True
    return False


def wake_worker(name: str) -> dict:
    """ワーカーを**時計を待たずに起こす**(画面の「今すぐ起こす」)。

    **断る理由は書き分ける。** 押しても何も起きないときに「起きませんでした」
    だけだと、行列が空なのか枠が詰まっているのかが押した人に読めない ——
    どちらなのかで次にすることが逆になる(積むのを待つ / 窓が明くのを待つ)。
    """
    try:
        worker = workers.get(name)
    except ValueError as e:
        raise HTTPException(409, {"error": str(e)}) from None
    if worker is None:
        raise HTTPException(404, {"error": f"ワーカー「{name}」がありません"})
    if not workers.queued(name):
        raise HTTPException(409, {
            "error": f"ワーカー「{name}」の待ち行列は空です",
            "hint": "巡回の側が自分を積むまで、起こしても流すものがありません",
        })
    if workers.pick(worker) is None:
        raise HTTPException(429, _all_full(name))
    if busy := ingest_busy():
        raise HTTPException(409, {
            "error": f"いま取り込みが走っています({busy})",
            "hint": "取り込みは同時に 1 本だけ。終わってからもう一度押してください"
                    " —— 行列はそのまま残っているので、順番は飛びません",
        })
    if not _flush_one(worker, datetime.now(UTC), wake=True):
        raise HTTPException(409, {
            "error": f"ワーカー「{name}」から流せませんでした",
            "hint": "取り込みが走っている最中かもしれません(少し置いてからもう一度)",
        })
    return {"ok": True, "worker": name}


def ingest_busy() -> str:
    """いま取り込みが走っているなら、その相手の名前。走っていなければ空。

    **正は trigger の側**(同時に 1 ジョブしか受けない)。こちらで数えても、
    アプリが 2 本立っている構成では食い違う。
    """
    from app.views.admin import _fetch_trigger_status

    job = _fetch_trigger_status() or {}
    return str(job.get("source") or "?") if job.get("state") == "running" else ""


def _worker_due(worker, now: datetime) -> bool:
    last = _parse_iso(workers.last_at(worker.name))
    if last is None:
        return True
    return now - last >= timedelta(minutes=max(worker.interval_minutes, 1))


def _parse_iso(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def start_collection_bake(
    name: str, sweep: str | None = None, run_once: dict | None = None,
) -> dict:
    """収集の取り込みを 1 本起こす(集めるのも焼くのも向こうで起きる)。

    **予定は起こせたときだけ進める** —— trigger が混んでいて断られたのに次回へ送ると、
    その回は黙って飛ばされる。

    **どの巡回を起こしたかは定義側に控える**(`pending_sweep`)。取り込みは収集の
    名前しか運べないので、素材を作る側はそこを読む。

    **起こせたら、ワーカーの待ち行列からも外す**(`workers.drop`)。画面の
    「今すぐ実行」も外のアプリからの依頼も行列を通さずその場で走らせるので、
    外さないと**待っていたぶんがあとでもう一度流れる**(枠を 1 回ぶん余計に食う)。
    **見るのは全部のワーカー** —— 巡回にいま書いてあるワーカーだけを見ていた頃は、
    積んだ後に付け替えていれば前のワーカーの行列に残った。

    **走っている最中は起こさない。** 取り込みは同時に 1 本しか受けないので
    どのみち断られるが、**断られる前に控え(`pending_sweep`)を書いてしまう** ——
    残ると、いま走っている取り込みがその巡回のつもりで素材を取りに来る
    (押した覚えのない回が、押した覚えのない設定で走る)。
    先に確かめ、それでも擦れ違ったら控えを戻す。
    """
    from app.views.admin import TRIGGER_URL, trigger_run

    if not TRIGGER_URL:
        raise HTTPException(503, {
            "error": "chiezo-trigger が設定されていません(CHIEZO_TRIGGER_URL 未設定)",
            "hint": "集めるのも焼くのも取り込みの中で起きるので、trigger が要る",
        })
    # **時計を持たない巡回は単独では走らせない**(割り込みで頼まれたときだけ動く)。
    # 起こす前に断る —— 起こしてから断ると、1 本ぶんの取り込みが空振りする
    this = collect.require_runnable(collect.get(name), sweep)
    if busy := ingest_busy():
        raise HTTPException(409, {
            "error": f"いま取り込みが走っています({busy})",
            "hint": "取り込みは同時に 1 本だけ。終わってからもう一度押してください",
        })
    # **どの巡回のぶんかは、起こす前に控える。** 取り込みは収集の名前しか運べないので、
    # 素材を作る側は控えを読む —— 起こしてから書くと、取り込みのほうが先に素材を
    # 取りに来たときに控えがまだ空で、「次に走るはずの巡回」へ倒れる
    # (押した巡回ではないものが走る)
    before = collect.get(name)
    was, was_run = before.pending_sweep, before.pending_run
    # **1 回だけの上書きも起こす前に書く**(区画の名指しと頼む相手)——
    # `mark_started` は起こした後なので間に合わない
    collect.mark_pending(name, this.name, run_once)
    try:
        trigger_run(name)
    except Exception:
        # **起こせなかったぶんの控えは戻す。** 残すと、いま走っている取り込みが
        # この巡回のつもりで素材を取りに来る。
        # **ただし、走っているのがこの収集そのものなら戻さない。** 時計はプロセス
        # ごとに立っている(`--workers 2`)ので、同じ周に 2 本が同じ組を起こしに
        # いく —— 負けたほうが控えを戻すと、**勝ったほうが起こした取り込みが読む
        # 控えを消す**ことになり、素材は「次に走るはずの巡回」で組まれる
        # (押した巡回ではないものが走る)。聞きに行けなければ戻す側へ倒す
        running = ""
        with suppress(Exception):
            running = ingest_busy()
        if running != name:
            collect.restore_pending(name, was, was_run)
        raise
    # **行列に居たなら外す。** どの道で走ったかに関わらず、その回はもう走っている
    # —— 画面の「今すぐ実行」も、外のアプリからの依頼(`POST /v1/collect/{name}/run`)も
    # 行列を通さずその場で走らせるので、残すと**あとでもう一度流れる**(枠を 1 回ぶん
    # 余計に食う)。**全部のワーカーを見る**(`drop`)—— 積んだ後に巡回のワーカーを
    # 付け替えていれば、積まれているのは前のワーカーの行列で、いま書いてある
    # ワーカーだけ見ても居ない(試し撃ちで相手を上書きした回も同じ)
    with suppress(Exception):
        workers.drop(name, this.name)
    # **起こせたときだけ予定を進める** —— 混んでいて断られたのに次回へ送ると、
    # その回は黙って飛ばされる(trigger_run が例外にするのでここへは来ない)
    return collect.to_public(collect.mark_started(name, this.name))


async def _ask_for_collection(item, messages: list[dict]) -> tuple[str, str, str]:
    """収集の設定(相手・モデル・深さ・web)で AI に 1 往復投げる。

    収集の実行と、プロンプトの相談の**両方から使う** —— 同じ相手で試せないと、
    相談で作った指示文が本番で通るか分からない。

    返すのは `(本文, 相手, モデル)`。**実際に走ったほうを返す** —— 設定が
    「Chiezo の既定にまかせる」なら、頼む前の `item` には相手もモデルも入って
    いない。1 件ごとの署名(`collect.signed`)がそれでは空になる。
    """
    cfg = await answer.ensure_model(
        answer.require_settings(item.backend, item.model, item.effort)
    )
    spec = providers.get(cfg.name)
    via_bridge = bool(spec and spec.bridge)
    if item.web and not via_bridge and websearch.is_enabled():
        return await agent.complete_with_web(cfg, messages), cfg.name, cfg.model
    extra = {"chiezo_web": True} if item.web and via_bridge else {}
    body = await answer.complete_message(cfg, messages, **extra)
    # **走ってから名乗るモデルを優先する**(`ran_model`)。送るのは別名のことが
    # あり、どの世代に解決されるかは相手が決める
    return answer.content_of(body), cfg.name, cfg.ran_model or cfg.model


async def draft_collection_prompt(
    want: str, current: str = "", feedback: str = "", name: str | None = None
) -> str:
    """AI と相談して、収集の指示文の案を作る。

    **相談は web 検索を開けない**(指示文を書くのに外を見る必要が無く、そのぶん速い)。
    既存の収集を直すときはその相手・モデルを使う —— 本番と違う相手で書いた案は、
    本番で通るか分からない。
    """
    item = collect.get(name) if name else None
    settings = (
        item if item is not None
        else collect.Collection(
            name="", description="", prompt="", interval_minutes=60, enabled=False,
            backend=None, model=None, effort=None, web=False, cursor="",
            created_at="", updated_at="",
        )
    )
    # 相談のときだけ web を閉じる(指示文を書くのに外は要らない)
    settings = replace(settings, web=False)
    content, _who, _model = await _ask_for_collection(
        settings, collect.build_draft_messages(want, current, feedback)
    )
    draft = collect.clean_draft(content or "")
    if not draft:
        raise HTTPException(502, {"error": "AI が指示文を返しませんでした"})
    return draft


async def _harvest(item, sweep=None) -> dict | None:
    """外向きの道具(`app/feeds.py`)を回して、素材を取る。

    **落ちても収集は止めない。** これは参考であって情報源ではなく、AI は自分でも
    調べる —— 1 本の不調で収集ごと止めるほうが損。取れなかった数は素材の中で伝える。

    **「前回より後」の基準はその巡回の前回**(`{recent}` と同じ読み方)。収集の
    前回を基準にすると、**別の巡回が走った時刻でその窓が食われる** —— 見出しを
    溜める回が 1 時間おきでも、その直前に整理が走っていれば見るのは数分ぶんだけで、
    あいだに配信されたものはどの回も溜めないまま流れていく。整理の側でも噛み合わない
    (4 時間ごとに回るのに、差し込まれる見出しは直前の回からのぶんしかない)。
    """
    spec = feeds.normalize(item.feed)
    if spec is None:
        return None
    since = (sweep.last_run_at if sweep is not None else None) or item.last_run_at
    return await feeds.fetch(spec, since)


async def _collect_items(
    item, previous: dict, sources: dict, keys: list[str], sweep=None, focus=None, feed=None,
    seen: set[str] | None = None, used: list | None = None,
) -> tuple[list[dict], str | None, str]:
    """1 回ぶん集める。**最初の 1 回だけ機械的に埋められる**。

    抽出の指定を持っていて、まだ進み具合が入っていなければ AI を呼ばず、手元の
    長期記憶から引いて組み立てる —— 名前や年代のような**既に書いてあること**を
    AI に書かせると、存在しないものが混ざるうえ、毎回違うものが返る。
    進み具合が入った 2 回目からは、いつもどおり AI が肉付けする。

    **区画ごとに 1 回ずつ聞く。** まとめて 1 回で聞くこともできるが、それだと
    `{current}` がその区画のぶんだけになる意味が消える(区画を切った理由そのもの)。
    1 回に何区画まで見るかは巡回が決める(`Sweep.per_run`)。

    **途中でこけたら、そこまでのぶんも捨てる。** 半端に焼くと、見終わっていない区画に
    印が付くか、印の付いていない区画の中身だけが入れ替わる —— どちらも後から読めない。

    `used` を渡すと、**実際に頼んだ相手**をそこへ書く(戻り値にできないため)。
    ワーカーを使う回は区画ごとに振り替わるので、控えに残すのは「決めた相手」では
    足りない —— 残さないと、履歴の相手の欄が既定の名前のままになる。
    """
    # **機械で引く回か**(`collect.uses_extract`)。条件はあちらが持つ ——
    # 書き写すと、片方だけ直したときに食い違う
    if collect.uses_extract(item, sweep):
        spec = extract.normalize(item.extract)
        # **一度外された見出しは拾い直させない。** 墓標は「これは違う」という判断で、
        # 拾う側が知らないと毎回同じものを並べ直す(足す側で弾かれるので中身は
        # 増えないが、タグの名簿では一緒に出てくる語の枠まで食う)
        retired = {title for title, doc in (previous or {}).items() if collect.is_removed(doc)}
        items, next_cursor = await asyncio.to_thread(extract.run, spec, sources, retired)
        return items, next_cursor, ""
    # **外の道具で引く回**(`Sweep.use_feed`)。フィードが配っている見出しを
    # そのまま溜める。**進み具合には触らない** —— 次にどこから読むかは
    # 道具の側が「前回の実行より後」で決める(`feeds.SINCE_LAST_RUN`)
    if sweep is not None and sweep.use_feed:
        if feed is None:
            return [], None, "この収集に外向きの道具が付いていません"
        note = ""
        if failed := feed.get("failed"):
            # 黙って減らさない —— 少ないのが世の中の都合か、道具の不調かで意味が違う
            note = f"{feed.get('tried')} 件の出典のうち {failed} 件は取れませんでした"
        return feeds.to_items(feed), None, note
    worker = _worker_of(sweep) if sweep is not None else None
    step = workers.pick(worker) if worker is not None else None
    if worker is not None and step is None:
        # **どれも詰まっているなら、無理に頼まない。** ここまで来ているのは時計が
        # 起こした後なので、明けた窓が塞がったということ —— 断れば控えに理由が残り、
        # 予定は進まないので次の周で走り直せる
        raise HTTPException(429, _all_full(sweep.worker))
    asked = item if sweep is None else sweep.applied_to(item, step)
    collected: list[dict] = []
    # **AI が目を通したのは、差し込まれたものだけ**(`notes.UNREVIEWED_TAG`)。
    # 返りだけを見ていると、読んだうえで直す必要が無かった 1 件が未精査のまま残る
    shown: set[str] = seen if seen is not None else set()
    cursor = None
    notes: list[str] = []
    ran_by = ran_model = ""
    for key in keys or [None]:
        content = None
        # **区画ごとに相手を見直す。** 1 回で何区画も回るので、決めるのが回の頭
        # 1 度きりだと、**途中で窓が閉まっても同じ相手に投げ続ける** —— 本番で、
        # 41% で通した相手が 2 区画目で 89% に跳ね、残り 4 区画ぶんを投げ切って
        # から断られ、29 分ぶんの収穫がまるごと消えた。
        # 断られたときは**同じ区画を次の段で**引き受ける(区画を飛ばさない)
        while True:
            if worker is not None:
                step = workers.pick(worker)
                if step is None:
                    break
                asked = sweep.applied_to(item, step)
            if used is not None and step is not None:
                used.append(step)
            try:
                content, ran_by, ran_model = await _ask_for_collection(
                    asked,
                    collect.build_messages(
                        item, previous, key, sources, sweep, focus, feed, shown,
                    ),
                )
            except HTTPException as e:
                # **相手が「使い切った」と言ったら、その言い分を控える**
                # (`workers.avoid_for_now`)。控えてある使用率は定時にしか採らないので、
                # 長い 1 回の途中で窓が閉まっても次の採取まで気づけない ——
                # 断られた事実のほうが新しい。**枠と関係ない失敗では締め出さない**
                if worker is None or step is None or not workers.looks_full(_reason_of(e)):
                    raise
                # **避けるのはその段のモデルが食う枠だけ** —— 相手ごと避けると、
                # 同じ相手の別の枠に置いた段まで巻き添えで飛ばされる
                workers.avoid_for_now(step.backend, _reason_of(e), step.model)
                notes.append(f"{step.backend} が枠切れを返したので、次の段へ回しました")
                continue
            break
        if content is None:
            # どの段も詰まった。**集めたぶんは捨てない** —— 残りの区画は印を
            # 付けずに見送るので、次の回がそこから続ける
            if not collected:
                raise HTTPException(429, _all_full(sweep.worker))
            notes.append(_gave_up(sweep.worker, key))
            break
        items, next_cursor, note = collect.parse_response(content)
        # **1 件ずつに署名を載せる。** ワーカーを使う回は区画ごとに相手が振り替わる
        # ので、回の単位で 1 つに丸めると半分の文書に嘘の署名が付く
        collected += collect.signed(items, ran_by, ran_model)
        cursor = next_cursor or cursor
        # **どの区画で切れたかまで残す。** 何区画かまとめて回るので、
        # 「切れました」だけでは次にどこを狭めればよいか分からない
        if note:
            notes.append(f"{key}: {note}" if key else note)
    return collected, cursor, " / ".join(notes)


def _all_full(worker_name: str) -> dict:
    return {
        "error": f"ワーカー「{worker_name}」のどの相手も枠に余裕がありません",
        "hint": f"使用率が {workers.QUOTA_LIMIT:.0f}% を超えている相手と、"
                "相手自身が枠切れを返した相手は避けます",
    }


def _gave_up(worker_name: str, key) -> str:
    """途中で窓が閉まったときの一言。**どこまで見たかが読めるように区画も書く**。"""
    where = f"({key} の手前)" if key else ""
    return f"ワーカー「{worker_name}」のどの相手も枠に余裕がなくなったので、残りは見送りました{where}"


def _reason_of(e: HTTPException) -> str:
    """相手が言ってきた理由。**中身の形は 1 つではない**ので、文字にして渡す。"""
    detail = e.detail
    if isinstance(detail, dict):
        return " / ".join(str(v) for v in detail.values())
    return str(detail)


async def collect_material(name: str, sources: dict) -> str:
    """いま AI に集めさせて、焼く素材(NDJSON)を組み立てる。

    **取り込みの中で呼ばれる**(`/v1/collect/fetch`)。集めた瞬間に焼かれるので、
    途中に置き場が要らない。**結果は必ず定義側に控える** —— 無人で回る層なので、
    その場に居合わせない人が後から「何が起きたか」を読めることのほうが大事。

    **失敗は例外にする**(控えを残したうえで)。素材を返せないまま取り込みを続けさせると、
    前世代のまま焼き直した新しい世代ができて、集められなかったことが履歴から消える。
    """
    # **ここから先の AI 呼び出しは「収集」のもの**（`app/ai_inflight.py`）。
    # 無人で回る層なので、走っているものを見に来た人が
    # 「これは自分が頼んだものではない」と分かる必要がある
    with ai_inflight.called_by(f"collect:{name}"):
        return await _collect_material(name, sources)


def _who_ran(used: list) -> dict:
    """実際に頼んだ相手。**振り替わった回は並べて残す**。

    **1 つに丸めない。** 途中で窓が閉まって次の段へ回った回は、どちらの相手も
    その回を走らせている —— 片方だけ残すと、枠の動きと履歴が食い違う。
    **並びは頼んだ順**(先に頼んだほうが先)。
    """
    if not used:
        return {}
    backends: list[str] = []
    models: list[str] = []
    for step in used:
        if step.backend and step.backend not in backends:
            backends.append(step.backend)
        if step.model and step.model not in models:
            models.append(step.model)
    return {"backend": " → ".join(backends), "model": " → ".join(models), "effort": ""}


def _default_backend_name() -> str:
    """名指ししなかったときに頼むことになる相手。**控えを残すためだけに引く**。

    ここで断らない —— 相手が 1 つも有効でなければ呼び出し自体が先に落ちるので、
    控えのために例外を足す意味が無い(控えが取れないだけで収集を止めない、と同じ判断)。
    """
    with suppress(Exception):
        return (answer.backend_names() or [""])[0]
    return ""


def _phase_done(label: str, name: str, since: float, count: int) -> float:
    """1 回の中の段ごとに、かかった時間を残す。**次の段の起点を返す**。

    **控え(`app/collect_log.py`)は 1 回ぶんの合計しか持たない。** 遅いのが
    区画の突き合わせなのか AI なのか焼くところなのかは、合計からは読めない ——
    本番で 1 回 8.7 時間の回を追ったとき、**どこに消えているのかを外から
    確かめる手がどこにも無かった**(取り込みのログも「fetch を始めた」で止まる)。
    """
    now = time.monotonic()
    log.info("collect %s: %s に %.1f 秒(%d)", name, label, now - since, count)
    return now


async def _collect_material(name: str, sources: dict) -> str:
    # **かかった時間を測る。** 回ごとに桁が違い(相手も区画の大きさも回ごとに変わる)、
    # **遅くなったことは件数からは読めない** —— 同じ件数を返していても、5 分が
    # 20 分になっていれば一周の見込みが 4 倍ずれる。測るのは集めるところまでで、
    # 焼くぶんは入らない(控えを書いてから流すので、ここではまだ終わっていない)
    started = time.monotonic()
    item = await asyncio.to_thread(collect.get, name)
    # 前世代は 1 度だけ読んで使い回す。プロンプトへ差し込む素材であり、
    # 消えたものを数える相手であり、doc_id を引き継ぐ元でもある
    # **前世代は 1 行ずつ読む。** 丸ごと dict に読むと行の数だけメモリが要る ——
    # 50 万件の地図の名簿で 1.8 GB になった(実測)。2 周するので、読み直せるように
    # 「呼ぶと流れてくるもの」で渡す
    previous = lambda: collect.stream_previous(name, sources)  # noqa: E731
    # **区画を名指しされた回は割り直さない**(`pending_run`)。理由は 2 つ:
    # ①割り直しは母集団を丸ごと 1 周舐めるので、**1 区画だけ試すための回で
    # いちばん重い処理を走らせることになる**(枠と時間を節約したくて押す口なのに)。
    # ②**割り直すと名指しした鍵が台帳から消えることがある** —— そうなると
    # どの文書も一致せず、AI は「誰も居ない」と読んで何も返さない(空振りに
    # 1 回ぶんの枠を使う)。**台帳をいまのまま使う**のが名指しの意味でもある。
    # 割り直したいときは区画の面の「区画を割り直す」を先に押す
    once = item.pending_run or {}
    named = list(once.get("partitions") or [])
    # **区画は集める前に決める。** 何を見るかが決まっていないと、渡す素材も
    # 差し込む文も作れない(台帳が無ければ空で返り、今までどおり全体を見る)
    phase = time.monotonic()
    ledger = item.partitions if named else await asyncio.to_thread(
        collect.plan_partitions, item, sources, previous
    )
    phase = _phase_done("台帳を決める", name, phase, len(ledger))
    # **割り直した台帳で素材を組む。** 定義に入っているのは走る前の台帳なので、
    # この回で割り直したときに食い違う —— 区画を選ぶのは新しい台帳から、
    # どの文書がその区画かを判ずるのは古い台帳から、になり、**差し込みが丸ごと空になる**
    # (名簿を作り直した直後の回がまさにそれで、AI は「誰も居ない」と読んで何も返さない)
    item = replace(item, partitions=ledger)
    # **どの巡回のぶんかは、起こした側が控えてある**(取り込みは名前しか運べない)
    focus = collect.normalize_focus(item.pending_focus)
    # **割り込みは割り込み用の巡回で走らせる**(`on_demand`)。定時の巡回の設定を
    # 流用すると、どちらの都合で選んだ相手なのかが言えなくなる ——
    # 割り込みは人が待っている場面なので、速い相手に頼みたい / 1 件をじっくり
    # 調べさせたい、のどちらもある
    sweep = (
        collect.sweep_for_focus(item, focus)
        if focus is not None
        else collect.sweep_named(item, item.pending_sweep)
    )
    if focus is not None:
        # **割り込みは 1 回きり。** 見るのは頼まれたところだけで、区画の順番には触らない
        keys = [focus.partition] if focus.partition else []
        # 割り込みは必ず「直す」側で焼く(名指しの 1 件を直せないと割り込みの意味が無い)
        baked_as = item
    elif sweep.use_extract or sweep.use_feed:
        # **機械で引く回は区画を見ない。** 指定を 1 本引いて全部を返すので、
        # 区画を選ぶと**見てもいない区画に「回った」印が付く**(一周が嘘になる)
        keys = []
        baked_as = item
    else:
        # **1 回だけの上書き**(画面の「この区画だけ 1 回走らせる」)。区画を名指し
        # されていればそこだけ見る —— 枠が細いときに、直したコードを本番の形で
        # 1 区画ぶんだけ試すための道。**ふつうの回として走る**(印も予定も進む)
        sweep = collect.asked_for_run(sweep, once)
        if named:
            # **台帳に無い鍵では走らせない。** どの文書も一致しないので AI は
            # 「誰も居ない」と読んで何も返さず、**空振りに 1 回ぶんの枠を使う**
            # (節約のために押した口で、いちばん起きてほしくない)。
            # **1 つでも欠けたら走らせない** —— 通した鍵だけで走ると、押した人には
            # 全部を見たように見えて、抜けた区画だけが黙って飛ばされる
            here = {p["key"] for p in ledger}
            if missing := [k for k in named if k not in here]:
                raise HTTPException(409, {
                    "error": f"区画「{missing[0]}」は台帳にありません"
                             + (f"(ほか {len(missing) - 1} 件)" if len(missing) > 1 else ""),
                    "hint": "割り直しで鍵が変わったかもしれません"
                            "(区画の面から選び直してください)",
                })
            keys = named
        else:
            keys = partitioning.pick(
                ledger, sweep.name, sweep.per_run(len(ledger)),
                partitioning.normalize(item.partition),
            )
        baked_as = item
    # **直す回かどうかは、その回の依頼文が語っている。** 収集ぜんたいの設定として
    # 持っていた頃(`mode`)は巡回ごとに決められなかった —— いまは巡回ごとに決まる
    edits = focus is not None or collect.edits_what_is_there(
        sweep.prompt or item.prompt, sweep.only_new
    )
    label = collect_log.FOCUS_LABEL if focus is not None else sweep.name
    # **誰に頼んだ回かも控える。** 巡回ごとに相手を変えられるので、回の名前だけでは
    # 何で走ったのか読めない。**「既定にまかせる」は名前に開いて残す** ——
    # 空欄のまま残すと、後から読む人には既定がどれだったのか確かめようがない
    # (相手の一覧は設定しだいで変わる)。
    # **AI を呼ばない回には誰も書かない**(`collect.asks_ai`)—— 既定の相手を
    # 書いておくと、機械で引いた回が「その相手に頼んだ回」として履歴に並ぶ
    # **AI が目を通した見出し**。差し込まれたものだけが入り、焼くときに未精査の印を
    # 外す(`notes.UNREVIEWED_TAG`)。**機械で引く回では空のまま** —— 誰も読んでいない
    shown: set[str] = set()
    who = {
        "backend": sweep.backend or _default_backend_name(),
        "model": sweep.model or "",
        "effort": sweep.effort or "",
    } if collect.asks_ai(item, sweep) else {"backend": "", "model": "", "effort": ""}
    # **実際に頼んだ相手**。ワーカーを使う回は区画ごとに振り替わるので、
    # 走り終えてからでないと分からない(`_collect_items` が書く)
    used: list = []
    try:
        feed = await _harvest(item, sweep)
        # **差し込むぶんだけ取り出す。** 区画で切ってあれば、その区画のぶんだけ ——
        # 全部を持つと、区画で切った意味がメモリの側から消える
        for_prompt = await asyncio.to_thread(
            collect.prompt_docs, item, previous, keys, focus
        )
        phase = _phase_done("差し込むぶんを選ぶ", name, phase, len(for_prompt))
        items, next_cursor, note = await _collect_items(
            item, for_prompt, sources, keys, sweep, focus, feed, shown, used
        )
        # **控えに残すのは、決めた相手ではなく頼んだ相手。** ワーカーを使う回は
        # 巡回に相手が書いていないので、書き換えないと履歴が既定の名前で埋まる
        who.update(_who_ran(used) or {})
        # **数えるのは流し始める前。** 流している途中でステータスは変えられないので、
        # 断るならここで断る(`bake_survey`)。素材そのものは 1 行ずつ返すので、
        # ここでは組み立てない —— 50 万件の名簿では 1 本の文字列が 460 MB になる
        phase = _phase_done("AI に聞く", name, phase, len(keys))
        plan = await asyncio.to_thread(
            # **足すだけの回は、既にある見出しに触らない。** 割り込みは別 ——
            # あれは名指しで「ここを直して」なので、必ず直す側で走る
            collect.bake_survey, baked_as, sources, previous, items,
            focus is None and sweep.only_new, edits,
        )
        phase = _phase_done("焼く前に数える", name, phase, plan.get("total") or 0)
        diff = plan["diff"]
        # **機械で名簿を作り直した回は、その場で区画を割り直す。**
        # 台帳は次に走るまで古いままで、**1 回に何区画を見るかはそこから決まる** ——
        # 割り直さないと、次の巡回が AI を何回叩くのかが始まるまで誰にも見えない
        # (60 万件を 1 区画として持ったまま「1 回に 1 区画」と出る)。
        # **件数は割り直した台帳のものが正**なので、古い台帳で数えたぶんは使わない
        if item.partition and collect.uses_extract(item, sweep):
            ledger = await asyncio.to_thread(
                collect.plan_partitions_next, item, sources, previous, items,
                focus is None and sweep.only_new, edits,
            )
            # **中身が動いた区画は、どの巡回の「見た」も外す。** 名簿を作り直すと
            # 区画の母集団が入れ替わるのに、印はそのまま残る —— 割られた区画の子は
            # 親の記録を写すので(`partition._inherited`)、膨らんだ区画ほど
            # 「見終わったこと」になって一周が終わるまで誰にも見られない
            ledger = partitioning.cleared_where_changed(item.partitions, ledger)
            diff["partition_counts"] = {}
    except Exception as e:
        if hasattr(items := locals().get("items"), "close"):
            items.close()
        # **落ちた回にも、頼んだ相手を残す。** 既定の名前のままだと、控えを見た人は
        # 「claude が壊れた答えを返した」と読む —— 実際に返したのは別の相手だった
        who.update(_who_ran(used) or {})
        reason = f"{type(e).__name__}: {e}"
        log.warning("collect %s failed: %s", name, reason)
        await asyncio.to_thread(
            collect.record_result,
            name, status="error", error=reason, sweep=sweep.name, partitions=ledger,
            focus=focus is not None,
        )
        await asyncio.to_thread(
            collect_log.record,
            name, status=collect_log.STATUS_ERROR, error=reason, sweep=label, scope=keys,
            ms=int((time.monotonic() - started) * 1000), **who,
        )
        raise
    await asyncio.to_thread(
        collect.record_result,
        name,
        status="ok",
        added=diff["added"],
        skipped=diff["skipped"],
        removed=diff["removed"],
        updated=diff["updated"],
        removed_titles=diff["removed_titles"],
        next_cursor=next_cursor,
        sweep=sweep.name,
        visited=keys,
        # **焼いたあとの人数で台帳を書き直す。** 回の頭で数えた値のままにすると、
        # 見終わったばかりの区画が見る前の人数で出る(`collect.partition_counts`)。
        # 割り直した回は数えた値を渡さない —— そちらは割ったときの数を持っている
        partitions=partitioning.counted(ledger, diff.get("partition_counts") or {}),
        focus=focus is not None,
    )
    await asyncio.to_thread(
        collect_log.record,
        name, status=collect_log.STATUS_OK, diff=diff, sweep=label, scope=keys, error=note,
        ms=int((time.monotonic() - started) * 1000), **who,
    )
    log.info(
        # **同じ URL で弾いたぶんも出す**(`collect.url_key`)。skipped に混ぜたままだと、
        # 入るはずのものが入らないときに、既にあったのか弾かれたのかが読めない
        "collect %s (%s/%s%s): added=%d updated=%d kept=%d removed=%d skipped=%d dup=%d",
        name, "直す" if edits else "足す", label, f" {len(keys)} 区画" if keys else "",
        diff["added"], diff["updated"], diff["kept"], diff["removed"], diff["skipped"],
        diff.get("duplicates") or 0,
    )
    # **控えを書いてから流す。** 読み手が途中で切っても、何をしたかは残る。
    # **流し終えたら置き場を片づける** —— 抽出は一時の SQLite に名簿を載せるので、
    # 残すとファイルが溜まる(`app/extract.py` の `Roster`)
    def flow():
        try:
            yield from _logged_stream(
                collect.bake_lines(
                    baked_as, sources, previous, items,
                    focus is None and sweep.only_new, edits, plan, label,
                    # **AI を呼ばない回で入るものは未精査**(`collect.asks_ai`)——
                    # フィードも機械抽出も、宣伝や的外れをそのまま引き受ける
                    not collect.asks_ai(item, sweep), shown,
                    # **機械で引く回は、運んできた脇書きを入れ替える**
                    # (`collect.uses_extract`)—— 数えた値は回るたびに変わる
                    collect.uses_extract(item, sweep),
                ),
                name, plan.get("rows"),
            )
        finally:
            if hasattr(items, "close"):
                items.close()

    return flow()


def _logged_stream(lines, name: str, expected):
    """素材を流しながら、**途中で落ちたら何行目までだったかを残す**。

    **流し始めたら断れない**(ステータスは 1 度しか送れない)ので、途中で落ちると
    **短いだけの正しい素材**として相手に届く。受け取る側は行数で気づくが
    (`meta.min_docs`)、気づけるのは「足りない」ことだけ —— **なぜ切れたかは
    こちら側にしか無い**。素通しすると、収集の名前も件数も付かない形で
    uvicorn の控えに残り、どの回のどこで切れたのかを後から結べない。

    **投げ直す。** ここで握りつぶすと、短い素材がそのまま焼き上がって
    前の世代が捨てられる(受け取る側の検証が最後の歯止め)。
    """
    sent = 0
    try:
        for line in lines:
            sent += 1
            yield line
    except Exception:
        # 1 行目は meta なので、届いた文書は `sent - 1` 件 ——
        # 取り込み側の「only N docs」とそのまま突き合わせられる
        log.exception(
            "collect %s: 素材を流している途中で落ちた(%d 行目 / 文書 %d 件を送信済み。"
            "焼く予定は %s 件)",
            name, sent, max(sent - 1, 0), expected,
        )
        raise


async def collect_preview(name: str, sources: dict, sweep_name: str | None = None) -> dict:
    """いま AI に集めさせて、**焼かずに**前世代との差分だけ返す。

    プロンプトを育てるための道具。作り直し(整理)は前世代を置き換えるので、
    「何が増えて・何が残って・何が消えるか」を見てから焼けないと、直しようがない
    (件数と勘で調整することになる)。

    **長期記憶には一切書かない。定義側の控えも進めない** —— カーソルも次回の予定も
    動かさないので、試したことが本番の進み具合に混ざらない。
    """
    item = await asyncio.to_thread(collect.get, name)
    # **どの巡回のつもりで試すかを選べる。** 相手も 1 回に見る量も巡回ごとに違うので、
    # 名指しできないと「じっくりで聞いたらどうなるか」を試せない
    sweep = collect.require_runnable(item, sweep_name)
    # 焼く経路と同じく 1 行ずつ読む(丸ごと持つと、数十万件の収集で GB 単位になる)
    previous = lambda: collect.stream_previous(name, sources)  # noqa: E731
    ledger = await asyncio.to_thread(collect.plan_partitions, item, sources, previous)
    # **割り直した台帳で素材を組む。** 定義に入っているのは走る前の台帳なので、
    # この回で割り直したときに食い違う —— 区画を選ぶのは新しい台帳から、
    # どの文書がその区画かを判ずるのは古い台帳から、になり、**差し込みが丸ごと空になる**
    # (名簿を作り直した直後の回がまさにそれで、AI は「誰も居ない」と読んで何も返さない)
    item = replace(item, partitions=ledger)
    # **下見は 1 区画だけ。** 何区画でも見られるが、下見は「この指示文でどうなるか」を
    # 見るためのもので、1 区画あれば分かる(そのぶん安く、待たされない)
    keys = partitioning.pick(ledger, sweep.name, 1, partitioning.normalize(item.partition))
    feed = await _harvest(item, sweep)
    for_prompt = await asyncio.to_thread(collect.prompt_docs, item, previous, keys, None)
    items, next_cursor, note = await _collect_items(
        item, for_prompt, sources, keys, sweep, None, feed
    )
    edits = collect.edits_what_is_there(
        sweep.prompt or item.prompt, sweep.only_new
    )
    # **下見でも丸ごとは組まない。** 数えるだけで足りる(焼かないので素材は要らない)
    diff: dict = {}
    await asyncio.to_thread(
        lambda: deque(
            collect.stream_docs(item, previous(), items, sweep.only_new, edits, diff),
            maxlen=0,
        )
    )
    return {
        "name": name,
        "edits": edits,
        **diff,
        "next_cursor": next_cursor,
        # **下見では印を付けない。** 進み具合を動かさないのが下見の約束なので、
        # 「今回どこを見たか」だけ見せる(台帳を進めるのは焼くときだけ)
        "sweep": sweep.name,
        "partition": keys[0] if keys else None,
        # 答えが途中で切れていたら、そう出す(件数だけ見て「少ない」と読まれないように)
        "note": note,
        # 焼こうとしたら止まるかどうか。止まる理由もそのまま出す
        "blocked": collect.shrink_blocked(item, diff),
    }


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
    # 選べるモデルと考える量を、裏で先に聞いておく(`answer.warm_choices`)。
    # CLI ブリッジは聞かれてから CLI を起こすので、冷えたまま開くと一覧が出るまで数秒
    # かかる —— 最初に画面を開いた人にそれを払わせない。立ち上がりは待たせない
    choices = asyncio.create_task(answer.warm_choices())
    # 収集の時計。置き場が無ければ回さない(機能フラグと同じ扱い)
    collector = (
        asyncio.create_task(_run_collections(app))
        if collect.is_enabled() and COLLECT_TICK_SECONDS > 0
        else None
    )
    # 枠の時計。置き場が無ければ回さない(記録できないので採っても残らない)
    sampler = (
        asyncio.create_task(_sample_quotas())
        if usage_store.is_enabled() and QUOTA_SAMPLE_MINUTES > 0 and QUOTA_TICK_SECONDS > 0
        else None
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
    # **CLI ブリッジ用の、生成の道具を出さない口。** 理由は `build_mcp` の説明にある
    # (絵を頼んだ相手が絵を頼み返す)。塞ぐ手立てが接続先しか無いので、口を分ける
    knowledge = build_mcp(app, with_media=False, with_writes=False)
    app.state.mcp_knowledge_asgi = build_mcp_app(knowledge)
    try:
        async with mcp.session_manager.run(), knowledge.session_manager.run():
            yield
    finally:
        for task in (watcher, collector, sampler, choices):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(title="Chiezo", version="0.2", lifespan=lifespan)


# **AI を使う口。ここへはブリッジを通さない。**
#
# 相手を指名して頼んでいるのに、指名された側が別の相手へ聞きに行く —— 依頼として
# 成立していないし、誰が答えたのかも誰の枠を使ったのかも読めなくなる。
# 詳しい理由と、道具を取り上げるだけでは止まらないことは `media.refuse_bridge_caller`。
#
# **口ごとに書かずに 1 か所で見る。** 口ごとだと、付け忘れが「枠が二重に減って
# 初めて気づく」類の漏れになる。中間層なら**本体の検査より手前**でもある ——
# 口ごとの検査は、本体が壊れているときに素通りして 422 になっていた。
# **生成の口はここに並べない。** あちらは「会話している相手に、その相手自身を
# 頼む」のは通すので、本体(`_refuse_bridge_for`)で相手まで見て判断する。
_AI_PATHS = (
    "/v1/ask",
    "/v1/chat",
    "/v1/ai/complete",
    "/v1/media/transcribe",
    "/v1/collect/draft",
)


def _asks_an_ai(path: str) -> bool:
    """その口は AI を使うか。**読む口(search / doc / filter)は通す** ——
    ブリッジの値打ちは「Chiezo の知識を引かせる」ことなので。
    """
    if path.rstrip("/") in _AI_PATHS or path.startswith("/v1/collect/draft"):
        return True
    # 収集の「今すぐ 1 回」。名前が途中に入るので後ろで見る
    return path.startswith("/v1/collect/") and path.rstrip("/").endswith("/run")


def _cors_origins() -> list[str]:
    """ブラウザから直接読ませるオリジン。`CHIEZO_CORS_ORIGINS` にカンマ区切りで書く。

    **既定は空(誰にも開けない)。** ここは LAN 内前提で認証を持たない読み取り口なので、
    `*` で開けると「その端末で開いている任意のページ」が中身を読めることになる。
    読ませたい相手(travel-log の画面など)のオリジンだけを書く。

    **オリジンはスキームとポートまで含めて一致する** —— `https://例.test` と
    `http://例.test:7040` は別物なので、開く経路のぶんだけ並べる。
    """
    raw = os.environ.get("CHIEZO_CORS_ORIGINS", "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _cors_headers(origin: str, *, preflight: bool = False) -> dict[str, str]:
    """許したオリジンへ返す印。**資格情報は許さない** ——
    Cookie を送らせる必要が無く、許すと許可オリジンの取り違えがそのまま
    他人の資格での読み取りになる(`Access-Control-Allow-Credentials` を出さない)。
    """
    if not preflight:
        return {"access-control-allow-origin": origin}
    return {
        "access-control-allow-origin": origin,
        # 読む口だけを開ける。書き込み(POST)はブラウザから使わせない
        "access-control-allow-methods": "GET, OPTIONS",
        "access-control-allow-headers": "*",
        "access-control-max-age": "600",
    }


@app.middleware("http")
async def allow_the_browser_to_read(request: Request, call_next):
    """許可したオリジンのページに、応答の中身を読ませる。

    **ブラウザは `Access-Control-Allow-Origin` の無い応答を JS へ渡さない。**
    付けないと、外のページからは「届いているのに読めない」という形で失敗する
    (サーバー側のログには 200 が並ぶので、原因が分かりにくい)。

    **自前で書いているのは、許すかどうかを毎回環境変数から読むため。**
    `CORSMiddleware` は組み立て時の設定で固まるので、テストでも運用でも
    入れ替えにプロセスの作り直しが要る。やることは「一致したら印を返す」だけで、
    許すのは GET と事前確認(preflight)に限る。

    **一致は完全一致**(前方一致にしない) —— `https://例.test` を許したつもりで
    `https://例.test.attacker.example` まで通ることになるため。
    """
    origin = (request.headers.get("origin") or "").rstrip("/")
    allowed = bool(origin) and origin in _cors_origins()
    if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        # 事前確認は本体へ流さない(GET しか持たない口は 405 を返すため)。
        # 許していないオリジンには印を付けずに返し、ブラウザ側で止めてもらう
        return Response(status_code=204 if allowed else 403,
                        headers=_cors_headers(origin, preflight=True) if allowed else None)
    response = await call_next(request)
    if allowed:
        response.headers.update(_cors_headers(origin))
        # 同じ URL の応答をオリジンごとに別物として扱わせる(中間のキャッシュ対策)
        response.headers.append("vary", "Origin")
    return response


@app.middleware("http")
async def refuse_orders_from_the_cli(request: Request, call_next):
    """Chiezo が動かしている CLI からの、AI を使う口への依頼を断る。"""
    if _asks_an_ai(request.url.path):
        try:
            media.refuse_bridge_caller(request.client.host if request.client else "")
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return await call_next(request)


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


@app.get("/icon.svg", include_in_schema=False)
def app_icon_svg():
    """ホーム画面のアイコン(大きさを問わない形)。中身は `assets/icon.svg`。"""
    return Response(content=APP_ICON_SVG, media_type="image/svg+xml")


@app.get("/manifest.webmanifest", include_in_schema=False)
def app_manifest():
    """ホーム画面から**帯を消して**開くための宣言(`app/pages.py` の `APP_MANIFEST`)。

    **Service Worker は持たない** —— 欲しいのは開き方だけで、オフラインで
    動かしたいわけではない(抱え込むと、更新しても古い版が出続ける)。
    帯が消えるぶんの戻る・進む・読み直しは、画面の下に自前で出す
    (`pages.TOUCH_SCRIPT`)。
    """
    return JSONResponse(content=APP_MANIFEST, media_type="application/manifest+json")


# ---- ヘルスチェック・ソース一覧 -------------------------------------------


@app.get("/healthz")
def healthz(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    notes.refresh_count(sources)
    return {
        "status": "ok",
        "sources": {
            s.name: {"docs": s.doc_count, "dump_date": s.dump_date} for s in sources.values()
        },
    }


@app.get("/v1/sources")
def list_sources(request: Request):
    sources: dict[str, Source] = request.app.state.sources
    # 短期記憶(notes)の件数だけは走査で追えないので、読む直前に数え直す
    # (理由は notes.count())。長期側は走査で数えた値をそのまま使う。
    notes.refresh_count(sources)
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
                # **何ができるか・何が入っているかもここで返す。**
                # 以前は設定ファイル(CLAUDE.md)に 23 ソースぶん焼き込んでいたが、
                # あれは生成した瞬間の写しで、増えるたびに人が再生成しないと古くなる。
                # 古い一覧は無いものへ投げさせ、**あるものを使わせない**
                **claude_config.describe(s),
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
    """全文検索の本体。該当件数ではなく返す件数に比例した数の行しか読まない。

    素直に書くと `docs_fts MATCH ... JOIN docs ORDER BY <bm25 と人気度>` になるが、
    並べ替えに docs の rank_score と title が要るせいで、該当した文書を全部
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
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
):
    src = get_source(request, source)
    extra_where, extra_params = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, include_removed=include_removed
    )
    # FTS 側は docs に別名 d を付けて JOIN するため、列名を修飾した版も用意する
    extra_where_d, _ = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, column_prefix="d.",
        include_removed=include_removed,
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
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
):
    src = get_source(request, source)
    field_list = parse_fields(fields)
    where, params = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, include_removed=include_removed
    )
    where_d, _ = build_attribute_filters(
        src, area=area, feature=feature, bbox=bbox, tag=tag, column_prefix="d.",
        include_removed=include_removed,
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


def removed_clause(
    src: Source, include_removed: bool = False, column_prefix: str = ""
) -> tuple[str, list]:
    """**読者に出さないものを外す**断片(`notes.HIDDEN_TAGS`)。

    既定で外す —— 読む側が全員「この印を除く」を覚えていなくても、出てこない
    ようにするため。新しい読み手を書くたびに同じ約束を思い出す必要があるのは、
    いつか必ず抜ける。含めたいときだけ `include_removed` を渡す。

    **外すのは 1 つではない。** 消えたもの(`REMOVED_TAG`)に加えて、**まだ AI が
    目を通していないもの**(`UNREVIEWED_TAG`)も外す —— 機械で入るものには宣伝も
    的外れも混ざるので、整理が回るまでのあいだ読者の画面に並んでいた。
    **引数は増やさない** —— 印ごとに分けると、新しい読み手が片方だけ思い出す。

    **タグの索引を持たない古いソースでは何もしない**(絞りようが無い)。
    """
    if include_removed or src.schema_version < TAG_MIN_SCHEMA_VERSION:
        return "", []
    marks = ", ".join("?" * len(notes.HIDDEN_TAGS))
    return (
        f" AND {column_prefix or 'docs.'}doc_id NOT IN"
        f" (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag IN ({marks}))",
        list(notes.HIDDEN_TAGS),
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
    include_removed: bool = False,
) -> tuple[str, list]:
    """属性条件を ` AND ...` の形の SQL 断片とパラメータに変換する。

    条件が 1 つも指定されなくても、**消えたものを外す断片だけは付く**
    (`removed_clause`)。呼び出し側の SQL に無条件で連結してよい。
    """
    hidden, hidden_params = removed_clause(src, include_removed, column_prefix)
    if not any((feature, area, bbox, wikidata, tag)):
        return hidden, hidden_params
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
    return "".join(f" AND {clause}" for clause in where) + hidden, params + hidden_params


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
    tags: list[str] | None = None,
    include_removed: bool = False,
) -> tuple[str, list] | None:
    """絞り込み条件を「doc_id を返す SELECT」に変換する(/filter 用)。行本体を読まない。

    どの条件も、値を実体で持つ表か索引だけで doc_id の集合に落とせる:

    - `tag` → doc_tags(idx_doc_tags_tag が (tag, doc_id) の被覆索引)
    - `bbox` → doc_coords(実体の lat/lon を持つ表。生成列の索引では経度の判定に
      行本体が要る。schema_version 4 から)
    - `feature` / `area` → idx_docs_feature_area / idx_docs_area_feature。
      生成列でも索引には計算済みの値が入っているので、doc_id だけを取り出す
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
    if not any((feature, area, bbox, wikidata, tag, tags)):
        return None
    if src.schema_version < FILTER_MIN_SCHEMA_VERSION:
        return None
    # 持っていない属性で絞ろうとしていないか(0 件ではなく理由を返す)。
    # ここは /filter の経路で、search / doc は build_attribute_filters 側で同じ検査をする。
    require_attributes(src, feature=feature, area=area)
    parts: list[str] = []
    params: list = []
    if tag or tags:
        if src.schema_version < TAG_MIN_SCHEMA_VERSION:
            return None
        # **タグ名を実体の配列で受け取れる**(`tags`)。カンマ区切りの文字列に畳むと、
        # カンマを含むタグ名が 2 つに割れる —— 末尾一致で広げたタグ名は
        # こちらが作った文字列ではないので、区切り文字が入っていないと言い切れない
        tags = list(tags) if tags else split_tags(tag)
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
    joined = " INTERSECT ".join(parts)
    if not include_removed and src.schema_version >= TAG_MIN_SCHEMA_VERSION:
        # **読者に出さない印の付いたものを外す**(`notes.HIDDEN_TAGS`)。消えたものと、
        # まだ AI が目を通していないもの。EXCEPT なら doc_tags の索引の中だけで
        # 終わるので、INTERSECT の並びに足しても行本体は読まない。
        # **外す印は search / doc と同じ並びを使う** —— ここだけ消えたものしか
        # 外していなかったため、他の口では出ないものが一括抽出にだけ並んでいた
        marks = ",".join("?" * len(notes.HIDDEN_TAGS))
        joined = f"{joined} EXCEPT SELECT doc_id FROM doc_tags WHERE tag IN ({marks})"
        params.extend(notes.HIDDEN_TAGS)
    return joined, params


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

    - 既定(名指し無し): 該当文書を全部 docs から読んで並べ替える。
      費用 ≒ `total` 行の読み出し。offset には依らない。
    - `INDEXED BY idx_docs_rank`: 並び順そのものを持つ索引を上から走査し、
      該当を `need` 件(= offset + limit)拾った時点で打ち切る(doc_id の判定は
      ブルームフィルタで索引の中だけで済む)。該当が索引に一様に散らばっていれば
      費用 ≒ `doc_count * need / total` 件の走査。total には依らず、深い頁ほど伸びる。

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
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
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
        src, feature=feature, area=area, bbox=bbox, wikidata=wikidata, tag=tag,
        include_removed=include_removed,
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
            src, feature=feature, area=area, bbox=bbox, wikidata=wikidata, tag=tag,
            include_removed=include_removed,
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


@app.get("/v1/{source}/recent")
def recent_docs(
    request: Request,
    source: str,
    since: str | None = Query(
        None, description="この時刻**以降**だけ(ISO8601。updated_at と同じ書き方)"
    ),
    limit: int = Query(RECENT_LIMIT_DEFAULT, ge=1, le=RECENT_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    fields: str | None = None,
    max_chars: int = Query(0, ge=0),
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
):
    """**新しい順**に文書を並べる(溜まっていくソース向け)。

    `search` は語が要り、`filter` はタグや属性での絞り込みなので、
    **「この 1 日で何が入ったか」を引く手段がこれまで無かった**。集めたものを
    読む側(重要なものを選ばせる、通知の候補にする)はまずこれを訊く。

    **持っていないソースは 409 で断る**(`require_recency_index`)。`updated_at` の
    索引はコアスキーマに無いので、黙って通すと `docs` の全走査になり、しかも
    ダンプ由来のソースでは並べても意味を成さない(記事の版や取り込み時刻なので)。

    `since` は**その時刻を含む**。時刻は秒までしか持たないので、「より後」にすると
    **同じ秒に入ったものを黙って落とす** —— 取りこぼすより、同じものが 1 件返るほうが
    後から直せる。読む側は応答の `updated_at` を次の `since` に渡し、`doc_id` で
    重複を落とす(並びが `updated_at` の降順・`doc_id` の降順で固定なので、
    突き合わせは 1 件見るだけで済む)。
    """
    src = get_source(request, source)
    require_recency_index(src)
    field_list = parse_fields(fields, RECENT_DEFAULT_FIELDS, FILTER_ALLOWED_FIELDS)
    where, params = "", []
    if since:
        where = " WHERE updated_at >= ?"
        params.append(since)
    # **消えたものを外す**。`WHERE` がまだ無いときは自分で立てる
    hidden, hidden_params = removed_clause(src, include_removed)
    if hidden:
        where += hidden if where else " WHERE 1=1" + hidden
        params.extend(hidden_params)
    rows = db.query(
        src.path,
        f"SELECT {', '.join(field_list)} FROM docs INDEXED BY idx_docs_updated{where}"
        " ORDER BY updated_at DESC, doc_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "source": src.name,
        "since": since,
        "limit": limit,
        "offset": offset,
        "docs": [doc_response(r, field_list, max_chars) for r in rows],
    }


class SourceQuery(BaseModel):
    """ソースへ流す SQL(読むだけ)。"""

    sql: str = PydField(
        description="SELECT だけ。1 文だけ。LIMIT は付けなくてよい(こちらで付ける)",
    )
    limit: int = PydField(
        db.SELECT_LIMIT_DEFAULT,
        ge=1,
        le=db.SELECT_LIMIT_MAX,
        description="返す行数の上限",
    )


@app.post("/v1/{source}/query")
def query_source(request: Request, source: str, body: SourceQuery):
    """**SQL でソースを直に引く**(読むだけ)。

    `search` / `filter` では数えられないことのための口 —— **タグの共起、期間ごとの
    件数、上位 N**。どれも「集めたものから別の見方を育てる」ときに要るもので、
    道具の側に無いと、引く側が全件を持ち帰って自分で数えることになる。

    **通すのは SELECT(と WITH …… SELECT)だけ。** 接続は読み取り専用で開いており、
    加えて authorizer が SELECT 以外を落とす —— とくに `ATTACH`(読み取り専用でも
    別のファイルは足せるので、ソースでない DB を開かれる道が残る)。

    **1 文だけ。** 複数文を許すと、前半で条件を作って後半で別のことをする書き方が通る。

    主な表(どのソースも同じ形):

    - `docs(doc_id, title, opening, body, tags, links, updated_at, rank_score, extra)`
      —— `tags` / `links` / `extra` は JSON。`feature` / `area` / `lat` / `lon` /
      `wikidata` は `extra` からの生成列
    - `doc_tags(tag, doc_id)` —— タグの転置表。**共起を数えるならここ**
    - `tag_counts(tag, docs)` —— タグごとの文書数(集計済み)
    - `aliases(alias, doc_id)` / `doc_coords(lat, lon, doc_id)`
    - `docs_fts(title, body)` —— 全文検索(`MATCH` で引く)
    """
    src = get_source(request, source)
    sql = (body.sql or "").strip().rstrip(";").strip()
    if not sql:
        raise HTTPException(400, {"error": "sql を入れてください"})
    if ";" in sql:
        raise HTTPException(400, {
            "error": "sql は 1 文だけにしてください",
            "hint": "前半で条件を作って後半で別のことをする書き方を通さないため",
        })
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise HTTPException(400, {
            "error": "sql は SELECT(または WITH … SELECT)だけです",
            "hint": "この口は読むだけ。書き換えは収集の側(AI が返した文書)からしか起きない",
        })
    try:
        columns, rows, truncated = db.select(src.path, sql, body.limit)
    except db.QueryTimeout:
        raise HTTPException(504, {
            "error": "時間内に終わりませんでした",
            "hint": "絞り込みを足すか、LIMIT を小さくしてください",
        }) from None
    except sqlite3.OperationalError as e:
        # **エラーはそのまま返す。** 握り潰すと、書いた側は列名が違うのか
        # 表が無いのかを確かめようがない(AI はこれを読んで自分で直す)
        raise HTTPException(400, {"error": str(e)}) from None
    return {
        "source": src.name,
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "count": len(rows),
        "truncated": truncated,
    }


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
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
):
    src = get_source(request, source)
    hidden, hidden_params = removed_clause(src, include_removed)
    rows = db.query(
        src.path,
        "SELECT doc_id, title FROM docs WHERE title LIKE ? ESCAPE '\\'"
        f"{hidden} ORDER BY rank_score DESC, title LIMIT ?",
        (escape_like(prefix) + "%", *hidden_params, limit),
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
    include_removed: bool = Query(False, description="消えたもの(_chiezo_removed)も含める"),
):
    src = get_source(request, source)
    hidden, hidden_params = removed_clause(src, include_removed)
    rows = db.query(
        src.path,
        f"SELECT doc_id, title FROM docs WHERE 1=1{hidden} ORDER BY RANDOM() LIMIT ?",
        (*hidden_params, limit),
    )
    return {"source": source, "results": [dict(r) for r in rows]}


# ---- notes(短期記憶。唯一書き込めるソース) ----------------------------------
#
# 実体は app/notes.py。ここは HTTP の口だけを持つ。`CHIEZO_NOTES_DIR` 未設定なら 503。
# 読み出しは専用の recall のほかに、コアスキーマなので /v1/chiezo_memory/search・doc・filter・
# tags・/notes/ のブラウズ画面もそのまま効く(ソース種別を意識しない設計のおかげ)。


# **書き込む口もソース名で出す。** 読む口(`/v1/{source}/…`)はソース名で決まるので、
# ここだけ別の名前にすると、1 つの置き場が 2 つの名前で並ぶ。
@app.post("/v1/chiezo_memory")
def remember(
    request: Request,
    text: str = Body(..., embed=True, min_length=1, description="覚えておく内容"),
    title: str | None = Body(None, embed=True, description="省略時は本文の 1 行目から作る"),
    tags: str | None = Body(None, embed=True, description="カンマ区切り"),
    extra: dict | None = Body(
        None, embed=True, description="タグで表せない構造(並び順など)。省略時は持たない"
    ),
):
    created = notes.add(text=text, title=title, tags=tags, extra=extra)
    # 作られたばかり(初回の追記)ならソースとして登録し直す。件数は読む側が
    # 数え直すので、ここでは触らない(notes.count() 参照)。
    if notes.SOURCE_NAME not in request.app.state.sources:
        request.app.state.sources = scan_all(request.app.state.data_dir)
    return created


@app.get("/v1/chiezo_memory/recall")
def recall_notes(
    request: Request,
    q: str | None = Query(None, description="全文検索。省略すると時系列だけで引く"),
    since: str | None = Query(None, description="この日時以降(例 2026-07-31)"),
    until: str | None = Query(None, description="この日時以前"),
    tag: str | None = Query(None, description="タグで絞る。カンマ区切りで AND"),
    limit: int = Query(notes.RECALL_LIMIT_DEFAULT, ge=1, le=notes.RECALL_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    fields: str | None = Query(
        None,
        description=(
            f"返す項目。カンマ区切り。省略時は {','.join(notes.RECALL_FIELDS)}。"
            f"名指ししたときだけ返るもの: {','.join(notes.RECALL_OPTIONAL_FIELDS)}"
        ),
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


@app.patch("/v1/chiezo_memory/{doc_id}")
def update_note(
    request: Request,
    doc_id: int,
    text: str | None = Body(None, embed=True, description="本文を差し替える。省略は今のまま"),
    title: str | None = Body(None, embed=True, description="見出しを差し替える。省略は今のまま"),
    tags: str | None = Body(
        None, embed=True, description="カンマ区切りで丸ごと置き換え。空文字で全部外す。省略は今のまま"
    ),
    extra: dict | None = Body(
        None, embed=True, description="丸ごと置き換え。空の dict で外す。省略は今のまま"
    ),
):
    updated = notes.update(doc_id, text=text, title=title, tags=tags, extra=extra)
    if updated is None:
        raise HTTPException(404, {"error": f"note not found: doc_id={doc_id}"})
    return updated


@app.delete("/v1/chiezo_memory/{doc_id}")
def forget(request: Request, doc_id: int):
    if not notes.delete(doc_id):
        raise HTTPException(404, {"error": f"note not found: doc_id={doc_id}"})
    return {"deleted": doc_id}


# ---- 集める(AI に集めさせて溜めていく。既定では無効)-------------------------
#
# 実体は `app/collect.py`。**溜め先は notes と別のソース**で、収集ごとに 1 つ持つ。
# 定義は notes の 1 件に JSON でまとめ、時計は上の `_run_collections` が回す。


class CollectionCreate(BaseModel):
    name: str = PydField(description="ソース名になる。英小文字・数字・_")
    prompt: str = PydField(description="AI へ渡す本文。{cursor} が今の進み具合に置き換わる")
    interval_minutes: int = 60
    description: str = ""
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    web: bool = True
    keep_ratio: float | None = PydField(
        None,
        description="作り直しで前世代の何割を下回ったら断るか(0 で守りを外す)",
    )
    requested_by: str = PydField("", description="依頼元の名乗り(画面に出る手がかり)")
    extract: dict | list[dict] | None = PydField(
        None,
        description="最初の 1 回を AI ではなく機械的に埋める指定"
        "(どのソースの・どのタグを・何件・タグをどう読み替えるか)。"
        "進み具合が空のときだけ使い、2 回目からは AI が肉付けする。"
        "**配列で書くとソースをまたいで 1 つの名簿になる**"
        "(同じ見出しが複数のソースに居たら畳む。どの本の値を採るかは項目ごとで、"
        "その項目を勝ちにいくと書いた本(`provides`)のうち先頭のものが入る。"
        "空いたところは書いた順にどの本からでも埋める)",
    )
    kind: str | None = PydField(
        None,
        description="収集の種類。flow(流れ。時とともに増える流れを追い、古いものは"
        "順に落とす)か stock(網羅。ある括りの全部を集めて精査し続ける。既定)",
    )
    keep_days: int | None = PydField(
        None,
        description="流れの収集が持つ日数(既定 30)。0 なら期限では落とさない。"
        "網羅では使わない",
    )
    verify_tags: list[dict] | None = PydField(
        None,
        description="タグの値が実在するかを確かめる指定。"
        '[{"prefix": "代表作", "source": "jawiki"}] と書くと、'
        "`代表作:<見出し>` の見出しがそのソースに無いタグを焼く前に落とす",
    )
    feed: dict | None = PydField(
        None,
        description="外向きの道具(RSS / Atom)。urls に取ってくる先を書く。"
        "**取ってきたものをそのまま溜めるわけではない** —— プロンプトの {feed} へ"
        "参考として差し込むだけで、何を溜めるかは AI が決める(自分でも調べる)",
    )
    material: dict | None = PydField(
        None,
        description="**材料に読む別のソース**。"
        '{"source": "tazuna_tech", "tag": "ニュース,記事", "limit": 60} と書くと、'
        "プロンプトの {material} へ**前回この巡回が走ってから**そのソースに入ったものが"
        "差し込まれる。**中身は写さない**(読むだけ) —— 集めたものを材料にして、"
        "別の見方を別の収集に育てるためのもの",
    )
    sweeps: list[dict] | None = PydField(
        None,
        description="巡回。**同じ収集を別々の時計で回す**ためのもので、"
        "ざっと全体を拾うもの(cover_days に一周の日数)と、少数をじっくり調べるもの"
        "(partitions_per_run と強いモデル)を分けて持てる。"
        "書かなければ、interval_minutes で 1 本だけ回る",
    )
    partition: dict | None = PydField(
        None,
        description="回る先の割り方(by=geo / tag / title、target、母集団の source など)。"
        "**割るのは対象としている空間**なので、まだ 1 件も集めていない範囲にも区画ができる。"
        "プロンプトの {partition} に今回見る範囲が差し込まれる",
    )


class CollectionPatch(BaseModel):
    """渡した項目だけ差し替える(未指定は触らない)。

    **`enabled` はここに無い。** 動かすかどうかは Chiezo 側だけが決める
    (`/admin` の「有効にする」)—— 置いてしまうと、外のアプリが自分で作った収集を
    自分で動かせることになり、「依頼」を分けた意味が消える。
    """

    description: str | None = None
    prompt: str | None = None
    interval_minutes: int | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    web: bool | None = None
    cursor: str | None = None
    keep_ratio: float | None = None
    extract: dict | list[dict] | None = PydField(
        None, description="抽出の指定(配列で複数ソース)。空のオブジェクトを渡すと外れる"
    )
    verify_tags: list[dict] | None = PydField(
        None, description="タグの値が実在するかを確かめる指定。空の配列を渡すと外れる"
    )
    kind: str | None = PydField(None, description="収集の種類(flow / stock)")
    keep_days: int | None = PydField(None, description="流れの収集が持つ日数。0 で落とさない")
    feed: dict | None = PydField(
        None, description="外向きの道具。空のオブジェクトを渡すと外れる"
    )
    material: dict | None = PydField(
        None, description="材料に読む別のソース。空のオブジェクトを渡すと外れる"
    )
    partition: dict | None = PydField(
        None,
        description="区画の割り方。空のオブジェクトを渡すと外れる。"
        "**割り方を変えると台帳は作り直す**(鍵の意味が変わるため)",
    )
    partitions: list[dict] | None = PydField(
        None,
        description=(
            "区画の台帳。空の配列を渡すと割り直して最初から回り直せる"
            "(2 周目を粗いまま繰り返させず、精度を上げて回り直したいときに使う)"
        ),
    )
    sweeps: list[dict] | None = PydField(
        None,
        description="巡回。**同じ収集を別々の時計で回す**ためのもので、"
        "ざっと全体を拾うもの(cover_days に一周の日数)と、少数をじっくり調べるもの"
        "(partitions_per_run と強いモデル)を分けて持てる。"
        "空の配列を渡すと、上の interval_minutes で 1 本だけ回る形に戻る",
    )


@app.get("/v1/ingest/status")
def ingest_status():
    """いま取り込みが走っているか。**外のアプリが押す前に判断できるように**。

    集めるのも焼くのも取り込みの中で起きるので、走っている間に「いま集めて」と
    頼んでも 409 で断られる。押してから断られるのと、押せないことが見えているのとでは
    別物なので、状態のほうを配る。

    **返すのは状態と対象だけ**(ログの中身は返さない。管理画面から読めれば足りるうえ、
    取り込みのログには置き場のパスのような内部の事情が混ざる)。
    """
    from app.views.admin import TRIGGER_URL, _fetch_trigger_status

    if not TRIGGER_URL:
        raise HTTPException(503, {
            "error": "chiezo-trigger が設定されていません(CHIEZO_TRIGGER_URL 未設定)",
        })
    status = _fetch_trigger_status() or {}
    state = status.get("state") or "unknown"
    return {
        "state": state,
        "running": state == "running",
        "source": status.get("source"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
    }


@app.get("/v1/collect")
def collect_list():
    """収集の一覧。**間隔・次にいつ走るか・溜まった件数**まで返す。

    3 つ揃って初めて「動いているか」が判断できるので、一覧の時点で載せる
    (件数だけ別の口にすると、画面が収集の数だけ問い合わせることになる)。
    """
    collect.require_enabled()
    return {
        "collections": [collect.to_public(c, with_partitions=False) for c in collect.load()]
    }


@app.post("/v1/collect")
def collect_create(request: Request, body: CollectionCreate):
    """収集を**依頼する**(外のアプリからも呼べる)。

    **作られるのは必ず止めた状態**で、動き出すのは Chiezo の管理画面で
    「有効にする」を押したときだけ。**呼んだだけで AI が動き出さない**ことが、
    この口を外へ開けておける理由 —— 悪意が無くても、試しに叩いただけで
    定期実行が始まってしまうのは困る。`requested_by` に名乗ってもらうと、
    有効にするか決める人の手がかりになる(印であって認証ではない)。
    """
    collect.require_enabled()
    item = collect.create(
        name=body.name.strip(),
        prompt=body.prompt,
        interval_minutes=body.interval_minutes,
        description=body.description,
        backend=body.backend,
        model=body.model,
        effort=body.effort,
        web=body.web,
        keep_ratio=body.keep_ratio,
        extract_spec=body.extract,
        kind=collect.normalize_kind(body.kind),
        keep_days=body.keep_days,
        verify_tags=body.verify_tags,
        partition_spec=body.partition,
        feed_spec=body.feed,
        material_spec=body.material,
        sweeps=body.sweeps,
        requested_by=body.requested_by,
    )
    # 作った時点で空の DB ができる。**ここでソースを取り直さないと、1 回目が走るまで
    # `/v1/<name>/search` が 404 になる**(まだ登録されていないソースに見える)
    request.app.state.sources = scan_all(request.app.state.data_dir)
    return collect.to_public(item)


# **固定のパスは `/{name}` より先に宣言する。** 後ろに置くと `sources` や `fetch` が
# 名前として解釈され、「収集「sources」がありません」で 404 になる。
@app.get("/v1/collect/sources")
def collect_sources_catalog():
    """焼ける収集の一覧(ingest が引くカタログ)。

    取り込み側のプラグイン契約(`ingest/sources/remote.py`)。**無効なら空で返す** ——
    404 にすると、収集を使っていない構成で ingest 側が毎回エラーを踏む。
    """
    return {"sources": collect.catalog()}


@app.get("/v1/collect/fetch")
async def collect_fetch(request: Request, source: str = Query(..., description="焼く収集の名前")):
    """焼く素材(NDJSON)。**ここで AI に集めさせる。**

    取り込みの中から呼ばれるので、集めた瞬間に焼かれる —— 途中に置き場が要らない。
    返すのは**前世代 + いま集めたぶん**で、前世代を混ぜるのが「毎回焼き直すのに
    積み上がる」の要。返す形は `ingest/sources/remote.py` が読む形。

    **AI の応答ぶん待たせる**(十数秒〜数分)。取り込み側は待つ前提で作られている
    (ダンプのダウンロードも同じくらいかかる)。
    """
    collect.require_enabled()
    lines = await collect_material(source, request.app.state.sources)

    def flow():
        for line in lines:
            yield line.encode() + b"\n"

    # **1 行ずつ流す。** 丸ごと組んでから返していた頃は、50 万件の名簿で 1 本の
    # 文字列が 460 MB になった —— 取り込み側は元から流し込みで受けている
    # (`ingest/sources/remote.py` が `copyfileobj` でそのままファイルへ落とす)。
    # **断るのは流し始める前に済ませてある**(`collect.bake_survey`)
    return StreamingResponse(flow(), media_type="application/x-ndjson")


@app.post("/v1/collect/{name}/preview")
async def collect_preview_now(
    request: Request,
    name: str,
    sweep: str | None = Query(None, description="どの巡回のつもりで試すか(省くと次に走るはずのもの)"),
):
    """**焼かずに**1 回集めさせて、前世代との差分だけ返す。

    **止めている収集は断る**(`run` と同じ)。焼かないとはいえ AI は 1 回動くので、
    外のアプリが自分で作った収集を自分で回せる状態にはしない。
    **管理画面の「ドライラン」はこの口を通さない**ので、有効にする前の
    試し撃ちはそちらからできる。

    長期記憶には一切書かず、カーソルも次回の予定も動かさない。
    """
    collect.require_enabled()
    if not collect.get(name).enabled:
        raise HTTPException(403, {
            "error": f"収集「{name}」は止まっています",
            "hint": "動かすかどうかは Chiezo 側で決めます(管理画面の「有効にする」)",
        })
    return await collect_preview(name, request.app.state.sources, sweep)


# **`/{name}` より先に置く。** 後ろに置くと `changes` が収集の名前として解釈される
# (`/v1/collect/sources` と同じ罠)。
@app.get("/v1/collect/changes")
def collect_changes(
    name: str | None = Query(None, description="収集の名前。省略すると全部の収集"),
    limit: int = Query(50, ge=1, le=500),
    sweep: str | None = Query(None, description="巡回の名前。省略すると全部の回"),
):
    """**直近どこに修正が入ったか**(`app/collect_log.py`)。

    定義側の控え(`last_added` など)は**最新の 1 回で上書きされる**ので、
    「減り続けているのか、ある日だけ荒れたのか」はここでしか読めない。

    **回で絞れる**(`sweep`)。間隔は回ごとに桁違いなので、新しい順に並べるだけでは
    短い回が長い回を押し流す —— 1 時間ごとの回が並びを埋めると、4 時間ごとの回の
    差分が残らない。

    **記録の置き場(`CHIEZO_STATE_DIR`)が無ければ空で返す。** 404 にすると、
    控えを持たない構成で読む側が毎回エラーを踏む(`/v1/collect/sources` と同じ判断)。
    """
    collect.require_enabled()
    return {"changes": collect_log.recent(name, limit, sweep)}


@app.get("/v1/collect/{name}/partition")
def collect_partition(
    request: Request,
    name: str,
    key: str = Query(..., min_length=1, description="区画の鍵(`/v1/collect/{name}` の partitions)"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """**その区画に入っているものを全部**返す。

    区画は「この範囲の全員」を並べて漏れを問う単位なので、中身を引けないと
    その問いが成り立たない —— 差し込み(`{current}`)でしか見えなかったころは、
    **1 回の依頼に載る量が上限**だった(入り切らないぶんは黙って落ちる)。
    ここから引けば、AI が必要なだけ自分で辿れる。

    **消えたものも返す**(`_chiezo_removed` が付いたまま)。何を外したのかが
    分からないと、同じものをもう一度挙げることになる —— 読み口の既定と違うのは、
    ここが「集める層のための口」だから。

    区画を持たない収集では 400 で断る(鍵の意味が無い)。
    """
    collect.require_enabled()
    item = collect.get(name)
    if not item.partition:
        raise HTTPException(400, {
            "error": f"収集「{name}」は区画を持っていません",
            "hint": "区画の割り方は PATCH の partition で設定します",
        })
    # **全文書は読まない**(`partition_docs` が鍵から範囲を作って SQL で絞る)——
    # 読んでいた頃は、区画を 1 つ引くのに本番で 48.9 秒かかっていた
    members = collect.partition_docs(item, request.app.state.sources, key)
    ordered = sorted(members.values(), key=lambda d: d["title"])
    return {
        "name": name,
        "key": key,
        "describes": collect.describe_partition(item, key, request.app.state.sources),
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        "docs": [
            {
                "title": d["title"],
                "body": d.get("body") or "",
                "tags": d.get("tags") or [],
                "updated_at": d.get("updated_at") or "",
                "extra": d.get("extra") or {},
            }
            for d in ordered[offset : offset + limit]
        ],
    }


@app.get("/v1/collect/{name}")
def collect_get(
    request: Request,
    name: str,
    samples: int = Query(5, ge=0, le=50),
    include_removed: bool = Query(
        False,
        description=(
            "消えたもの・まだ AI が目を通していないものも見本に含める。"
            "既定は含めない(読む側が自分で落とさなくて済むように)"
        ),
    ),
):
    """1 つぶんの設定と、**焼いてあるもののうち新しい数件**。

    見本を添えるのは、プロンプトを直すかどうかの判断に「実際に何が集まったか」が
    要るため。途中の置き場を持たないので、見に行く先は長期記憶になる。

    **見本も既定で印の付いたものを外す**(`collect.recent`)。ここだけ外していな
    かったせいで、**読む側が自分で落とすことになっていた** —— 名前は他の読み口と
    そろえて `include_removed`(語彙を 2 つにしない)。
    """
    collect.require_enabled()
    item = collect.get(name)
    return {
        **collect.to_public(item),
        "recent": collect.recent(
            name, request.app.state.sources, samples, include_removed
        ),
    }


@app.patch("/v1/collect/{name}")
def collect_patch(name: str, body: CollectionPatch):
    collect.require_enabled()
    return collect.to_public(collect.update(name, **body.model_dump(exclude_none=True)))


@app.delete("/v1/collect/{name}")
def collect_delete(name: str):
    """定義を消す。**溜めたものは残る**。

    **外のアプリに、溜まったものを消す手段は渡さない** —— 消すのは別の意思決定で、
    しかも取り消せない。まとめて消したいときは管理画面の「削除」を使う
    (画面を開けるのは Chiezo を操作している人だけ、という前提の差)。
    """
    collect.require_enabled()
    collect.remove(name)
    return {"ok": True}


class ExtractDraft(BaseModel):
    """依頼文から抽出の指定を書かせる。`name` を渡すとその収集の相手といまの指定を踏まえる。"""

    want: str = PydField("", description="どういう条件で抽出してほしいかを、ふつうの言葉で")
    name: str | None = PydField(None, description="直したい収集の名前(相手と現在の指定を使う)")
    # **収集を作る前にも書かせる**ので、そのときは名前で相手を引けない。
    # 指定を書くのは道具を何度も引く仕事なので、遅い相手だと十数分待つことになる
    backend: str | None = PydField(None, description="書かせる相手(未指定なら Chiezo の既定)")
    model: str | None = PydField(None, description="モデル(未指定なら相手の既定)")
    effort: str | None = PydField(None, description="考える量(未指定なら相手の既定)")


@app.post("/v1/collect/draft-extract")
async def collect_draft_extract(request: Request, body: ExtractDraft):
    """依頼文を**抽出の指定**にして返す。**保存はしない**。

    ここだけ AI が要る。指定さえできれば以降は AI を呼ばずに同じ結果が出るので、
    **決定的なのは指定のほうで、AI は書き起こす係**にすぎない。

    書けたら**その場で引いてみて、何件あるか・最初の数件がどうなるかを添えて返す** ——
    タグは完全一致でしか引けないので、それらしい名前を書かれると静かな 0 件になる。
    0 件のときは実在するタグ名を候補として返す(書いた本人には確かめようがない)。
    """
    collect.require_enabled()
    if not body.want.strip():
        raise HTTPException(400, {"error": "want(集めたいもの)を入れてください"})
    sources = request.app.state.sources
    item = collect.get(body.name) if body.name else None
    current = item.extract if item else None
    base_settings = item or collect.Collection(
        name="", description="", prompt="", interval_minutes=60, enabled=False,
        backend=None, model=None, effort=None, web=False, cursor="",
        created_at="", updated_at="",
    )
    # 名指しがあればそちらを使う(収集を作る前は、名前で相手を引けない)
    named = {
        key: value
        for key, value in (("backend", body.backend), ("model", body.model), ("effort", body.effort))
        if value
    }
    # 指定を書くのに外は要らない(引く先は手元の長期記憶)
    settings = replace(base_settings, **named, web=False)
    content, _who, _model = await _ask_for_collection(
        settings, extract.build_draft_messages(body.want, sources, current)
    )
    drafted = extract.parse_draft(content or "")

    # **「機械では引けない」も答えのうち。** 引けないものを無理に指定へ落とすと、
    # 当たらないタグで静かな 0 件になる。頼んだ側は今までどおり AI に集めさせればよい
    if drafted.get("extract", drafted) is None:
        reason = str(drafted.get("reason") or "").strip()
        log.info("draft extract: 機械では引けないと判断された: %s", reason)
        return {"extract": None, "reason": reason, "total": 0, "matched": 0, "sample": []}

    spec = extract.normalize(drafted.get("extract") if "extract" in drafted else drafted)
    probed = await asyncio.to_thread(_probe, spec, sources)

    # 足りなければ、**実在するタグを見せて 1 度だけ選び直させる**。タグ名は手元に
    # しか無いので、書く側は当てるしかない —— 「画家」のような一般名は実在するが
    # 数件しか付いておらず、欲しいものは「19世紀フランスの画家」の側にある。
    # 選び直しても増えなければ、最初の指定のほうを返す(悪くしない)
    if probed["candidates"]:
        content, _who, _model = await _ask_for_collection(
            settings,
            extract.build_retry_messages(body.want, spec, probed["total"], probed["candidates"]),
        )
        try:
            retried = extract.normalize(extract.parse_draft(content or ""))
            reprobed = await asyncio.to_thread(_probe, retried, sources)
        except HTTPException as e:
            log.warning("draft extract retry refused: %r", e.detail)
            retried, reprobed = None, None
        if retried and reprobed["total"] > probed["total"]:
            spec, probed = retried, reprobed

    return {"extract": extract.to_json(spec), **probed}


def _probe(spec: dict, sources: dict) -> dict:
    """書けた指定を実際に引いてみる。**保存する前に空振りが分かる**ようにする。

    候補は取れた数に関わらず添える —— 0 件だけが失敗ではない。それらしい一般名を
    書くと「実在はするが数件しか付いていないタグ」に当たり、静かに痩せた図になる。
    """
    roster, _cursor = extract.run(spec, sources)
    try:
        # **下見なので読み切る。** ここは人が待っている道で、書けた指定が空振りか
        # どうかを見るためのもの —— 引く件数は指定の側で絞られている
        items = list(roster)
    finally:
        if hasattr(roster, "close"):
            roster.close()
    matched = extract.count(spec, sources)
    # **当たっている数も返す。** 取った数だけ見せると、絞られていることに気づけない
    # (614 件に当たっているのに 30 件返っても、見ている側には分からない)
    return {
        "total": len(items),
        "matched": matched,
        "sample": items[:3],
        "candidates": extract.similar_tags(spec, sources) if extract.looks_thin(spec, len(items)) else [],
    }


class CollectionDraft(BaseModel):
    """プロンプトの相談。`name` を渡すとその収集の相手・いまの指示文を踏まえて直す。"""

    want: str = PydField("", description="集めたいものをふつうの言葉で")
    current: str = PydField("", description="いまの指示文(直したいとき)")
    feedback: str = PydField("", description="どう直したいか")
    name: str | None = None


@app.post("/v1/collect/draft")
async def collect_draft(body: CollectionDraft, request: Request):
    """AI に収集の指示文を書いてもらう(**保存はしない**)。

    決まり(返させる JSON の形・`{cursor}` の使い方・title が重複の鍵)を
    こちらが system で教えるので、頼む側は「何を集めたいか」だけ書けばよい。
    """
    collect.require_enabled()
    if not (body.want.strip() or body.name):
        raise HTTPException(400, {"error": "want か name のどちらかが要ります"})
    draft = await draft_collection_prompt(body.want, body.current, body.feedback, body.name)
    return {"prompt": draft}


class CollectionFocus(BaseModel):
    """割り込み —— 「ここが間違っているから直して」を、巡回とは別の道で頼む。"""

    note: str = PydField(
        description="どう直してほしいか。**これが無いと受け付けない** —— "
        "何をどう直すかが書かれていない割り込みは、1 回ぶんの AI の呼び出しに"
        "しかならない(巡回でやれば済む)",
    )
    titles: list[str] = PydField(
        default_factory=list,
        description="直してほしい見出し。**名指ししたものは必ず AI に見せる** —— "
        "区画を渡すだけでは、直してほしい 1 件が差し込みに載る保証がない",
    )
    partition: str | None = PydField(
        None, description="見てほしい区画。省くと、名指ししたものだけを見る"
    )
    prompt: str | None = PydField(
        None,
        description="**その回だけの依頼文**(差し込み口 `{cursor}` などはいつもどおり効く)。"
        "**保存しない** —— 定義のプロンプトは育てながら使うもので、1 回きりの"
        "頼みごとで書き換わると、次の定時の回が知らない文で走ることになる",
    )
    sweep: str | None = PydField(
        None,
        description="どの巡回の設定(相手・モデル・考える量)で走らせるか。"
        "省くと、時計を持たない巡回(`on_demand`)がある収集ではそれを使う",
    )
    requested_by: str = PydField("", description="依頼元の名乗り(画面に出る手がかり)")


def start_focus_bake(name: str, raw: dict) -> dict:
    """割り込みを 1 本起こす。**予定は進めない**。

    `start_collection_bake` と分けてあるのはここ 1 点のため —— あちらは次回の予定を
    進める(それが定時の巡回の時計)。割り込みで進めると、頼むたびに一周が伸びる。

    **控えるのが先、起こすのが後。** 逆にすると、起こされた取り込みが素材を取りに来た
    ときにまだ依頼が書かれておらず、その回はふつうの巡回として走る —— 依頼は残るので、
    **次の定時の回を乗っ取る**。押した人からは「押した瞬間に巡回が前倒しで動いただけ」
    に見え、頼んだものはいつまでも走らない(実際にそうなった)。

    **起こせなかったら取り下げる。** 残すと、次に走る定時の回が割り込みとして走る。
    """
    from app.views.admin import TRIGGER_URL, trigger_run

    # **依頼が読めるかを最初に見る。** 起こしてから断ると、指示文の無い依頼のために
    # 1 本ぶんの取り込みが走る(そして何も直らない)。サーバーの設定より先に見るのは、
    # 読めない依頼は設定がどうであれ読めないから —— 理由を取り違えさせない
    focus = collect.require_focus(raw)
    if not TRIGGER_URL:
        raise HTTPException(503, {
            "error": "chiezo-trigger が設定されていません(CHIEZO_TRIGGER_URL 未設定)",
            "hint": "集めるのも焼くのも取り込みの中で起きるので、trigger が要る",
        })
    collect.request_focus(name, focus)

    try:
        trigger_run(name)
    except Exception:
        collect.clear_focus(name)
        raise

    return {"name": name, "focus": focus.to_json()}


@app.post("/v1/collect/{name}/focus")
def collect_focus_now(name: str, body: CollectionFocus):
    """**この部分を集中的に直して**、を割り込ませる。**止めている収集は断る**。

    **定時の巡回に影響を出さない**のが約束 —— 進み具合(`cursor`)も、どの巡回の
    予定も、区画の巡回記録も動かさない。動くのは中身だけ。動かすと、割り込むたびに
    一周が伸びたり、見ていない区画に印が付いたりする。

    **必ず「直す」側で走る**。足すだけの収集でも、名指しで渡された 1 件を直せなければ
    割り込みの意味が無い。

    **返るのは「起こした」まで**(`run` と同じ)。取り込みは向こうで走る。
    """
    collect.require_enabled()
    if not collect.get(name).enabled:
        raise HTTPException(403, {
            "error": f"収集「{name}」は止まっています",
            "hint": "動かすかどうかは Chiezo 側で決めます(管理画面の「有効にする」)",
        })
    return start_focus_bake(name, body.model_dump())


@app.post("/v1/collect/{name}/run")
def collect_run_now(
    request: Request,
    name: str,
    sweep: str | None = Query(None, description="走らせる巡回の名前(省くと次に走るはずのもの)"),
):
    """予定を待たずに 1 回、集めて焼く。**止めている収集は断る**。

    有効にしていないものをここから走らせられると、`enabled` を REST から
    触れなくした意味が無くなる(呼ぶたびに 1 回ぶんの AI が動く)。
    **管理画面の「今すぐ実行」はこの口を通さない**ので、有効にする前の試し撃ちは
    そちらからできる —— 画面を開けるのは Chiezo を操作している人だけ、という前提。

    **返るのは「起こした」まで**。取り込みは向こうで走るので、進み具合は
    管理画面(または chiezo-trigger の `/status`)で見る。

    **巡回を名指しできる。** 相手も 1 回に見る量も巡回ごとに違うので、
    「じっくりのほうを今すぐ 1 回」が頼めないと、名指しした意味が半分になる。
    **時計を持たない巡回は断る**(割り込みで頼まれたときだけ動くもの)。
    """
    collect.require_enabled()
    if not collect.get(name).enabled:
        raise HTTPException(403, {
            "error": f"収集「{name}」は止まっています",
            "hint": "動かすかどうかは Chiezo 側で決めます(管理画面の「有効にする」)",
        })
    return start_collection_bake(name, sweep)


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

    rag と違い流し始めた後にしかできない仕事が本体(道具を引くこと自体が目的で、
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
# 直前のやり取りが要るので、こちらは messages をまるごと受け取る。
# サーバーは会話の状態を持たない(履歴はクライアントが持って毎回送る)。読み取り専用・
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
    # 「覚える・思い出す」の道具を渡すか。書き込みを伴うので、同じく切れるようにする。
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
# `/v1/chat` とは目的が違う。 あちらは知識ベースを引いて答えるための口で、必ず抽出が
# 混ざる。こちらは渡したプロンプトをそのまま相手に投げるだけ —— 呼び出す側が自分の
# 材料とプロンプトを持っていて、Chiezo に借りたいのは「話せる相手と鍵」だけ、という使い方
# (例: tech-antenna のサマリー生成)。認証情報は相手ごとに Chiezo が握っているので、
# 呼ぶ側は鍵を持たずに済み、管理画面で on にした相手をそのまま使える。


class AiMessage(BaseModel):
    # `/v1/chat` の ChatMessage と違い system を許す。プロンプトを組むのは呼ぶ側で、
    # 役割の付け方までこちらで決めない
    role: str = PydField(pattern="^(system|user|assistant)$")
    content: str


class AiCompleteRequest(BaseModel):
    messages: list[AiMessage]
    # どの相手に投げるか。空なら「先頭の相手」(`/v1/chat` と同じ規則)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    # 相手自身の web 検索を開けるか(既定は開けない)。
    # CLI ブリッジで包んだ相手だけが持つ道具なので、それ以外に頼まれたら断る
    # —— 黙って道具無しで答えさせると、呼ぶ側は「調べた結果」として受け取ってしまう。
    web: bool | None = None
    # **誰が頼んだか**(画面の「依頼元」に出る)。**必須にはしない** —— 名乗りが
    # 無いだけで AI を借りられなくなるのは、この口の値打ち(鍵だけ借りる)に合わない。
    # ただし**無名の依頼は後から追えない**: 待たされている人が「これは自分のか」を
    # 判断できず、枠を食っている相手も特定できない。名乗ってほしい。
    requested_by: str | None = None


@app.get("/v1/ai/backends")
async def ai_backends() -> dict:
    """いま話せる相手と、その相手で選べるモデル・エフォート。

    呼ぶ側が画面を作れるだけの材料を返す。 一覧は管理画面で on にしたものだけで、
    モデルは相手に聞けた場合はその答え(聞けなければコードの控え)。

    **ワーカーも同じ一覧に混ぜる**(`kind`)。収集を外から頼むアプリは、この口から
    作った選択肢を巡回の `backend` に入れて送ってくる —— **一覧に無いものは
    選ばせようがない**ので、混ぜないと「ワーカーに頼む巡回」を外から作れない
    (Chiezo の画面からしか作れない機能になる)。
    **`kind` で見分けられるようにする** —— ワーカーはモデルも考える量も持たず、
    `/v1/ai/complete` の相手にもならない(渡す先はそのときの枠で決まる)。
    """
    names = answer.backend_names()
    # **どちらも `answer` 側を通す**(控えを返すだけなので速い)。
    # 決め打ちを直に読んでいた頃は、ブリッジが名乗る段階も、選んでも効かない相手で
    # 欄を隠す判断も、この口にだけ届いていなかった —— 呼ぶ側の画面と Chiezo 自身の
    # 画面で、並ぶ候補が食い違うことになる
    models, efforts = await asyncio.gather(
        asyncio.gather(*(answer.available_models(name) for name in names)),
        asyncio.gather(*(answer.available_efforts(name) for name in names)),
    )

    return {
        "backends": [
            {
                "id": name,
                "label": answer.backend_label(name),
                # 相手か、ワーカーか。**足したのは後から**なので、書いていない読み手が
                # 今までどおり動くように、相手のほうを既定の値にしてある
                "kind": "backend",
                "models": list(available),
                "efforts": list(levels),
                # モデルを必ず指定しないといけない相手か(false なら「既定」を選べる)
                "model_required": bool(spec.model_required) if spec else True,
                # web 検索を開けるか(`/v1/ai/complete` の `web=true`)。
                # CLI ブリッジの相手は自前の検索を、それ以外は Chiezo の SearXNG を使う。
                # 呼ぶ側はこれを見て「調べさせる仕事に使える相手」を絞れる ——
                # 出さないと、選べてしまってから 400 で断られることになる
                "web": bool(spec and spec.bridge) or websearch.is_enabled(),
            }
            for name, available, levels in zip(names, models, efforts, strict=True)
            for spec in (providers.get(name),)
        ] + _worker_choices()
    }


def _worker_choices() -> list[dict]:
    """相手の一覧へ混ぜるワーカーのぶん(`app/workers.py`)。

    **巡回の `backend` にそのまま入れられる値を返す**(`workers.option_for`)——
    呼ぶ側に「ワーカーは別の欄へ」を覚えさせない。
    **定義が読めなければ何も出さない** —— 相手の一覧まで落とさない。
    """
    try:
        found = workers.load()
    except (ValueError, RuntimeError):
        return []
    return [
        {
            "id": workers.option_for(w.name),
            "label": w.name,
            "kind": "worker",
            # **どちらも持たない。** 渡す先はそのときの枠で決まるので、ここで 1 つ
            # 選んでもどの相手に対する指定なのかが決まらない(段ごとの指定は
            # ワーカーの側が持っている)
            "models": [],
            "efforts": [],
            "model_required": False,
            # 段のどれかが web を開けるなら開ける
            "web": any(
                bool((spec := providers.get(step.backend)) and spec.bridge)
                for step in w.steps
            ) or websearch.is_enabled(),
        }
        for w in found
    ]


@app.get("/v1/ai/bridges")
async def ai_bridges() -> dict:
    """立っている CLI ブリッジと、動かしているイメージのコミット(`build`)。

    **問い合わせるのはこの口を叩いたときだけ。** 管理画面のように描画のたびに
    聞きに行くと、落ちている相手があるだけで重くなる(`/v1/ai/backends` と同じ方針)。
    """
    return {"bridges": await answer.bridge_builds()}


@app.get("/v1/ai/failures")
async def ai_failures(limit: int = 50) -> dict:
    """AI への問い合わせが失敗したときの控え(新しい順)。

    無人の呼び出しのために置いてある。 定期実行が朝に落ちても、その場に居合わせないと
    理由は流れて消える —— 呼んだ側のログに残るのは「llm error 502」のような一行だけで、
    相手が何と言って断ったのかは分からない。ここには相手・モデル・状態・理由と
    プロンプトの大きさが残る。

    中身(プロンプト・応答)は残していない。 呼んだ側の材料がそのまま入るため
    (`app/ai_log.py`)。大きさだけ残すのは、失敗が大きさに寄っているのかを見分けるため。
    """
    return {"failures": ai_log.recent(limit)}


@app.get("/v1/ai/inflight")
async def ai_inflight_now(limit: int = Query(50, ge=1, le=200)) -> dict:
    """いま走っている AI への依頼(会話も生成も)。

    控えの表(`/v1/ai/failures` と使用量)に行が立つのは往復が**終わってから**なので、
    走っている最中は何も見えなかった。頼んだ本人が待っているうちは分かるが、無人で
    回る層が動かしているぶんは、遅いのか止まっているのか呼べてすらいないのかの
    区別が付かない。

    2 つに分けて返す。**素材の持ち主が違う**ためで、片方に寄せると意味が壊れる:

    - `calls` …… 相手との 1 往復(`app/ai_inflight.py`)。始まりに 1 行立て、
      終わったら消える。相手・モデル・種類・開始時刻に加えて、**依頼文そのもの**を
      持つ(`prompt`。長いものは切る) —— 終われば消える表なので、溜め続ける失敗の
      控えとは扱いを変えてある。止めるかどうかは中身を見ないと決められない
    - `jobs` …… 文章と絵と音と動画と読み上げ(`app/media.py`)。こちらは元から job と
      して状態を持っているので、`queued` と `running` をそのまま出す

    **文章の生成は両方に出る。** 中で会話の口を呼ぶので、`jobs` の 1 件に対して
    `calls` にも 1 行立つ。同じものだと分かるよう、`calls` 側は抱えている job の id を
    `job_id` に持つ(直に呼ばれたものは空) —— 数えるときはここで畳むこと。
    画面(`/admin/ai/history`)はそうしている。

    走らせたまま落ちたぶんは期限で消える(`--workers 2` なので、片方が再起動すれば
    走っていた往復は消えて行だけが残る)。
    """
    return {
        "calls": ai_inflight.running(limit),
        "jobs": media.running_jobs(limit),
    }


@app.get("/v1/ai/usage")
async def ai_usage(
    backend: str = Query("", description="1 相手だけ見るとき(既定は全部)"),
    refresh: bool = Query(False, description="相手に聞き直す(既定は控えを返す)"),
) -> dict:
    """各 AI の使用量。相手が言う枠(残りが分かる)と、Chiezo が使ったぶん。

    既定では相手へ問い合わせない(控えてある値を返す)—— この口は画面や
    ダッシュボードが定期的に引くもので、引かれるたびに外へ出ていくと、
    見ているだけで相手のレート制限に当たる。取り直すときだけ `refresh=1`。

    枠を聞ける相手は限られる(`quota.supported`)。聞けない相手にも `spent` は出る
    —— そちらは Chiezo が自分で数えているので、全部の相手で同じ物差しになる。
    ただし Chiezo を通していない利用は入らない(手元の端末で回した CLI など)。
    """
    name = (backend or "").strip().lower()
    # 絵と音だけの相手(ElevenLabs・自前の GPU)もこの表に並ぶので、名前もそちらまで見る
    # —— 画面に出ている行を名指しできないと、その行だけ取り直せない。
    if name and usage.spec_of(name) is None:
        raise HTTPException(404, {"error": f"unknown backend: {name}",
                                  "backends": [r["id"] for r in usage.rows()]})
    if refresh:
        await (usage.refresh(name) if name else usage.refresh_all())

    rows = [r for r in usage.rows() if not name or r["id"] == name]
    return {
        # いつからの数かを添える。書かないと、入れたばかりの環境の「0 回」が
        # 「使っていない」と読めてしまう。
        "recorded_since": usage_store.first_recorded_at(),
        "windows": [w for w, _ in usage.SPENT_WINDOWS],
        "backends": [
            {
                "id": r["id"],
                "label": r["label"],
                "enabled": r["enabled"],
                "billing": r["billing"],
                "quota": r["quota"].as_dict(),
                "spent": {window: value.as_dict() for window, value in r["spent"].items()},
            }
            for r in rows
        ],
    }


@app.post("/v1/ai/complete")
async def ai_complete(body: AiCompleteRequest, request: Request) -> dict:
    """渡されたメッセージをそのまま相手へ投げて、本文を返す(1 往復)。

    道具は既定では渡さない。`web=true` のときだけ相手自身の web 検索を開ける
    —— ニュースの収集のように「いまの外の情報」が要る仕事を、`/v1/chat` の抽出を
    混ぜずに頼めるようにするため。

    web の出し方は相手で変わる:

    - CLI ブリッジで包んだ相手(claude / codex / antigravity)は自前の web 検索を持つ。
      `chiezo_web` を立てるだけでよい(道具の往復が無いぶん速い)
    - API で直に叩く相手(gemini / openai 等)は OpenAI 互換の口に検索の項目が無いので、
      Chiezo が立てている SearXNG を道具として貸す(`agent.complete_with_web`)。
      知識ベースの道具は渡さない —— この口は抽出を混ぜないためにある

    どちらも無理なとき(SearXNG を設定していない)は断る —— 黙って道具無しで答えさせると、
    呼ぶ側はそれを「調べた結果」として受け取り、学習データから作った話が最新の材料として
    保存されてしまう(実際に「取得不可」と答えるべき場面で古い答えが返った)。
    """
    messages = [m.model_dump() for m in body.messages if (m.content or "").strip()]
    if not messages:
        raise HTTPException(400, {"error": "messages must not be empty"})

    cfg = await answer.ensure_model(answer.require_settings(body.backend, body.model, body.effort))

    spec = providers.get(cfg.name)
    via_bridge = bool(spec and spec.bridge)
    want_web = body.web is True
    if want_web and not via_bridge and not websearch.is_enabled():
        raise HTTPException(400, {
            "error": "web search is not available",
            "reason": f"{answer.backend_label(cfg.name)} は自前の web 検索を持たず、"
                      "Chiezo の web 検索も設定されていません",
            "hint": "CHIEZO_WEB_SEARCH_URL に SearXNG を設定するか、"
                    "CLI ブリッジ経由の相手(claude / codex / antigravity)を選んでください",
        })

    # **外のアプリからの依頼**。収集の時計が動かしているぶんと見分けるための印で、
    # 名乗り(`requested_by`)があれば一緒に残す —— 「外のアプリ」とだけ出しても、
    # どのアプリが枠を食っているのかは読めない
    with ai_inflight.called_by(ai_inflight.caller_of("api", body.requested_by or "")):
        if want_web and not via_bridge:
            # SearXNG を道具として貸す(往復あり)。知識ベースの道具は渡さない。
            content = await agent.complete_with_web(cfg, messages)
        else:
            extra = {"chiezo_web": True} if want_web else {}
            content = answer.content_of(await answer.complete_message(cfg, messages, **extra))
    if not content:
        raise HTTPException(502, {"error": "empty response from llm"})

    return {
        "backend": cfg.name,
        "label": answer.backend_label(cfg.name),
        # 実際に使われたモデル。呼ぶ側が「どれが書いたか」を残せるようにする
        "model": cfg.model,
        "effort": cfg.effort,
        # 実際に web を開けたか。 頼まなければ false。呼ぶ側が
        # 「調べさせたつもりで調べていない」を検出できるようにする
        "web": want_web,
        "content": content,
    }


# ---- 絵と音の生成(ゲーム素材などを作る)-------------------------------------
#
# 知識を引くのとは別の仕事だが、口は Chiezo にまとめてある —— クライアント(MCP)の
# 登録先を増やしたくないため。重い処理は例によって別コンテナ(ComfyUI)で、
# 外部サービス(Gemini / OpenAI / ElevenLabs)と選べる。
# 実体は `app/media.py` / `app/media_backends.py`。


class ImageRequest(BaseModel):
    prompt: str
    # 何案かを 1 組として見比べるための名前。同じ名前を付けた依頼が画面で横に並ぶ
    group: str = ""
    # 元にする絵。 前に作った絵のパス(image_status が返すもの)か、届く URL。
    # `edit` は「これを直す(他は変えない)」、`reference` は「これを参考に別のものを
    # 描く(絵柄を合わせ、中身は新しく)」。**両方は渡せない**(言い方が逆になる)
    edit: str = ""
    # **参考は何枚でも渡せる**(文字列 1 本でも配列でも受ける)。役割が分かれることが
    # あるため —— 姿勢の見本と絵柄の見本を同時に渡す、など。**役割の名前は持たない**。
    # 渡した順に相手側のファイル名が決まるので、「1 枚目は姿勢、2 枚目は絵柄」と
    # 依頼文に書けばよい。**直すほう(`edit`)は 1 枚だけ**
    reference: str | list[str] = ""
    # **姿勢の見本（骨組みの絵）。** `edit` / `reference` とは役割が別 ——
    # あちらは「これを直す / 絵柄を合わせる」で、こちらは**この姿勢で描く**。
    # 自前の GPU では ControlNet に、CLI ブリッジ越しの相手には参考の 1 枚として渡る。
    # **言葉で姿勢は伝わらない**ので、絵で渡す口が要る
    pose: str = ""
    # 相手。空なら既定(自前の GPU)
    backend: str | None = None
    model: str | None = None
    size: str = "1024x1024"
    # 0 なら毎回振り直す。同じ絵を作り直したいときに指定する
    seed: int = 0
    count: int = 1
    negative: str = ""
    steps: int = 25
    # **誰が頼んだか**(画面に出る手がかり)。必須にはしない —— 名乗りが無いだけで
    # 断るのは、鍵だけ借りに来るこの口の値打ちに合わない。話す口と同じ扱い
    requested_by: str = ""


class AudioRequestBody(BaseModel):
    prompt: str
    # 何案かを 1 組として見比べるための名前。同じ名前を付けた依頼が画面で横に並ぶ
    group: str = ""
    # 参考にする音。前に作った音のパス(audio_status が返すもの)か、届く URL。
    # 似た音色・雰囲気・楽器編成で作らせる(曲だけ)
    reference: str = ""
    # 相手。空なら既定(自前の GPU)
    backend: str | None = None
    model: str | None = None
    # 効果音(sfx)か曲(music)か。モデルも口も別物なので選ばせる
    sound: str = media_providers.SOUND_SFX
    # 0 なら相手の既定。長さを指定できない相手(Lyria)に渡すと 400
    seconds: float = 0
    seed: int = 0
    count: int = 1
    # 空なら器楽として頼む
    lyrics: str = ""
    negative: str = ""
    # 繋いで鳴らせる素材にするか(効くのは ElevenLabs の効果音だけ)
    loop: bool = False
    steps: int = 50
    # **誰が頼んだか**(画面に出る手がかり)。必須にはしない —— 名乗りが無いだけで
    # 断るのは、鍵だけ借りに来るこの口の値打ちに合わない。話す口と同じ扱い
    requested_by: str = ""


class VideoRequestBody(BaseModel):
    prompt: str
    # 相手。空なら既定(自前の GPU)
    backend: str | None = None
    model: str | None = None
    size: str = "1280x720"
    # 0 なら相手の一覧のいちばん短いもの。一覧に無い値は 400(丸めない)
    seconds: float = 0
    seed: int = 0
    count: int = 1
    negative: str = ""
    # 音も一緒に作らせるか(効くのは Veo 系だけ)
    audio: bool = True
    steps: int = 20
    # 見比べで束ねる名前。**絵と音だけに付けていて、動画と読み上げでは
    # 取りこぼしていた** —— 何案か作って選ぶのは種類を問わず起きることなので、
    # job を作る口はどれも受け取る
    group: str | None = None
    # **誰が頼んだか**(画面に出る手がかり)。必須にはしない —— 名乗りが無いだけで
    # 断るのは、鍵だけ借りに来るこの口の値打ちに合わない。話す口と同じ扱い
    requested_by: str = ""


class SpeechRequestBody(BaseModel):
    # 読み上げる文章そのもの(絵や音の「こういうものを作って」ではない)
    text: str
    backend: str | None = None
    model: str | None = None
    # 空なら相手の既定の声
    voice: str = ""
    speed: float = 1.0
    # ISO 639-1。空なら相手が文章から見当をつける
    language: str = ""
    # 読み方の指示(効くのは OpenAI の gpt-4o-mini-tts だけ)
    instructions: str = ""
    seed: int = 0
    count: int = 1
    # 見比べで束ねる名前(上の動画と同じ理由)
    group: str | None = None
    # **誰が頼んだか**(画面に出る手がかり)。必須にはしない —— 名乗りが無いだけで
    # 断るのは、鍵だけ借りに来るこの口の値打ちに合わない。話す口と同じ扱い
    requested_by: str = ""


@app.get("/v1/capabilities")
async def capabilities_list() -> dict:
    """chiezo 経由で AI に頼めることの一覧（会話・声・画像・動画・音楽・SE）。

    画面と同じ語彙を返す(`app/capabilities.py`)。まだ無いものも並べる ——
    表から消すと「頼めるのか分からない」になり、聞かれるたびにコードを読み直すことになる。
    `supported=false` は実装が無いという意味で、「相手がいない」(実装はあるが鍵が
    未登録・GPU が無い等)とは別。
    """
    return {"capabilities": capabilities.overview(await capabilities.usable_now())}


# 頼める相手を引ける kind。文字起こしも並べる(job にはならないが、
# 「誰に頼めるか」は他と同じように知りたい)。
_MEDIA_KINDS = (
    media_providers.KIND_IMAGE,
    media_providers.KIND_AUDIO,
    media_providers.KIND_VIDEO,
    media_providers.KIND_SPEECH,
    media_providers.KIND_TRANSCRIBE,
)


class MediaTextRequest(BaseModel):
    prompt: str
    # どの相手に書かせるか。空なら「先頭の相手」(`/v1/ai/complete` と同じ規則)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    # 何案かを 1 組として見比べるための名前。絵や音と同じ扱い
    group: str | None = None
    # **誰が頼んだか**(画面に出る手がかり)。必須にはしない —— 名乗りが無いだけで
    # 断るのは、鍵だけ借りに来るこの口の値打ちに合わない。話す口と同じ扱い
    requested_by: str = ""


@app.get("/v1/media/backends")
async def media_backends_list(
    kind: str = Query(media_providers.KIND_IMAGE,
                      description="image / audio / video / speech / transcribe"),
) -> dict:
    """その kind を頼める相手と、選べるモデル・サイズ・長さ・声。"""
    if kind not in _MEDIA_KINDS:
        raise HTTPException(
            400, {"error": f"kind は {' / '.join(_MEDIA_KINDS)} のどれかにしてください"}
        )
    return {"backends": await media.backends(kind), "kind": kind, "enabled": media.is_enabled()}


def _refuse_bridge_for(request: Request, backend: str) -> None:
    """**Chiezo が動かしている CLI からの生成依頼。** 相手まで見て判断する ——
    会話している相手に、その相手自身を頼むのは通す(理由は
    `media.refuse_bridge_caller`)。別の相手へ回すのと、生成中の相手は断る。
    """
    media.refuse_bridge_caller(
        request.client.host if request.client else "", (backend or "").strip().lower()
    )


def _edit_source(edit: str) -> tuple[str, str]:
    """来た値を (path, url) に振り分ける。

    呼ぶ側に 2 つの引数を書き分けさせない —— `image_status` が返すのはパスと URL の
    両方で、どちらを持っているかは場面による。
    """
    return ("", edit) if edit.startswith(("http://", "https://")) else (edit, "")


async def _source_image(
    edit: str, reference: str | list[str]
) -> tuple[tuple[bytes, ...], str, tuple[str, ...]]:
    """元にする絵と、その使い道と、**指し先の並び**を返す。

    **両方は受け取らない。** 相手への言い方が逆(片方は「他を変えるな」、もう片方は
    「別のものを描け」)なので、両方渡されたらどちらの意図か決めようがない。

    **参考は複数、直すのは 1 枚。** 何枚も同時に直すという指示は成立しない。

    3 つ目を返すのは**画面に出すため**。中身(bytes)は相手へ渡したら消えるので、
    どこのものを元にしたかは指し先を控えておくしかない。
    """
    refs = [reference] if isinstance(reference, str) else list(reference)
    refs = [r.strip() for r in refs if r and r.strip()]
    if edit and refs:
        raise HTTPException(400, {"error": "edit と reference は同時に渡せません"})
    if not (edit or refs):
        return (), media_backends.SOURCE_EDIT, ()
    if edit:
        mode, refs = media_backends.SOURCE_EDIT, [edit]
    else:
        mode = media_backends.SOURCE_REFERENCE
    loaded = tuple([await media.load_image(*_edit_source(r)) for r in refs])
    return loaded, mode, tuple(refs)


@app.post("/v1/media/image")
async def media_image(body: ImageRequest, request: Request) -> dict:
    """描き始めて job を返す(待たない)。進み具合は下の口で引く。"""
    _refuse_bridge_for(request, body.backend or "")
    source, mode, ref = await _source_image(body.edit, body.reference)
    pose = await media.load_image(*_edit_source(body.pose)) if body.pose else b""
    return media.start_image_job(
        prompt=body.prompt,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        size=body.size,
        seed=body.seed,
        count=body.count,
        negative=body.negative,
        steps=body.steps,
        group=body.group,
        sources=source,
        source_mode=mode,
        source_refs=ref,
        pose=pose,
        pose_ref=body.pose,
        requested_by=media.caller_name(body.requested_by, request.headers.get("user-agent", "")),
    )


@app.post("/v1/media/audio")
async def media_audio(body: AudioRequestBody, request: Request) -> dict:
    """作り始めて job を返す(待たない)。進み具合は絵と同じ口で引く。"""
    _refuse_bridge_for(request, body.backend or "")
    source = await media.load_image(*_edit_source(body.reference)) if body.reference else b""
    return media.start_audio_job(
        prompt=body.prompt,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        sound=body.sound,
        seconds=body.seconds,
        seed=body.seed,
        count=body.count,
        lyrics=body.lyrics,
        negative=body.negative,
        loop=body.loop,
        steps=body.steps,
        group=body.group,
        source=source,
        source_ref=body.reference or "",
        requested_by=media.caller_name(body.requested_by, request.headers.get("user-agent", "")),
    )


@app.post("/v1/media/video")
async def media_video(body: VideoRequestBody, request: Request) -> dict:
    """作り始めて job を返す(待たない)。進み具合は絵と同じ口で引く。

    絵より待つ(数分〜十数分)ので、呼ぶ側は間を空けて引きに来ること。
    """
    _refuse_bridge_for(request, body.backend or "")
    return media.start_video_job(
        prompt=body.prompt,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        size=body.size,
        seconds=body.seconds,
        seed=body.seed,
        count=body.count,
        negative=body.negative,
        audio=body.audio,
        steps=body.steps,
        group=(body.group or "").strip(),
        requested_by=media.caller_name(body.requested_by, request.headers.get("user-agent", "")),
    )


@app.post("/v1/media/speech")
async def media_speech(body: SpeechRequestBody, request: Request) -> dict:
    """読み上げ始めて job を返す(待たない)。進み具合は絵と同じ口で引く。"""
    _refuse_bridge_for(request, body.backend or "")
    return media.start_speech_job(
        text=body.text,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        voice=body.voice,
        speed=body.speed,
        language=body.language,
        instructions=body.instructions,
        seed=body.seed,
        count=body.count,
        group=(body.group or "").strip(),
        requested_by=media.caller_name(body.requested_by, request.headers.get("user-agent", "")),
    )


@app.post("/v1/media/text")
async def media_text(body: MediaTextRequest, request: Request) -> dict:
    """文章を書かせる。**すぐには返らない**(job_id を返す)。

    `/v1/ai/complete` との違いは 1 つだけ —— **結果が job として残る**ので、
    何案か書かせて画面で読み比べ、採用の印を付けられる。長い文章ほど、
    会話に貼って読ませるより画面で読むほうが早い。

    絵や音と同じ表に入るので、`/v1/media/groups` も `/v1/media/picks` も
    そのまま使える。
    """
    _refuse_bridge_for(request, body.backend or "")
    return media.start_text_job(
        prompt=body.prompt,
        backend=(body.backend or "").strip(),
        model=(body.model or "").strip(),
        effort=(body.effort or "").strip(),
        group=(body.group or "").strip(),
        requested_by=media.caller_name(body.requested_by, request.headers.get("user-agent", "")),
    )


@app.post("/v1/media/jobs/{job_id}/cancel")
async def media_cancel(job_id: str) -> dict:
    """走っている(または並んでいる)生成を止める。

    **向こう側の CLI までは止まらない**(こちらの待ち枠が空くだけ)。
    詳しくは `media.cancel_job`。
    """
    return media.cancel_job(job_id)


@app.post("/v1/media/transcribe")
async def media_transcribe(
    request: Request,
    file: UploadFile = File(..., description="文字起こしする音声・動画"),
    backend: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
) -> dict:
    """音を文字にする。この口だけ job にならない(その場で文字が返る)。

    multipart で受ける。 送る側が既に音を持っているので、base64 に膨らませて
    JSON に載せる意味がない(1 割以上大きくなり、両側で詰め替えの手間が増える)。
    """
    return await media.transcribe(
        data=await file.read(),
        filename=file.filename or "audio",
        mime=file.content_type or "application/octet-stream",
        backend=backend.strip(),
        model=model.strip(),
        language=language.strip(),
    )


@app.post("/v1/media/upload")
async def media_upload(
    request: Request,
    file: UploadFile = File(..., description="見比べに並べたいもの（絵・音・動画・文章）"),
    prompt: str = Form("", description="何を作ったものか（見出しになる。空ならファイル名）"),
    group: str = Form("", description="見比べで束ねる名前。同じ名前が 1 組になる"),
    kind: str = Form("", description="image / audio / video / text。空なら中身から見分ける"),
    model: str = Form("", description="手元で使った道具やモデルの名前（控え）"),
    requested_by: str = Form("", description="誰が持ち込んだかの名乗り（画面に出る）"),
) -> dict:
    """**手元で作ったものを持ち込む。** 生成させずに見比べへ 1 件足す口。

    これが無かったころは、見比べに載せる手段が「chiezo に作らせる」しか無かった ——
    手元で仕上げたものや別の道具で作ったものを、生成させたものと並べられなかった。
    比べたいのは出どころではなく出来のほうなので、出どころで弾かない。

    multipart で受ける（`transcribe` と同じ理由。送る側が既に中身を持っているので、
    base64 に膨らませて JSON に載せる意味がない）。**生成の口ではないので課金は走らない。**
    """
    return await run_in_threadpool(
        media.save_upload,
        stream=file.file,
        filename=file.filename or "",
        mime=file.content_type or "",
        prompt=prompt,
        group=group,
        kind=kind,
        model=model,
        requested_by=media.caller_name(requested_by, request.headers.get("user-agent", "")),
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


@app.get("/v1/media/groups")
async def media_groups(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, description="遡る数(新しい順に数えて何組目から)"),
) -> dict:
    """見比べる組の一覧(見出し・日時・種類・件数まで)。案の中身は下の口で。"""
    return {"groups": media.job_groups(limit, offset)}


@app.get("/v1/media/groups/{key:path}")
async def media_group(key: str) -> dict:
    """組を 1 つ。開いたときだけ案の中身を運ぶ。"""
    group = media.job_group(key)
    if group is None:
        raise HTTPException(404, {"error": f"unknown group: {key}"})
    return group


class PickBody(BaseModel):
    # 選んだ理由や注文。頼んだ側が次に活かせるように、一言だけ添えられる
    note: str = ""


@app.post("/v1/media/jobs/{job_id}/pick")
async def media_pick(job_id: str, body: PickBody) -> dict:
    """この案を採用と印す。同じ組の他の印は外れる。"""
    return media.pick_job(job_id, body.note)


@app.delete("/v1/media/jobs/{job_id}/pick")
async def media_unpick(job_id: str) -> dict:
    """採用の印を外す。"""
    return media.unpick_job(job_id)


@app.get("/v1/media/picks")
async def media_picks(
    limit: int = Query(20, ge=1, le=100), group: str = Query("")
) -> dict:
    """採用された案。**依頼した AI はここを見に来る。**"""
    return {"picks": media.picked_jobs(limit, group.strip())}


@app.get("/media/{path:path}", include_in_schema=False)
async def media_file(path: str):
    """出来た画像・音を配る。置き場の外は返さない(`../` を踏ませない)。"""
    return FileResponse(media.resolve(path))


# ---- 画面(人間向け HTML)-----------------------------------------------------
#
# 実体は app/views/ に分けてある。REST(この上)と画面は変更の理由が別で、
# 同じファイルに置くと管理画面の HTML だけで 700 行を占めるため。
# import はここ(定義の後ろ)で行う —— views は app/deps.py から共有の
# 下ごしらえを取るので main を import しない設計だが、登録の位置は末尾に揃える。

app.include_router(views_admin.router)
app.include_router(views_ai_settings.router)
app.include_router(views_ai_usage.router)
app.include_router(views_ai_workers.router)
app.include_router(views_browse.router)
# **見比べより先に登録する。** あちらの `/admin/media/{key:path}` は総取りなので、
# 後にすると `/admin/media/ask` が組の名前として吸われる
app.include_router(views_media_ask.router)
app.include_router(views_media_compare.router)
app.include_router(views_chat.router)
# やること層(タスク・ルール)は管理画面の 1 面(`/admin/todo`)。**専用の REST も
# 別プロセスも持たない** —— Chiezo は安全なネットワークの中からしか触らせない、と
# 決めたので、外に出すための認証つきの面(旧 chiezo-tasks)ごと畳んである。
app.include_router(views_todo.router)


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


async def _mcp_knowledge_asgi(scope, receive, send):
    """`/mcp/knowledge` を、生成の道具を出さない MCP アプリへ委譲する。

    **`/mcp` より先にマウントする。** Starlette は登録順に照合するので、
    後にすると `/mcp` が配下ごと拾ってしまう。
    """
    inner = getattr(app.state, "mcp_knowledge_asgi", None)
    if inner is None:  # lifespan を通らずに呼ばれた場合(通常は起こらない)
        raise RuntimeError("MCP app is not initialized (lifespan did not run)")
    await inner(scope, receive, send)


# ツールの実体は上のエンドポイント関数そのもの(app/mcp_server.py 参照)。
app.mount("/mcp/knowledge", _mcp_knowledge_asgi)
app.mount("/mcp", _mcp_asgi)
