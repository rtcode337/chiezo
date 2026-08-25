"""やること層のアプリ(`chiezo-tasks`)。**外に出すのはこれだけ**。

知識ベース本体(`app/main.py` / ポート 7010)は今までどおり LAN 内・認証なしのまま
にしておきたい。あちらを公開すると、サーバー側の鍵で AI を叩く `/v1/ai/complete`、
課金の走る `/v1/media/*`、数時間の取り込みを起動できる `/admin`、メモを消せる
`DELETE /v1/notes/{doc_id}` まで一緒に外へ出てしまう。**外へ出す面と出さない面を
プロセスごと分ける**のが、認証を 1 枚かぶせるより確実に安い。

同じイメージから起動し、**`notes` の SQLite を本体と共有する**。notes は
WAL + `busy_timeout` で開くので、別プロセスから書いても問題ない
(`app/notes.py` の `_connect` 参照)。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db, notes, tasks_api, tasks_auth

log = logging.getLogger("chiezo.tasks")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # notes は追記される DB なので `immutable=1` で開いてはいけない。ここで登録しないと
    # `db.get_connection` が immutable で開き、書き込みの途中のページを掴みうる
    # (本体は `app/main.py` の走査で登録している。こちらは notes しか読まない)。
    notes.ensure_db()
    path = notes.notes_path()
    if path is not None:
        db.set_mutable_paths([path])
    else:
        log.warning("CHIEZO_NOTES_DIR が未設定なので、やること層は何も保存できない")
    yield
    db.close_thread_connections()


def create_app() -> FastAPI:
    app = FastAPI(title="Chiezo tasks", lifespan=lifespan, docs_url=None, redoc_url=None)
    tasks_api.install_error_handlers(app)
    app.include_router(tasks_api.router)
    # 認証・CSRF・レート制限・セキュリティヘッダ。**ルーターより後に仕込む**
    # (Starlette のミドルウェアは後から足したものが外側に来るので、
    #  ここで足したものが全ルートを包む)
    tasks_auth.install(app)
    if tasks_auth.config.dev:
        log.warning("CHIEZO_TASKS_DEV=true。認証を通していない。本番で立ててはいけない")
    elif not tasks_auth.config.configured:
        log.error(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ALLOWED_EMAIL が未設定のため"
            " ログインを無効化した。/api は 401 を返し続ける"
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "notes": notes.is_enabled()}

    return app


app = create_app()
