"""chiezo-trigger: 管理画面からの初期化リクエストを受けて ingest を実行する内部専用サービス。

chiezo-api とは別コンテナ(ingest イメージを流用し、CMD だけ本ファイルの uvicorn 起動に
差し替える)。/data への書き込み権限を持つのはこのサービスと one-shot の chiezo-ingest
プロファイルのみで、chiezo-api は引き続き /data を read-only でマウントする。
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
    except Exception as e:
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

    chiezo-api の管理画面がこれを引いて「未初期化データの初期化」を組み立てる。
    ソース定義は ingest 側にしかなく、api は ingest のコードを import しない
    (コンテナも依存関係も別)ため、名前・種別・表示用のメタだけを HTTP で渡す。
    `osm_<国>` 195 件・`<lang>wiki` 348 件は、api 側で 1 行ずつ持たせるのは現実的でない。

    アダプタは実体化せずに答える(カタログ由来だけで 500 超あり、全部作ると無駄が大きい)。

    `schema_version` はこのイメージが焼くスキーマバージョン(`core.SCHEMA_VERSION`)。
    管理画面が「最新のスキーマバージョン」と再構築を促す表示に使う(api 側の対応最大
    バージョンと通常は一致するが、正はあくまで取り込みを実行する ingest 側)。
    """
    from core import SCHEMA_VERSION, is_low_memory_build
    from sources import ADAPTERS, remote
    from sources.osm_regions import CONTINENTS, OSM_REGIONS
    from sources.wikipedia_editions import WIKIPEDIA_EDITIONS

    # osm のノード座標索引はカタログの既定を実行時設定が上書きしうる
    # (OSM_NODE_INDEX > BUILD_PROFILE=low_memory > 既定。sources/osm.py の
    # node_index_kind と同じ優先順)。管理画面の必要メモリ表示が実際の実行条件と
    # 食い違わないよう、ここで解決してから返す。memory_gb は「RAM 索引で焼く場合の
    # 目安」のままでよい(api 側がディスク索引時の 2 GiB 表示を組み立てる)。
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
    if source not in ADAPTERS and source not in {s.name for s in remote.catalog()}:
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
