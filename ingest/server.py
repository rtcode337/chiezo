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
import threading
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
# ファイル名になるので狭く取る(`app/collect.py` の NAME_RE と同じ形)
SOURCE_NAME_RE = __import__("re").compile(r"^[a-z][a-z0-9_]{1,30}$")

_lock = threading.Lock()
_status: dict = {
    "state": "idle",  # idle | running | done | error
    "source": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_log_tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)


class _TailHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_tail.append(self.format(record))


_tail_handler = _TailHandler()
_tail_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger("chiezo.ingest").addHandler(_tail_handler)
logging.getLogger("chiezo.ingest").setLevel(logging.INFO)


def _run_job(source: str) -> None:
    from main import run as ingest_run

    try:
        ingest_run(source, DATA_DIR)
        with _lock:
            _status["state"] = "done"
            _status["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    # SystemExit も捕まえる。取り込み側は「設定が違う」類の行き止まり
    # (リリースが見つからない・依存が入っていない)を raise SystemExit で表すが、
    # これは Exception ではないので素通りする —— ジョブは daemon スレッドなので、
    # 抜けた瞬間にスレッドだけ黙って死に、state が "running" のまま残る。
    # そうなると画面は「走っている」を映し続け、さらに start_run が 409 で
    # 新しい取り込みを断り続ける(コンテナを再起動するまで直らない)。
    # KeyboardInterrupt は含めない(こちらは止めに来た合図なので通す)。
    except (Exception, SystemExit) as e:
        log.exception("ingest job failed: source=%s", source)
        with _lock:
            _status["state"] = "error"
            _status["error"] = str(e)
            _status["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")


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
def delete_source(name: str):
    """焼いたソースを消す。**集めたものだけ**が対象。

    **消せるのはここだけ**。`chiezo-app` は `corpus/` を読み取り専用でマウントして
    いるので、あちらからはファイルに触れない(長期記憶へ書けるのは ingest だけ、
    という線の裏返し)。

    **種別を確かめてから消す**(`meta.source_kind` が `collect`)。ダンプ由来の
    ソースは作り直すのに数時間かかるうえ、この口は収集の設定を消すついでに
    呼ばれる —— 名前の取り違えで jawiki が飛ぶ経路を作らない。

    消すのは、いまの世代・1 つ前の世代・シンボリックリンク・焼く前に残った素材。
    **走っている最中は断る**(切り替えの途中を壊さないため)。
    """
    with _lock:
        if _status["state"] == "running":
            raise HTTPException(409, {"error": "ingest is running"})

    if not SOURCE_NAME_RE.match(name):
        raise HTTPException(400, {"error": f"invalid source name: {name}"})

    link = DATA_DIR / f"{name}.db"
    if not link.exists() and not link.is_symlink():
        raise HTTPException(404, {"error": f"unknown source: {name}"})

    kind = _source_kind(link)
    if kind != COLLECT_KIND:
        raise HTTPException(
            409,
            {
                "error": f"source {name} is not deletable here",
                "reason": f"source_kind={kind!r}(消せるのは集めたものだけ)",
                "hint": "ダンプ由来のソースは作り直しに時間がかかるので、"
                        "この口からは消せないようにしてある",
            },
        )

    removed = _remove_source_files(name)
    log.info("deleted source %s (%d files)", name, len(removed))
    return {"ok": True, "source": name, "removed": removed}


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


def _remove_source_files(name: str) -> list[str]:
    """世代・リンク・焼く前に残った素材を消す。消えたものの名前を返す。"""
    targets = [DATA_DIR / f"{name}.db", *DATA_DIR.glob(f"{name}-*.db")]
    # 焼く前に落ちて残っている素材(次の実行が読み直すもの)も一緒に片付ける
    targets += list((DATA_DIR / "dumps").glob(f"{name}-*"))

    removed = []
    for path in targets:
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
    return removed


@app.get("/status")
def status():
    with _lock:
        return {**_status, "log_tail": list(_log_tail)}


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
        )
    thread = threading.Thread(target=_run_job, args=(source,), daemon=True)
    thread.start()
    return JSONResponse(status_code=202, content={"status": "started", "source": source})
