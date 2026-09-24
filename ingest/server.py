"""chiezo-trigger: 管理画面からの初期化リクエストを受けて ingest を実行する内部専用サービス。

chiezo-app とは別コンテナ(ingest イメージを流用し、CMD だけ本ファイルの uvicorn 起動に
差し替える)。/data への書き込み権限を持つのはこのサービスと one-shot の chiezo-ingest
プロファイルのみで、chiezo-app は引き続き /data を read-only でマウントする。
Docker の内部ネットワークのみで到達可能にし、ホストへポート公開しない
(docker-compose.yml 参照)。

同時に実行できるジョブは 1 つまで。状態はプロセス内メモリのみで保持する
(このプロセスが再起動すれば消える。長時間の一括取り込みバッチという用途上、
永続化は不要と判断)。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("chiezo.trigger")

DATA_DIR = Path(os.environ.get("CHIEZO_DATA_DIR", "/data"))
LOG_TAIL_LINES = 200

# 消してよいソースの種別。**集めたものだけ** —— ダンプ由来のものは作り直すのに
# 数時間かかるうえ、この口は収集の設定を消すついでに呼ばれる
COLLECT_KIND = "collect"
# ファイル名になるので狭く取る(`app/collect.py` の NAME_RE と同じ形)。
# `/` も `..` も通さないので、名前から組み立てたパスは DATA_DIR の中に留まる
SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

_lock = threading.Lock()
_status: dict = {
    # idle | running | done | error | stopped
    # **stopped は error と分ける** —— 人が降ろしたのと、落ちたのとでは
    # 次にすることが逆になる(押し直すだけ / 原因を調べる)
    "state": "idle",
    "source": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    # 「止める」を押してから、実際に降りるまでのあいだ
    "stopping": False,
}
_log_tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)

# **最後に落ちた回の控え。** 状態もログも「いまの 1 本」ぶんしか持たないので、
# **次の取り込みが始まった瞬間に、落ちた回の理由とログが消えていた** ——
# 収集は 1 時間おきに回るし、別の収集が続けて走ることもある(実際、落ちた 56 秒後に
# 次が始まって何も読めなくなった)。無人で回る層は、その場に居合わせない人が
# 後から原因を追う —— 読む手が残っていないと、同じことがもう一度起きるまで
# 分からない。**上書きするのは次に落ちたときだけ**。
_last_failure: dict | None = None


class _TailHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_tail.append(self.format(record))


class _JstFormatter(logging.Formatter):
    """時刻を**日本時間**で書く。

    **この控えは画面に出る** —— 取り込みの進み具合を読みに来た人が見るものなので、
    コンテナのタイムゾーンに決めさせない(`TZ` を渡していなければ UTC で書かれ、
    画面の他の時刻と 9 時間ずれたまま並ぶ)。

    **固定の +09:00 でよい。** 日本時間は夏時間を持たないので、タイムゾーン DB を
    引かずに済む —— tzdata の入っていないイメージでも壊れない。
    """

    converter = staticmethod(lambda secs: time.gmtime((secs or 0) + JST_OFFSET_SECONDS))


JST_OFFSET_SECONDS = 9 * 3600

_tail_handler = _TailHandler()
_tail_handler.setFormatter(_JstFormatter("%(asctime)s JST %(levelname)s %(message)s"))
logging.getLogger("chiezo.ingest").addHandler(_tail_handler)
logging.getLogger("chiezo.ingest").setLevel(logging.INFO)


def _run_job(source: str) -> None:
    from main import run as ingest_run

    from core import Stopped

    try:
        ingest_run(source, DATA_DIR)
        with _lock:
            _status["state"] = "done"
            _status["stopping"] = False
            _status["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    # **止めたのは失敗ではない。** 切り替えより前で降りるので、いま配信している
    # 世代はそのまま —— 押し直せば続きから始まる(集めた素材は残してある)
    except Stopped as e:
        log.info("ingest job stopped: source=%s", source)
        with _lock:
            _status["state"] = "stopped"
            _status["stopping"] = False
            _status["error"] = str(e)
            _status["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    # SystemExit も捕まえる。取り込み側は「設定が違う」類の行き止まり
    # (リリースが見つからない・依存が入っていない)を raise SystemExit で表すが、
    # これは Exception ではないので素通りする —— ジョブは daemon スレッドなので、
    # 抜けた瞬間にスレッドだけ黙って死に、state が "running" のまま残る。
    # そうなると画面は「走っている」を映し続け、さらに start_run が 409 で
    # 新しい取り込みを断り続ける(コンテナを再起動するまで直らない)。
    # KeyboardInterrupt は含めない(こちらは止めに来た合図なので通す)。
    except (Exception, SystemExit) as e:
        global _last_failure
        log.exception("ingest job failed: source=%s", source)
        with _lock:
            _status["state"] = "error"
            _status["stopping"] = False
            _status["error"] = str(e)
            _status["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            # **落ちた回は別に控える**(次の取り込みが始まっても消えないように)
            _last_failure = {
                "source": source,
                "started_at": _status["started_at"],
                "finished_at": _status["finished_at"],
                "error": str(e),
                "log_tail": list(_log_tail),
            }


app = FastAPI(title="chiezo-trigger", version="0.1")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/sources")
def sources():
    """取り込める(= 初期化できる)ソースのカタログ。

    chiezo-app の管理画面がこれを引いて「未初期化データの初期化」を組み立てる。
    ソース定義は ingest 側にしかなく、app は ingest のコードを import しない
    (コンテナも依存関係も別)ため、名前・種別・表示用のメタだけを HTTP で渡す。
    `osm_<国>` 195 件・`<lang>wiki` 348 件は、app 側で 1 行ずつ持たせるのは現実的でない。

    アダプタは実体化せずに答える(カタログ由来だけで 500 超あり、全部作ると無駄が大きい)。

    `schema_version` はこのイメージが焼くスキーマバージョン(`core.SCHEMA_VERSION`)。
    管理画面が「最新のスキーマバージョン」と再構築を促す表示に使う(app 側の対応最大
    バージョンと通常は一致するが、正はあくまで取り込みを実行する ingest 側)。
    """
    from core import SCHEMA_VERSION, is_low_memory_build
    from sources import ADAPTERS, remote
    from sources import collect as collect_sources
    from sources.osm_regions import CONTINENTS, OSM_REGIONS
    from sources.wikipedia_editions import WIKIPEDIA_EDITIONS

    # osm のノード座標索引はカタログの既定を実行時設定が上書きしうる
    # (OSM_NODE_INDEX > BUILD_PROFILE=low_memory > 既定。sources/osm.py の
    # node_index_kind と同じ優先順)。管理画面の必要メモリ表示が実際の実行条件と
    # 食い違わないよう、ここで解決してから返す。memory_gb は「RAM 索引で焼く場合の
    # 目安」のままでよい(app 側がディスク索引時の 2 GiB 表示を組み立てる)。
    forced_node_index = os.environ.get("OSM_NODE_INDEX") or (
        "sparse_file_array" if is_low_memory_build() else None
    )

    catalog: dict[str, dict] = {}
    for name in ADAPTERS:
        if name.startswith("osm_") or name in WIKIPEDIA_EDITIONS:
            continue
        adapter = ADAPTERS[name]()
        catalog[name] = {"kind": adapter.source_kind, "lang": adapter.lang}
    for edition in WIKIPEDIA_EDITIONS.values():
        catalog[edition.wiki_id] = {
            "kind": "wikipedia",
            "lang": edition.lang,
            "group": "wikipedia",
            "label": edition.label,
            "label_en": edition.label_en,
            "autonym": edition.autonym,
            "articles": edition.articles,
        }
    for region in OSM_REGIONS.values():
        catalog[region.source] = {
            "kind": "osm",
            "lang": region.lang,
            "group": "osm",
            "slug": region.slug,
            "label": region.label,
            "label_en": region.label_en,
            "continent": region.continent,
            "region": region.region,
            "pbf_bytes": region.pbf_bytes,
            "memory_gb": region.memory_gb,
            "node_index": forced_node_index or region.node_index,
        }
    # 集めたソース(app/collect.py が定義を持つ)。実行時に増えるので、
    # ここで配信側に聞いて並べる —— 一覧に出ないと管理画面から焼く導線が出ない。
    for src in collect_sources.catalog():
        catalog[src.name] = {
            "kind": src.kind,
            "lang": src.lang,
            "label": src.label,
            "memory_gb": src.memory_gb,
        }
    # 別コンテナのプラグイン(CHIEZO_PLUGIN_SOURCES)が提供するソース。
    # 落ちていても catalog() が警告だけ出して飛ばすので、ここで止まることはない
    # (プラグイン 1 つの不調で管理画面の一覧が丸ごと消えるほうが困る)。
    for src in remote.catalog():
        catalog[src.name] = {
            "kind": src.kind,
            "lang": src.lang,
            "label": src.label,
            "memory_gb": src.memory_gb,
            "plugin": src.base_url,
        }
    return {
        "sources": catalog,
        "continents": list(CONTINENTS),
        "schema_version": SCHEMA_VERSION,
    }


@app.delete("/source/{name}")
def delete_source(name: str, expect: str = COLLECT_KIND):
    """焼いたソースを消す。**呼ぶ側が種別を名乗る**。

    **消せるのはここだけ**。`chiezo-app` は `corpus/` を読み取り専用でマウントして
    いるので、あちらからはファイルに触れない(長期記憶へ書けるのは ingest だけ、
    という線の裏返し)。

    **名乗った種別と焼いてあるものが食い違えば断る**(`meta.source_kind`)。
    既定は `collect` —— この口は**収集の設定を消すついでにも呼ばれる**ので、
    名乗らない呼び出しでは集めたものしか消えない(名前の取り違えで jawiki が
    飛ぶ経路を作らない)。**名乗れば他の種別も消せる** —— 画面から名前を打って
    消しに来た人は、何を消すか分かっている。使わなくなったソースを片付ける手段が
    どこにも無いと、手でファイルを消しに行くことになる(実際にそうなった)。

    消すのは、いまの世代・1 つ前の世代・シンボリックリンク・焼く前に残った素材。
    **走っている最中は断る**(切り替えの途中を壊さないため)。
    """
    with _lock:
        if _status["state"] == "running":
            raise HTTPException(409, {"error": "ingest is running"})

    if not SOURCE_NAME_RE.match(name):
        raise HTTPException(400, {"error": f"invalid source name: {name}"})

    link = _find_link(name)
    if link is None:
        raise HTTPException(404, {"error": f"unknown source: {name}"})

    kind = _source_kind(link)
    if kind != expect:
        raise HTTPException(
            409,
            {
                "error": f"source {name} is not deletable here",
                "reason": f"source_kind={kind!r} だが {expect!r} として消そうとした",
                "hint": "呼ぶ側が種別を名乗る(`?expect=<種別>`)。名乗らなければ"
                        "集めたものだけが対象で、名前の取り違えでは消えない",
            },
        )

    removed = _remove_source_files(name)
    log.info("deleted source %s (%d files)", name, len(removed))
    return {"ok": True, "source": name, "removed": removed}


@app.post("/source/{name}/rollback")
def rollback_source(name: str):
    """いま配信しているものを、**1 つ前の世代へ戻す**。

    **戻せるのもここだけ**。`chiezo-app` は `corpus/` を読み取り専用でマウントして
    いるので、あちらからはリンクに触れない(消すのと同じ線)。

    **消さずにリンクを張り替えるだけ。** ブルーグリーンは世代を 2 つ残すので、
    戻したあとに押し直せば元へ戻る —— 焼き直しが中身を壊したときに、
    確かめながら行き来できる。

    **走っている最中は断る**(切り替えの途中を壊さないため)。
    """
    with _lock:
        if _status["state"] == "running":
            raise HTTPException(409, {"error": "ingest is running"})

    if not SOURCE_NAME_RE.match(name):
        raise HTTPException(400, {"error": f"invalid source name: {name}"})

    link = _find_link(name)
    if link is None:
        raise HTTPException(404, {"error": f"unknown source: {name}"})

    live = link.resolve()
    others = [p for p in _generations(name) if p != live]
    if not others:
        raise HTTPException(409, {
            "error": f"source {name} has no earlier generation",
            "hint": "残るのは 1 つ前までで、焼き直すたびに入れ替わります",
        })
    # **いちばん新しい「いま以外」へ戻す。** 戻したあとに押し直すと、
    # いま外したほうがまた「いま以外でいちばん新しい」になるので元へ戻る
    target = max(others, key=lambda p: p.name)
    _point_link_at(link, target)
    log.info("rolled back %s: %s -> %s", name, live.name, target.name)
    return {"ok": True, "source": name, "now": target.name, "was": live.name}


def _generations(name: str) -> list[Path]:
    """その名前の世代ファイル。**リンクそのものは外す**(指す先と二重に数えない)。"""
    head = f"{name}-"
    return [
        p for p in _entries(DATA_DIR)
        if p.name.startswith(head) and p.name.endswith(".db") and not p.is_symlink()
    ]


def _point_link_at(link: Path, target: Path) -> None:
    """リンクを張り替える。**別名で作ってから置き換える**(`ingest/main.py` と同じ)——
    消してから作ると、その一瞬だけソースが消えて見える。
    """
    tmp = link.with_name(link.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target.name)
    tmp.replace(link)


def _source_kind(link: Path) -> str | None:
    """焼いてある DB の meta から種別を読む。読めなければ None。"""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{link}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT source_kind FROM meta LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _entries(directory: Path) -> list[Path]:
    """ディレクトリの直下にあるものを並べる。無ければ空。

    **消す対象は名前から組み立てず、ここで並べたものから選ぶ**。組み立てると
    渡された名前がそのままパスになるので、名前の検査に穴があいた瞬間に置き場の
    外へ届いてしまう。並べたものから選ぶ限り、届く先は置き場の中に限られる。
    壊れたリンク(切り替えの途中で残ったもの)も並ぶ。
    """
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _find_link(name: str) -> Path | None:
    """いま配信しているものを指すリンク。無ければ None。"""
    return next((p for p in _entries(DATA_DIR) if p.name == f"{name}.db"), None)


def _remove_source_files(name: str) -> list[str]:
    """世代・リンク・焼く前に残った素材を消す。消えたものの名前を返す。"""
    generation = f"{name}-"
    targets = [
        p
        for p in _entries(DATA_DIR)
        if p.name == f"{name}.db" or (p.name.startswith(generation) and p.name.endswith(".db"))
    ]
    # 焼く前に落ちて残っている素材(次の実行が読み直すもの)も一緒に片付ける
    targets += [p for p in _entries(DATA_DIR / "dumps") if p.name.startswith(generation)]

    removed = []
    for path in targets:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
    return removed


def _build() -> dict:
    """このイメージの素性(ビルド元のコミットと、焼いた日時)。

    **取り込みは app とは別のイメージ**なので、片方だけ古いままが普通に起きる ——
    タグ(`latest`)では見分けが付かず、デプロイ先が pull し忘れていても外からは
    分からない。chiezo-app の管理画面がここを読んで並べる。

    渡されていなければ空(手元ビルド)。**そのときも鍵は返す** —— 「まだ出して
    いない」と「不明」は読む側で区別できたほうがよい。
    """
    import os

    return {
        "sha": (os.environ.get("CHIEZO_BUILD_SHA") or "").strip(),
        "built_at": (os.environ.get("CHIEZO_BUILD_TIME") or "").strip(),
    }


@app.get("/status")
def status():
    """いまの 1 本と、**最後に落ちた回**、それに動いているイメージの素性。

    落ちた回を別に返すのは、**次の取り込みが始まると状態もログも上書きされる**
    から —— 読みに来たときには既に消えている、が普通に起きる。
    """
    with _lock:
        return {
            **_status, "log_tail": list(_log_tail), "last_failure": _last_failure,
            "build": _build(),
        }


@app.post("/stop")
def stop_run():
    """走っている取り込みを**安全なところで降ろす**。

    **殺さない。** 走っているのは daemon スレッドで、外から止める手段はそもそも
    無い —— 印を立てて、取り込みの側が区切りのいいところで見に行く
    (`core.check_stop`)。降りるのは**切り替えより前**なので、
    **いま配信している世代はそのまま残る**。

    **すぐには止まらないことがある。** 外から素材が届くのを待っている最中は、
    動いているのは向こうでこちらは待っているだけなので、印を見る手が無い ——
    素材が届き始めるか、焼き始めるかしたところで降りる。

    集めた素材は捨てない(`Stopped` では `on_broken` を呼ばない)ので、
    押し直せば**AI を呼び直さずに**続きから焼ける。
    """
    from core import request_stop

    with _lock:
        if _status["state"] != "running":
            raise HTTPException(409, {"error": "no job is running"})
        _status["stopping"] = True
        source = _status["source"]
    request_stop()
    log.info("stop requested: source=%s", source)
    return {"ok": True, "source": source, "stopping": True}


@app.post("/run/{source}")
def start_run(source: str):
    from sources import ADAPTERS, remote

    # プラグインのソースもここで通す。 `/sources` に出したものは実行できなければ
    # ならない —— 管理画面はカタログからボタンを組み立てるので、片方だけ知っていると
    # 「ボタンはあるのに押すと unknown source」になる(実際にそうなった)。
    from sources import collect as collect_sources

    known = {s.name for s in remote.catalog()} | {s.name for s in collect_sources.catalog()}
    if source not in ADAPTERS and source not in known:
        raise HTTPException(404, {"error": f"unknown source: {source}"})
    with _lock:
        if _status["state"] == "running":
            raise HTTPException(
                409,
                {
                    "error": f"a job is already running: {_status['source']}",
                    "status": {**_status, "log_tail": list(_log_tail)},
                },
            )
        _log_tail.clear()
        _status.update(
            state="running",
            source=source,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            finished_at=None,
            error=None,
            stopping=False,
        )
    # **前の回の印を必ず下ろす**(下ろし忘れると、始めた瞬間に降りる)
    from core import clear_stop

    clear_stop()
    thread = threading.Thread(target=_run_job, args=(source,), daemon=True)
    thread.start()
    return JSONResponse(status_code=202, content={"status": "started", "source": source})
