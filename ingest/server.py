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
from datetime import datetime, timezone
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
            _status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as e:  # noqa: BLE001 - バックグラウンドジョブの失敗を状態として残す
        log.exception("ingest job failed: source=%s", source)
        with _lock:
            _status["state"] = "error"
            _status["error"] = str(e)
            _status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


app = FastAPI(title="chiezo-trigger", version="0.1")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/status")
def status():
    with _lock:
        return {**_status, "log_tail": list(_log_tail)}


@app.post("/run/{source}")
def start_run(source: str):
    from sources import ADAPTERS

    if source not in ADAPTERS:
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
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            finished_at=None,
            error=None,
        )
    thread = threading.Thread(target=_run_job, args=(source,), daemon=True)
    thread.start()
    return JSONResponse(status_code=202, content={"status": "started", "source": source})
